"""
Business logic ported from the original Streamlit app.py, with every
`st.*` call removed. This module has ZERO framework dependency (FastAPI or
otherwise) — every function takes explicit arguments and returns a value,
which is what makes it testable and reusable from a REST API.

Two changes from the original, everywhere they applied:
  1. `st.error(...)` (a hard stop) -> raise LogicError(same message). The API
     layer turns this into an HTTP 4xx/5xx with that message as the detail.
  2. `st.warning(...)` / `st.info(...)` (non-fatal, shown alongside a
     successful result) -> collected into a `warnings: list[str]` returned
     alongside the actual data, so the frontend can toast them without the
     request having failed.

Everything else — every prompt, every formula, every pricing rule — is
unchanged from app.py.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

import anthropic
import openpyxl
import pdfplumber
from anthropic import Anthropic
from docx import Document
from docx.enum.table import WD_ALIGN_VERTICAL
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor
from openai import APIError, APITimeoutError, OpenAI, RateLimitError
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.utils.exceptions import InvalidFileException
from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Flowable,
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from . import config

logger = logging.getLogger("bom_backend")

FONTS_DIR = Path(__file__).parent / "assets" / "fonts"
_THAI_FONTS_REGISTERED = False


def _ensure_thai_fonts_registered() -> None:
    """
    reportlab's base-14 fonts have no Thai glyphs — every style used for the
    quotation PDF must reference these registered names explicitly, there is
    no automatic fallback. Registers once per process.

    "TP Tankhun" (by verywhale, distributed via f0nt.com) is used per an
    explicit client request for the quotation document specifically —
    free to use/embed in generated documents, but its license forbids
    redistributing the raw font FILE itself online, so this repo must never
    push backend/app/assets/fonts/TPTankhun-*.ttf to a public remote.
    """
    global _THAI_FONTS_REGISTERED
    if _THAI_FONTS_REGISTERED:
        return
    pdfmetrics.registerFont(TTFont("TPTankhun", str(FONTS_DIR / "TPTankhun-Regular.ttf")))
    pdfmetrics.registerFont(TTFont("TPTankhun-Bold", str(FONTS_DIR / "TPTankhun-Bold.ttf")))
    _THAI_FONTS_REGISTERED = True


class LogicError(Exception):
    """Raised for the same conditions the original app surfaced via st.error()."""


# --------------------------------------------------------------------------------------
# Data Models
# --------------------------------------------------------------------------------------

PENDANT_LAMP_KEYWORDS = ["โคมไฟห้อยเพดาน", "pendant lamp", "pendant light"]

# Matches the room-header band in the frontend's ExcelStylePreview (bg-slate-900 / text-white).
ROOM_HEADER_FILL = PatternFill(start_color="FF000000", end_color="FF000000", fill_type="solid")
ROOM_HEADER_FONT = Font(color="FFFFFFFF", bold=True)

# Whole-baht display for every price/formula cell write_mapping_to_excel writes —
# the underlying value/formula keeps full precision, this only hides the decimals.
MONEY_FORMAT = "#,##0"


@dataclass
class ColumnMapping:
    """
    Maps logical BOM fields to Excel column letters, matching the real
    'รายการเพิ่มเติม / รายการ TBC เดิม' template structure. Updated per client
    template (their columns run 2 earlier than the original app.py-era
    template for E-L, and 4 earlier for the Q/R-equivalent columns, since
    the template no longer has the two blank spacer columns that used to
    sit between N and Q):
      E=ต้นทุน ALT, F=ALT+ค่า protect+ค่าขน, G=ALT+5%+VAT7%,
      H=P'May Overhead+vat7%, I=Other+5%/Overhead+vat7%,
      J=The chosen price, K=10DK Price, L=งานจัดซื้อ เบิกจ่ายตามราคาจริง,
      M=10DK's work (mirrors K), N=หมายเหตุ (mirrors L).
    """
    supplier_col: str = "A"
    room_col: str = "B"
    item_col: str = "C"
    qty_col: str = "D"
    custom_made_price_col: str = "E"
    purchased_price_col: str = "L"
    start_row: int = 5
    generate_formulas: bool = True
    multiplier_anchor_row: int = 5
    formula_h_col: str = "F"
    formula_i_col: str = "G"
    formula_k_col: str = "I"
    formula_j_col: str = "H"
    formula_l_col: str = "J"
    formula_m_col: str = "K"
    formula_additional_item_col: str = "M"
    formula_purchase_compare_col: str = "N"


def apply_known_price_adjustments(item_name: str, unit_price: float) -> tuple[float, str]:
    """
    Auto-detects pricing exceptions documented in the pricing policy and
    adjusts the raw unit price accordingly, returning (adjusted_price, note).
    Currently handles: pendant lamps — per policy, these add a fixed
    remote-control fee (4,500) + installation fee (2,000) on top of the
    quoted price before any markup is applied.
    """
    name_lower = (item_name or "").lower()
    if any(kw.lower() in name_lower for kw in PENDANT_LAMP_KEYWORDS):
        surcharge = 4500 + 2000
        return unit_price + surcharge, f"+รีโมท 4,500 +ติดตั้ง 2,000 (โคมไฟห้อยเพดาน) = รวม {unit_price + surcharge:,.0f}"
    return unit_price, ""


def detect_alt_or_pmay(supplier: str) -> str:
    """Classifies a custom-made supplier name into 'ALT', 'PMAY', or 'UNKNOWN'."""
    s = (supplier or "").lower()
    if "alt" in s:
        return "ALT"
    if "may" in s:
        return "PMAY"
    return "UNKNOWN"


# --------------------------------------------------------------------------------------
# Price sanity-check / auto-correction
# --------------------------------------------------------------------------------------

_NUMBER_WITH_COMMA_RE = re.compile(r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b")


def validate_and_fix_price(ai_price: float | None, raw_text: str) -> tuple[float | None, str]:
    """
    Cross-checks an AI-extracted unit price against numbers actually present
    in the raw supplier text, catching the "AI read a thousands-comma as a
    decimal point" failure mode. See the original app.py for the full
    rationale — logic unchanged.
    """
    if ai_price is None or ai_price <= 0 or not raw_text:
        return ai_price, ""

    for cand in _NUMBER_WITH_COMMA_RE.findall(raw_text):
        cand_value = float(cand.replace(",", ""))
        if abs(ai_price * 1000 - cand_value) < 0.01:
            note = (
                f"⚠️ auto-corrected: AI อ่านราคาเป็น {ai_price:,.3f} แต่พบเลข '{cand}' "
                f"ในข้อความดิบใบเสนอราคา (คาดว่า AI อ่าน comma เป็นจุดทศนิยมผิด) "
                f"— ระบบแก้เป็น {cand_value:,.2f} ให้อัตโนมัติ กรุณาตรวจสอบอีกครั้ง"
            )
            return cand_value, note
    return ai_price, ""


def normalize_thousands_commas(text: str) -> str:
    """Strips the thousands-comma from every properly comma-formatted number, before sending to the AI."""
    return _NUMBER_WITH_COMMA_RE.sub(lambda m: m.group(0).replace(",", ""), text)


# --------------------------------------------------------------------------------------
# OpenAI Client
# --------------------------------------------------------------------------------------

def get_openai_client() -> OpenAI | None:
    """Instantiates the OpenAI client from the OPENAI_API_KEY env var. Returns None if missing."""
    if not config.OPENAI_API_KEY:
        return None
    return OpenAI(
        api_key=config.OPENAI_API_KEY,
        timeout=config.AI_TIMEOUT_SECONDS,
        max_retries=config.AI_MAX_RETRIES,
    )


def get_anthropic_client() -> Anthropic | None:
    """Instantiates the Anthropic client from the ANTHROPIC_API_KEY env var. Returns None if missing."""
    if not config.ANTHROPIC_API_KEY:
        return None
    return Anthropic(
        api_key=config.ANTHROPIC_API_KEY,
        timeout=config.AI_TIMEOUT_SECONDS,
        max_retries=config.AI_MAX_RETRIES,
    )


def call_claude_json(
    client: Anthropic,
    system_prompt: str,
    user_prompt: str,
    tool_name: str,
    tool_description: str,
    input_schema: dict[str, Any],
    model: str | None = None,
    max_tokens: int = 4096,
    timeout: float | None = None,
    max_retries: int | None = None,
) -> dict[str, Any]:
    """
    Claude has no OpenAI-style response_format={'type':'json_object'} mode, so
    this uses a single forced tool call instead — Claude must call `tool_name`
    with arguments matching `input_schema`, which is a more reliable way to
    get back exactly-shaped JSON than asking it to emit a raw JSON text block.
    Raises LogicError on any failure, mirroring call_openai_json. `model`
    defaults to config.ANTHROPIC_MODEL if not given, so callers that need a
    different model for one call site (e.g. price matching) don't have to
    change the app-wide default. `timeout`/`max_retries` similarly override
    the client's own config.AI_TIMEOUT_SECONDS default for one call — needed
    for Opus-tier models, which think before answering by default and can
    genuinely take longer than the 60s tuned for older, non-thinking models.
    """
    if timeout is not None or max_retries is not None:
        client = client.with_options(
            timeout=timeout if timeout is not None else config.AI_TIMEOUT_SECONDS,
            max_retries=max_retries if max_retries is not None else config.AI_MAX_RETRIES,
        )
    try:
        response = client.messages.create(
            model=model or config.ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            tools=[{"name": tool_name, "description": tool_description, "input_schema": input_schema}],
            tool_choice={"type": "tool", "name": tool_name},
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == tool_name:
                return block.input
        raise LogicError("⚠️ Claude did not return the expected structured output.")
    except LogicError:
        raise
    except anthropic.APITimeoutError as e:
        raise LogicError("⏱️ The Claude API request timed out. Please try again.") from e
    except anthropic.RateLimitError as e:
        raise LogicError("🚦 Claude rate limit reached. Wait a moment and retry.") from e
    except anthropic.APIError as e:
        raise LogicError(f"❌ Claude API error: {e}") from e
    except Exception as e:  # noqa: BLE001
        logger.exception("Unexpected error calling Claude")
        raise LogicError(f"⚠️ Unexpected error calling Claude: {e}") from e


def call_openai_json(client: OpenAI, system_prompt: str, user_prompt: str) -> dict[str, Any]:
    """
    Calls the OpenAI Chat Completions API in JSON mode and returns the parsed
    dict. Raises LogicError (with the same user-facing message the original
    app showed via st.error) on any failure.
    """
    try:
        response = client.chat.completions.create(
            model=config.OPENAI_MODEL,
            response_format={"type": "json_object"},
            temperature=0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        content = response.choices[0].message.content
        return json.loads(content)
    except APITimeoutError as e:
        raise LogicError("⏱️ The OpenAI API request timed out. Please try again.") from e
    except RateLimitError as e:
        raise LogicError("🚦 OpenAI rate limit reached. Wait a moment and retry.") from e
    except APIError as e:
        raise LogicError(f"❌ OpenAI API error: {e}") from e
    except json.JSONDecodeError as e:
        raise LogicError("⚠️ The AI response was not valid JSON. Please retry the step.") from e
    except Exception as e:  # noqa: BLE001
        logger.exception("Unexpected error calling OpenAI")
        raise LogicError(f"⚠️ Unexpected error calling OpenAI: {e}") from e


# --------------------------------------------------------------------------------------
# PDF Text Extraction
# --------------------------------------------------------------------------------------

_THAI_CHAR_RE = re.compile(r"[฀-๿]")
_MOJIBAKE_CHARS_RE = re.compile(r"[¤èÒÐÑÃéçÊÍÔÙâãÅì¹]")


def clean_thai_extracted_text(text: str) -> str:
    """Collapses a stray single space pdfplumber sometimes inserts between two Thai characters."""
    return re.sub(r"(?<=[฀-๿]) (?=[฀-๿])", "", text)


def detect_thai_mojibake(text: str, sample_size: int = 2000) -> bool:
    """Heuristic check for a PDF whose embedded Thai font uses a broken/legacy encoding."""
    sample = text[:sample_size]
    if not sample.strip():
        return False
    mojibake_hits = len(_MOJIBAKE_CHARS_RE.findall(sample))
    thai_hits = len(_THAI_CHAR_RE.findall(sample))
    return mojibake_hits > 20 and mojibake_hits > thai_hits


def extract_text_from_pdf(file_bytes: bytes) -> tuple[str, str | None]:
    """
    Extracts text from a PDF's raw bytes using pdfplumber, with the same
    Thai-specific handling as the original (tight character clustering,
    stray-space cleanup, mojibake detection).

    Returns (text, warning_or_None). Raises LogicError if the PDF has no
    pages, or has no extractable text at all (both were `return None` +
    st.warning/st.error stops in the original — callers there always treated
    a None return as "stop this step", which LogicError reproduces).
    """
    try:
        text_chunks = []
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            if len(pdf.pages) == 0:
                raise LogicError("The uploaded PDF has no pages.")
            for page_num, page in enumerate(pdf.pages, start=1):
                # use_text_flow=True reads glyphs in the PDF content stream's own
                # order instead of re-sorting by (x, y) position — the geometric
                # sort is what causes Thai combining vowels/tone marks (ิ ่ ้ ...)
                # to land one character late in some PDF generators' fonts
                # (e.g. "หิน" extracting as "หนิ", "ไม่เกิน" as "ไมเ่กนิ").
                page_text = page.extract_text(x_tolerance=1, y_tolerance=3, use_text_flow=True) or ""
                if page_text.strip():
                    page_text = clean_thai_extracted_text(page_text)
                    text_chunks.append(f"--- Page {page_num} ---\n{page_text}")
                else:
                    tables = page.extract_tables()
                    for table in tables:
                        rows = ["\t".join(cell or "" for cell in row) for row in table]
                        if rows:
                            text_chunks.append(f"--- Page {page_num} (table) ---\n" + "\n".join(rows))

        full_text = "\n\n".join(text_chunks).strip()

        if not full_text:
            raise LogicError(
                "⚠️ No selectable text could be extracted from this PDF. "
                "It may be a scanned image without a text layer. "
                "Please upload a text-based PDF or run OCR on it first "
                "(e.g., with a tool like OCRmyPDF) before uploading."
            )

        warning = None
        if detect_thai_mojibake(full_text):
            warning = (
                "⚠️ PDF นี้น่าจะใช้ฟอนต์ไทยแบบ custom encoding ที่ทำให้ข้อความที่ดึงออกมาอ่านไม่ออก "
                "(เช่น '¤èÒ´Óà¹Ô¹¡ÒÃ' แทนที่จะเป็นข้อความไทยปกติ) — เป็นปัญหาที่ระดับไฟล์ PDF เอง "
                "แก้ด้วยโค้ดไม่ได้ครับ วิธีแก้: เปิดไฟล์ด้วย Adobe Acrobat หรือโปรแกรม PDF แล้ว "
                "'Print to PDF' ใหม่อีกรอบ (จะฝังฟอนต์แบบมาตรฐาน) หรือรัน OCR ทับ (เช่น OCRmyPDF) "
                "ก่อนอัปโหลดใหม่"
            )

        return full_text, warning

    except LogicError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("PDF extraction failed")
        raise LogicError(f"❌ Failed to read PDF: {e}") from e


def _current_export_source_hash(excel_bytes: bytes | None, mapping_rows: list[dict[str, Any]]) -> str:
    """Fingerprints everything that affects the exported file, to detect a stale download."""
    h = hashlib.md5()
    h.update(excel_bytes or b"")
    h.update(json.dumps(mapping_rows, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8"))
    return h.hexdigest()


# --------------------------------------------------------------------------------------
# Excel I/O
# --------------------------------------------------------------------------------------

def load_workbook_from_bytes(file_bytes: bytes):
    """Loads a workbook from raw bytes, preserving formulas (data_only=False)."""
    try:
        return openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=False, keep_vba=False)
    except InvalidFileException as e:
        raise LogicError("❌ The uploaded file is not a valid .xlsx workbook.") from e
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to load Excel workbook")
        raise LogicError(f"❌ Failed to read Excel file: {e}") from e


def get_append_start_row(ws, start_row: int, before_row: int | None = None) -> int:
    """Returns a safe row to start appending brand-new data at — see app.py for the full rationale."""
    max_col = max(ws.max_column, 1)
    scan_from = ws.max_row if before_row is None else min(ws.max_row, before_row - 1)
    for r in range(scan_from, start_row - 1, -1):
        if any(ws.cell(row=r, column=c).value not in (None, "") for c in range(1, max_col + 1)):
            return r + 1
    return start_row


_SUM_RANGE_RE = re.compile(
    r"SUM\(\s*\$?([A-Za-z]+)\$?(\d+)\s*:\s*\$?\1\$?(\d+)\s*\)", re.IGNORECASE
)


def find_grand_total_row(ws, start_row: int) -> int | None:
    """Looks for a running-total row like =SUM(G5:G67) — see app.py for the full rationale."""
    max_col = max(ws.max_column, 1)
    for r in range(start_row, ws.max_row + 1):
        for c in range(1, max_col + 1):
            val = ws.cell(row=r, column=c).value
            if isinstance(val, str) and val.startswith("=") and "SUM(" in val.upper():
                m = _SUM_RANGE_RE.search(val)
                if m and int(m.group(3)) < r:
                    return r
    return None


def extend_sum_ranges(ws, total_row: int, old_last_row: int, new_last_row: int) -> int:
    """Extends every SUM(...) formula in total_row ending at old_last_row to end at new_last_row instead."""
    max_col = max(ws.max_column, 1)
    extended = 0
    for c in range(1, max_col + 1):
        cell = ws.cell(row=total_row, column=c)
        val = cell.value
        if not (isinstance(val, str) and val.startswith("=") and "SUM(" in val.upper()):
            continue
        m = _SUM_RANGE_RE.search(val)
        if m and int(m.group(3)) == old_last_row:
            col_letter, start_r = m.group(1), m.group(2)
            new_formula = (
                val[: m.start()]
                + f"SUM({col_letter}{start_r}:{col_letter}{new_last_row})"
                + val[m.end() :]
            )
            cell.value = new_formula
            extended += 1
    return extended


def write_mapping_to_excel(
    file_bytes: bytes,
    sheet_name: str,
    mapping_rows: list[dict[str, Any]],
    col_map: ColumnMapping,
    furniture_total: float | None = None,
    baseline_furniture_value: float | None = None,
) -> tuple[bytes, list[str]]:
    """
    Appends new Room-section headers + item rows below the last used row of
    the template. Identical logic to the original app.py's
    write_mapping_to_excel — see there for the full column-by-column and
    formula-chain rationale. Returns (workbook_bytes, warnings).
    """
    wb = load_workbook_from_bytes(file_bytes)
    warnings: list[str] = []

    try:
        if sheet_name not in wb.sheetnames:
            raise LogicError(f"❌ Sheet '{sheet_name}' not found in the workbook.")
        ws = wb[sheet_name]

        supplier_idx = column_index_from_string(col_map.supplier_col)
        room_idx = column_index_from_string(col_map.room_col)
        item_idx = column_index_from_string(col_map.item_col)
        qty_idx = column_index_from_string(col_map.qty_col)
        custom_price_idx = column_index_from_string(col_map.custom_made_price_col)
        purchased_price_idx = column_index_from_string(col_map.purchased_price_col)
        h_idx = column_index_from_string(col_map.formula_h_col)
        i_idx = column_index_from_string(col_map.formula_i_col)
        j_idx = column_index_from_string(col_map.formula_j_col)
        k_col = col_map.formula_k_col
        k_idx = column_index_from_string(col_map.formula_k_col)
        l_idx = column_index_from_string(col_map.formula_l_col)
        m_idx = column_index_from_string(col_map.formula_m_col)
        q_idx = column_index_from_string(col_map.formula_additional_item_col)
        r_idx = column_index_from_string(col_map.formula_purchase_compare_col)
        anchor = col_map.multiplier_anchor_row

        room_header_min_col = min(
            supplier_idx, room_idx, item_idx, qty_idx, custom_price_idx, purchased_price_idx,
            h_idx, i_idx, j_idx, k_idx, l_idx, m_idx, q_idx, r_idx,
        )
        room_header_max_col = max(
            supplier_idx, room_idx, item_idx, qty_idx, custom_price_idx, purchased_price_idx,
            h_idx, i_idx, j_idx, k_idx, l_idx, m_idx, q_idx, r_idx,
        )

        rooms_order: list[str] = []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in mapping_rows:
            room = str(row.get("room") or "Unspecified")
            if room not in grouped:
                grouped[room] = []
                rooms_order.append(room)
            grouped[room].append(row)

        total_row = find_grand_total_row(ws, col_map.start_row)
        old_last_row = get_append_start_row(ws, col_map.start_row, before_row=total_row) - 1
        current_row = old_last_row + 1

        if total_row is not None:
            rows_needed = sum(1 + len(items) for items in grouped.values())
            available = total_row - current_row
            if available < rows_needed:
                warnings.append(
                    f"⚠️ พบแถวสรุปยอดที่แถว {total_row} แต่เหลือช่องว่างให้เขียนแค่ {max(available,0)} "
                    f"แถว (ต้องการ {rows_needed} แถว) — ระบบจะเขียนข้อมูลต่อจากที่มีอยู่ตามปกติ "
                    f"แต่คุณต้องตรวจสอบ/ขยายช่วง SUM ที่แถว {total_row} ด้วยตัวเองหลัง export"
                )
                total_row = None
                current_row = get_append_start_row(ws, col_map.start_row)

        for room in rooms_order:
            ws.cell(row=current_row, column=room_idx).value = room
            for col in range(room_header_min_col, room_header_max_col + 1):
                cell = ws.cell(row=current_row, column=col)
                cell.fill = ROOM_HEADER_FILL
                cell.font = ROOM_HEADER_FONT
            current_row += 1

            item_no = 1
            for row in grouped[room]:
                order_type = row.get("order_type", "จัดซื้อ (ราคาจริง ไม่บวกกำไร)")
                unit_price = row.get("unit_price", 0) or 0
                unit_price, adj_note = apply_known_price_adjustments(row.get("item_name", ""), unit_price)

                item_text = build_item_display_text(
                    row.get("item_name", ""),
                    str(row.get("quotation_spec") or ""),
                    str(row.get("spec") or ""),
                    order_type,
                )

                ws.cell(row=current_row, column=room_idx).value = item_no
                ws.cell(row=current_row, column=item_idx).value = item_text
                ws.cell(row=current_row, column=qty_idx).value = row.get("quantity", 0)

                if order_type == "สั่งผลิต":
                    alt_price = row.get("alt_price", 0) or 0
                    pmay_price = row.get("pmay_price", 0) or 0
                    other_maker_price = row.get("other_maker_price", 0) or 0
                    if alt_price <= 0 and pmay_price <= 0 and other_maker_price <= 0 and unit_price > 0:
                        alt_price = unit_price

                    supplier = row.get("supplier", "")
                    if supplier:
                        ws.cell(row=current_row, column=supplier_idx).value = supplier
                    r = current_row

                    if alt_price > 0:
                        ws.cell(row=r, column=custom_price_idx).value = alt_price
                        ws.cell(row=r, column=custom_price_idx).number_format = MONEY_FORMAT
                        if col_map.generate_formulas:
                            ws.cell(row=r, column=h_idx).value = f"={col_map.custom_made_price_col}{r}*{col_map.formula_h_col}${anchor}"
                            ws.cell(row=r, column=h_idx).number_format = MONEY_FORMAT
                            ws.cell(row=r, column=i_idx).value = f"={col_map.formula_h_col}{r}*{col_map.formula_i_col}${anchor}"
                            ws.cell(row=r, column=i_idx).number_format = MONEY_FORMAT
                    if pmay_price > 0:
                        ws.cell(row=r, column=j_idx).value = f"=({pmay_price}*{col_map.formula_i_col}${anchor})"
                        ws.cell(row=r, column=j_idx).number_format = MONEY_FORMAT
                    if other_maker_price > 0:
                        ws.cell(row=r, column=k_idx).value = f"=({other_maker_price}*{col_map.formula_i_col}${anchor})"
                        ws.cell(row=r, column=k_idx).number_format = MONEY_FORMAT
                    if col_map.generate_formulas and (alt_price > 0 or pmay_price > 0 or other_maker_price > 0):
                        ws.cell(row=r, column=l_idx).value = f"=MAX({col_map.formula_i_col}{r}:{k_col}{r})"
                        ws.cell(row=r, column=l_idx).number_format = MONEY_FORMAT
                        ws.cell(row=r, column=m_idx).value = f"=ROUNDUP({col_map.formula_l_col}{r}*{col_map.formula_m_col}${anchor},-3)"
                        ws.cell(row=r, column=m_idx).number_format = MONEY_FORMAT
                        ws.cell(row=r, column=q_idx).value = f"={col_map.formula_m_col}{r}"
                        ws.cell(row=r, column=q_idx).number_format = MONEY_FORMAT

                elif order_type == "จัดซื้อ (บวกกำไร 10DK)":
                    supplier = row.get("supplier", "")
                    if supplier:
                        ws.cell(row=current_row, column=supplier_idx).value = supplier
                    if col_map.generate_formulas:
                        r = current_row
                        ws.cell(row=r, column=k_idx).value = f"=({unit_price}*{col_map.formula_i_col}${anchor})"
                        ws.cell(row=r, column=k_idx).number_format = MONEY_FORMAT
                        ws.cell(row=r, column=l_idx).value = f"=MAX({col_map.formula_i_col}{r}:{k_col}{r})"
                        ws.cell(row=r, column=l_idx).number_format = MONEY_FORMAT
                        ws.cell(row=r, column=m_idx).value = f"=ROUNDUP({col_map.formula_l_col}{r}*{col_map.formula_m_col}${anchor},-3)"
                        ws.cell(row=r, column=m_idx).number_format = MONEY_FORMAT
                        ws.cell(row=r, column=q_idx).value = f"={col_map.formula_m_col}{r}"
                        ws.cell(row=r, column=q_idx).number_format = MONEY_FORMAT
                    else:
                        ws.cell(row=current_row, column=k_idx).value = unit_price
                        ws.cell(row=current_row, column=k_idx).number_format = MONEY_FORMAT

                else:  # "จัดซื้อ (ราคาจริง ไม่บวกกำไร)"
                    supplier = row.get("supplier", "")
                    if supplier:
                        ws.cell(row=current_row, column=supplier_idx).value = supplier
                    ws.cell(row=current_row, column=purchased_price_idx).value = unit_price
                    ws.cell(row=current_row, column=purchased_price_idx).number_format = MONEY_FORMAT
                    if col_map.generate_formulas:
                        r = current_row
                        ws.cell(row=r, column=r_idx).value = f"={col_map.purchased_price_col}{r}"
                        ws.cell(row=r, column=r_idx).number_format = MONEY_FORMAT

                # Auto price-adjustment note (e.g. pendant lamp remote/install
                # surcharge) — shown in the Step 3 preview's "หมายเหตุ" column but
                # previously dropped on export. Column N is the designated
                # remark column, so write it straight into the cell's value —
                # overwrites that column's purchase-compare formula above for
                # จัดซื้อ rows when there's a note (per explicit instruction).
                if adj_note:
                    ws.cell(row=current_row, column=r_idx).value = adj_note

                current_row += 1
                item_no += 1

        if total_row is not None:
            new_last_row = current_row - 1
            extended = extend_sum_ranges(ws, total_row, old_last_row, new_last_row)
            if extended:
                warnings.append(
                    f"ℹ️ ขยายช่วง SUM ที่แถวสรุปยอด (แถว {total_row}) ให้ครอบคลุมแถวใหม่แล้ว "
                    f"({extended} สูตร) — ควรเปิดไฟล์ตรวจสอบยอดรวมอีกครั้งก่อนใช้งานจริง"
                )
            else:
                warnings.append(
                    f"⚠️ พบแถวสรุปยอดที่แถว {total_row} แต่ไม่สามารถขยายช่วง SUM ให้อัตโนมัติได้ "
                    f"(รูปแบบสูตรอาจไม่ตรงกับที่ระบบรองรับ) — กรุณาตรวจสอบ/แก้ไขช่วง SUM ที่แถว "
                    f"{total_row} เองหลัง export"
                )

        if furniture_total is not None and furniture_total > 0:
            summary_row = get_append_start_row(ws, col_map.start_row)
            ws.cell(row=summary_row, column=item_idx).value = (
                f"ยอดค่าเฟอร์นิเจอร์ (สรุป ณ ตอน export): {furniture_total:,.0f} บาท"
            )
            summary_row += 1
            has_baseline = bool(baseline_furniture_value and baseline_furniture_value > 0)
            # If the baseline (furniture value from the very first client quotation)
            # was never filled in, fall back to the current furniture_total so the
            # 12% management fee and net total still get computed — a blank field
            # shouldn't block the export from showing a real number.
            effective_baseline = baseline_furniture_value if has_baseline else furniture_total
            management_fee = effective_baseline * 0.12
            net_total = furniture_total + management_fee
            if has_baseline:
                ws.cell(row=summary_row, column=item_idx).value = (
                    f"ค่าดำเนินการ 12% (จากมูลค่าฐาน {effective_baseline:,.0f} บาท): "
                    f"{management_fee:,.0f} บาท"
                )
            else:
                ws.cell(row=summary_row, column=item_idx).value = (
                    f"ค่าดำเนินการ 12% (ยังไม่ได้กรอกมูลค่าฐานจากใบเสนอราคาแรกสุด — ใช้ยอดค่าเฟอร์นิเจอร์ปัจจุบัน "
                    f"{effective_baseline:,.0f} บาท แทนชั่วคราว): {management_fee:,.0f} บาท"
                )
            summary_row += 1
            ws.cell(row=summary_row, column=item_idx).value = f"ราคารวมสุทธิ: {net_total:,.0f} บาท"

        # openpyxl never computes the price formulas it just wrote (H/I/J/K/L/M/Q
        # above) — no cached value at all. If the uploaded template was saved
        # with Excel's Manual calculation mode (common in heavy BOM/finance
        # templates), that setting round-trips untouched through load/save, so
        # Excel would open this file and show those cells blank/0 until the
        # user manually presses F9 — silently disagreeing with the preview
        # (compute_price_preview), which always shows the fully computed
        # numbers. Forcing a full recalc on open fixes this regardless of the
        # template's own calc mode.
        wb.calculation.fullCalcOnLoad = True

        output = io.BytesIO()
        wb.save(output)
        return output.getvalue(), warnings

    except LogicError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to write mapping into Excel")
        raise LogicError(f"❌ Failed to write data into Excel: {e}") from e


def read_anchor_value(ws, col_letter: str, anchor_row: int) -> float | None:
    """Reads a numeric multiplier from the anchor row — handles both plain numbers and simple arithmetic formulas."""
    val = ws[f"{col_letter}{anchor_row}"].value
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str) and val.startswith("="):
        expr = val[1:]
        if re.fullmatch(r"[0-9.\s+\-*/()]+", expr):
            try:
                return float(eval(expr, {"__builtins__": {}}, {}))  # noqa: S307 - char-restricted
            except Exception:  # noqa: BLE001
                return None
    return None


def compute_price_preview(
    excel_bytes: bytes, rows: list[dict[str, Any]], col_map: ColumnMapping, sheet_name: str
) -> dict[str, Any]:
    """
    Replicates the Excel formula chain in pure Python, so the review table
    can show real computed numbers before anything is written to the file.
    Same as the original, except `excel_bytes` is now a parameter instead of
    being read from `st.session_state` directly.

    Returns {"rows": [...], "warnings": [...]}. The H/I/M anchor cells (the
    Loading Factor, the "+5%+VAT7%" multiplier, and the 10DK profit
    multiplier) must already contain a number or a simple arithmetic formula
    in the uploaded Excel template — if one is missing, every downstream
    column that depends on it comes back empty, which is confusing with no
    explanation, so that's surfaced here as an explicit warning instead of a
    silently blank table.
    """
    wb = load_workbook_from_bytes(excel_bytes)
    preview: list[dict[str, Any]] = []
    ws = wb[sheet_name] if sheet_name in wb.sheetnames else wb.active

    h_mult = read_anchor_value(ws, col_map.formula_h_col, col_map.multiplier_anchor_row)
    i_mult = read_anchor_value(ws, col_map.formula_i_col, col_map.multiplier_anchor_row)
    m_mult = read_anchor_value(ws, col_map.formula_m_col, col_map.multiplier_anchor_row)

    def roundup_thousand(x: float) -> float:
        return math.ceil(x / 1000.0) * 1000.0

    for row in rows:
        order_type = row.get("order_type", "จัดซื้อ (ราคาจริง ไม่บวกกำไร)")
        supplier = row.get("supplier", "")
        raw_unit_price = float(row.get("unit_price", 0) or 0)
        unit_price, adj_note = apply_known_price_adjustments(row.get("item_name", ""), raw_unit_price)
        qty = float(row.get("quantity", 0) or 0)
        display_name = build_item_display_text(
            row.get("item_name", ""),
            str(row.get("quotation_spec") or ""),
            str(row.get("spec") or ""),
            order_type,
        )
        out: dict[str, Any] = {
            "room": row.get("room", ""),
            "item_name": display_name,
            "quantity": qty,
            "order_type": order_type,
            "supplier": supplier,
            "unit_price": unit_price,
            "auto_note": adj_note,
        }
        if order_type == "สั่งผลิต":
            alt_price = float(row.get("alt_price", 0) or 0)
            pmay_price = float(row.get("pmay_price", 0) or 0)
            other_maker_price = float(row.get("other_maker_price", 0) or 0)
            if alt_price <= 0 and pmay_price <= 0 and other_maker_price <= 0 and unit_price > 0:
                alt_price = unit_price

            i_val = None
            if alt_price > 0:
                g = alt_price
                h = g * h_mult if h_mult is not None else None
                i_val = h * i_mult if (h is not None and i_mult is not None) else None
                out.update({"g_cost": g, "h_loading": h, "i_plus_vat": i_val})

            j_val = None
            if pmay_price > 0:
                j_val = pmay_price * i_mult if i_mult is not None else None
                out.update({"j_pmay": j_val})

            k_val = None
            if other_maker_price > 0:
                k_val = other_maker_price * i_mult if i_mult is not None else None
                out.update({"k_other": k_val})

            candidates = [v for v in (i_val, j_val, k_val) if v is not None]
            l = max(candidates) if candidates else None
            m = roundup_thousand(l * m_mult) if (l is not None and m_mult is not None) else None
            out.update({"l_chosen": l, "m_10dk_price": m})
        elif order_type == "จัดซื้อ (บวกกำไร 10DK)":
            k = unit_price * i_mult if i_mult is not None else None
            l = k
            m = roundup_thousand(l * m_mult) if (l is not None and m_mult is not None) else None
            out.update({"k_other": k, "l_chosen": l, "m_10dk_price": m})
        else:
            out.update({"n_actual_price": unit_price})
        # M (10DK Price) and N (จัดซื้อ actual price) are already lot totals for
        # this line, not per-unit — do NOT multiply by qty (matches every other
        # total in the app: _quotation_row_totals, main.py's _quotation_totals,
        # QuotationPreview.tsx's quotationTotals — all sum these as-is).
        out["line_total"] = out.get("m_10dk_price") or out.get("n_actual_price") or 0
        preview.append(out)

    warnings: list[str] = []
    needs_h = any(r.get("order_type") == "สั่งผลิต" for r in rows)
    needs_i_m = any(r.get("order_type") in ("สั่งผลิต", "จัดซื้อ (บวกกำไร 10DK)") for r in rows)
    anchor = col_map.multiplier_anchor_row
    if needs_h and h_mult is None:
        warnings.append(
            f"⚠️ ไม่พบตัวเลขที่ช่อง {col_map.formula_h_col}{anchor} ในไฟล์ Excel (Loading Factor ALT) — "
            f"คอลัมน์ 'ALT+ค่า protect+ค่าขน' และคอลัมน์ที่คำนวณต่อจากมันจะว่างจนกว่าจะมีค่านี้ "
            f"(ถ้ายังไม่ได้กด ลองกรอก/กด apply ที่การ์ด 'Loading Factor (ALT)' ด้านบนก่อน)"
        )
    if needs_i_m and i_mult is None:
        warnings.append(
            f"⚠️ ไม่พบตัวเลขที่ช่อง {col_map.formula_i_col}{anchor} ในไฟล์ Excel (ตัวคูณ +5%+VAT7%) — "
            f"กรุณากรอกค่านี้ในไฟล์เทมเพลตก่อน คอลัมน์ที่เกี่ยวข้อง (I, J, K) จะว่างจนกว่าจะกรอก"
        )
    if needs_i_m and m_mult is None:
        warnings.append(
            f"⚠️ ไม่พบตัวเลขที่ช่อง {col_map.formula_m_col}{anchor} ในไฟล์ Excel (ตัวคูณกำไร 10DK) — "
            f"คอลัมน์ '10DK Price' จะว่างจนกว่าจะกรอกค่านี้ในไฟล์เทมเพลต"
        )

    return {"rows": preview, "warnings": warnings}


# --------------------------------------------------------------------------------------
# Client-facing quotation ("ใบเสนอราคา")
# --------------------------------------------------------------------------------------

_QA_SUFFIX_RE = re.compile(r"\s*\[(?:ราคา[^\]]*ถูกแก้ไขอัตโนมัติ[^\]]*|Price Not Found)\]\s*")


def strip_qa_markers(item_name: str) -> str:
    """
    Removes internal QA/audit tags (autocorrect flags, [Price Not Found])
    that must never appear in a client-facing document. Does NOT touch the
    unbracketed ORDER_TYPE_SUFFIX appended by compute_price_preview/
    write_mapping_to_excel — that's intentional client-facing content.
    """
    return _QA_SUFFIX_RE.sub(" ", item_name).strip()


_LABEL_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _letter_label(index: int) -> str:
    """0-based index -> A, B, ..., Z, AA, AB, ... (same scheme as Excel columns)."""
    index += 1
    letters = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = _LABEL_LETTERS[rem] + letters
    return letters


def assign_quotation_labels(rows: list[dict[str, Any]]) -> list[str]:
    """
    Assigns the client-facing item label per the reference quotation
    template's own convention: a continuous numeric sequence for rows priced
    under "10DK's work" (custom-made, or purchased-with-markup), a
    continuous lettered sequence for rows priced under "งานจัดซื้อ...ราคาจริง"
    (purchased at cost — including ones still "TBC"), and the literal
    "Client's" for rows the customer supplies themselves (never priced).
    Computed fresh from the current rows every time this is called — never
    persisted — so edits made in the browser (toggling "Client's", filling
    in a price) immediately shift which sequence a row belongs to.
    """
    labels: list[str] = []
    num = 0
    letter_i = 0
    for row in rows:
        if row.get("is_client_owned"):
            labels.append("Client's")
        elif row.get("dk_work_price") is not None:
            num += 1
            labels.append(str(num))
        else:
            labels.append(_letter_label(letter_i))
            letter_i += 1
    return labels


# Matches a combined "<Room Name> - <Floor>" room string, as commonly
# written in source floor-plan/furniture-list files (e.g. "Living & Play
# Area - 1st Floor"), so build_quotation_rows can split it into the
# quotation's own two-level Floor > Room grouping automatically instead of
# requiring the floor to be typed in by hand every time.
_FLOOR_SUFFIX_RE = re.compile(
    r"^(?P<room>.+?)\s*[-–—:]\s*(?P<floor>"
    r"\d+(?:st|nd|rd|th)\s*Floor"
    r"|Floor\s*\d+"
    r"|Ground\s*Floor"
    r"|Roof(?:top)?(?:\s*Floor)?"
    r"|Basement(?:\s*Floor)?"
    r"|ชั้น(?:ที่)?\s*\S+"
    r")\s*$",
    re.IGNORECASE,
)


def split_room_floor(room: str) -> tuple[str, str]:
    """
    Splits a combined "<Room Name> - <Floor>" string into (room, floor).
    Returns (room, "") unchanged if no recognizable floor suffix is found —
    the floor band then stays blank, same as before this split existed, and
    can still be filled in by hand in the quotation preview table.
    """
    m = _FLOOR_SUFFIX_RE.match(room.strip())
    if not m:
        return room.strip(), ""
    return m.group("room").strip(), m.group("floor").strip()


def build_quotation_rows(preview_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Reduces compute_price_preview()-shaped rows (or
    parse_exported_excel_for_quotation's output, same shape) down to the
    client-facing quotation columns: a floor/room grouping key, the 10DK
    price (M) and the purchased-at-cost price (N), a client-safe remark, and
    a display label (see assign_quotation_labels). Returns (rows, warnings)
    — one warning per row missing both prices.
    """
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    for i, row in enumerate(preview_rows, start=1):
        item_name = strip_qa_markers(str(row.get("item_name", "")))
        # 0 and None both mean "no price yet" throughout this codebase (e.g.
        # write_mapping_to_excel's `if alt_price > 0` checks) — compute_price_preview's
        # default order_type branch always sets n_actual_price to a real
        # float (0.0 when unpriced), it's never omitted/None like the other
        # branches, so a falsy check (not a None check) is required here.
        dk_work_price = row.get("m_10dk_price") or None
        actual_price_purchase = row.get("n_actual_price") or None
        # Round off here, not just at display time — compute_price_preview's
        # chained multiplier math (unit_price * h_mult * i_mult * m_mult...)
        # accumulates binary floating-point noise (e.g. 151812.93750000003),
        # which would otherwise show up raw in the quotation's editable
        # price inputs (those aren't run through any formatter).
        if dk_work_price is not None:
            dk_work_price = round(dk_work_price, 2)
        if actual_price_purchase is not None:
            actual_price_purchase = round(actual_price_purchase, 2)
        if dk_work_price is None and actual_price_purchase is None:
            warnings.append(f"⚠️ รายการ '{item_name}' ยังไม่มีราคา — จะไม่รวมในยอดสุทธิ")
        room_name, floor = split_room_floor(str(row.get("room", "")))
        rows.append({
            "item_no": i,
            "floor": floor,
            "room": room_name,
            "item_name": item_name,
            "quantity": row.get("quantity", 0),
            "dk_work_price": dk_work_price,
            "actual_price_purchase": actual_price_purchase,
            "is_client_owned": False,
            "remark": str(row.get("auto_note") or "").strip(),
        })
    for row, label in zip(rows, assign_quotation_labels(rows)):
        row["label"] = label
    return rows, warnings


def read_workbook_from_bytes_computed(file_bytes: bytes):
    """
    Loads a workbook with data_only=True, so formula cells return their
    last-saved cached value (what Excel computed and stored on save) rather
    than the raw formula string — and return None if no cached value exists
    at all (e.g. the file was never opened/saved in real Excel since export).
    Separate from load_workbook_from_bytes, which is always data_only=False
    and used only for formula-preserving writes; a data_only=True workbook
    cannot have formulas written back into it.
    """
    try:
        return openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, keep_vba=False)
    except InvalidFileException as e:
        raise LogicError("❌ The uploaded file is not a valid .xlsx workbook.") from e
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to read Excel file (computed-values mode)")
        raise LogicError(f"❌ Failed to read Excel file: {e}") from e


def parse_exported_excel_for_quotation(
    file_bytes: bytes, sheet_name: str, col_map: ColumnMapping
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Reverse-parses a previously-exported (and possibly hand-edited-in-Excel)
    workbook's room-header + item rows back into a flat list of
    {room, item_name, quantity, m_10dk_price, n_actual_price} dicts, for the
    transient "upload Excel" quotation-PDF path. Purely a read — never
    persists anything, and the caller must not write this into any
    project's stored state.

    Mirrors write_mapping_to_excel's write pattern in reverse: a "room
    header" row is one where the item-name column is empty but the room
    column holds a non-empty string (write_mapping_to_excel writes the room
    name into room_idx ONLY on header rows — on item rows that same column
    instead holds the item's running number, so it is never read as a room
    name here).

    The 10DK's-work and purchase-price columns are read from fixed columns
    K and L respectively — per client request, hardcoded specifically for
    this "upload Excel" quotation path rather than col_map's own
    formula_m_col/purchased_price_col (M/N), which the write/compute-preview
    pipeline elsewhere in this module still uses unchanged.
    """
    wb = read_workbook_from_bytes_computed(file_bytes)
    if sheet_name not in wb.sheetnames:
        raise LogicError(f"❌ Sheet '{sheet_name}' not found in the workbook.")
    ws = wb[sheet_name]

    room_idx = column_index_from_string(col_map.room_col)
    item_idx = column_index_from_string(col_map.item_col)
    qty_idx = column_index_from_string(col_map.qty_col)
    m_idx = column_index_from_string("K")
    n_idx = column_index_from_string("L")

    # find_grand_total_row() detects the summary row by its literal
    # "=SUM(...)" formula text — but `ws` above was loaded with
    # data_only=True (so K/L resolve to computed numbers, needed to read
    # each item's actual price), and a data_only=True sheet never exposes
    # formula strings, only their cached values. So detection needs a
    # second, formula-preserving load of the same bytes purely to locate
    # the boundary; without this, the grand-total row (item column holds
    # "ราคารวมสุทธิ: ... บาท", K/L hold the SUM'd totals) was being read
    # back in as if it were an ordinary priced item.
    formula_wb = load_workbook_from_bytes(file_bytes)
    formula_ws = formula_wb[sheet_name] if sheet_name in formula_wb.sheetnames else None
    total_row = find_grand_total_row(formula_ws, col_map.start_row) if formula_ws is not None else None
    last_row = (total_row - 1) if total_row is not None else ws.max_row

    def as_price(val: Any) -> float | None:
        return float(val) if isinstance(val, (int, float)) and val else None

    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    current_room = ""
    item_rows_found = 0
    priced_rows_found = 0

    for r in range(col_map.start_row, last_row + 1):
        item_val = ws.cell(row=r, column=item_idx).value
        room_val = ws.cell(row=r, column=room_idx).value
        item_text = str(item_val).strip() if item_val is not None else ""

        if not item_text:
            if room_val not in (None, ""):
                current_room = str(room_val).strip()
            continue

        item_rows_found += 1
        m_price = as_price(ws.cell(row=r, column=m_idx).value)
        n_price = as_price(ws.cell(row=r, column=n_idx).value)
        if m_price is not None or n_price is not None:
            priced_rows_found += 1
        else:
            warnings.append(
                f"⚠️ แถว {r}: รายการ '{item_text}' ไม่พบราคาในช่อง "
                f"{get_column_letter(m_idx)}/{get_column_letter(n_idx)}"
            )

        rows.append({
            "room": current_room,
            "item_name": strip_qa_markers(item_text),
            "quantity": ws.cell(row=r, column=qty_idx).value or 0,
            "m_10dk_price": m_price,
            "n_actual_price": n_price,
        })

    if item_rows_found > 0 and priced_rows_found == 0:
        warnings.append(
            "⚠️ ไม่พบราคาในแถวใดเลย — ถ้าไฟล์นี้มีสูตรที่ยังไม่เคยถูกคำนวณ (เช่น export แล้วอัปโหลดกลับทันที "
            "โดยไม่เคยเปิดด้วย Microsoft Excel) กรุณาเปิดไฟล์ด้วย Excel แล้วบันทึกอีกครั้งก่อนอัปโหลด "
            "เพื่อให้ Excel คำนวณค่าสูตรและบันทึกผลลัพธ์ไว้ในไฟล์"
        )

    # Cross-check against the file's own grand-total row (if one was
    # detected): re-summing every parsed row's K/L value — with the exact
    # same rules the app actually uses for the displayed Total (no quantity
    # multiplier, "Option 1 only" — see _is_priced_option) — should match
    # that row's own SUM(K...)/SUM(L...) cached value almost exactly, since
    # real exported files already build their own total the same option-
    # aware way. A mismatch means the parse likely missed a row (or grabbed
    # an extra one), worth surfacing instead of silently trusting either
    # number. (An earlier version of this check compared the raw,
    # un-filtered sum instead — that produced a false-positive warning on
    # every file with an "Option 2/3" alternate, since the file's own total
    # already excludes those but the raw re-sum didn't.)
    if total_row is not None:

        def excel_column_total(idx: int) -> float | None:
            val = ws.cell(row=total_row, column=idx).value
            return float(val) if isinstance(val, (int, float)) else None

        excel_k_total = excel_column_total(m_idx)
        excel_l_total = excel_column_total(n_idx)
        priced_rows = [r for r in rows if _is_priced_option(r["item_name"])]
        parsed_k_total = sum(r["m_10dk_price"] or 0 for r in priced_rows)
        parsed_l_total = sum(r["n_actual_price"] or 0 for r in priced_rows)

        def check_total(col_letter: str, label: str, excel_total: float | None, parsed_total: float) -> None:
            if excel_total is None:
                return
            if abs(excel_total - parsed_total) > 1:  # >1 บาท — ignore float rounding dust
                warnings.append(
                    f"⚠️ ยอดรวมคอลัมน์ {col_letter} ({label}) ไม่ตรงกับไฟล์ต้นฉบับ — ไฟล์ Excel เดิมแสดง "
                    f"{excel_total:,.0f} บาท แต่ระบบคำนวณได้ {parsed_total:,.0f} บาท "
                    f"(อาจมีบางแถวถูกข้ามหรือรวมผิดพลาด กรุณาตรวจสอบไฟล์ต้นฉบับ)"
                )

        check_total("K", "10DK Price", excel_k_total, parsed_k_total)
        check_total("L", "เบิกจ่ายตามราคาจริง", excel_l_total, parsed_l_total)

    return rows, warnings


_QUOTATION_HEADERS = [
    "#",
    "Furniture List",
    "จำนวน",
    "10DK's work",
    "ประมาณการงานจัดซื้อ\nเบิกจ่ายตามราคาจริง",
    "หมายเหตุ",
]
# Raw mm numbers (not reportlab points) so the DOCX table can be given the
# exact same column proportions via docx.shared.Mm — this is what keeps the
# Word export's table looking identical to the PDF's instead of Word's
# default auto-sized columns.
_QUOTATION_COL_WIDTHS_MM = [18, 69, 14, 24, 30, 25]
_QUOTATION_COL_WIDTHS = [w * mm for w in _QUOTATION_COL_WIDTHS_MM]

# Matches the reference template's own palette exactly: a plain white
# column-header row with solid black grid lines, a medium-gray "Floor" band,
# and a near-black (not navy) "Room" band.
_QUOTATION_HEADER_BG = "#ffffff"
_QUOTATION_GRID_COLOR = "#000000"
_QUOTATION_FLOOR_BG = "#a6a6a6"
_QUOTATION_ROOM_BG = "#262626"
_QUOTATION_CLIENT_ROW_BG = "#e5e7eb"
# Rows priced under the purchase column (lettered labels A, B, C, ...) — 30%
# gray, per client request, to set them apart from the numbered rows.
_QUOTATION_LETTER_ROW_BG = "#b3b3b3"


def _fmt_money(v: float | None) -> str:
    return "" if v is None else f"{v:,.2f}"


_REMARK_EMPHASIS_RE = re.compile(r"\*\*(.+?)\*\*")


def _split_remark_emphasis(line: str) -> list[tuple[str, bool]]:
    """
    Splits a remark line on `**text**` markers into (segment, is_emphasized)
    pairs — matches the reference template's own convention, where only
    part of a remark bullet is bold+underlined (e.g. "ราคาดังกล่าว
    **ไม่รวมฟูกที่นอน**"), not the whole line.
    """
    segments: list[tuple[str, bool]] = []
    pos = 0
    for m in _REMARK_EMPHASIS_RE.finditer(line):
        if m.start() > pos:
            segments.append((line[pos : m.start()], False))
        segments.append((m.group(1), True))
        pos = m.end()
    if pos < len(line):
        segments.append((line[pos:], False))
    return segments or [(line, False)]


def _sized_image(image_bytes: bytes, max_w: float, max_h: float) -> Image:
    """Scales an image to fit within (max_w, max_h) while preserving aspect ratio."""
    iw, ih = ImageReader(io.BytesIO(image_bytes)).getSize()
    scale = min(max_w / iw, max_h / ih)
    return Image(io.BytesIO(image_bytes), width=iw * scale, height=ih * scale)


def _quotation_group_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("floor") or "").strip(), str(row.get("room") or "Unspecified"))


_OPTION_RE = re.compile(r"option\s*(\d+)", re.IGNORECASE)


def _is_priced_option(item_name: str) -> bool:
    """An item name with no "Option N" label always counts. One that does
    (e.g. "TV Console Option 1" / "Option 2" / "Option 3" — alternate design
    choices for the same piece, client picks one) counts only as "Option 1"
    — the others are shown for reference but excluded from the total, since
    summing every option would charge for a single piece multiple times."""
    m = _OPTION_RE.search(item_name)
    return not m or m.group(1) == "1"


def _quotation_row_totals(row: dict[str, Any]) -> tuple[float, float]:
    """Returns (dk_work_line_total, purchase_line_total) — 0 for a "Client's"
    row (never priced) or a non-"Option 1" alternate (see _is_priced_option).
    The price columns (10DK's work / purchase) are taken as-is, NOT
    multiplied by quantity — per explicit client instruction, quantity is
    informational only and never factors into the Total."""
    if row.get("is_client_owned") or not _is_priced_option(str(row.get("item_name") or "")):
        return 0.0, 0.0
    dk = float(row.get("dk_work_price") or 0) if row.get("dk_work_price") is not None else 0.0
    purchase = float(row.get("actual_price_purchase") or 0) if row.get("actual_price_purchase") is not None else 0.0
    return dk, purchase


def _build_quotation_table_story(
    rows: list[dict[str, Any]],
    deposit_deduction: float = 0,
    remarks: str = "",
    grand_total_note: str = "",
) -> list[Any]:
    """
    Builds the priced-furniture table + totals + remarks flowables shared by
    both the standalone quotation PDF (generate_quotation_pdf, which wraps
    this with its own letterhead/title) and the combined contract PDF
    (generate_contract_pdf, which appends this as the document's final
    section — matching the reference contract template's own "เอกสารแนบ
    (18)" price table). Self-contained (registers fonts, defines its own
    styles) so either caller can use it without sharing any state.
    """
    _ensure_thai_fonts_registered()

    def esc(text: Any) -> str:
        return xml_escape(str(text if text is not None else ""))

    body_style = ParagraphStyle("QuoteBody", fontName="TPTankhun", fontSize=13, leading=16)
    body_red_style = ParagraphStyle("QuoteBodyRed", parent=body_style, textColor=colors.HexColor("#dc2626"))
    header_style = ParagraphStyle("QuoteHeader", fontName="TPTankhun-Bold", fontSize=13, leading=16)
    floor_style = ParagraphStyle(
        "QuoteFloor", fontName="TPTankhun-Bold", fontSize=10, leading=13, textColor=colors.white
    )
    room_style = ParagraphStyle(
        "QuoteRoom", fontName="TPTankhun-Bold", fontSize=13, leading=16, textColor=colors.white
    )
    totals_label_style = ParagraphStyle("TotLabel", fontName="TPTankhun", fontSize=13, leading=16)
    totals_label_bold_style = ParagraphStyle("TotLabelBold", parent=totals_label_style, fontName="TPTankhun-Bold")
    totals_value_style = ParagraphStyle("TotValue", parent=totals_label_style, alignment=TA_RIGHT)
    totals_value_bold_style = ParagraphStyle("TotValueBold", parent=totals_value_style, fontName="TPTankhun-Bold")
    totals_value_red_style = ParagraphStyle(
        "TotValueRed", parent=totals_value_style, textColor=colors.HexColor("#dc2626")
    )
    totals_label_red_style = ParagraphStyle(
        "TotLabelRed", parent=totals_label_style, textColor=colors.HexColor("#dc2626")
    )
    totals_note_style = ParagraphStyle(
        "TotNote", fontName="TPTankhun", fontSize=13, leading=16, textColor=colors.HexColor("#dc2626")
    )
    remarks_heading_style = ParagraphStyle("RemarksHeading", fontName="TPTankhun-Bold", fontSize=10, leading=13)

    story: list[Any] = []

    # A single continuous table for the whole document: the column-header
    # row sits once at the very top (repeated on later pages via
    # repeatRows=1), with Floor/Room band rows and item rows below it as
    # ordinary rows of the same table — matching the reference template,
    # which never re-prints the column header between rooms.
    header_row = [Paragraph(h, header_style) for h in _QUOTATION_HEADERS]
    table_data: list[list[Any]] = [header_row]
    band_rows: list[tuple[int, str]] = []  # (row_index, "floor" | "room")
    client_row_indices: list[int] = []
    letter_row_indices: list[int] = []

    groups_order: list[tuple[str, str]] = []
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = _quotation_group_key(row)
        grouped.setdefault(key, [])
        if key not in groups_order:
            groups_order.append(key)
        grouped[key].append(row)

    dk_work_subtotal = 0.0
    purchase_subtotal = 0.0
    last_floor: str | None = None
    for floor, room in groups_order:
        if floor and floor != last_floor:
            table_data.append([Paragraph(esc(floor), floor_style), "", "", "", "", ""])
            band_rows.append((len(table_data) - 1, "floor"))
        last_floor = floor or last_floor

        table_data.append([Paragraph(esc(room), room_style), "", "", "", "", ""])
        band_rows.append((len(table_data) - 1, "room"))

        for row in grouped[(floor, room)]:
            is_client = bool(row.get("is_client_owned"))
            price = row.get("dk_work_price")
            purchase = row.get("actual_price_purchase")
            dk_total, purchase_total = _quotation_row_totals(row)
            dk_work_subtotal += dk_total
            purchase_subtotal += purchase_total
            if is_client:
                client_row_indices.append(len(table_data))
                price_cell = ""
                purchase_cell = ""
            else:
                price_cell = _fmt_money(price)
                # Matches the reference template's own convention: a
                # not-yet-priced item shows "TBC" rather than a blank cell.
                purchase_cell = _fmt_money(purchase) if (price or purchase) else "TBC"
                # Same "lettered" condition as assign_quotation_labels: no
                # 10DK's-work price -> priced under the purchase column.
                if price is None:
                    letter_row_indices.append(len(table_data))
            qty = row.get("quantity")
            table_data.append([
                Paragraph(esc(row.get("label", "")), body_style),
                Paragraph(esc(row.get("item_name", "")), body_style),
                Paragraph(_fmt_money(qty) if qty else "", body_style),
                Paragraph(price_cell, body_style),
                Paragraph(purchase_cell, body_red_style if purchase_cell == "TBC" else body_style),
                Paragraph(esc(row.get("remark", "")), body_style),
            ])

    table = Table(table_data, colWidths=_QUOTATION_COL_WIDTHS, repeatRows=1)
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(_QUOTATION_HEADER_BG)),
        ("GRID", (0, 0), (-1, -1), 0.75, colors.HexColor(_QUOTATION_GRID_COLOR)),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("ALIGN", (0, 1), (0, -1), "CENTER"),
        ("ALIGN", (2, 1), (4, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for ridx, kind in band_rows:
        bg = _QUOTATION_FLOOR_BG if kind == "floor" else _QUOTATION_ROOM_BG
        style_cmds.append(("SPAN", (0, ridx), (-1, ridx)))
        style_cmds.append(("BACKGROUND", (0, ridx), (-1, ridx), colors.HexColor(bg)))
        style_cmds.append(("ALIGN", (0, ridx), (-1, ridx), "LEFT"))
        style_cmds.append(("LEFTPADDING", (0, ridx), (-1, ridx), 6))
    for ridx in client_row_indices:
        style_cmds.append(("BACKGROUND", (0, ridx), (-1, ridx), colors.HexColor(_QUOTATION_CLIENT_ROW_BG)))
    for ridx in letter_row_indices:
        style_cmds.append(("BACKGROUND", (0, ridx), (-1, ridx), colors.HexColor(_QUOTATION_LETTER_ROW_BG)))
    table.setStyle(TableStyle(style_cmds))
    story.append(table)

    vat = dk_work_subtotal * 0.07
    grand_total = dk_work_subtotal + vat - (deposit_deduction or 0)

    def totals_row(
        label: str,
        dk_val: float | None,
        purchase_val: float | None = None,
        note: str = "",
        bold: bool = False,
        dk_style: ParagraphStyle | None = None,
        label_style: ParagraphStyle | None = None,
    ) -> list[Any]:
        label_style = label_style or (totals_label_bold_style if bold else totals_label_style)
        value_style = totals_value_bold_style if bold else totals_value_style
        return [
            # The label must live in column 0, not 1 — SPAN(0,i)-(2,i) below
            # displays only the top-left cell of the merged range; anything
            # placed in columns 1/2 instead is silently discarded.
            Paragraph(esc(label), label_style),
            "",
            "",
            Paragraph(_fmt_money(dk_val), dk_style or value_style) if dk_val is not None else "",
            Paragraph(_fmt_money(purchase_val), value_style) if purchase_val is not None else "",
            Paragraph(esc(note), totals_note_style) if note else "",
        ]

    totals_data = [totals_row("Total", dk_work_subtotal, purchase_subtotal), totals_row("Vat 7 %", vat)]
    if deposit_deduction:
        totals_data.append(totals_row(
            "หักค่ามัดจำออกแบบ", deposit_deduction,
            dk_style=totals_value_red_style, label_style=totals_label_red_style,
        ))
    totals_data.append(totals_row("Grand Total", grand_total, purchase_subtotal, grand_total_note, bold=True))

    totals_table = Table(totals_data, colWidths=_QUOTATION_COL_WIDTHS)
    grand_total_row_idx = len(totals_data) - 1
    totals_table.setStyle(TableStyle([
        *[("SPAN", (0, i), (2, i)) for i in range(len(totals_data))],
        ("GRID", (0, 0), (-1, -1), 0.75, colors.HexColor(_QUOTATION_GRID_COLOR)),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(_QUOTATION_LETTER_ROW_BG)),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEABOVE", (1, grand_total_row_idx), (-1, grand_total_row_idx), 0.75, colors.HexColor(_QUOTATION_GRID_COLOR)),
    ]))
    story.append(totals_table)

    remark_lines = [line.strip() for line in (remarks or "").splitlines() if line.strip()]
    if remark_lines:
        story.append(Spacer(1, 6 * mm))
        story.append(Paragraph("<u>Remarks:</u>", remarks_heading_style))
        for line in remark_lines:
            bits = "".join(
                f"<b><u>{esc(text)}</u></b>" if emph else esc(text)
                for text, emph in _split_remark_emphasis(line)
            )
            story.append(Paragraph(f"- {bits}", body_style))

    return story


def generate_quotation_pdf(
    rows: list[dict[str, Any]],
    client_name: str,
    project_name: str,
    quotation_date: str,
    logo_bytes: bytes | None = None,
    deposit_deduction: float = 0,
    remarks: str = "",
    grand_total_note: str = "",
) -> bytes:
    """
    Renders the client-facing "ใบเสนอราคา" as a real PDF via reportlab's
    platypus flowables, matching the reference template exactly: a
    right-aligned letterhead, a centered bold title, two-level Floor > Room
    header bands, a light (not dark) column-header row, "Client's" rows
    shaded gray with blank price cells, a Total row split into the 10DK's-
    work and purchase-at-cost columns, VAT 7% (on the 10DK's-work column
    only), an optional red "หักค่ามัดจำออกแบบ" deduction row, a bold Grand
    Total row with a small note, and a final "Remarks:" bullet section.
    rows must already carry a `label` (see assign_quotation_labels) and be
    given in the order they should render — caller is responsible for
    floor/room-adjacency. logo_bytes, if given (the company's once-uploaded
    logo — see db.get_company_logo), is placed above the company block in
    the letterhead; otherwise that space is simply left blank.
    """
    _ensure_thai_fonts_registered()

    def esc(text: Any) -> str:
        return xml_escape(str(text if text is not None else ""))

    # Letterhead (company name / address / date) only — per client request,
    # size 7.5pt. Font is "Avenir" per that same request, but Avenir is a
    # commercial font not present on this system/repo — using TPTankhun as a
    # placeholder here until the actual Avenir .ttf/.otf files are supplied.
    title_style = ParagraphStyle("QuoteTitle", fontName="TPTankhun-Bold", fontSize=7.5, leading=10)
    meta_style = ParagraphStyle("QuoteMeta", fontName="TPTankhun", fontSize=7.5, leading=10)
    title_center_style = ParagraphStyle(
        "QuoteTitleCenter", fontName="TPTankhun-Bold", fontSize=14, leading=17, alignment=TA_CENTER
    )
    title_right_style = ParagraphStyle("QuoteTitleRight", parent=title_style, alignment=TA_RIGHT)
    meta_right_style = ParagraphStyle("QuoteMetaRight", parent=meta_style, alignment=TA_RIGHT)

    story: list[Any] = []

    # Letterhead: logo stacked above the company info block, both
    # right-aligned as a single column (matches the reference template).
    story.append(Spacer(1, 6 * mm))
    if logo_bytes:
        logo = _sized_image(logo_bytes, 10 * mm, 20 * mm)
        logo.hAlign = "RIGHT"
        story.append(logo)
        story.append(Spacer(1, 2 * mm))
    story.append(Paragraph("10DK Co., Ltd", title_right_style))
    story.append(Paragraph("141 Major Tower Thonglo, Khlong Ton Nua, Bangkok 10110", meta_right_style))
    if quotation_date:
        story.append(Paragraph(esc(quotation_date), meta_right_style))
    story.append(Spacer(1, 6 * mm))

    project_name = project_name.strip()
    client_name = client_name.strip()
    if project_name and client_name:
        title_line = f"รายการเฟอร์นิเจอร์และราคาสำหรับ{esc(project_name)}: {esc(client_name)}"
    elif project_name:
        title_line = f"รายการเฟอร์นิเจอร์และราคาสำหรับ{esc(project_name)}"
    elif client_name:
        title_line = f"รายการเฟอร์นิเจอร์และราคา: {esc(client_name)}"
    else:
        title_line = "รายการเฟอร์นิเจอร์และราคา"
    story.append(Paragraph(title_line, title_center_style))
    story.append(Spacer(1, 6 * mm))

    story.extend(_build_quotation_table_story(rows, deposit_deduction, remarks, grand_total_note))

    output = io.BytesIO()
    doc = SimpleDocTemplate(
        output, pagesize=A4, topMargin=10 * mm, bottomMargin=10 * mm, leftMargin=10 * mm, rightMargin=10 * mm
    )
    doc.build(story)
    return output.getvalue()


def _build_contract_text_story(details: dict[str, Any]) -> list[Any]:
    """
    Renders ข้อ 1-8 of the 10DK interior-design contract template. Only the
    template's actual blanks are read from `details` (a ContractDetails
    dict) — the fixed clauses (ช่างฝีมือ, ไม่รวมงานแก้ไข, ความเสียหาย,
    ดอกเบี้ย 15% ต่อปี, รับประกัน 1 ปี, การปิดท้ายสัญญา) are hardcoded here
    exactly as worded in the reference template.
    """
    _ensure_thai_fonts_registered()

    def esc(text: Any) -> str:
        return xml_escape(str(text if text is not None else ""))

    def g(key: str) -> str:
        return esc(details.get(key) or "")

    title_style = ParagraphStyle("ContractTitle", fontName="TPTankhun-Bold", fontSize=18, leading=22, alignment=TA_CENTER)
    subtitle_style = ParagraphStyle("ContractSubtitle", fontName="TPTankhun", fontSize=15, leading=19, alignment=TA_CENTER)
    date_style = ParagraphStyle("ContractDate", fontName="TPTankhun", fontSize=15, leading=19, alignment=TA_RIGHT)
    body_style = ParagraphStyle(
        "ContractBody", fontName="TPTankhun", fontSize=15, leading=21, alignment=TA_JUSTIFY, firstLineIndent=24
    )
    clause_label_style = ParagraphStyle("ContractClauseLabel", parent=body_style, fontName="TPTankhun-Bold")
    sign_style = ParagraphStyle("ContractSign", fontName="TPTankhun", fontSize=15, leading=19, alignment=TA_CENTER)

    money = lambda v: _fmt_money(v) if v is not None else "___________"  # noqa: E731

    story: list[Any] = [
        Paragraph("สัญญาจ้างตกแต่งภายใน", title_style),
        Paragraph(f"สำหรับ{g('property_description')}", subtitle_style),
        Spacer(1, 4 * mm),
        Paragraph(f"วันที่ {g('contract_date')}", date_style),
        Spacer(1, 4 * mm),
        Paragraph(
            f"สัญญาฉบับนี้ทำขึ้นที่{g('property_description')} ระหว่าง {g('client_name')} "
            f"เลขที่บัตรประชาชน {g('client_id_number')} ที่อยู่ {g('client_address')} "
            f"ซึ่งต่อไปในสัญญานี้ เรียกว่า &ldquo;ผู้ว่าจ้าง&rdquo; ฝ่ายหนึ่ง กับ {g('contractor_name')} "
            f"โดย {g('contractor_signatory')} กรรมการผู้มีอำนาจ อยู่ที่ {g('contractor_address')} "
            f"ซึ่งต่อไปนี้ในสัญญานี้เรียกว่า &ldquo;ผู้รับจ้าง&rdquo; อีกฝ่ายหนึ่ง "
            f"คู่สัญญาทั้งสองฝ่ายตกลงทำสัญญากัน มีข้อความดังต่อไปนี้",
            body_style,
        ),
        Spacer(1, 3 * mm),
    ]

    def clause(number: int, text: str) -> None:
        story.append(Paragraph(f"<b>ข้อ {number}.</b> {text}", body_style))
        story.append(Spacer(1, 2 * mm))

    clause(
        1,
        f"ผู้ว่าจ้างตกลงจ้าง และผู้รับจ้างตกลงรับจ้างออกแบบตกแต่งภายในสำหรับ{g('property_description')} "
        f"ซึ่งรวมถึงดำเนินการจัดหาเฟอร์นิเจอร์ หรือสั่งทำเฟอร์นิเจอร์ ตามแบบเฟอร์นิเจอร์ "
        f"รายการที่ {g('included_item_range')} ในเอกสารแนบท้ายสัญญา หน้า {g('included_item_page')} "
        f"ยกเว้นเฟอร์นิเจอร์ รายการที่ {g('excluded_item_range')} ในเอกสารแนบท้ายสัญญา หน้า {g('excluded_item_page')} "
        f"ซึ่งผู้ว่าจ้างจะต้องชำระราคาเองตามราคาที่ซื้อจริง",
    )
    clause(
        2,
        f"ผู้ว่าจ้างตกลงชำระค่าจ้างให้แก่ผู้รับจ้าง รวมเป็นเงิน {money(details.get('total_price'))} บาท "
        f"แบ่งชำระเป็น 3 งวด ดังนี้ งวดที่ 1 จำนวน {money(details.get('installment_1_amount'))} บาท "
        f"ชำระวันทำสัญญา งวดที่ 2 จำนวน {money(details.get('installment_2_amount'))} บาท "
        f"ชำระวันส่งมอบงาน โดยผู้รับจ้างจะแจ้งให้ผู้ว่าจ้างทราบล่วงหน้าเป็นลายลักษณ์อักษร ไม่น้อยกว่า 3 วัน "
        f"งวดที่ 3 จำนวน {money(details.get('installment_3_amount'))} บาท ชำระวันส่งมอบงานครบถ้วน "
        f"โดยผู้รับจ้างจะแจ้งให้ผู้ว่าจ้างทราบล่วงหน้าเป็นลายลักษณ์อักษร ไม่น้อยกว่า 3 วัน "
        f"โดยชำระด้วยวิธีโอนเข้าบัญชี{g('bank_name')} {g('bank_branch')} บัญชีเงินฝากออมทรัพย์ "
        f"ชื่อบัญชี {g('bank_account_name')} เลขที่บัญชี {g('bank_account_number')}",
    )
    clause(
        3,
        "ผู้รับจ้างสัญญาว่าจะจัดหาช่างฝีมือดี พร้อมควบคุมการทำงาน เพื่อทำเฟอร์นิเจอร์ตามแบบในเอกสารแนบท้ายสัญญา"
        "พร้อมติดตั้งจนกว่างานจะแล้วเสร็จ",
    )
    clause(
        4,
        "งานตามสัญญานี้ไม่รวมงานแก้ไขความเสียหายความผิดพลาดจากการก่อสร้างของผู้ว่าจ้างหรือผู้รับเหมารายอื่นของผู้ว่าจ้าง",
    )
    clause(
        5,
        f"ผู้รับจ้างสัญญาว่าจะทำงานที่ว่าจ้างให้แล้วเสร็จ โดยแบ่งการส่งมอบงานออกเป็นสองช่วง ดังนี้ "
        f"ช่วงที่ 1 ผู้รับจ้างสัญญาว่าจะทำงานในส่วนของ {g('phase_1_rooms')} ให้แล้วเสร็จภายในวันที่ {g('phase_1_date')} "
        f"หลังจากวันลงนามในสัญญาและทำการชำระเงินงวดแรกเรียบร้อยแล้ว "
        f"ช่วงที่ 2 ผู้รับจ้างสัญญาว่าจะทำงานในส่วนของ {g('phase_2_rooms')} ให้แล้วเสร็จภายในวันที่ {g('phase_2_date')} "
        f"หลังจากวันลงนามในสัญญาและทำการชำระเงินงวดแรกเรียบร้อยแล้ว "
        f"โดยผู้ว่าจ้างตกลงจะเตรียมพื้นที่หน้างานให้อยู่ในสภาพเรียบร้อยพร้อมที่ผู้รับจ้างจะสามารถทำงานได้"
        f"โดยไม่มีผู้รับเหมารายอื่น เข้าทำงานพร้อมกันในพื้นที่หน้างาน เป็นเวลาอย่างน้อย {g('prep_area_days') or '30'} วัน "
        f"ก่อนครบกำหนดเวลาดำเนินงานดังกล่าว แต่ถ้าผู้รับจ้างทำงานไม่แล้วเสร็จตามกำหนดเวลาดังกล่าวโดยไม่ใช่ความผิดของ"
        f"ผู้รับจ้าง ผู้รับจ้างไม่ต้องรับผิดต่อผู้ว่าจ้าง",
    )
    clause(
        6,
        "ผู้รับจ้างจะรับผิดชอบต่อผู้ว่าจ้างในความเสียหายที่เกิดจากการทำงานของผู้รับจ้าง รวมทั้งการกระทำของคนงาน "
        "ช่าง หรือบริวารของผู้รับจ้างในบริเวณที่ทำงานในสถานที่ของผู้ว่าจ้าง เว้นแต่กรณีเกิดจากเหตุสุดวิสัย",
    )
    clause(7, "หากมีหนี้เงินที่ผู้ว่าจ้างค้างชำระ ผู้ว่าจ้างตกลงเสียดอกเบี้ยให้แก่ผู้รับจ้างในอัตราร้อยละ 15 ต่อปี")
    clause(
        8,
        "ผู้รับจ้างรับประกันผลงานเป็นระยะเวลา 1 ปีนับแต่วันส่งมอบงาน โดยผู้รับจ้างจะรับผิดชอบแก้ไขซ่อมแซมเฟอร์นิเจอร์"
        "ที่ชำรุดเสียหายจากการผลิตหรือการติดตั้งของผู้รับจ้างด้วยค่าใช้จ่ายของผู้รับจ้างเอง "
        "การรับประกันผลงานดังกล่าวไม่รวมรอยขีดข่วนหรือความเสียหายที่เกิดจากการใช้งานในชีวิตประจำวันหรือเกิดจาก"
        "การเคลื่อนย้ายของผู้ว่าจ้างเองภายหลังจากการส่งมอบงาน และไม่รวมความเสียหายที่เกิดจากการใช้งานผิดวิธีหรือ"
        "ผิดวัตถุประสงค์ของเฟอร์นิเจอร์",
    )

    story.append(Paragraph(
        "สัญญานี้ทำขึ้นสองฉบับ มีข้อความตรงกัน เก็บไว้ฝ่ายละฉบับ ทั้งสองฝ่ายได้อ่านและเข้าใจข้อความในสัญญาแล้ว "
        "ถูกต้องตามความประสงค์ทุกประการ จึงลงชื่อและประทับตรา (ถ้ามี) ไว้เป็นสำคัญต่อหน้าพยาน",
        body_style,
    ))
    story.append(Spacer(1, 12 * mm))

    def sign_cell(text: str) -> Paragraph:
        return Paragraph(text, sign_style)

    sign_rows = [
        ["ลงชื่อ....................................................ผู้ว่าจ้าง", "ลงชื่อ....................................................ผู้รับจ้าง"],
        [f"({g('client_name')})", f"({g('contractor_signatory')})"],
        ["", g("contractor_title")],
        ["", g("contractor_name")],
        [Spacer(1, 10 * mm), Spacer(1, 10 * mm)],
        ["ลงชื่อ....................................................พยาน", "ลงชื่อ....................................................พยาน"],
        [f"({g('witness_1_name')})", f"({g('witness_2_name')})"],
    ]
    sign_table = Table(
        [[sign_cell(cell) if isinstance(cell, str) else cell for cell in row] for row in sign_rows],
        colWidths=[85 * mm, 85 * mm],
    )
    sign_table.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(sign_table)

    return story


class _CornerNote(Flowable):
    """
    Pins `lines` to the bottom-right corner of whatever page this flowable
    ends up on — page-absolute coordinates, not flow-relative — matching the
    reference contract template's own bottom-right "หมายเหตุ:" footnote on
    plan/perspective/furniture-list attachment pages. wrap() reports zero
    size so it doesn't push the image/title above it around; draw() writes
    directly onto the current page's canvas (self.canv, set by the platypus
    frame machinery right before draw() is called).
    """

    def __init__(self, lines: list[str], font_name: str = "TPTankhun", font_size: float = 8, margin: float = 10 * mm):
        super().__init__()
        self.lines = lines
        self.font_name = font_name
        self.font_size = font_size
        self.margin = margin

    def wrap(self, available_width: float, available_height: float) -> tuple[float, float]:
        return (0, 0)

    def draw(self) -> None:
        canv = self.canv
        page_w, _page_h = A4
        canv.saveState()
        canv.setFont(self.font_name, self.font_size)
        y = self.margin
        for line in reversed(self.lines):
            canv.drawRightString(page_w - self.margin, y, line)
            y += self.font_size + 2
        canv.restoreState()


# Fixed footnote text per attachment type, matching the reference contract
# template's own wording exactly. "furniture_list" has two blanks
# ({item_range}/{reference_note}) filled in from that attachment's own
# saved fields — see ContractAttachmentUpload in schemas.py.
_ATTACHMENT_PLAN_PERSPECTIVE_NOTE = "หมายเหตุ: แบบอาจมีการปรับเปลี่ยนรายละเอียดได้ตามความเหมาะสม"


def _attachment_remark_lines(attachment: dict[str, Any]) -> list[str]:
    kind = attachment.get("attachment_type") or ""
    if kind in ("plan", "perspective"):
        return [_ATTACHMENT_PLAN_PERSPECTIVE_NOTE]
    if kind == "furniture_list":
        item_range = attachment.get("item_range") or ""
        reference_note = attachment.get("reference_note") or ""
        return [
            "หมายเหตุ:",
            "1. แบบอาจมีการปรับเปลี่ยนรายละเอียดได้ตามความเหมาะสม",
            f"2. รายการสั่งผลิตเฟอร์นิเจอร์ลำดับที่ {item_range} ที่รวมอยู่ในสัญญาฉบับนี้",
            f" แสดงตามเอกสารแนบท้ายในสัญญา {reference_note}",
        ]
    return []


def generate_contract_pdf(
    details: dict[str, Any],
    attachments: list[dict[str, Any]],
    quotation_rows: list[dict[str, Any]],
    deposit_deduction: float = 0,
    remarks: str = "",
    grand_total_note: str = "",
) -> bytes:
    """
    The combined "ทำสัญญา" document: contract text (ข้อ 1-8) -> each saved
    attachment page (floor plans / furniture renders / marked-up photos,
    each a dict with image_bytes/title/floor/zone/attachment_type/
    item_range/reference_note — see db.get_contract_attachments_full) ->
    the priced furniture table (reusing _build_quotation_table_story) —
    same page order as the reference template. One reportlab story built
    into a single PDF, no separate PDF-merge step needed.
    """
    _ensure_thai_fonts_registered()

    attachment_title_style = ParagraphStyle(
        "AttachmentTitle", fontName="TPTankhun-Bold", fontSize=11, leading=14, alignment=TA_CENTER
    )
    attachment_subtitle_style = ParagraphStyle(
        "AttachmentSubtitle", fontName="TPTankhun", fontSize=10, leading=13, alignment=TA_CENTER
    )
    price_title_style = ParagraphStyle(
        "PriceTitle", fontName="TPTankhun-Bold", fontSize=14, leading=17, alignment=TA_CENTER
    )

    story: list[Any] = _build_contract_text_story(details)

    for attachment in attachments:
        story.append(PageBreak())
        title = attachment.get("title") or ""
        if title:
            story.append(Paragraph(xml_escape(title), attachment_title_style))
            story.append(Spacer(1, 2 * mm))
        subtitle = " ".join(s for s in (attachment.get("floor"), attachment.get("zone")) if s)
        if subtitle:
            story.append(Paragraph(xml_escape(subtitle), attachment_subtitle_style))
        if title or subtitle:
            story.append(Spacer(1, 3 * mm))
        img = _sized_image(attachment["image_bytes"], 180 * mm, 240 * mm)
        img.hAlign = "CENTER"
        story.append(img)
        remark_lines = _attachment_remark_lines(attachment)
        if remark_lines:
            story.append(_CornerNote(remark_lines))

    if quotation_rows:
        story.append(PageBreak())
        client_name = details.get("client_name") or ""
        property_description = details.get("property_description") or ""
        title_line = (
            f"รายการเฟอร์นิเจอร์และราคาสำหรับ{xml_escape(property_description)}: {xml_escape(client_name)}"
            if property_description
            else "รายการเฟอร์นิเจอร์และราคา"
        )
        story.append(Paragraph(title_line, price_title_style))
        story.append(Spacer(1, 6 * mm))
        story.extend(_build_quotation_table_story(quotation_rows, deposit_deduction, remarks, grand_total_note))

    output = io.BytesIO()
    doc = SimpleDocTemplate(
        output, pagesize=A4, topMargin=10 * mm, bottomMargin=10 * mm, leftMargin=10 * mm, rightMargin=10 * mm
    )
    doc.build(story)
    return output.getvalue()


def _set_cell_background(cell, hex_color: str) -> None:
    """python-docx has no high-level cell-shading API — this is the documented
    workaround (raw <w:shd> in the cell's tcPr)."""
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:fill"), hex_color)
    tc_pr.append(shd)


def _set_cell_top_border(cell, hex_color: str, size: int = 6) -> None:
    """Adds a single top border line to a cell — same OXML workaround as
    _set_cell_background, python-docx has no high-level cell-border API.
    `size` is in eighths of a point (6 = 0.75pt, matching the PDF path's
    LINEABOVE above the Grand Total row)."""
    tc_pr = cell._tc.get_or_add_tcPr()
    borders = OxmlElement("w:tcBorders")
    top = OxmlElement("w:top")
    top.set(qn("w:val"), "single")
    top.set(qn("w:sz"), str(size))
    top.set(qn("w:color"), hex_color)
    borders.append(top)
    tc_pr.append(borders)


# "TP Tankhun" ships as two independent font families (not a style-linked
# regular/bold pair), same as the Sarabun TTFs used for the PDF path — so a
# bold run must reference "TP Tankhun Bold" by name, not rely on Word
# synthesizing bold from the regular family.
_DOCX_FONT = "TP Tankhun"
_DOCX_FONT_BOLD = "TP Tankhun Bold"


def _set_cell_text(
    cell, text: str, *, bold: bool = False, color: RGBColor | None = None, align=None, size: float | None = None
) -> None:
    cell.text = ""
    p = cell.paragraphs[0]
    if align is not None:
        p.alignment = align
    run = p.add_run(text)
    run.font.name = _DOCX_FONT_BOLD if bold else _DOCX_FONT
    run.bold = bold
    if color is not None:
        run.font.color.rgb = color
    if size is not None:
        run.font.size = Pt(size)


def generate_quotation_docx(
    rows: list[dict[str, Any]],
    client_name: str,
    project_name: str,
    quotation_date: str,
    logo_bytes: bytes | None = None,
    deposit_deduction: float = 0,
    remarks: str = "",
    grand_total_note: str = "",
) -> bytes:
    """
    Word-document counterpart to generate_quotation_pdf — same data, same
    Floor > Room two-level grouping, same "Client's"/TBC conventions, same
    dual-column totals + deposit + remarks, exported as .docx instead of
    .pdf. Word doesn't embed fonts by default (unlike the PDF path, which
    must), so "TP Tankhun" here is aspirational: it renders correctly with
    that look on a machine that has the font installed, and gracefully
    falls back to the system's own Thai-capable font otherwise — either way
    the Thai text itself always displays correctly.
    """
    doc = Document()
    doc.styles["Normal"].font.name = _DOCX_FONT
    doc.styles["Normal"].font.size = Pt(10)
    # Flush-to-margin paragraphs, per client spec: indent Left/Right 0",
    # spacing Before 0pt / After 4pt — applied at the style level so it
    # covers every paragraph in the document without per-paragraph overrides.
    normal_format = doc.styles["Normal"].paragraph_format
    normal_format.left_indent = Pt(0)
    normal_format.right_indent = Pt(0)
    normal_format.space_before = Pt(0)
    normal_format.space_after = Pt(4)
    section = doc.sections[0]
    section.top_margin = Mm(10)
    section.bottom_margin = Mm(10)
    section.left_margin = Mm(10)
    section.right_margin = Mm(10)

    # Letterhead: logo stacked above the company info block, both
    # right-aligned as a single column (matches the reference template).
    if logo_bytes:
        logo_para = doc.add_paragraph()
        logo_para.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        iw, ih = PILImage.open(io.BytesIO(logo_bytes)).size
        target_w_mm = 10.0
        target_h_mm = target_w_mm * ih / iw
        logo_para.add_run().add_picture(io.BytesIO(logo_bytes), width=Mm(target_w_mm), height=Mm(target_h_mm))

    # Letterhead (company name / address / date) only — per client request,
    # size 7.5pt. Font is "Avenir" per that same request, but Avenir is a
    # commercial font not present on this system/repo — using _DOCX_FONT as
    # a placeholder here until the actual Avenir font files are supplied.
    company_para = doc.add_paragraph()
    company_para.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = company_para.add_run("10DK Co., Ltd")
    run.font.name = _DOCX_FONT_BOLD
    run.bold = True
    run.font.size = Pt(7.5)

    address_para = doc.add_paragraph()
    address_para.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    address_run = address_para.add_run("141 Major Tower Thonglo, Khlong Ton Nua, Bangkok 10110")
    address_run.font.name = _DOCX_FONT
    address_run.font.size = Pt(7.5)

    if quotation_date:
        date_para = doc.add_paragraph()
        date_para.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        date_run = date_para.add_run(quotation_date)
        date_run.font.name = _DOCX_FONT
        date_run.font.size = Pt(7.5)

    project_name = project_name.strip()
    client_name = client_name.strip()
    if project_name and client_name:
        title_line = f"รายการเฟอร์นิเจอร์และราคาสำหรับ{project_name}: {client_name}"
    elif project_name:
        title_line = f"รายการเฟอร์นิเจอร์และราคาสำหรับ{project_name}"
    elif client_name:
        title_line = f"รายการเฟอร์นิเจอร์และราคา: {client_name}"
    else:
        title_line = "รายการเฟอร์นิเจอร์และราคา"
    title_para = doc.add_paragraph()
    title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_run = title_para.add_run(title_line)
    title_run.bold = True
    title_run.font.name = _DOCX_FONT_BOLD
    title_run.font.size = Pt(14)

    _add_quotation_table_to_docx(doc, rows, deposit_deduction, remarks, grand_total_note)

    output = io.BytesIO()
    doc.save(output)
    return output.getvalue()


def _add_quotation_table_to_docx(
    doc: Document,
    rows: list[dict[str, Any]],
    deposit_deduction: float = 0,
    remarks: str = "",
    grand_total_note: str = "",
) -> None:
    """Appends the item table + totals + remarks section to `doc` — extracted
    out of generate_quotation_docx so generate_contract_docx can attach the
    exact same table as its final section, mirroring how
    _build_quotation_table_story is shared on the PDF side."""
    groups_order: list[tuple[str, str]] = []
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = _quotation_group_key(row)
        grouped.setdefault(key, [])
        if key not in groups_order:
            groups_order.append(key)
        grouped[key].append(row)

    # A single continuous table for the whole document: the column-header
    # row sits once at the very top, with Floor/Room band rows (merged
    # across every column) and item rows below it as ordinary rows of the
    # same table — matching the reference template, which never re-prints
    # the column header between rooms.
    def _set_row_widths(cells) -> None:
        for cell, w in zip(cells, _QUOTATION_COL_WIDTHS_MM):
            cell.width = Mm(w)

    table = doc.add_table(rows=1, cols=len(_QUOTATION_HEADERS))
    table.style = "Table Grid"
    table.autofit = False
    _set_row_widths(table.rows[0].cells)
    for i, h in enumerate(_QUOTATION_HEADERS):
        cell = table.rows[0].cells[i]
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        _set_cell_background(cell, _QUOTATION_HEADER_BG.lstrip("#"))
        _set_cell_text(cell, h.replace("\n", " "), bold=True, size=13, align=WD_ALIGN_PARAGRAPH.CENTER)

    def add_band_row(text: str, bg_hex: str, size: float | None = None) -> None:
        row_cells = table.add_row().cells
        _set_row_widths(row_cells)
        merged = row_cells[0]
        for c in row_cells[1:]:
            merged = merged.merge(c)
        _set_cell_background(merged, bg_hex.lstrip("#"))
        _set_cell_text(merged, text, bold=True, color=RGBColor(0xFF, 0xFF, 0xFF), size=size)

    dk_work_subtotal = 0.0
    purchase_subtotal = 0.0
    last_floor: str | None = None
    for floor, room in groups_order:
        if floor and floor != last_floor:
            add_band_row(floor, _QUOTATION_FLOOR_BG)
        last_floor = floor or last_floor

        add_band_row(room, _QUOTATION_ROOM_BG, size=13)

        for row in grouped[(floor, room)]:
            is_client = bool(row.get("is_client_owned"))
            price = row.get("dk_work_price")
            purchase = row.get("actual_price_purchase")
            dk_total, purchase_total = _quotation_row_totals(row)
            dk_work_subtotal += dk_total
            purchase_subtotal += purchase_total
            if is_client:
                price_cell = ""
                purchase_cell = ""
            else:
                price_cell = _fmt_money(price)
                purchase_cell = _fmt_money(purchase) if (price or purchase) else "TBC"

            cells = table.add_row().cells
            _set_row_widths(cells)
            if is_client:
                for c in cells:
                    _set_cell_background(c, _QUOTATION_CLIENT_ROW_BG.lstrip("#"))
            elif price is None:
                # Same "lettered" condition as assign_quotation_labels: no
                # 10DK's-work price -> priced under the purchase column.
                for c in cells:
                    _set_cell_background(c, _QUOTATION_LETTER_ROW_BG.lstrip("#"))
            qty = row.get("quantity")
            _set_cell_text(cells[0], str(row.get("label", "")), align=WD_ALIGN_PARAGRAPH.CENTER, size=13)
            _set_cell_text(cells[1], str(row.get("item_name", "")), size=13)
            _set_cell_text(cells[2], _fmt_money(qty) if qty else "", align=WD_ALIGN_PARAGRAPH.RIGHT, size=13)
            _set_cell_text(cells[3], price_cell, align=WD_ALIGN_PARAGRAPH.RIGHT, size=13)
            _set_cell_text(
                cells[4],
                purchase_cell,
                align=WD_ALIGN_PARAGRAPH.RIGHT,
                color=RGBColor(0xDC, 0x26, 0x26) if purchase_cell == "TBC" else None,
                size=13,
            )
            _set_cell_text(cells[5], str(row.get("remark", "")), size=13)

    vat = dk_work_subtotal * 0.07
    grand_total = dk_work_subtotal + vat - (deposit_deduction or 0)

    doc.add_paragraph()
    totals_rows: list[tuple[str, float | None, float | None, bool, bool]] = [
        ("Total", dk_work_subtotal, purchase_subtotal, False, False),
        ("Vat 7 %", vat, None, False, False),
    ]
    if deposit_deduction:
        totals_rows.append(("หักค่ามัดจำออกแบบ", deposit_deduction, None, False, True))
    totals_rows.append(("Grand Total", grand_total, purchase_subtotal, True, False))

    # Reuses the item table's exact 6-column layout (label spans cols 0-2,
    # values sit in cols 3/4, note in col 5) so the totals visually line up
    # under the item table above them, matching the PDF path's totals_table
    # (which literally reuses the same colWidths + SPAN(0,i)-(2,i)).
    grand_total_row_idx = len(totals_rows) - 1
    totals_table = doc.add_table(rows=len(totals_rows), cols=6)
    totals_table.style = "Table Grid"
    totals_table.autofit = False
    for i, (label, dk_val, purchase_val, bold, red) in enumerate(totals_rows):
        row_cells = totals_table.rows[i].cells
        _set_row_widths(row_cells)
        if i == 0:
            for c in row_cells:
                _set_cell_background(c, _QUOTATION_LETTER_ROW_BG.lstrip("#"))
        label_cell = row_cells[0]
        for c in row_cells[1:3]:
            label_cell = label_cell.merge(c)
        _set_cell_text(label_cell, label, bold=bold, color=RGBColor(0xDC, 0x26, 0x26) if red else None, size=13)
        _set_cell_text(
            row_cells[3],
            _fmt_money(dk_val) if dk_val is not None else "",
            bold=bold,
            color=RGBColor(0xDC, 0x26, 0x26) if red else None,
            align=WD_ALIGN_PARAGRAPH.RIGHT,
            size=13,
        )
        _set_cell_text(
            row_cells[4],
            _fmt_money(purchase_val) if purchase_val is not None else "",
            bold=bold,
            align=WD_ALIGN_PARAGRAPH.RIGHT,
            size=13,
        )
        _set_cell_text(
            row_cells[5],
            grand_total_note if label == "Grand Total" else "",
            color=RGBColor(0xDC, 0x26, 0x26),
            size=13,
        )
        if i == grand_total_row_idx:
            for c in (label_cell, row_cells[3], row_cells[4], row_cells[5]):
                _set_cell_top_border(c, _QUOTATION_GRID_COLOR.lstrip("#"))

    remark_lines = [line.strip() for line in (remarks or "").splitlines() if line.strip()]
    if remark_lines:
        doc.add_paragraph()
        heading_para = doc.add_paragraph()
        heading_run = heading_para.add_run("Remarks:")
        heading_run.bold = True
        heading_run.underline = True
        heading_run.font.name = _DOCX_FONT_BOLD
        heading_run.font.size = Pt(13)
        for line in remark_lines:
            line_para = doc.add_paragraph()
            dash_run = line_para.add_run("- ")
            dash_run.font.name = _DOCX_FONT
            dash_run.font.size = Pt(13)
            for text, emph in _split_remark_emphasis(line):
                run = line_para.add_run(text)
                run.font.size = Pt(13)
                if emph:
                    run.bold = True
                    run.underline = True
                    run.font.name = _DOCX_FONT_BOLD
                else:
                    run.font.name = _DOCX_FONT


def _build_contract_text_docx(doc: Document, details: dict[str, Any]) -> None:
    """Word counterpart to _build_contract_text_story — same ข้อ 1-8 wording
    and blanks, appended directly to `doc` instead of returned as platypus
    flowables. Fixed clauses are hardcoded here exactly as in the PDF path."""

    def g(key: str) -> str:
        return str(details.get(key) or "")

    money = lambda v: _fmt_money(v) if v is not None else "___________"  # noqa: E731

    def styled_para(text: str, *, bold: bool = False, size: float = 15, align=WD_ALIGN_PARAGRAPH.CENTER):
        p = doc.add_paragraph()
        p.alignment = align
        run = p.add_run(text)
        run.font.name = _DOCX_FONT_BOLD if bold else _DOCX_FONT
        run.bold = bold
        run.font.size = Pt(size)
        return p

    def body_para(text: str, *, bold_prefix: str = "") -> None:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        p.paragraph_format.first_line_indent = Pt(24)
        if bold_prefix:
            prefix_run = p.add_run(bold_prefix)
            prefix_run.font.name = _DOCX_FONT_BOLD
            prefix_run.bold = True
            prefix_run.font.size = Pt(15)
        run = p.add_run(text)
        run.font.name = _DOCX_FONT
        run.font.size = Pt(15)

    styled_para("สัญญาจ้างตกแต่งภายใน", bold=True, size=18)
    styled_para(f"สำหรับ{g('property_description')}", size=15)
    styled_para(f"วันที่ {g('contract_date')}", size=15, align=WD_ALIGN_PARAGRAPH.RIGHT)
    body_para(
        f"สัญญาฉบับนี้ทำขึ้นที่{g('property_description')} ระหว่าง {g('client_name')} "
        f"เลขที่บัตรประชาชน {g('client_id_number')} ที่อยู่ {g('client_address')} "
        f'ซึ่งต่อไปในสัญญานี้ เรียกว่า "ผู้ว่าจ้าง" ฝ่ายหนึ่ง กับ {g("contractor_name")} '
        f"โดย {g('contractor_signatory')} กรรมการผู้มีอำนาจ อยู่ที่ {g('contractor_address')} "
        f'ซึ่งต่อไปนี้ในสัญญานี้เรียกว่า "ผู้รับจ้าง" อีกฝ่ายหนึ่ง '
        f"คู่สัญญาทั้งสองฝ่ายตกลงทำสัญญากัน มีข้อความดังต่อไปนี้"
    )

    def clause(number: int, text: str) -> None:
        body_para(text, bold_prefix=f"ข้อ {number}. ")

    clause(
        1,
        f"ผู้ว่าจ้างตกลงจ้าง และผู้รับจ้างตกลงรับจ้างออกแบบตกแต่งภายในสำหรับ{g('property_description')} "
        f"ซึ่งรวมถึงดำเนินการจัดหาเฟอร์นิเจอร์ หรือสั่งทำเฟอร์นิเจอร์ ตามแบบเฟอร์นิเจอร์ "
        f"รายการที่ {g('included_item_range')} ในเอกสารแนบท้ายสัญญา หน้า {g('included_item_page')} "
        f"ยกเว้นเฟอร์นิเจอร์ รายการที่ {g('excluded_item_range')} ในเอกสารแนบท้ายสัญญา หน้า {g('excluded_item_page')} "
        f"ซึ่งผู้ว่าจ้างจะต้องชำระราคาเองตามราคาที่ซื้อจริง",
    )
    clause(
        2,
        f"ผู้ว่าจ้างตกลงชำระค่าจ้างให้แก่ผู้รับจ้าง รวมเป็นเงิน {money(details.get('total_price'))} บาท "
        f"แบ่งชำระเป็น 3 งวด ดังนี้ งวดที่ 1 จำนวน {money(details.get('installment_1_amount'))} บาท "
        f"ชำระวันทำสัญญา งวดที่ 2 จำนวน {money(details.get('installment_2_amount'))} บาท "
        f"ชำระวันส่งมอบงาน โดยผู้รับจ้างจะแจ้งให้ผู้ว่าจ้างทราบล่วงหน้าเป็นลายลักษณ์อักษร ไม่น้อยกว่า 3 วัน "
        f"งวดที่ 3 จำนวน {money(details.get('installment_3_amount'))} บาท ชำระวันส่งมอบงานครบถ้วน "
        f"โดยผู้รับจ้างจะแจ้งให้ผู้ว่าจ้างทราบล่วงหน้าเป็นลายลักษณ์อักษร ไม่น้อยกว่า 3 วัน "
        f"โดยชำระด้วยวิธีโอนเข้าบัญชี{g('bank_name')} {g('bank_branch')} บัญชีเงินฝากออมทรัพย์ "
        f"ชื่อบัญชี {g('bank_account_name')} เลขที่บัญชี {g('bank_account_number')}",
    )
    clause(
        3,
        "ผู้รับจ้างสัญญาว่าจะจัดหาช่างฝีมือดี พร้อมควบคุมการทำงาน เพื่อทำเฟอร์นิเจอร์ตามแบบในเอกสารแนบท้ายสัญญา"
        "พร้อมติดตั้งจนกว่างานจะแล้วเสร็จ",
    )
    clause(
        4,
        "งานตามสัญญานี้ไม่รวมงานแก้ไขความเสียหายความผิดพลาดจากการก่อสร้างของผู้ว่าจ้างหรือผู้รับเหมารายอื่นของผู้ว่าจ้าง",
    )
    clause(
        5,
        f"ผู้รับจ้างสัญญาว่าจะทำงานที่ว่าจ้างให้แล้วเสร็จ โดยแบ่งการส่งมอบงานออกเป็นสองช่วง ดังนี้ "
        f"ช่วงที่ 1 ผู้รับจ้างสัญญาว่าจะทำงานในส่วนของ {g('phase_1_rooms')} ให้แล้วเสร็จภายในวันที่ {g('phase_1_date')} "
        f"หลังจากวันลงนามในสัญญาและทำการชำระเงินงวดแรกเรียบร้อยแล้ว "
        f"ช่วงที่ 2 ผู้รับจ้างสัญญาว่าจะทำงานในส่วนของ {g('phase_2_rooms')} ให้แล้วเสร็จภายในวันที่ {g('phase_2_date')} "
        f"หลังจากวันลงนามในสัญญาและทำการชำระเงินงวดแรกเรียบร้อยแล้ว "
        f"โดยผู้ว่าจ้างตกลงจะเตรียมพื้นที่หน้างานให้อยู่ในสภาพเรียบร้อยพร้อมที่ผู้รับจ้างจะสามารถทำงานได้"
        f"โดยไม่มีผู้รับเหมารายอื่น เข้าทำงานพร้อมกันในพื้นที่หน้างาน เป็นเวลาอย่างน้อย {g('prep_area_days') or '30'} วัน "
        f"ก่อนครบกำหนดเวลาดำเนินงานดังกล่าว แต่ถ้าผู้รับจ้างทำงานไม่แล้วเสร็จตามกำหนดเวลาดังกล่าวโดยไม่ใช่ความผิดของ"
        f"ผู้รับจ้าง ผู้รับจ้างไม่ต้องรับผิดต่อผู้ว่าจ้าง",
    )
    clause(
        6,
        "ผู้รับจ้างจะรับผิดชอบต่อผู้ว่าจ้างในความเสียหายที่เกิดจากการทำงานของผู้รับจ้าง รวมทั้งการกระทำของคนงาน "
        "ช่าง หรือบริวารของผู้รับจ้างในบริเวณที่ทำงานในสถานที่ของผู้ว่าจ้าง เว้นแต่กรณีเกิดจากเหตุสุดวิสัย",
    )
    clause(7, "หากมีหนี้เงินที่ผู้ว่าจ้างค้างชำระ ผู้ว่าจ้างตกลงเสียดอกเบี้ยให้แก่ผู้รับจ้างในอัตราร้อยละ 15 ต่อปี")
    clause(
        8,
        "ผู้รับจ้างรับประกันผลงานเป็นระยะเวลา 1 ปีนับแต่วันส่งมอบงาน โดยผู้รับจ้างจะรับผิดชอบแก้ไขซ่อมแซมเฟอร์นิเจอร์"
        "ที่ชำรุดเสียหายจากการผลิตหรือการติดตั้งของผู้รับจ้างด้วยค่าใช้จ่ายของผู้รับจ้างเอง "
        "การรับประกันผลงานดังกล่าวไม่รวมรอยขีดข่วนหรือความเสียหายที่เกิดจากการใช้งานในชีวิตประจำวันหรือเกิดจาก"
        "การเคลื่อนย้ายของผู้ว่าจ้างเองภายหลังจากการส่งมอบงาน และไม่รวมความเสียหายที่เกิดจากการใช้งานผิดวิธีหรือ"
        "ผิดวัตถุประสงค์ของเฟอร์นิเจอร์",
    )

    body_para(
        "สัญญานี้ทำขึ้นสองฉบับ มีข้อความตรงกัน เก็บไว้ฝ่ายละฉบับ ทั้งสองฝ่ายได้อ่านและเข้าใจข้อความในสัญญาแล้ว "
        "ถูกต้องตามความประสงค์ทุกประการ จึงลงชื่อและประทับตรา (ถ้ามี) ไว้เป็นสำคัญต่อหน้าพยาน"
    )
    doc.add_paragraph()

    sign_rows = [
        ["ลงชื่อ....................................................ผู้ว่าจ้าง", "ลงชื่อ....................................................ผู้รับจ้าง"],
        [f"({g('client_name')})", f"({g('contractor_signatory')})"],
        ["", g("contractor_title")],
        ["", g("contractor_name")],
        ["", ""],
        ["ลงชื่อ....................................................พยาน", "ลงชื่อ....................................................พยาน"],
        [f"({g('witness_1_name')})", f"({g('witness_2_name')})"],
    ]
    sign_table = doc.add_table(rows=len(sign_rows), cols=2)
    sign_table.autofit = False
    for r, row in enumerate(sign_rows):
        cells = sign_table.rows[r].cells
        for c, text in enumerate(row):
            cells[c].width = Mm(85)
            _set_cell_text(cells[c], text, align=WD_ALIGN_PARAGRAPH.CENTER, size=15)
        if r == 4:
            # Blank spacer row (matches the PDF's Spacer(1, 10*mm) between
            # the signatory block and the witness block) — extra space
            # after this row's (empty) paragraphs instead of a real gap,
            # since docx tables can't hold non-paragraph flowables.
            for cell in cells:
                cell.paragraphs[0].paragraph_format.space_after = Pt(20)


def _docx_image_size_mm(image_bytes: bytes, max_w_mm: float, max_h_mm: float) -> tuple[float, float]:
    """Scales an image to fit within (max_w_mm, max_h_mm) while preserving
    aspect ratio — docx counterpart to _sized_image (which works in points
    for reportlab); using mm bounds directly here gives the same physical
    output size as the PDF path for a fixed-resolution source like the
    attachment canvas's 1000x700 export."""
    iw, ih = PILImage.open(io.BytesIO(image_bytes)).size
    scale = min(max_w_mm / iw, max_h_mm / ih)
    return iw * scale, ih * scale


def generate_contract_docx(
    details: dict[str, Any],
    attachments: list[dict[str, Any]],
    quotation_rows: list[dict[str, Any]],
    deposit_deduction: float = 0,
    remarks: str = "",
    grand_total_note: str = "",
) -> bytes:
    """
    Word counterpart to generate_contract_pdf — same three sections
    (contract text -> attachment pages -> priced furniture table) combined
    into one .docx. Word has no page-absolute positioning (unlike the PDF
    path's _CornerNote), so each attachment's remark note is placed as a
    small paragraph directly under its image instead of pinned to the
    page's bottom-right corner — close enough for a Word working copy that
    people mainly use to tweak wording before re-exporting to PDF.
    """
    doc = Document()
    doc.styles["Normal"].font.name = _DOCX_FONT
    doc.styles["Normal"].font.size = Pt(15)
    section = doc.sections[0]
    section.top_margin = Mm(10)
    section.bottom_margin = Mm(10)
    section.left_margin = Mm(10)
    section.right_margin = Mm(10)

    _build_contract_text_docx(doc, details)

    for attachment in attachments:
        doc.add_page_break()
        title = attachment.get("title") or ""
        if title:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run(title)
            run.font.name = _DOCX_FONT_BOLD
            run.bold = True
            run.font.size = Pt(13)
        subtitle = " ".join(s for s in (attachment.get("floor"), attachment.get("zone")) if s)
        if subtitle:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run(subtitle)
            run.font.name = _DOCX_FONT
            run.font.size = Pt(11)

        img_para = doc.add_paragraph()
        img_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        w_mm, h_mm = _docx_image_size_mm(attachment["image_bytes"], 180.0, 240.0)
        img_para.add_run().add_picture(io.BytesIO(attachment["image_bytes"]), width=Mm(w_mm), height=Mm(h_mm))

        remark_lines = _attachment_remark_lines(attachment)
        for line in remark_lines:
            note_para = doc.add_paragraph()
            note_para.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            note_run = note_para.add_run(line)
            note_run.font.name = _DOCX_FONT
            note_run.font.size = Pt(8)

    if quotation_rows:
        doc.add_page_break()
        client_name = (details.get("client_name") or "").strip()
        property_description = (details.get("property_description") or "").strip()
        title_line = (
            f"รายการเฟอร์นิเจอร์และราคาสำหรับ{property_description}: {client_name}"
            if property_description
            else "รายการเฟอร์นิเจอร์และราคา"
        )
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(title_line)
        run.font.name = _DOCX_FONT_BOLD
        run.bold = True
        run.font.size = Pt(14)
        doc.add_paragraph()
        _add_quotation_table_to_docx(doc, quotation_rows, deposit_deduction, remarks, grand_total_note)

    output = io.BytesIO()
    doc.save(output)
    return output.getvalue()


# --------------------------------------------------------------------------------------
# AI Prompts — Step 2: Furniture List Extraction
# --------------------------------------------------------------------------------------

FURNITURE_EXTRACTION_SYSTEM_PROMPT = """You are a data extraction assistant specialized in reading \
furniture schedules and floor plan notes for interior design / construction projects.

Given raw text extracted from ONE PAGE of a PDF (a floor plan legend, furniture list, or bill \
of quantities), extract EVERY distinct furniture item mentioned into structured JSON.

CRITICAL — completeness: your #1 priority is not missing anything. Extract ALL items, including:
- Items explicitly marked "(Optional)", "(ทางเลือก)", "alternate", "TBC", or similar — these are \
still real items and must be included, not skipped just because they're optional/tentative.
- Items in numbered lists, lettered alternates (A, B, C...), legend callouts, and footnotes.
- Items that appear only briefly or in small text (e.g. a single line item easy to skim past).
Before finalizing your answer, re-scan the ENTIRE text one more time specifically looking for \
any item you have not yet listed — a rushed first pass commonly misses 1-2 items buried in a \
long list. It is far worse to omit a real item than to include one.

Other rules:
- Group items by "room" as stated in the source text. If no room is given, use "Unspecified".
- "item_name" should be a clean, human-readable furniture description (no item codes unless \
that's all that's available). If an item is marked optional/tentative, keep that in the name, \
e.g. "Pendant Light (Optional)".
- "quantity" must be a positive integer. If not stated, default to 1.
- "spec" should capture EVERY descriptive specification detail the source text states for that \
item — not just size/dimensions and material, but also surface finish/coating (e.g. "ผิวลามิเนต", \
"ปิดผิววีเนียร์", "พ่นสี", "เคลือบ PU", "laminate finish", "powder-coated"), color, fabric/upholstery \
type, and any other descriptive spec note (brand, model, price-cap notes like "ราคาไม่เกิน "). \
Combine everything stated into one short comma-separated string, e.g. "270x100xH73 cm, ท็อปไม้จริง \
Ash Wood ทำสีธรรมชาติ" or "220x10xH276 cm, HMR ปิดผิวลามิเนต". If nothing at all is stated, use an \
empty string "" — never invent a spec detail that isn't in the text.
- Do not invent items that are not present in the text.
- Merge duplicate entries of the exact same item within the same room by summing quantities \
(keep whichever entry's spec is non-empty if only one of them has one).

Respond ONLY with a JSON object of this exact shape (no extra commentary):
{
  "items": [
    {"room": "Bedroom 1", "item_name": "King Size Bed", "quantity": 1, "spec": "180x200 cm, ไม้ MDF ผิวลามิเนตสีขาว"}
  ]
}
"""

_PAGE_SPLIT_RE = re.compile(r"--- Page \d+ ---")

FURNITURE_EXTRACTION_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "room": {"type": "string"},
                    "item_name": {"type": "string"},
                    "quantity": {"type": "number"},
                    "spec": {"type": "string"},
                },
                "required": ["room", "item_name", "quantity"],
            },
        }
    },
    "required": ["items"],
}


def _extract_furniture_page(user_prompt: str) -> dict[str, Any]:
    """
    Picks an AI provider for one page's extraction call — Claude if
    ANTHROPIC_API_KEY is configured (it reads Thai more reliably), else
    OpenAI. Raises LogicError if neither is configured.
    """
    anthropic_client = get_anthropic_client()
    if anthropic_client is not None:
        return call_claude_json(
            anthropic_client,
            FURNITURE_EXTRACTION_SYSTEM_PROMPT,
            user_prompt,
            tool_name="record_furniture_items",
            tool_description="Records the furniture items extracted from this page of text.",
            input_schema=FURNITURE_EXTRACTION_TOOL_SCHEMA,
        )
    openai_client = get_openai_client()
    if openai_client is not None:
        return call_openai_json(openai_client, FURNITURE_EXTRACTION_SYSTEM_PROMPT, user_prompt)
    raise LogicError("No AI provider configured — set ANTHROPIC_API_KEY or OPENAI_API_KEY.")


def group_items_by_room(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reorders a furniture list so items sharing the same room sit contiguously — see app.py for rationale."""
    room_order: list[str] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        room = str(item.get("room") or "Unspecified")
        if room not in grouped:
            grouped[room] = []
            room_order.append(room)
        grouped[room].append(item)
    return [item for room in room_order for item in grouped[room]]


def extract_furniture_list(pdf_text: str) -> list[dict[str, Any]]:
    """
    Extracts the furniture list page by page (better completeness on long
    documents than one giant prompt), then merges. Raises LogicError if
    every page's extraction failed. Provider (Claude vs OpenAI) is chosen
    internally per call — see _extract_furniture_page.
    """
    pages = [p.strip() for p in _PAGE_SPLIT_RE.split(pdf_text) if p.strip()]
    if not pages:
        pages = [pdf_text]

    merged: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    any_success = False

    for page_text in pages:
        user_prompt = f"Extracted PDF text (one page):\n\n{page_text}"
        result = _extract_furniture_page(user_prompt)
        items = result.get("items")
        if not isinstance(items, list):
            continue
        any_success = True
        for item in items:
            room = str(item.get("room", "Unspecified"))
            name = str(item.get("item_name", "")).strip()
            if not name:
                continue
            qty = item.get("quantity", 1) or 1
            spec = str(item.get("spec") or "").strip()
            key = (room, name.lower())
            if key in merged:
                merged[key]["quantity"] = (merged[key].get("quantity", 0) or 0) + qty
                if not merged[key].get("spec") and spec:
                    merged[key]["spec"] = spec
            else:
                merged[key] = {"room": room, "item_name": name, "quantity": qty, "spec": spec, "verified": True}
                order.append(key)

    if not any_success:
        raise LogicError("⚠️ The AI response did not contain a valid 'items' list.")

    return group_items_by_room([merged[k] for k in order])


# --------------------------------------------------------------------------------------
# AI Prompts — Step 3: Semantic Matching & Price Extraction
# --------------------------------------------------------------------------------------

BUCKET_ORDER_TYPE = {
    "ALT": "สั่งผลิต",
    "P'May": "สั่งผลิต",
    "OTHER_MAKER": "จัดซื้อ (บวกกำไร 10DK)",
    "PURCHASE": "จัดซื้อ (ราคาจริง ไม่บวกกำไร)",
}

# Client-facing display suffix appended to item names in the preview/export
# layer (never persisted) so the pricing category is visible at a glance.
ORDER_TYPE_SUFFIX = {
    "สั่งผลิต": "สั่งผลิต",
    "จัดซื้อ (บวกกำไร 10DK)": "จัดซื้อ",
    "จัดซื้อ (ราคาจริง ไม่บวกกำไร)": "จัดซื้อ เบิกจ่ายตามจริง",
}


def build_item_display_text(
    item_name: str, quotation_spec: str, spec: str, order_type: str
) -> str:
    """
    Builds the single combined string shown in the "Furniture List" column of
    both the Excel export (write_mapping_to_excel) and the on-screen preview
    (compute_price_preview) — kept as one shared function so the two stay in
    sync. Pattern: "{name} สเปค {quotation_spec} ขนาด {spec} - {order type
    suffix}", each labeled segment only included when that field has content.
    quotation_spec is the AI-extracted detail from the supplier's quotation
    PDF (e.g. material/construction notes); spec is ขนาด (size), from Step 2.
    """
    text = item_name
    quotation_spec = quotation_spec.strip()
    if quotation_spec:
        text = f"{text} สเปค {quotation_spec}"
    spec = spec.strip()
    if spec:
        text = f"{text} ขนาด {spec}"
    suffix = ORDER_TYPE_SUFFIX.get(order_type)
    if suffix:
        text = f"{text} - {suffix}"
    return text


def build_price_matching_system_prompt(fixed_supplier: str | None) -> str:
    """Builds the Step-3 matching prompt for one upload bucket — see app.py for the full rationale."""
    if fixed_supplier:
        supplier_clause = (
            f'- Every document in this batch is already known to be from a single supplier: '
            f'"{fixed_supplier}". You do NOT need to detect or guess the supplier name — set '
            f'"supplier" to exactly "{fixed_supplier}" for every matched item.'
        )
    else:
        supplier_clause = (
            '- Also identify which supplier/company the matched price came from (from the '
            "document's filename, or from a company name mentioned inside that document's text, "
            'whichever is more informative) and put it in "supplier". If genuinely unclear, leave '
            '"supplier" empty.'
        )

    price_column_clause = ""
    if fixed_supplier == "ALT":
        price_column_clause = (
            "\n- ALT quotation PDFs always follow the same fixed table format: the LAST column "
            'is labeled "จำนวนเงิน" (amount/total). This is the exact number to extract as '
            '"unit_price" — it is the raw ต้นทุน ALT (ALT cost) already, with NOTHING added on '
            "top. Do not add tax, markup, or any other adjustment to this number — take it "
            "exactly as printed in the จำนวนเงิน column. Ignore other numeric columns in the same "
            "row (e.g. unit price × quantity breakdowns) — จำนวนเงิน is always the correct one to use."
        )

    # Other-maker and Purchase quotations often show per-unit price + a discount
    # column + a post-discount "ราคารวม" total — ALT's price_column_clause above
    # already handles ALT's own fixed จำนวนเงิน-column format correctly, and
    # per instruction this rule is scoped to Other/Purchase only (not P'May).
    discount_price_clause = ""
    if fixed_supplier in ("Other", None):
        discount_price_clause = (
            "\n- IMPORTANT — pricing before discount, multiplied by quantity: this batch's "
            "quotations often show a table with a per-unit price column, a discount column "
            '(percentage and/or amount), and a final "ราคารวม" (total) column that already has '
            "the discount subtracted. Extract the PRE-discount per-unit price and multiply it by "
            "the quantity shown in THAT quotation line (not the furniture list's own quantity) — "
            'report that product as "unit_price". NEVER use the post-discount "ราคารวม" column, '
            "and never report just the bare per-unit price without multiplying by the quotation's "
            "own quantity — this app does not multiply unit_price by quantity again downstream, "
            "so unit_price must already be the full pre-discount total for the quantity quoted. "
            "Example: quotation shows quantity 2 and per-unit price 8,500 -> report unit_price "
            "as 17,000 (8,500 × 2), not 8,500 and not whatever the post-discount ราคารวม column "
            "shows."
        )

    alt_info_clause = ""
    if fixed_supplier == "ALT":
        alt_info_clause = (
            "\n- SEPARATELY, look at the LAST line item(s) in this ALT quotation (usually near "
            'the end, after the item list) for THREE specific numbers, each printed as an actual '
            "baht figure in the document (do NOT calculate or estimate these — only report a "
            "number if you see it explicitly printed):\n"
            '  1. The total/sum of all furniture item costs in this quote (before any surcharge)\n'
            '  2. "ค่า Protection พื้น" (floor protection fee) — a separate baht amount\n'
            '  3. "ค่าดำเนินการ 10%" (10% management/handling fee) — this is ALSO printed as an '
            "actual baht amount in the document (not just a percentage label), since it's 10% "
            "of some base value already computed by the supplier — extract that printed number.\n"
            'Report them in "alt_batch_info": {"sum_of_item_costs": <number>, '
            '"protection_fee": <number>, "management_fee": <number>}. If any of these three '
            'aren\'t explicitly stated as numbers in the text, omit "alt_batch_info" entirely '
            "(do not guess or calculate missing values)."
        )

    return f"""You are a procurement assistant. You will be given:
1. A JSON furniture list, where each item has "index" (its position in the original list), \
"room", "item_name", "quantity", and optionally "spec" (size/dimensions/material — may be an \
empty string if not known).
2. Raw text extracted from one or more supplier price quotation PDFs, all belonging to the \
SAME pricing batch. If there are multiple documents, each is marked with a "===== Supplier \
document: <filename> =====" header so you know which text belongs to which file.

Your job:
- IMPORTANT — check filenames first: suppliers sometimes name each quotation PDF file after the \
specific furniture item it quotes (e.g. a file named "Pendant Light Option 1.pdf" or \
"TV_Console_quote.pdf" almost certainly quotes that exact item — even if that exact item name \
never literally appears written out inside the document's own text). Before matching by reading \
through all the text, check whether any document's filename (given in its "===== Supplier \
document: <filename> =====" header) closely matches an item's name. If it does:
  1. First look for that item's price within that specific document's text as usual.
  2. If the document's text alone doesn't clearly name or describe the item, but the filename \
match itself is strong and unambiguous, treat the filename match as sufficient evidence on its \
own — extract whatever single/primary price figure is quoted in that document and assign it to \
that item. Do NOT skip an item just because its name isn't explicitly written inside the \
document's text when the filename already tells you which item that document is for.
- For each item in the furniture list, semantically match it against this batch's quotation \
text (e.g., "King Size Bed" may match a line like "6-foot wooden bed frame" or "Bed Frame - \
King - Solid Oak"). When an item's "spec" is present, use it too — it's especially useful for \
telling apart multiple similar items in the same quote that only differ by size or material \
(e.g. two sofas that differ only in fabric, or two tables that differ only in dimensions).
- Room/zone hints: the quotation text sometimes explicitly states which room/zone an item is \
for, often in parentheses right after the item's description — e.g. "(โซน Son's bedroom - 4th \
Floor)" or similar. Each furniture list item already has its own "room" field — treat a \
matching room/zone hint in the quotation text as strong confirmation you've matched the right \
item, especially when disambiguating between multiple visually similar items (e.g. two \
different chairs quoted for different rooms).
- Extract the correct unit price (a number, no currency symbols) for each matched item from \
the text.
- CRITICAL — number formatting: Thai and English business documents commonly write prices \
with a comma (,) as the THOUSANDS separator and a period (.) as the decimal separator, e.g. \
"12,610" means twelve thousand six hundred ten (12610), NOT 12. NEVER truncate, round, or \
drop digits from a price. Always read the FULL number including every digit before AND after \
any comma. Double-check each extracted price against the source text before including it in \
your answer — a price under 100 for furniture items is almost always a sign you mis-read the \
number; re-check the source text in that case.{price_column_clause}{discount_price_clause}
{supplier_clause}
- If the same item could be matched more than once within this batch, choose the lowest \
valid price.
- If an item cannot be confidently matched anywhere in THIS batch's text, simply OMIT it from \
"mapped_items" entirely — do not include a zero-price guess. This batch may just not contain \
that item; it might be matched in a different batch instead.
- Never fabricate a price that is not present in the text.
- Also check the matched line in the quotation text for any descriptive detail about the item \
beyond its name and price — material, construction, or finish notes (e.g. "โครงไม้จริง กรุไม้อัด \
ปิดวีเนียร์", "ซ่อนไฟ LED Strip Light ความยาวไม่เกิน 16 m."). If present, copy that text into \
"quotation_spec" for that item. This is genuinely optional — most quotation lines are just a name \
and a price with nothing else to extract, so leave "quotation_spec" out entirely (do not invent \
detail that isn't in the text, and do not repeat the item's own name/spec back).
- IMPORTANT — stone/granite top add-ons: some numbered line items (e.g. a TV console, kitchen \
counter, or sideboard) list TWO separate priced components under the SAME item number: the base \
furniture piece, and a "Top หิน..." / "Top หินจริง..." (stone/granite top) line quoted \
separately, usually phrased like "Top หินจริง (ราคาไม่เกิน 6,900.-/ตร.ม) พร้อมติดตั้ง" — \
sometimes with its own note like "(รวมค่าตัด เจียรขอบ และติดตั้ง)". These two lines belong to \
ONE furniture item, not two — ADD the stone top's price to the base item's price and report a \
SINGLE combined unit_price. For example, if item #2 "TV CONSOLE" shows a base price of 36,000 \
and is followed by "Top หินจริง...พร้อมติดตั้ง" at 28,000, report unit_price as 64,000 (36,000 + \
28,000) for that one item — do NOT create a separate matched item just for the stone top line, \
and do NOT report only the base price while dropping the stone top's price.
- ALWAYS include the original "index" field (copied exactly from the input item) in every \
output row, so results can be merged back to the correct item afterward.{alt_info_clause}

Respond ONLY with a JSON object of this exact shape (no extra commentary):
{{
  "mapped_items": [
    {{"index": 0, "unit_price": 350.00, "supplier": "ABC Furniture Co.", \
"quotation_spec": "โครงไม้จริง กรุไม้อัดปิดวีเนียร์(ราคาไม่เกิน 1500.-/แผ่น)"}}
  ]
}}
"""


# Forced tool-call schema for match_prices_bucket's Claude request — see
# call_claude_json's docstring for why a tool call is used instead of a raw
# JSON response mode (Claude has none).
_PRICE_MATCH_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "mapped_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Original index from the input furniture list."},
                    "unit_price": {"type": "number"},
                    "supplier": {"type": "string"},
                    "quotation_spec": {
                        "type": "string",
                        "description": "Extra descriptive detail found for this item, if any — see instructions.",
                    },
                },
                "required": ["index", "unit_price"],
            },
        },
        "alt_batch_info": {
            "type": "object",
            "properties": {
                "sum_of_item_costs": {"type": "number"},
                "protection_fee": {"type": "number"},
                "management_fee": {"type": "number"},
            },
        },
    },
    "required": ["mapped_items"],
}


def match_prices_bucket(
    client: Anthropic,
    indexed_furniture_list: list[dict[str, Any]],
    supplier_text: str,
    fixed_supplier: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """
    Runs price matching for ONE upload bucket. Returns (mapped_items,
    alt_batch_info) — alt_batch_info is only ever populated when
    fixed_supplier == "ALT". Raises LogicError if the AI response is malformed.

    Uses Claude Opus 5 (not the app-wide ANTHROPIC_MODEL default, which stays
    on Sonnet for furniture-list extraction) — this step follows several
    layered, nuanced rules at once (pre-discount pricing, room/zone matching,
    stone-top merging) that benefit from the stronger model.
    """
    system_prompt = build_price_matching_system_prompt(fixed_supplier)
    normalized_text = normalize_thousands_commas(supplier_text)
    user_prompt = (
        f"Furniture list (JSON, with original index):\n"
        f"{json.dumps(indexed_furniture_list, ensure_ascii=False)}\n\n"
        f"Supplier quotation text for this batch:\n{normalized_text}"
    )
    result = call_claude_json(
        client,
        system_prompt,
        user_prompt,
        tool_name="report_price_matches",
        tool_description="Reports the price-matching results for the furniture list against this supplier quotation batch.",
        input_schema=_PRICE_MATCH_INPUT_SCHEMA,
        model="claude-opus-5",
        max_tokens=8192,
        # Opus 5 thinks before answering by default, and this request carries
        # the full furniture list (often 100+ items) plus the whole supplier
        # PDF's text — routinely takes longer than the 60s tuned for the
        # non-thinking model this replaced. One retry, not the default two,
        # since up to 4 buckets run sequentially per house and a slow-but-
        # correct request rarely gets faster on retry.
        timeout=180,
        max_retries=1,
    )
    mapped = result.get("mapped_items")
    if not isinstance(mapped, list):
        raise LogicError("⚠️ The AI response did not contain a valid 'mapped_items' list.")

    for item in mapped:
        fixed_price, warn_note = validate_and_fix_price(item.get("unit_price"), supplier_text)
        item["unit_price"] = fixed_price
        item["_price_autocorrect_note"] = warn_note

    alt_batch_info = result.get("alt_batch_info") if fixed_supplier == "ALT" else None
    if not isinstance(alt_batch_info, dict):
        alt_batch_info = None
    return mapped, alt_batch_info


def merge_bucket_results(
    furniture_list: list[dict[str, Any]],
    bucket_results: list[tuple[str, list[dict[str, Any]]]],
) -> tuple[list[dict[str, Any]], str | None]:
    """
    Merges match results from up to 4 independently-processed upload buckets
    back into one final mapping list, keyed by each item's original
    position. Same merge/classification rules as app.py. Returns
    (final_mapping, warning_or_None) for the auto-corrected-prices count.
    """
    final_mapping: list[dict[str, Any]] = []
    for item in furniture_list:
        final_mapping.append({
            "room": item.get("room", ""),
            "item_name": item.get("item_name", ""),
            "quantity": item.get("quantity", 1),
            "unit_price": 0,
            "alt_price": 0,
            "pmay_price": 0,
            "other_maker_price": 0,
            "supplier": "",
            "order_type": "จัดซื้อ (ราคาจริง ไม่บวกกำไร)",
            "spec": item.get("spec", ""),
            "quotation_spec": "",
        })

    autocorrected_total = 0
    ordered_buckets = sorted(bucket_results, key=lambda br: 0 if br[0] == "PURCHASE" else 1)

    for bucket_label, mapped in ordered_buckets:
        if not mapped:
            continue
        for row in mapped:
            idx = row.get("index")
            if not isinstance(idx, int) or not (0 <= idx < len(final_mapping)):
                continue
            price = row.get("unit_price") or 0
            if price <= 0:
                continue

            entry = final_mapping[idx]
            autocorrected = bool(row.get("_price_autocorrect_note"))

            quotation_spec = str(row.get("quotation_spec") or "").strip()
            if quotation_spec:
                entry["quotation_spec"] = quotation_spec

            if bucket_label == "ALT":
                entry["alt_price"] = price
                entry["order_type"] = "สั่งผลิต"
                if autocorrected:
                    entry["item_name"] += " [ราคา ALT ถูกแก้ไขอัตโนมัติ - โปรดตรวจสอบ]"
                    autocorrected_total += 1
            elif bucket_label == "P'May":
                entry["pmay_price"] = price
                entry["order_type"] = "สั่งผลิต"
                if autocorrected:
                    entry["item_name"] += " [ราคา P'May ถูกแก้ไขอัตโนมัติ - โปรดตรวจสอบ]"
                    autocorrected_total += 1
            elif bucket_label == "OTHER_MAKER":
                entry["other_maker_price"] = price
                entry["unit_price"] = price
                entry["order_type"] = "จัดซื้อ (บวกกำไร 10DK)"
                if autocorrected:
                    entry["item_name"] += " [ราคา Other ถูกแก้ไขอัตโนมัติ - โปรดตรวจสอบ]"
                    autocorrected_total += 1
            else:  # PURCHASE
                entry["unit_price"] = price
                entry["supplier"] = row.get("supplier") or ""
                if autocorrected:
                    entry["item_name"] += " [ราคาถูกแก้ไขอัตโนมัติ - โปรดตรวจสอบ]"
                    autocorrected_total += 1

    for entry in final_mapping:
        has_any_price = (
            entry["unit_price"] > 0
            or entry["alt_price"] > 0
            or entry["pmay_price"] > 0
            or entry["other_maker_price"] > 0
        )
        if not has_any_price and "[Price Not Found]" not in entry["item_name"]:
            entry["item_name"] += " [Price Not Found]"

    warning = None
    if autocorrected_total:
        warning = (
            f"⚠️ พบและแก้ไขราคาที่น่าจะอ่านผิด (comma เป็นจุดทศนิยม) โดยอัตโนมัติ "
            f"{autocorrected_total} รายการ — สังเกตแท็ก '[...ถูกแก้ไขอัตโนมัติ - โปรดตรวจสอบ]' "
            f"ในตารางรีวิวด้านล่าง และตรวจสอบราคาอีกครั้งก่อน export"
        )

    return final_mapping, warning


# --------------------------------------------------------------------------------------
# Review-table helpers (Step 3 review grid)
# --------------------------------------------------------------------------------------

SUSPICIOUS_PRICE_THRESHOLD = 100


def flag_suspicious_prices(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Flags rows with an implausibly low price (< 100 บาท — furniture is
    essentially never priced under this), the same auto-flag the original
    app recomputed on every render. Returns new dicts with `suspicious: bool`
    set; does not mutate the input.
    """
    out = []
    for item in rows:
        prices = [
            float(item.get("unit_price", 0) or 0),
            float(item.get("alt_price", 0) or 0),
            float(item.get("pmay_price", 0) or 0),
            float(item.get("other_maker_price", 0) or 0),
        ]
        suspicious = any(0 < p < SUSPICIOUS_PRICE_THRESHOLD for p in prices)
        out.append({**item, "suspicious": suspicious})
    return out
