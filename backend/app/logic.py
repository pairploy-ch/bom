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
from typing import Any

import openpyxl
import pdfplumber
from openai import APIError, APITimeoutError, OpenAI, RateLimitError
from openpyxl.utils import column_index_from_string
from openpyxl.utils.exceptions import InvalidFileException

from . import config

logger = logging.getLogger("bom_backend")


class LogicError(Exception):
    """Raised for the same conditions the original app surfaced via st.error()."""


# --------------------------------------------------------------------------------------
# Data Models
# --------------------------------------------------------------------------------------

PENDANT_LAMP_KEYWORDS = ["โคมไฟห้อยเพดาน", "pendant lamp", "pendant light"]


@dataclass
class ColumnMapping:
    """
    Maps logical BOM fields to Excel column letters, matching the real
    'รายการเพิ่มเติม / รายการ TBC เดิม' template structure — see the original
    app.py for the full column-by-column rationale. Unchanged from the
    original; only relocated.
    """
    supplier_col: str = "A"
    room_col: str = "B"
    item_col: str = "C"
    qty_col: str = "D"
    custom_made_price_col: str = "G"
    purchased_price_col: str = "N"
    start_row: int = 5
    generate_formulas: bool = True
    multiplier_anchor_row: int = 5
    formula_h_col: str = "H"
    formula_i_col: str = "I"
    formula_k_col: str = "K"
    formula_j_col: str = "J"
    formula_l_col: str = "L"
    formula_m_col: str = "M"
    formula_additional_item_col: str = "Q"
    formula_purchase_compare_col: str = "R"


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
        timeout=config.OPENAI_TIMEOUT_SECONDS,
        max_retries=config.OPENAI_MAX_RETRIES,
    )


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
                page_text = page.extract_text(x_tolerance=1, y_tolerance=3) or ""
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
            current_row += 1

            item_no = 1
            for row in grouped[room]:
                order_type = row.get("order_type", "จัดซื้อ (ราคาจริง ไม่บวกกำไร)")
                unit_price = row.get("unit_price", 0) or 0
                unit_price, _adj_note = apply_known_price_adjustments(row.get("item_name", ""), unit_price)

                ws.cell(row=current_row, column=room_idx).value = item_no
                ws.cell(row=current_row, column=item_idx).value = row.get("item_name", "")
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
                        if col_map.generate_formulas:
                            ws.cell(row=r, column=h_idx).value = f"={col_map.custom_made_price_col}{r}*{col_map.formula_h_col}${anchor}"
                            ws.cell(row=r, column=i_idx).value = f"={col_map.formula_h_col}{r}*{col_map.formula_i_col}${anchor}"
                    if pmay_price > 0:
                        ws.cell(row=r, column=j_idx).value = f"=({pmay_price}*{col_map.formula_i_col}${anchor})"
                    if other_maker_price > 0:
                        ws.cell(row=r, column=k_idx).value = f"=({other_maker_price}*{col_map.formula_i_col}${anchor})"
                    if col_map.generate_formulas and (alt_price > 0 or pmay_price > 0 or other_maker_price > 0):
                        ws.cell(row=r, column=l_idx).value = f"=MAX({col_map.formula_i_col}{r}:{k_col}{r})"
                        ws.cell(row=r, column=m_idx).value = f"=ROUNDUP({col_map.formula_l_col}{r}*{col_map.formula_m_col}${anchor},-3)"
                        ws.cell(row=r, column=q_idx).value = f"={col_map.formula_m_col}{r}"

                elif order_type == "จัดซื้อ (บวกกำไร 10DK)":
                    supplier = row.get("supplier", "")
                    if supplier:
                        ws.cell(row=current_row, column=supplier_idx).value = supplier
                    if col_map.generate_formulas:
                        r = current_row
                        ws.cell(row=r, column=k_idx).value = f"=({unit_price}*{col_map.formula_i_col}${anchor})"
                        ws.cell(row=r, column=l_idx).value = f"=MAX({col_map.formula_i_col}{r}:{k_col}{r})"
                        ws.cell(row=r, column=m_idx).value = f"=ROUNDUP({col_map.formula_l_col}{r}*{col_map.formula_m_col}${anchor},-3)"
                        ws.cell(row=r, column=q_idx).value = f"={col_map.formula_m_col}{r}"
                    else:
                        ws.cell(row=current_row, column=k_idx).value = unit_price

                else:  # "จัดซื้อ (ราคาจริง ไม่บวกกำไร)"
                    supplier = row.get("supplier", "")
                    if supplier:
                        ws.cell(row=current_row, column=supplier_idx).value = supplier
                    ws.cell(row=current_row, column=purchased_price_idx).value = unit_price
                    if col_map.generate_formulas:
                        r = current_row
                        ws.cell(row=r, column=r_idx).value = f"={col_map.purchased_price_col}{r}"

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
            if baseline_furniture_value and baseline_furniture_value > 0:
                management_fee = baseline_furniture_value * 0.12
                net_total = furniture_total + management_fee
                ws.cell(row=summary_row, column=item_idx).value = (
                    f"ค่าดำเนินการ 12% (จากมูลค่าฐาน {baseline_furniture_value:,.0f} บาท): "
                    f"{management_fee:,.0f} บาท"
                )
                summary_row += 1
                ws.cell(row=summary_row, column=item_idx).value = f"ราคารวมสุทธิ: {net_total:,.0f} บาท"
            else:
                ws.cell(row=summary_row, column=item_idx).value = (
                    "⚠️ ยังไม่ได้กรอกมูลค่าฐานสำหรับคำนวณค่าดำเนินการ 12% "
                    "(ราคารวมสุทธิด้านบนยังไม่รวมค่าดำเนินการ)"
                )

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
) -> list[dict[str, Any]]:
    """
    Replicates the Excel formula chain in pure Python, so the review table
    can show real computed numbers before anything is written to the file.
    Same as the original, except `excel_bytes` is now a parameter instead of
    being read from `st.session_state` directly.
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
        out: dict[str, Any] = {
            "room": row.get("room", ""),
            "item_name": row.get("item_name", ""),
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
        out["line_total"] = (out.get("m_10dk_price") or out.get("n_actual_price") or 0) * qty
        preview.append(out)
    return preview


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
- Do not invent items that are not present in the text.
- Merge duplicate entries of the exact same item within the same room by summing quantities.

Respond ONLY with a JSON object of this exact shape (no extra commentary):
{
  "items": [
    {"room": "Bedroom 1", "item_name": "King Size Bed", "quantity": 1}
  ]
}
"""

_PAGE_SPLIT_RE = re.compile(r"--- Page \d+ ---")


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


def extract_furniture_list(client: OpenAI, pdf_text: str) -> list[dict[str, Any]]:
    """
    Extracts the furniture list page by page (better completeness on long
    documents than one giant prompt), then merges. Raises LogicError if
    every page's extraction failed.
    """
    pages = [p.strip() for p in _PAGE_SPLIT_RE.split(pdf_text) if p.strip()]
    if not pages:
        pages = [pdf_text]

    merged: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    any_success = False

    for page_text in pages:
        user_prompt = f"Extracted PDF text (one page):\n\n{page_text}"
        result = call_openai_json(client, FURNITURE_EXTRACTION_SYSTEM_PROMPT, user_prompt)
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
            key = (room, name.lower())
            if key in merged:
                merged[key]["quantity"] = (merged[key].get("quantity", 0) or 0) + qty
            else:
                merged[key] = {"room": room, "item_name": name, "quantity": qty}
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
    "OTHER_MAKER": "สั่งผลิต",
    "PURCHASE": "จัดซื้อ (ราคาจริง ไม่บวกกำไร)",
}


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
"room", "item_name", and "quantity".
2. Raw text extracted from one or more supplier price quotation PDFs, all belonging to the \
SAME pricing batch. If there are multiple documents, each is marked with a "===== Supplier \
document: <filename> =====" header so you know which text belongs to which file.

Your job:
- IMPORTANT — check filenames first: suppliers sometimes name each quotation PDF file after the \
specific furniture item it quotes (e.g. a file named "Sideboard.pdf" or "TV_Console_quote.pdf" \
almost certainly quotes that exact item). Before matching by reading through all the text, check \
whether any document's filename (given in its "===== Supplier document: <filename> =====" \
header) closely matches an item's name — if so, treat that as a strong signal and prioritize \
looking for that item's price within that specific document's text.
- For each item in the furniture list, semantically match it against this batch's quotation \
text (e.g., "King Size Bed" may match a line like "6-foot wooden bed frame" or "Bed Frame - \
King - Solid Oak").
- Extract the correct unit price (a number, no currency symbols) for each matched item from \
the text.
- CRITICAL — number formatting: Thai and English business documents commonly write prices \
with a comma (,) as the THOUSANDS separator and a period (.) as the decimal separator, e.g. \
"12,610" means twelve thousand six hundred ten (12610), NOT 12. NEVER truncate, round, or \
drop digits from a price. Always read the FULL number including every digit before AND after \
any comma. Double-check each extracted price against the source text before including it in \
your answer — a price under 100 for furniture items is almost always a sign you mis-read the \
number; re-check the source text in that case.{price_column_clause}
{supplier_clause}
- If the same item could be matched more than once within this batch, choose the lowest \
valid price.
- If an item cannot be confidently matched anywhere in THIS batch's text, simply OMIT it from \
"mapped_items" entirely — do not include a zero-price guess. This batch may just not contain \
that item; it might be matched in a different batch instead.
- Never fabricate a price that is not present in the text.
- ALWAYS include the original "index" field (copied exactly from the input item) in every \
output row, so results can be merged back to the correct item afterward.{alt_info_clause}

Respond ONLY with a JSON object of this exact shape (no extra commentary):
{{
  "mapped_items": [
    {{"index": 0, "unit_price": 350.00, "supplier": "ABC Furniture Co."}}
  ]
}}
"""


def match_prices_bucket(
    client: OpenAI,
    indexed_furniture_list: list[dict[str, Any]],
    supplier_text: str,
    fixed_supplier: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """
    Runs price matching for ONE upload bucket. Returns (mapped_items,
    alt_batch_info) — alt_batch_info is only ever populated when
    fixed_supplier == "ALT". Raises LogicError if the AI response is malformed.
    """
    system_prompt = build_price_matching_system_prompt(fixed_supplier)
    normalized_text = normalize_thousands_commas(supplier_text)
    user_prompt = (
        f"Furniture list (JSON, with original index):\n"
        f"{json.dumps(indexed_furniture_list, ensure_ascii=False)}\n\n"
        f"Supplier quotation text for this batch:\n{normalized_text}"
    )
    result = call_openai_json(client, system_prompt, user_prompt)
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
                entry["order_type"] = "สั่งผลิต"
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
        has_any_price = entry["unit_price"] > 0 or entry["alt_price"] > 0 or entry["pmay_price"] > 0
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
