"""
Automated Furniture BOM & Price Mapping Web App
=================================================

A 3-step Streamlit pipeline that:
  1. Ingests an existing Excel BOM template (formulas/formatting preserved via openpyxl).
  2. Extracts a furniture list from a floor-plan / furniture-list PDF using GPT-4o-mini.
  3. Extracts a supplier quotation PDF, semantically matches items against the Step 2
     list using GPT-4o-mini, and writes the final Room / Item / Qty / Unit Price data
     into the cached Excel workbook starting at a user-defined row.

--------------------------------------------------------------------------------------
SETUP: OpenAI API Key as a Streamlit Secret
--------------------------------------------------------------------------------------
Locally:
    Create a file at `.streamlit/secrets.toml` in your project root with:

        OPENAI_API_KEY = "sk-...your-key..."

On Streamlit Community Cloud:
    Go to your app -> Settings -> Secrets, and paste:

        OPENAI_API_KEY = "sk-...your-key..."

The app reads it via `st.secrets["OPENAI_API_KEY"]` (see `get_openai_client()` below).
Never hardcode the key in source control.

--------------------------------------------------------------------------------------
requirements.txt (for Streamlit Community Cloud)
--------------------------------------------------------------------------------------
    streamlit>=1.33
    openpyxl>=3.1
    pdfplumber>=0.11
    openai>=1.30
--------------------------------------------------------------------------------------
"""

from __future__ import annotations  # Python 3.9 compatibility for `X | None` / `list[X]` type hints

import io
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import openpyxl
import pdfplumber
import streamlit as st
from openai import APIError, APITimeoutError, OpenAI, RateLimitError
from openpyxl.utils import column_index_from_string
from openpyxl.utils.exceptions import InvalidFileException

# --------------------------------------------------------------------------------------
# Config & Logging
# --------------------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("furniture_bom_app")

OPENAI_MODEL = "gpt-4o-mini"
OPENAI_TIMEOUT_SECONDS = 60
OPENAI_MAX_RETRIES = 2

st.set_page_config(
    page_title="Furniture BOM & Price Mapping",
    page_icon="🛋️",
    layout="wide",
)

# --------------------------------------------------------------------------------------
# Session State Initialization
# --------------------------------------------------------------------------------------

DEFAULT_STATE = {
    "excel_bytes": None,           # raw bytes of the uploaded template (kept pristine)
    "excel_filename": None,
    "furniture_list": None,        # list[dict] from Step 2
    "final_mapping": None,         # list[dict] from Step 3
    "step2_raw_text": None,
    "step3_raw_text": None,
    "alt_batch_info": None,        # {"sum_of_item_costs":..., "protection_fee":...} from Step 3 AI, if an ALT quote was detected
    "baseline_furniture_value": None,  # user-entered, for the 12% management-fee reference display
}
for key, default in DEFAULT_STATE.items():
    if key not in st.session_state:
        st.session_state[key] = default


# --------------------------------------------------------------------------------------
# Data Models
# --------------------------------------------------------------------------------------

PENDANT_LAMP_KEYWORDS = ["โคมไฟห้อยเพดาน", "pendant lamp", "pendant light"]


def apply_known_price_adjustments(item_name: str, unit_price: float) -> tuple[float, str]:
    """
    Auto-detects pricing exceptions documented in the pricing policy and
    adjusts the raw unit price accordingly, returning (adjusted_price, note).
    Currently handles: pendant lamps (โคมไฟห้อยเพดาน) — per policy, these add
    a fixed remote-control fee (4,500) + installation fee (2,000) on top of
    the quoted price before any markup is applied. Whether profit margin is
    then applied on top is still a judgment call left to the "ประเภท"
    dropdown (สั่งผลิต/จัดซื้อ บวกกำไร add it, จัดซื้อ ราคาจริง does not).
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
# GPT-4o-mini occasionally misreads a comma-thousands-separated number as a
# decimal point even when explicitly instructed not to (e.g. reads "12,000"
# as 12.000 -> rounds/truncates to 12). This is the single most common,
# well-understood failure mode we've seen in practice, so rather than only
# relying on prompt wording (which can't be guaranteed to hold 100% of the
# time), we cross-check every AI-extracted price against the actual raw
# supplier text after the fact and auto-correct this specific pattern.

# Matches numbers written with proper thousands-commas, e.g. 12,000 or
# 12,610.50 or 1,234,567 — used as the "ground truth" candidates to compare
# AI-extracted prices against.
_NUMBER_WITH_COMMA_RE = re.compile(r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b")


def validate_and_fix_price(ai_price: float | None, raw_text: str) -> tuple[float | None, str]:
    """
    Cross-checks an AI-extracted unit price against numbers actually present
    in the raw supplier text, to catch the specific failure mode where the
    AI reads a thousands-comma as a decimal point (e.g. reads "12,000" as
    12.0, which then gets stored/rounded down to 12).

    Strategy: find every properly comma-formatted number in the raw text.
    If `ai_price * 1000` matches one of those numbers almost exactly, this
    is very likely a dropped-thousands misread -> auto-correct to the value
    found in the source text, and return a warning note so the row can be
    flagged for human review before export.

    This intentionally only targets this one well-understood ×1000 pattern
    (not a general "does this number look right" heuristic) to avoid
    "correcting" prices that are legitimately small or that don't have a
    matching comma-formatted counterpart in the text.

    Returns (possibly-corrected price, warning note or "" if untouched).
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
    """
    Strips the thousands-comma from every properly comma-formatted number in
    the text (e.g. "12,610" -> "12610") BEFORE it's sent to the AI.

    This fixes the misread at its actual source: the ambiguity only exists
    because the comma can look like a decimal point to the model. If the
    model never sees a comma in the first place, there's nothing to
    misread — this is far more reliable than only catching the mistake
    after the fact via validate_and_fix_price(), which stays in place as a
    safety net for numbers this regex doesn't happen to catch (e.g. unusual
    spacing or OCR artifacts), but should trigger much less often once the
    obvious, well-formed cases are already disambiguated here.
    """
    return _NUMBER_WITH_COMMA_RE.sub(lambda m: m.group(0).replace(",", ""), text)


@dataclass
class ColumnMapping:
    """
    Maps logical BOM fields to Excel column letters, matching the real
    'รายการเพิ่มเติม / รายการ TBC เดิม' template structure:
      - Column A: Supplier name (only filled for 'จัดซื้อ' / purchased items)
      - Column B: Room/section header text, OR item running number
      - Column C: Item description
      - Column D: Quantity
      - Column G: ต้นทุน ALT — raw cost, only for 'สั่งผลิต' (custom-made) items
                  (downstream columns H/I/L/M contain formulas derived from G)
      - Column N: งานจัดซื้อ เบิกจ่ายตามราคาจริง — actual price, only for
                  'จัดซื้อ' (purchased) items

    If `generate_formulas` is on, these downstream formula columns are also
    auto-filled to match the template's existing formula pattern:
      สั่งผลิต (custom-made):
        H<row> = G<row>*H$<anchor_row>
        I<row> = H<row>*I$<anchor_row>
        L<row> = MAX(I<row>:K<row>)   — safe default: with J/K blank for new
                 items, Excel's MAX ignores blank cells and simply carries I
                 through, matching the template's existing convention. This
                 IS a business-judgment column in the original template
                 (some rows use MIN, some add manual adjustments) — review
                 it after export if a row needs different logic.
        M<row> = ROUNDUP(L<row>*M$<anchor_row>, -3)
        Q<row> = M<row>               — classifies the row as "รายการเพิ่มเติม"
                 (additional item), matching this app's use case
      จัดซื้อ (purchased):
        R<row> = N<row>               — mirrors the actual purchase price
                 into the comparison column, matching existing rows

    Adjust these in the sidebar if your template's columns/logic differ.
    """
    supplier_col: str = "A"
    room_col: str = "B"
    item_col: str = "C"
    qty_col: str = "D"
    custom_made_price_col: str = "G"   # used when order_type == "สั่งผลิต"
    purchased_price_col: str = "N"     # used when order_type == "จัดซื้อ"
    start_row: int = 5
    generate_formulas: bool = True
    multiplier_anchor_row: int = 5     # the H$5 / I$5 / M$5 anchor row
    formula_h_col: str = "H"
    formula_i_col: str = "I"
    formula_k_col: str = "K"
    formula_j_col: str = "J"          # P'May direct path -> "=(price*I$anchor)"
    formula_l_col: str = "L"
    formula_m_col: str = "M"
    formula_additional_item_col: str = "Q"  # สั่งผลิต -> "=M<row>"
    formula_purchase_compare_col: str = "R"  # จัดซื้อ -> "=N<row>"


# --------------------------------------------------------------------------------------
# OpenAI Client
# --------------------------------------------------------------------------------------

def get_openai_client() -> OpenAI | None:
    """Instantiate the OpenAI client from Streamlit secrets. Returns None if missing."""
    api_key = st.secrets.get("OPENAI_API_KEY") if hasattr(st, "secrets") else None
    if not api_key:
        return None
    return OpenAI(api_key=api_key, timeout=OPENAI_TIMEOUT_SECONDS, max_retries=OPENAI_MAX_RETRIES)


def call_openai_json(client: OpenAI, system_prompt: str, user_prompt: str) -> dict[str, Any] | None:
    """
    Calls the OpenAI Chat Completions API in JSON mode and returns the parsed dict.
    Returns None (and surfaces a Streamlit error) on any failure.
    """
    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            response_format={"type": "json_object"},
            temperature=0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        content = response.choices[0].message.content
        return json.loads(content)
    except APITimeoutError:
        st.error("⏱️ The OpenAI API request timed out. Please try again.")
    except RateLimitError:
        st.error("🚦 OpenAI rate limit reached. Wait a moment and retry.")
    except APIError as e:
        st.error(f"❌ OpenAI API error: {e}")
    except json.JSONDecodeError:
        st.error("⚠️ The AI response was not valid JSON. Please retry the step.")
    except Exception as e:  # noqa: BLE001 - surface any unexpected failure to the user
        logger.exception("Unexpected error calling OpenAI")
        st.error(f"⚠️ Unexpected error calling OpenAI: {e}")
    return None


# --------------------------------------------------------------------------------------
# PDF Text Extraction
# --------------------------------------------------------------------------------------

_THAI_CHAR_RE = re.compile(r"[\u0E00-\u0E7F]")

# Byte sequences/characters that commonly appear when a PDF's embedded Thai
# font uses a custom/legacy encoding (e.g. TIS-620) that gets misread as
# Latin-1 — producing "mojibake" like "¤èÒ´Óà¹Ô¹¡ÒÃ" instead of readable Thai.
# No amount of regex cleanup can fix this (the actual character data is
# wrong, not just misformatted) — it needs OCR instead.
_MOJIBAKE_CHARS_RE = re.compile(r"[¤èÒÐÑÃéçÊÍÔÙâãÅì¹]")


def clean_thai_extracted_text(text: str) -> str:
    """
    Fixes a common pdfplumber artifact with Thai PDFs: a stray single space
    inserted between individual Thai characters (caused by glyph spacing in
    some fonts, which pdfplumber can mistake for a word boundary). Thai
    script doesn't use spaces between characters within a word, so it's
    safe to collapse a single space that sits between two Thai characters —
    this does NOT touch spaces between Thai and non-Thai text (e.g. numbers,
    Latin brand names) or intentional multi-space/newline breaks.
    """
    return re.sub(r"(?<=[\u0E00-\u0E7F]) (?=[\u0E00-\u0E7F])", "", text)


def detect_thai_mojibake(text: str, sample_size: int = 2000) -> bool:
    """
    Heuristic check for the "wrong font encoding" failure mode: if a
    meaningful chunk of the extracted text is neither valid Thai script nor
    plain ASCII/digits, but instead matches the specific Latin-1-misread
    pattern common to TIS-620-encoded Thai fonts, text extraction has
    fundamentally failed for this PDF and no cleanup can recover it.
    """
    sample = text[:sample_size]
    if not sample.strip():
        return False
    mojibake_hits = len(_MOJIBAKE_CHARS_RE.findall(sample))
    thai_hits = len(_THAI_CHAR_RE.findall(sample))
    # If we see meaningfully more mojibake-pattern characters than actual
    # Thai script characters, this PDF's font encoding is broken.
    return mojibake_hits > 20 and mojibake_hits > thai_hits


def extract_text_from_pdf(uploaded_file) -> str | None:
    """
    Extracts text from an uploaded PDF using pdfplumber, with Thai-specific
    handling: tighter character-clustering tolerances (helps avoid spurious
    word breaks in Thai script), a cleanup pass that collapses stray spaces
    pdfplumber sometimes inserts between individual Thai characters, and
    detection of PDFs whose embedded Thai font uses a broken/legacy encoding
    (which produces unreadable "mojibake" that no text cleanup can fix —
    those need OCR instead, and we warn clearly rather than silently
    returning garbage to the AI).

    Returns None if no extractable text is found (e.g., a pure-image/scanned
    PDF with no OCR layer) or if the file cannot be parsed.
    """
    try:
        uploaded_file.seek(0)
        text_chunks = []
        with pdfplumber.open(uploaded_file) as pdf:
            if len(pdf.pages) == 0:
                st.warning("The uploaded PDF has no pages.")
                return None
            for page_num, page in enumerate(pdf.pages, start=1):
                # x_tolerance=1 clusters characters more tightly than
                # pdfplumber's default (3), which reduces spurious spaces
                # inserted mid-word in Thai script from certain fonts.
                page_text = page.extract_text(x_tolerance=1, y_tolerance=3) or ""
                if page_text.strip():
                    page_text = clean_thai_extracted_text(page_text)
                    text_chunks.append(f"--- Page {page_num} ---\n{page_text}")
                else:
                    # Fallback: try extracting text from tables if plain text failed
                    tables = page.extract_tables()
                    for table in tables:
                        rows = ["\t".join(cell or "" for cell in row) for row in table]
                        if rows:
                            text_chunks.append(f"--- Page {page_num} (table) ---\n" + "\n".join(rows))

        full_text = "\n\n".join(text_chunks).strip()

        if not full_text:
            st.warning(
                "⚠️ No selectable text could be extracted from this PDF. "
                "It may be a scanned image without a text layer. "
                "Please upload a text-based PDF or run OCR on it first "
                "(e.g., with a tool like OCRmyPDF) before uploading."
            )
            return None

        if detect_thai_mojibake(full_text):
            st.warning(
                "⚠️ PDF นี้น่าจะใช้ฟอนต์ไทยแบบ custom encoding ที่ทำให้ข้อความที่ดึงออกมาอ่านไม่ออก "
                "(เช่น '¤èÒ´Óà¹Ô¹¡ÒÃ' แทนที่จะเป็นข้อความไทยปกติ) — เป็นปัญหาที่ระดับไฟล์ PDF เอง "
                "แก้ด้วยโค้ดไม่ได้ครับ วิธีแก้: เปิดไฟล์ด้วย Adobe Acrobat หรือโปรแกรม PDF แล้ว "
                "'Print to PDF' ใหม่อีกรอบ (จะฝังฟอนต์แบบมาตรฐาน) หรือรัน OCR ทับ (เช่น OCRmyPDF) "
                "ก่อนอัปโหลดใหม่"
            )

        return full_text

    except Exception as e:  # noqa: BLE001
        logger.exception("PDF extraction failed")
        st.error(f"❌ Failed to read PDF: {e}")
        return None


# --------------------------------------------------------------------------------------
# Excel Handling (openpyxl only — never pandas.to_excel — to preserve formulas/formatting)
# --------------------------------------------------------------------------------------

def load_workbook_from_bytes(file_bytes: bytes):
    """Loads a workbook from raw bytes, preserving formulas (data_only=False)."""
    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=False, keep_vba=False)
        return wb
    except InvalidFileException:
        st.error("❌ The uploaded file is not a valid .xlsx workbook.")
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to load Excel workbook")
        st.error(f"❌ Failed to read Excel file: {e}")
    return None


def get_append_start_row(ws, start_row: int, before_row: int | None = None) -> int:
    """
    Returns a safe row to start appending brand-new data at.

    Naively using `ws.max_row + 1` is unreliable: Excel templates often carry
    cell *formatting* on many blank rows below the real data (reserved for
    future entries), which inflates `ws.max_row` far past where any actual
    content ends — appending there would leave a large, confusing gap.

    Scanning only the specific columns we intend to write to is ALSO
    unreliable: templates commonly have other content further down (e.g. a
    grand-total / remarks section using different columns) that would get
    silently overwritten if we only checked our own columns for emptiness.

    So this scans the FULL width of each row (every column actually used in
    the sheet) from the bottom up, and returns one past the last row that
    has a real value anywhere in it. Falls back to `start_row` if the sheet
    has no data yet.

    If `before_row` is given, the scan is capped there (exclusive) — used to
    search only within the safe zone above a detected grand-total row,
    instead of scanning the whole sheet.
    """
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
    """
    Looks for a 'grand total' row: the first row (scanning downward from
    start_row) containing a formula like =SUM(G5:G67) — a same-column,
    upward-referencing SUM. This is the standard shape of a running-total
    row in these BOM templates.

    Returns that row number, or None if no such row is found (meaning the
    template has no pre-built totals section — safe to just append at the
    bottom as before).
    """
    max_col = max(ws.max_column, 1)
    for r in range(start_row, ws.max_row + 1):
        for c in range(1, max_col + 1):
            val = ws.cell(row=r, column=c).value
            if isinstance(val, str) and val.startswith("=") and "SUM(" in val.upper():
                m = _SUM_RANGE_RE.search(val)
                if m and int(m.group(3)) < r:  # range ends above this row = running total
                    return r
    return None


def extend_sum_ranges(ws, total_row: int, old_last_row: int, new_last_row: int) -> int:
    """
    After writing new item rows into the blank space directly above a
    detected grand-total row (WITHOUT shifting any rows — see
    find_grand_total_row), extend every SUM(...) formula in `total_row`
    whose range currently ends exactly at `old_last_row` so it ends at
    `new_last_row` instead — i.e. so the total picks up the new rows.

    This is a pure text edit to formula strings; since no rows were
    physically moved, every other formula in the workbook (including ones
    referencing the total row itself by cell address) remains valid and
    untouched. Returns how many formulas were extended.
    """
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
) -> bytes | None:
    """
    Appends new Room-section headers + item rows below the last used row of
    the template (the template ships with headers only, so we never insert
    into or overwrite any existing data/formulas — only cell.value assignment
    via openpyxl on brand-new rows).

    Each row in `mapping_rows` is expected to have:
        room (str), item_name (str), quantity (number),
        unit_price (number),
        order_type: one of
            "สั่งผลิต"                          — Method 1: custom-made (ALT)
            "จัดซื้อ (บวกกำไร 10DK)"             — Method 2: purchased, 10DK markup
            "จัดซื้อ (ราคาจริง ไม่บวกกำไร)"       — Method 3: purchased, pass-through
        supplier (str, optional — meaningful for either "จัดซื้อ" method)

    Items are grouped by room in the order they first appear. For each room,
    a section header row (room name in the room column) is written, followed
    by its item rows with a running item number in the room column.
    """
    wb = load_workbook_from_bytes(file_bytes)
    if wb is None:
        return None

    try:
        if sheet_name not in wb.sheetnames:
            st.error(f"❌ Sheet '{sheet_name}' not found in the workbook.")
            return None
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

        # Group rows by room, preserving first-seen order.
        rooms_order: list[str] = []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in mapping_rows:
            room = str(row.get("room") or "Unspecified")
            if room not in grouped:
                grouped[room] = []
                rooms_order.append(room)
            grouped[room].append(row)

        # Detect a pre-built grand-total row (e.g. G69 = SUM(G5:G67)). If
        # found, we NEVER insert/shift rows (too risky — openpyxl doesn't
        # auto-adjust other formulas the way Excel's native "Insert Row"
        # does). Instead we write new items into the existing blank space
        # directly above it, then extend the SUM ranges to include them —
        # a pure text edit that leaves every other formula in the workbook
        # untouched and valid.
        total_row = find_grand_total_row(ws, col_map.start_row)
        old_last_row = get_append_start_row(ws, col_map.start_row, before_row=total_row) - 1
        current_row = old_last_row + 1

        if total_row is not None:
            rows_needed = sum(1 + len(items) for items in grouped.values())
            available = total_row - current_row
            if available < rows_needed:
                st.warning(
                    f"⚠️ พบแถวสรุปยอดที่แถว {total_row} แต่เหลือช่องว่างให้เขียนแค่ {max(available,0)} "
                    f"แถว (ต้องการ {rows_needed} แถว) — ระบบจะเขียนข้อมูลต่อจากที่มีอยู่ตามปกติ "
                    f"แต่คุณต้องตรวจสอบ/ขยายช่วง SUM ที่แถว {total_row} ด้วยตัวเองหลัง export"
                )
                total_row = None  # fall back to normal append, skip auto-extend
                # CRITICAL: current_row was computed bounded to `before_row`
                # (the old total_row) — must recompute unbounded, otherwise
                # we'd write into/through the total row and corrupt it.
                current_row = get_append_start_row(ws, col_map.start_row)

        for room in rooms_order:
            # Section header row, e.g. B<row> = "Living & Dining Area"
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
                    # Method 1 — Custom-made. ALT and P'May are NOT mutually
                    # exclusive: per policy, when both suppliers quoted this
                    # item, BOTH prices are written to the same row (G->H->I
                    # for ALT, J for P'May) and Excel's own =MAX(I:K) formula
                    # — which spans the adjacent I,J,K columns — picks
                    # whichever comes out higher after markup automatically.
                    alt_price = row.get("alt_price", 0) or 0
                    pmay_price = row.get("pmay_price", 0) or 0
                    # Back-compat: rows added/edited manually in the table
                    # (e.g. via "num_rows=dynamic") may only have the older
                    # generic "unit_price" field — treat that as an ALT cost.
                    if alt_price <= 0 and pmay_price <= 0 and unit_price > 0:
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
                    if col_map.generate_formulas and (alt_price > 0 or pmay_price > 0):
                        ws.cell(row=r, column=l_idx).value = f"=MAX({col_map.formula_i_col}{r}:{k_col}{r})"
                        ws.cell(row=r, column=m_idx).value = f"=ROUNDUP({col_map.formula_l_col}{r}*{col_map.formula_m_col}${anchor},-3)"
                        ws.cell(row=r, column=q_idx).value = f"={col_map.formula_m_col}{r}"

                elif order_type == "จัดซื้อ (บวกกำไร 10DK)":
                    # Method 2 — Purchased with 10DK markup: same downstream
                    # chain as custom-made, but the STARTING price must be the
                    # full/list price BEFORE any store discount (per policy —
                    # verify this in the review table, the AI may have
                    # extracted an already-discounted price from the quote).
                    # unit_price -> K (+5%+VAT7%) -> MAX(I:K) (L) ->
                    # ROUNDUP(chosen*1.45,-3) (M) -> classify as additional item (Q)
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
                    # Method 3 — Purchased at actual pass-through price (e.g.
                    # SB, Index, IKEA): use quoted price as-is, no markup.
                    supplier = row.get("supplier", "")
                    if supplier:
                        ws.cell(row=current_row, column=supplier_idx).value = supplier
                    ws.cell(row=current_row, column=purchased_price_idx).value = unit_price
                    if col_map.generate_formulas:
                        r = current_row
                        ws.cell(row=r, column=r_idx).value = f"={col_map.purchased_price_col}{r}"

                current_row += 1
                item_no += 1

        # Extend the grand-total SUM ranges to include the rows we just
        # wrote (pure text edit — no rows were shifted, so every other
        # formula in the workbook stays valid).
        if total_row is not None:
            new_last_row = current_row - 1
            extended = extend_sum_ranges(ws, total_row, old_last_row, new_last_row)
            if extended:
                st.info(
                    f"ℹ️ ขยายช่วง SUM ที่แถวสรุปยอด (แถว {total_row}) ให้ครอบคลุมแถวใหม่แล้ว "
                    f"({extended} สูตร) — ควรเปิดไฟล์ตรวจสอบยอดรวมอีกครั้งก่อนใช้งานจริง"
                )
            else:
                st.warning(
                    f"⚠️ พบแถวสรุปยอดที่แถว {total_row} แต่ไม่สามารถขยายช่วง SUM ให้อัตโนมัติได้ "
                    f"(รูปแบบสูตรอาจไม่ตรงกับที่ระบบรองรับ) — กรุณาตรวจสอบ/แก้ไขช่วง SUM ที่แถว "
                    f"{total_row} เองหลัง export"
                )

        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        return output.getvalue()

    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to write mapping into Excel")
        st.error(f"❌ Failed to write data into Excel: {e}")
        return None


# --------------------------------------------------------------------------------------
# AI Prompts — Step 2: Furniture List Extraction
# --------------------------------------------------------------------------------------

FURNITURE_EXTRACTION_SYSTEM_PROMPT = """You are a data extraction assistant specialized in reading \
furniture schedules and floor plan notes for interior design / construction projects.

Given raw text extracted from a PDF (a floor plan legend, furniture list, or bill of quantities), \
extract every distinct furniture item into structured JSON.

Rules:
- Group items by "room" as stated in the source text. If no room is given, use "Unspecified".
- "item_name" should be a clean, human-readable furniture description (no item codes unless \
that's all that's available).
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


def extract_furniture_list(client: OpenAI, pdf_text: str) -> list[dict[str, Any]] | None:
    user_prompt = f"Extracted PDF text:\n\n{pdf_text}"
    result = call_openai_json(client, FURNITURE_EXTRACTION_SYSTEM_PROMPT, user_prompt)
    if result is None:
        return None
    items = result.get("items")
    if not isinstance(items, list):
        st.error("⚠️ The AI response did not contain a valid 'items' list.")
        return None
    return items


# --------------------------------------------------------------------------------------
# AI Prompts — Step 3: Semantic Matching & Price Extraction
# --------------------------------------------------------------------------------------

# Step 3 is split into THREE independent upload buckets — "ALT", "P'May",
# and "OTHER" (the catch-all "เบิกจ่ายตามจริง" batch for stores like SB,
# Index, IKEA, etc.). This is deliberate: which bucket a PDF was uploaded
# into is decided by the HUMAN, not guessed by the AI, so `order_type` and
# `supplier` for ALT/P'May items are set deterministically from the bucket
# itself rather than relying on the AI (or a keyword-matching heuristic) to
# correctly detect the supplier name from free-text. The AI's job per
# bucket is narrowed to just "match items + extract prices" — which it's
# much more reliable at than "match items + extract prices + correctly
# identify which of two specific companies this text belongs to".
BUCKET_ORDER_TYPE = {
    "ALT": "สั่งผลิต",
    "P'May": "สั่งผลิต",
    "OTHER": "จัดซื้อ (ราคาจริง ไม่บวกกำไร)",
}


def build_price_matching_system_prompt(fixed_supplier: str | None) -> str:
    """
    Builds the Step-3 matching prompt for one upload bucket.

    `fixed_supplier` is "ALT", "P'May", or None:
      - "ALT" / "P'May": every document in this batch is already known to
        be from that supplier, so the AI doesn't need to detect/guess a
        supplier name — it's told to just stamp that exact value on every
        matched item. This also unlocks asking it to look for ALT's
        batch-total / protection-fee summary, which only makes sense when
        we know for certain this batch actually IS the ALT quote.
      - None: the "OTHER"/เบิกจ่ายตามจริง batch, which can span several
        different stores (SB, Index, IKEA, ...) in one upload, so the AI
        still needs to detect which store each price came from, same as
        the original single-batch prompt did.
    """
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

    alt_info_clause = ""
    if fixed_supplier == "ALT":
        alt_info_clause = (
            '\n- SEPARATELY, look for a TOTAL/summary section in this text (often near the end) '
            'stating: the total/sum of all item costs in this quote, and any separately-listed '
            '"ค่าดำเนินการ" / management fee / "Protection" / shipping fee charged on top. If '
            'found, report them in "alt_batch_info": {"sum_of_item_costs": <number>, '
            '"protection_fee": <number>}. If these totals aren\'t stated, omit "alt_batch_info" '
            "entirely (do not guess numbers)."
        )

    return f"""You are a procurement assistant. You will be given:
1. A JSON furniture list, where each item has "index" (its position in the original list), \
"room", "item_name", and "quantity".
2. Raw text extracted from one or more supplier price quotation PDFs, all belonging to the \
SAME pricing batch. If there are multiple documents, each is marked with a "===== Supplier \
document: <filename> =====" header so you know which text belongs to which file.

Your job:
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
number; re-check the source text in that case.
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
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    """
    Runs price matching for ONE upload bucket (ALT, P'May, or the mixed
    "OTHER"/เบิกจ่ายตามจริง batch). Returns (mapped_items, alt_batch_info) —
    alt_batch_info is only ever populated when fixed_supplier == "ALT".
    """
    system_prompt = build_price_matching_system_prompt(fixed_supplier)
    # Strip thousands-commas (e.g. "12,610" -> "12610") from the copy of the
    # text actually sent to the model — fixes the comma/decimal-point
    # misread at the source instead of only catching it afterward. The
    # ORIGINAL (comma-intact) `supplier_text` is kept for the safety-net
    # cross-check below and for the raw-text debug view.
    normalized_text = normalize_thousands_commas(supplier_text)
    user_prompt = (
        f"Furniture list (JSON, with original index):\n"
        f"{json.dumps(indexed_furniture_list, ensure_ascii=False)}\n\n"
        f"Supplier quotation text for this batch:\n{normalized_text}"
    )
    result = call_openai_json(client, system_prompt, user_prompt)
    if result is None:
        return None, None
    mapped = result.get("mapped_items")
    if not isinstance(mapped, list):
        st.error("⚠️ The AI response did not contain a valid 'mapped_items' list.")
        return None, None

    # Safety net only — with commas already stripped before the AI ever saw
    # the text, this should rarely trigger now. Still cross-checked against
    # the ORIGINAL text in case some numbers weren't caught by the
    # normalization regex (e.g. unusual spacing/OCR artifacts).
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
) -> list[dict[str, Any]]:
    """
    Merges match results from up to 3 independently-processed upload
    buckets (ALT / P'May / OTHER) back into one final mapping list, keyed
    by each item's original position (the "index" field the AI was asked
    to echo back) in `furniture_list`.

    ALT and P'May are NOT mutually exclusive: per the pricing policy, when
    an item has quotes from both custom-made suppliers, BOTH prices are
    kept side by side (alt_price / pmay_price) on the same row, and Excel's
    own `=MAX(I:K)` formula picks whichever comes out higher after markup —
    exactly matching "ใส่สูตร MAX ไว้เป็นค่าเริ่มต้นเสมอ" from the policy doc.
    This is why they're stored in separate fields rather than one shared
    "unit_price" the way OTHER/purchased items are.

    order_type is set to "สั่งผลิต" whenever EITHER alt_price or pmay_price
    is present (a custom-made quote takes priority in classification over a
    purchase quote for the same item, if both happen to exist) — otherwise
    it falls back to the OTHER bucket's purchase classification.
    """
    final_mapping: list[dict[str, Any]] = []
    for item in furniture_list:
        final_mapping.append({
            "room": item.get("room", ""),
            "item_name": item.get("item_name", ""),
            "quantity": item.get("quantity", 1),
            "unit_price": 0,       # used only for the two "จัดซื้อ" methods
            "alt_price": 0,        # ต้นทุน ALT — สั่งผลิต only
            "pmay_price": 0,       # P'May raw price — สั่งผลิต only
            "supplier": "",
            "order_type": "จัดซื้อ (ราคาจริง ไม่บวกกำไร)",
        })

    autocorrected_total = 0

    # Process OTHER first so a custom-made match (ALT/P'May) processed
    # afterward can override the order_type classification if both exist.
    ordered_buckets = sorted(bucket_results, key=lambda br: 0 if br[0] == "OTHER" else 1)

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
            else:  # OTHER
                entry["unit_price"] = price
                entry["supplier"] = row.get("supplier") or ""
                if autocorrected:
                    entry["item_name"] += " [ราคาถูกแก้ไขอัตโนมัติ - โปรดตรวจสอบ]"
                    autocorrected_total += 1

    for entry in final_mapping:
        has_any_price = entry["unit_price"] > 0 or entry["alt_price"] > 0 or entry["pmay_price"] > 0
        if not has_any_price and "[Price Not Found]" not in entry["item_name"]:
            entry["item_name"] += " [Price Not Found]"

    if autocorrected_total:
        st.warning(
            f"⚠️ พบและแก้ไขราคาที่น่าจะอ่านผิด (comma เป็นจุดทศนิยม) โดยอัตโนมัติ "
            f"{autocorrected_total} รายการ — สังเกตแท็ก '[...ถูกแก้ไขอัตโนมัติ - โปรดตรวจสอบ]' "
            f"ในตารางรีวิวด้านล่าง และตรวจสอบราคาอีกครั้งก่อน export"
        )

    return final_mapping


# --------------------------------------------------------------------------------------
# UI — Sidebar
# --------------------------------------------------------------------------------------

def render_sidebar() -> tuple[OpenAI | None, ColumnMapping]:
    st.sidebar.header("⚙️ Configuration")

    client = get_openai_client()
    if client is None:
        st.sidebar.error(
            "OPENAI_API_KEY not found in Streamlit secrets.\n\n"
            "Add it to `.streamlit/secrets.toml` locally, or under "
            "**App settings → Secrets** on Streamlit Community Cloud:\n\n"
            '`OPENAI_API_KEY = "sk-..."`'
        )
    else:
        st.sidebar.success("OpenAI API key loaded ✅")

    st.sidebar.divider()
    st.sidebar.subheader("Excel Column Mapping")
    st.sidebar.caption(
        "Matches the 'รายการเพิ่มเติม / รายการ TBC เดิม' template. "
        "Adjust only if your template's columns differ."
    )
    start_row = st.sidebar.number_input(
        "Start row (first row after headers)", min_value=1, value=5, step=1
    )
    supplier_col = st.sidebar.text_input("Supplier column", value="A", max_chars=2).strip().upper()
    room_col = st.sidebar.text_input("Room / Item-No column", value="B", max_chars=2).strip().upper()
    item_col = st.sidebar.text_input("Item Name column", value="C", max_chars=2).strip().upper()
    qty_col = st.sidebar.text_input("Quantity column", value="D", max_chars=2).strip().upper()
    custom_made_price_col = st.sidebar.text_input(
        "Price column — สั่งผลิต (custom-made)", value="G", max_chars=2
    ).strip().upper()
    purchased_price_col = st.sidebar.text_input(
        "Price column — จัดซื้อ (purchased, actual price)", value="N", max_chars=2
    ).strip().upper()

    st.sidebar.divider()
    st.sidebar.subheader("Auto-generate downstream formulas")
    generate_formulas = st.sidebar.checkbox(
        "เขียนสูตร H/I/L/M/Q/R ให้อัตโนมัติ (แนะนำให้เปิด แล้วตรวจทานหลัง export)",
        value=True,
    )
    st.sidebar.caption(
        "⚠️ สูตรคอลัมน์ L (ราคาที่เลือกใช้จริง) ใช้ `=MAX(I:K)` เป็นค่าเริ่มต้น "
        "ซึ่งในเทมเพลตต้นฉบับบางแถวอาจใช้ MIN หรือมีการปรับราคาด้วยมือ — "
        "ควรตรวจทานแถวที่เพิ่มใหม่หลัง export ทุกครั้ง"
    )
    multiplier_anchor_row = st.sidebar.number_input(
        "แถว multiplier anchor (H$row / I$row / M$row)", min_value=1, value=5, step=1
    )

    col_map = ColumnMapping(
        supplier_col=supplier_col or "A",
        room_col=room_col or "B",
        item_col=item_col or "C",
        qty_col=qty_col or "D",
        custom_made_price_col=custom_made_price_col or "G",
        purchased_price_col=purchased_price_col or "N",
        start_row=int(start_row),
        generate_formulas=generate_formulas,
        multiplier_anchor_row=int(multiplier_anchor_row),
    )
    return client, col_map


# --------------------------------------------------------------------------------------
# UI — Step 1
# --------------------------------------------------------------------------------------

def render_step1():
    st.header("Step 1 — Upload Excel BOM Template")
    uploaded_excel = st.file_uploader("Upload the Excel template (.xlsx)", type=["xlsx"], key="excel_uploader")

    if uploaded_excel is not None:
        file_bytes = uploaded_excel.read()
        wb = load_workbook_from_bytes(file_bytes)
        if wb is not None:
            st.session_state.excel_bytes = file_bytes
            st.session_state.excel_filename = uploaded_excel.name
            st.success(f"✅ Loaded '{uploaded_excel.name}' — sheets found: {', '.join(wb.sheetnames)}")
            st.session_state["_sheet_names"] = wb.sheetnames

    if st.session_state.excel_bytes:
        st.info(f"Current template on file: **{st.session_state.excel_filename}**")


# --------------------------------------------------------------------------------------
# UI — Step 2
# --------------------------------------------------------------------------------------

def render_step2(client: OpenAI | None):
    st.header("Step 2 — Upload Floor Plan / Furniture List PDF")

    if not st.session_state.excel_bytes:
        st.warning("⬆️ Please complete Step 1 first.")
        return

    uploaded_pdf = st.file_uploader("Upload furniture list PDF", type=["pdf"], key="furniture_pdf_uploader")

    if uploaded_pdf is not None and st.button("🔍 Extract Furniture List", type="primary"):
        if client is None:
            st.error("Cannot proceed: OpenAI API key is not configured.")
            return

        with st.spinner("Extracting text from PDF..."):
            pdf_text = extract_text_from_pdf(uploaded_pdf)

        if pdf_text is None:
            return

        st.session_state.step2_raw_text = pdf_text

        with st.spinner("Asking GPT-4o-mini to structure the furniture list..."):
            items = extract_furniture_list(client, pdf_text)

        if items is not None:
            st.session_state.furniture_list = items
            st.success(f"✅ Extracted {len(items)} furniture item(s).")

    if st.session_state.furniture_list:
        st.subheader("Extracted Furniture List")
        st.dataframe(st.session_state.furniture_list, use_container_width=True)


# --------------------------------------------------------------------------------------
# UI — Step 3
# --------------------------------------------------------------------------------------

def read_anchor_value(ws, col_letter: str, anchor_row: int) -> float | None:
    """
    Reads a numeric multiplier from the anchor row (e.g. H5, I5, M5). Handles
    both plain numbers (H5 = 1.21) and simple arithmetic formulas
    (I5 = '=1.05*1.07') by safely evaluating them — restricted to digits,
    '.', +, -, *, /, (, ) only, so this can never execute arbitrary code.
    Returns None if the cell is empty or not a recognizable number/formula.
    """
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


def compute_price_preview(rows: list[dict[str, Any]], col_map: ColumnMapping, sheet_name: str) -> list[dict[str, Any]]:
    """
    Replicates the Excel formula chain in pure Python so the review table can
    show REAL computed numbers (not just formula strings) before anything is
    written to the file — e.g. what 10DK Price will actually be for each row.
    Reads the live multiplier values from the uploaded template (H/I/M at the
    anchor row) so the preview matches this specific file's actual multipliers,
    not hardcoded assumptions.
    """
    wb = load_workbook_from_bytes(st.session_state.excel_bytes)
    preview: list[dict[str, Any]] = []
    if wb is None:
        return preview
    ws = wb[sheet_name] if sheet_name in wb.sheetnames else wb.active

    h_mult = read_anchor_value(ws, col_map.formula_h_col, col_map.multiplier_anchor_row)
    i_mult = read_anchor_value(ws, col_map.formula_i_col, col_map.multiplier_anchor_row)
    m_mult = read_anchor_value(ws, col_map.formula_m_col, col_map.multiplier_anchor_row)

    def roundup_thousand(x: float) -> float:
        import math
        return math.ceil(x / 1000.0) * 1000.0

    for row in rows:
        order_type = row.get("order_type", "จัดซื้อ (ราคาจริง ไม่บวกกำไร)")
        supplier = row.get("supplier", "")
        raw_unit_price = float(row.get("unit_price", 0) or 0)
        unit_price, adj_note = apply_known_price_adjustments(row.get("item_name", ""), raw_unit_price)
        qty = float(row.get("quantity", 0) or 0)
        out = {
            "Room": row.get("room", ""),
            "Item": row.get("item_name", ""),
            "Qty": qty,
            "ประเภท": order_type,
            "Supplier": supplier,
            "Unit Price": unit_price,
            "หมายเหตุอัตโนมัติ": adj_note,
        }
        if order_type == "สั่งผลิต":
            # ALT and P'May are NOT mutually exclusive — both may be present
            # on the same row, exactly matching what write_mapping_to_excel
            # will produce: MAX(I:K) picks whichever ends up higher.
            alt_price = float(row.get("alt_price", 0) or 0)
            pmay_price = float(row.get("pmay_price", 0) or 0)
            if alt_price <= 0 and pmay_price <= 0 and unit_price > 0:
                alt_price = unit_price  # back-compat fallback

            i_val = None
            if alt_price > 0:
                g = alt_price
                h = g * h_mult if h_mult is not None else None
                i_val = h * i_mult if (h is not None and i_mult is not None) else None
                out.update({"G (ต้นทุน)": g, "H (Loading)": h, "I (+5%+VAT)": i_val})

            j_val = None
            if pmay_price > 0:
                j_val = pmay_price * i_mult if i_mult is not None else None
                out.update({"J (P'May +5%+VAT)": j_val})

            candidates = [v for v in (i_val, j_val) if v is not None]
            l = max(candidates) if candidates else None  # MAX(I:K)
            m = roundup_thousand(l * m_mult) if (l is not None and m_mult is not None) else None
            out.update({"L (Chosen)": l, "M (10DK Price)": m})
        elif order_type == "จัดซื้อ (บวกกำไร 10DK)":
            k = unit_price * i_mult if i_mult is not None else None
            l = k  # MAX(I:K) with I empty -> K
            m = roundup_thousand(l * m_mult) if (l is not None and m_mult is not None) else None
            out.update({"K (+5%+VAT)": k, "L (Chosen)": l, "M (10DK Price)": m})
        else:
            out.update({"N (ราคาจริง)": unit_price})
        out["รวม (Unit x Qty)"] = (out.get("M (10DK Price)") or out.get("N (ราคาจริง)") or 0) * qty
        preview.append(out)
    return preview


def render_excel_style_preview(rows: list[dict[str, Any]], col_map: ColumnMapping, sheet_name: str) -> float:
    """
    Renders an HTML table styled to visually match the actual template
    (dark room-header bands, light-green item rows, real column headers —
    ต้นทุน ALT / ALT+ค่า protect+ค่าขน / ALT+5%+VAT7% / P'May / Other / The
    chosen price / 10DK Price / งานจัดซื้อเบิกจ่ายตามราคาจริง) using the SAME
    computed numbers from compute_price_preview(), so what you see here is
    exactly what the formulas will produce after export. Returns the grand
    total (sum of 10DK Price / actual price × quantity) for convenience.
    """
    preview_rows = compute_price_preview(rows, col_map, sheet_name)
    if not preview_rows:
        return 0.0

    def fmt_num(v, decimals=0):
        if v is None or v == "":
            return ""
        try:
            return f"{float(v):,.{decimals}f}"
        except Exception:  # noqa: BLE001
            return str(v)

    # NOTE: no leading whitespace on these lines — st.markdown() runs content
    # through a Markdown parser before rendering HTML, and any line indented
    # 4+ spaces is treated as a Markdown *code block*, which silently breaks
    # HTML rendering (this caused the raw tags to show up as literal text).
    css = (
        "<style>"
        ".bom-preview-wrap { overflow-x: auto; }"
        ".bom-preview-table { border-collapse: collapse; width: 100%; min-width: 1400px; "
        "font-family: 'Segoe UI', 'Tahoma', sans-serif; font-size: 12.5px; }"
        ".bom-preview-table th, .bom-preview-table td { border: 1px solid #999; padding: 6px 8px; text-align: right; }"
        ".bom-preview-table thead th { background: #eeeeee; text-align: center; font-weight: 700; color:#111; }"
        ".bom-preview-table td.tleft { text-align: left; }"
        ".bom-preview-table tr.room-row td { background: #1c1c1c; color: #fff; font-weight: 700; text-align: left; }"
        ".bom-preview-table tr.item-row { background: #eaf5e4; color:#111; }"
        ".bom-preview-table tr.item-row.alt2 { background: #ffffff; }"
        ".bom-preview-table td.note { color:#555; font-size: 11.5px; }"
        "</style>"
    )

    header_cols = [
        "Supplier", "#", "Furniture List", "จำนวน",
        "ต้นทุน ALT", "ALT+ค่า protect+ค่าขน", "ALT+5%<br>+VAT7%",
        "P'May<br>Overhead + vat7%", "Other +5% /<br>Overhead + vat7%",
        "The chosen price<br>(before discount, if any)", "10DK Price",
        "งานจัดซื้อ<br>เบิกจ่ายตามราคาจริง", "หมายเหตุ",
    ]

    html = [css, '<div class="bom-preview-wrap"><table class="bom-preview-table"><thead><tr>']
    for h in header_cols:
        html.append(f"<th>{h}</th>")
    html.append("</tr></thead><tbody>")

    current_room = None
    item_no = 0
    row_idx = 0
    for r in preview_rows:
        room = r.get("Room", "")
        if room != current_room:
            current_room = room
            item_no = 0
            html.append(f'<tr class="room-row"><td>Supplier</td><td colspan="12">{room}</td></tr>')
        item_no += 1
        row_idx += 1
        note = r.get("หมายเหตุอัตโนมัติ", "")
        alt_cls = " alt2" if row_idx % 2 == 0 else ""
        cells = [
            (r.get("Supplier", ""), True),
            (str(item_no), False),
            (r.get("Item", ""), True),
            (fmt_num(r.get("Qty")), False),
            (fmt_num(r.get("G (ต้นทุน)"), 2), False),
            (fmt_num(r.get("H (Loading)"), 2), False),
            (fmt_num(r.get("I (+5%+VAT)"), 2), False),
            (fmt_num(r.get("J (P'May +5%+VAT)"), 2), False),
            (fmt_num(r.get("K (+5%+VAT)"), 2), False),
            (fmt_num(r.get("L (Chosen)"), 2), False),
            (fmt_num(r.get("M (10DK Price)"), 0), False),
            (fmt_num(r.get("N (ราคาจริง)"), 2), False),
            (note, True),
        ]
        html.append(f'<tr class="item-row{alt_cls}">')
        for val, is_text in cells:
            cls = ' class="tleft"' if is_text else ""
            if val == note and is_text:
                cls = ' class="tleft note"'
            html.append(f"<td{cls}>{val}</td>")
        html.append("</tr>")

    html.append("</tbody></table></div>")
    st.markdown("".join(html), unsafe_allow_html=True)

    return sum(r.get("รวม (Unit x Qty)", 0) or 0 for r in preview_rows)


def render_step3(client: OpenAI | None, col_map: ColumnMapping):
    st.header("Step 3 — Upload Supplier Quotation & Final Mapping")

    if not st.session_state.furniture_list:
        st.warning("⬆️ Please complete Step 2 first.")
        return

    sheet_names = st.session_state.get("_sheet_names", [])
    sheet_name = st.selectbox("Target sheet in the Excel template", options=sheet_names) if sheet_names else None

    st.caption(
        "แยกอัปโหลดตามประเภทซัพพลายเออร์ 3 ช่อง — เพื่อให้ระบบกำหนด **ประเภท/สูตรราคา** ถูกต้อง "
        "100% ตามที่คุณเลือกเอง แทนที่จะให้ AI เดาว่าไฟล์ไหนเป็นของเจ้าไหน:"
    )

    col_alt, col_pmay, col_other = st.columns(3)
    with col_alt:
        st.markdown("**🏭 ALT** (สั่งผลิต)")
        alt_pdfs = st.file_uploader(
            "ใบเสนอราคา ALT", type=["pdf"], key="alt_pdf_uploader", accept_multiple_files=True
        )
    with col_pmay:
        st.markdown("**🏭 P'May** (สั่งผลิต)")
        pmay_pdfs = st.file_uploader(
            "ใบเสนอราคา P'May", type=["pdf"], key="pmay_pdf_uploader", accept_multiple_files=True
        )
    with col_other:
        st.markdown("**🛒 เบิกจ่ายตามจริง** (ร้านอื่น ๆ เช่น SB, Index, IKEA)")
        other_pdfs = st.file_uploader(
            "ใบเสนอราคาร้านอื่น ๆ", type=["pdf"], key="other_pdf_uploader", accept_multiple_files=True
        )

    has_any_upload = bool(alt_pdfs or pmay_pdfs or other_pdfs)

    if has_any_upload and st.button("🔗 Match Prices & Generate Excel", type="primary"):
        if client is None:
            st.error("Cannot proceed: OpenAI API key is not configured.")
            return
        if not sheet_name:
            st.error("No sheet selected/found in the workbook.")
            return

        # Tag each furniture item with its original list position so
        # results from each independently-processed bucket can be merged
        # back to the correct row afterward (see merge_bucket_results()).
        indexed_furniture_list = [
            {**item, "index": i} for i, item in enumerate(st.session_state.furniture_list)
        ]

        # (bucket_label, uploaded_files, fixed_supplier_for_prompt)
        buckets = [
            ("ALT", alt_pdfs, "ALT"),
            ("P'May", pmay_pdfs, "P'May"),
            ("OTHER", other_pdfs, None),
        ]

        bucket_results: list[tuple[str, list[dict[str, Any]]]] = []
        raw_texts_by_bucket: dict[str, str] = {}
        alt_batch_info = None

        for bucket_label, files, fixed_supplier in buckets:
            if not files:
                continue

            # Extract each PDF in this bucket separately, then combine with
            # clear separators so the AI knows which text came from which
            # file (still useful within the OTHER bucket, which can mix
            # several different stores).
            combined_chunks = []
            with st.spinner(f"Extracting text from {len(files)} {bucket_label} PDF(s)..."):
                for pdf_file in files:
                    text = extract_text_from_pdf(pdf_file)
                    if text:
                        combined_chunks.append(f"===== Supplier document: {pdf_file.name} =====\n{text}")

            if not combined_chunks:
                st.error(f"❌ Could not extract text from any {bucket_label} PDF(s).")
                continue

            bucket_text = "\n\n".join(combined_chunks)
            raw_texts_by_bucket[bucket_label] = bucket_text

            with st.spinner(f"Asking GPT-4o-mini to match {bucket_label} items and extract prices..."):
                mapped, alt_info = match_prices_bucket(
                    client, indexed_furniture_list, bucket_text, fixed_supplier
                )

            if mapped is None:
                continue
            bucket_results.append((bucket_label, mapped))
            if alt_info:
                alt_batch_info = alt_info

        if not bucket_results:
            st.error("❌ ไม่สามารถจับคู่ราคาได้จากไฟล์ที่อัปโหลด กรุณาตรวจสอบไฟล์อีกครั้ง")
            return

        final_mapping = merge_bucket_results(st.session_state.furniture_list, bucket_results)

        st.session_state.step3_raw_text = raw_texts_by_bucket
        st.session_state.alt_batch_info = alt_batch_info
        st.session_state.final_mapping = final_mapping
        st.success(
            f"✅ Matched against {len(bucket_results)} batch(es): "
            f"{', '.join(label for label, _ in bucket_results)}."
        )

    if st.session_state.final_mapping:
        # ALT Loading Factor auto-calculator — if the AI found an ALT quote's
        # total item costs + protection fee in the supplier PDF, offer to
        # compute the Loading Factor and apply it to the anchor cell (H<row>)
        # so all "สั่งผลิต"/ALT items downstream use the correct multiplier.
        alt_info = st.session_state.get("alt_batch_info")
        if alt_info and alt_info.get("sum_of_item_costs"):
            sum_costs = float(alt_info["sum_of_item_costs"])
            protection = float(alt_info.get("protection_fee") or 0)
            loading_factor = (sum_costs + protection) * 1.10 / sum_costs if sum_costs else None
            if loading_factor:
                st.info(
                    f"📐 พบข้อมูลใบเสนอราคา ALT: ผลรวมต้นทุนรายการ = {sum_costs:,.0f} บาท, "
                    f"ค่า Protection/ดำเนินการ = {protection:,.0f} บาท → **Loading Factor ที่คำนวณได้ = "
                    f"{loading_factor:.4f}**"
                )
                if st.button(f"✅ ใช้ค่านี้ (Apply {loading_factor:.4f} ไปที่ {col_map.formula_h_col}{col_map.multiplier_anchor_row})"):
                    wb_lf = load_workbook_from_bytes(st.session_state.excel_bytes)
                    if wb_lf is not None and sheet_name in wb_lf.sheetnames:
                        ws_lf = wb_lf[sheet_name]
                        ws_lf[f"{col_map.formula_h_col}{col_map.multiplier_anchor_row}"] = round(loading_factor, 4)
                        out_lf = io.BytesIO()
                        wb_lf.save(out_lf)
                        st.session_state.excel_bytes = out_lf.getvalue()
                        st.success(f"อัปเดต {col_map.formula_h_col}{col_map.multiplier_anchor_row} = {loading_factor:.4f} แล้ว")
                        st.rerun()

        # 12% management-fee reference (informational only — NOT written to
        # Excel, since the "first quotation" baseline is a project-history
        # value the app has no way to know on its own).
        with st.expander("💰 คำนวณค่าดำเนินการ 10DK 12% (อ้างอิงเท่านั้น ไม่เขียนลงไฟล์)"):
            baseline = st.number_input(
                "มูลค่าเฟอร์นิเจอร์ในใบเสนอราคาแรกสุด (บาท) — กรอกครั้งเดียว ใช้เป็นฐานคิดค่าธรรมเนียมจริง",
                min_value=0.0,
                value=float(st.session_state.get("baseline_furniture_value") or 0),
                step=1000.0,
            )
            st.session_state.baseline_furniture_value = baseline
            if baseline > 0:
                st.metric("ค่าดำเนินการ 12% จากมูลค่าฐาน (ใช้เรียกเก็บจริง)", f"{baseline * 0.12:,.0f} บาท")
            current_total = sum(
                max(
                    float(r.get("unit_price", 0) or 0),
                    float(r.get("alt_price", 0) or 0),
                    float(r.get("pmay_price", 0) or 0),
                ) * float(r.get("quantity", 0) or 0)
                for r in st.session_state.final_mapping
            )
            st.metric("12% จากมูลค่ารายการปัจจุบัน (เทียบเฉย ๆ ไม่เรียกเก็บ)", f"{current_total * 0.12:,.0f} บาท")

        with st.expander("🔍 ดูข้อความดิบที่ดึงจาก PDF ใบเสนอราคา (สำหรับตรวจสอบราคา)"):
            st.caption(
                "ถ้าตัวเลขราคาที่นี่ครบถ้วนถูกต้อง แต่ในตารางด้านล่างผิด "
                "แปลว่า AI ตีความเลขพลาดตอนจับคู่ — ลองรันใหม่อีกครั้ง"
            )
            raw_texts = st.session_state.step3_raw_text
            if isinstance(raw_texts, dict) and raw_texts:
                for bucket_label, text in raw_texts.items():
                    st.markdown(f"**{bucket_label}**")
                    st.text(text)
            else:
                st.text("(ไม่มีข้อมูล)")

        st.subheader("📝 ตรวจสอบและแก้ไขก่อนบันทึก")

        # Auto-flag suspiciously low prices (< 100 บาท) — furniture is
        # essentially never priced under this. This catches cases the AI's
        # own self-check / the comma-autocorrect above might still miss,
        # by surfacing them visibly in the review table rather than
        # trusting the number silently.
        SUSPICIOUS_PRICE_THRESHOLD = 100
        suspicious_items = []
        for item in st.session_state.final_mapping:
            prices = [
                float(item.get("unit_price", 0) or 0),
                float(item.get("alt_price", 0) or 0),
                float(item.get("pmay_price", 0) or 0),
            ]
            is_suspicious = any(0 < p < SUSPICIOUS_PRICE_THRESHOLD for p in prices)
            item["ราคาน่าสงสัย"] = "⚠️ ต่ำผิดปกติ" if is_suspicious else ""
            if is_suspicious:
                suspicious_items.append(item.get("item_name", ""))
        if suspicious_items:
            st.warning(
                f"⚠️ พบ {len(suspicious_items)} รายการที่ราคาต่ำผิดปกติ (ต่ำกว่า {SUSPICIOUS_PRICE_THRESHOLD} บาท) "
                f"ซึ่งมักเกิดจาก AI อ่านตัวเลขที่มีจุลภาคคั่นหลักพันผิด (เช่น '6,100' กลายเป็น '61'): "
                + ", ".join(f"**{n}**" for n in suspicious_items)
                + " — กรุณาเปิด '🔍 ดูข้อความดิบที่ดึงจาก PDF' ด้านบนเพื่อตรวจราคาที่ถูกต้อง แล้วแก้ในตารางด้านล่างเอง"
            )

        st.caption(
            "เลือก **ประเภท** ให้ถูกต้องต่อรายการ ตาม 3 วิธีคิดราคา:\n\n"
            "- **สั่งผลิต** — กรอกราคาที่ช่อง **ALT Price** และ/หรือ **P'May Price** (กรอกได้ทั้งคู่พร้อมกัน "
            "ถ้ามีใบเสนอราคาจากทั้ง 2 เจ้า — สูตร MAX จะเลือกเจ้าที่แพงกว่าให้อัตโนมัติตามนโยบาย)\n"
            "- **จัดซื้อ (บวกกำไร 10DK)** — ใช้ **Unit Price** เป็น**ราคาเต็มก่อนหักส่วนลด** (ไม่ใช่ราคาหลังลด) → บวก overhead + VAT + กำไร 45% เหมือนสั่งผลิต\n"
            "- **จัดซื้อ (ราคาจริง ไม่บวกกำไร)** — ใช้ **Unit Price** — ร้านดัง (SB/Index/IKEA ฯลฯ) ใส่ราคาตามใบเสนอราคาตรง ๆ ไม่บวกอะไรเพิ่ม\n\n"
            "🛠️ แถวที่มีแท็ก **[...ถูกแก้ไขอัตโนมัติ - โปรดตรวจสอบ]** ในชื่อรายการ คือแถวที่ระบบตรวจพบและแก้ไข "
            "รูปแบบ 'comma ถูกอ่านเป็นจุดทศนิยม' ให้อัตโนมัติแล้ว — ควรตรวจสอบราคานั้นอีกครั้งก่อน export"
        )

        edited_df = st.data_editor(
            st.session_state.final_mapping,
            use_container_width=True,
            num_rows="dynamic",
            column_config={
                "room": st.column_config.TextColumn("Room / ห้อง"),
                "item_name": st.column_config.TextColumn("Item Name / รายการ", width="large"),
                "quantity": st.column_config.NumberColumn("Quantity / จำนวน", min_value=0, step=1),
                "order_type": st.column_config.SelectboxColumn(
                    "ประเภท",
                    options=[
                        "สั่งผลิต",
                        "จัดซื้อ (บวกกำไร 10DK)",
                        "จัดซื้อ (ราคาจริง ไม่บวกกำไร)",
                    ],
                    required=True,
                    width="medium",
                ),
                "alt_price": st.column_config.NumberColumn(
                    "ALT Price (สั่งผลิต)", min_value=0, step=1, help="ต้นทุนจาก ALT — ใส่ได้พร้อมกับ P'May Price"
                ),
                "pmay_price": st.column_config.NumberColumn(
                    "P'May Price (สั่งผลิต)", min_value=0, step=1, help="ราคาจาก P'May — ใส่ได้พร้อมกับ ALT Price"
                ),
                "unit_price": st.column_config.NumberColumn(
                    "Unit Price (จัดซื้อเท่านั้น)", min_value=0, step=1
                ),
                "supplier": st.column_config.TextColumn("Supplier"),
                "ราคาน่าสงสัย": st.column_config.TextColumn("⚠️ เช็คราคา", width="small", disabled=True),
            },
            key="mapping_editor",
        )

        not_found = [
            i for i in edited_df if "[Price Not Found]" in str(i.get("item_name", ""))
        ] if isinstance(edited_df, list) else []
        if not_found:
            st.warning(f"⚠️ {len(not_found)} item(s) had no matching price in the supplier quotation.")

        # Full computed-price preview — styled to visually match the actual
        # Excel template (room bands, real column headers) using the SAME
        # numbers the formulas will produce after export.
        final_rows_for_preview = edited_df if isinstance(edited_df, list) else list(edited_df)
        if sheet_name and final_rows_for_preview:
            st.markdown("**👀 Preview ตารางเต็ม (จำลองหน้าตา Excel จริง พร้อมสูตรที่จะผูกให้)**")
            grand_total = render_excel_style_preview(final_rows_for_preview, col_map, sheet_name)
            st.caption(f"รวมทั้งหมด (10DK Price / ราคาจริง × จำนวน): **{grand_total:,.0f} บาท**")

        if st.button("✅ ยืนยันและสร้างไฟล์ Excel", type="primary"):
            if not sheet_name:
                st.error("No sheet selected/found in the workbook.")
            else:
                # st.data_editor returns a list[dict] when given list input
                final_rows = edited_df if isinstance(edited_df, list) else list(edited_df)
                with st.spinner("Writing data into your Excel template (formulas & formatting preserved)..."):
                    result_bytes = write_mapping_to_excel(
                        st.session_state.excel_bytes, sheet_name, final_rows, col_map
                    )
                if result_bytes is not None:
                    st.session_state["_final_excel_bytes"] = result_bytes
                    st.success("✅ สร้างไฟล์ Excel เรียบร้อยแล้ว — ดาวน์โหลดได้ด้านล่าง")

    if st.session_state.get("_final_excel_bytes"):
        st.download_button(
            label="⬇️ Download Completed Excel File",
            data=st.session_state["_final_excel_bytes"],
            file_name=f"completed_{st.session_state.excel_filename or 'BOM.xlsx'}",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------

def main():
    st.title("🛋️ Automated Furniture BOM & Price Mapping")
    st.caption(
        "Upload an Excel BOM template, a floor-plan furniture list PDF, and a supplier "
        "quotation PDF — this app extracts, semantically matches, and fills in prices "
        "automatically, without touching your existing formulas or formatting."
    )

    client, col_map = render_sidebar()

    tab1, tab2, tab3 = st.tabs(["1️⃣ Excel Template", "2️⃣ Furniture List", "3️⃣ Supplier Mapping"])
    with tab1:
        render_step1()
    with tab2:
        render_step2(client)
    with tab3:
        render_step3(client, col_map)

    st.divider()
    if st.button("🔄 Reset All / Start Over"):
        for key in list(st.session_state.keys()):
            del st.session_state[key]
        st.rerun()


if __name__ == "__main__":
    main()