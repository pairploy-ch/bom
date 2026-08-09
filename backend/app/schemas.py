"""Pydantic request/response models for the API."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

OrderType = Literal["สั่งผลิต", "จัดซื้อ (บวกกำไร 10DK)", "จัดซื้อ (ราคาจริง ไม่บวกกำไร)"]


# ------------------------------------------------------------------ shared --

class ColumnMappingIn(BaseModel):
    """Mirrors logic.ColumnMapping — sent by the client per-request (never
    persisted server-side, matching the original app's own sidebar-only,
    session-scoped behavior)."""
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


class FurnitureItem(BaseModel):
    room: str = ""
    item_name: str = ""
    quantity: float = 1
    # True = keep this item going into Step 3's price matching; False = excluded
    # there entirely. Defaults True so a freshly-extracted/added item counts
    # unless the user explicitly unchecks it.
    verified: bool = True
    # Free-text size/material/dimensions, e.g. "180x200cm, ไม้วีเนียร์" — either
    # AI-extracted from the PDF or typed in manually. Optional.
    spec: str = ""


class MappingRow(BaseModel):
    room: str = ""
    item_name: str = ""
    quantity: float = 1
    unit_price: float = 0
    alt_price: float = 0
    pmay_price: float = 0
    other_maker_price: float = 0
    supplier: str = ""
    order_type: OrderType = "จัดซื้อ (ราคาจริง ไม่บวกกำไร)"
    spec: str = ""
    # Response-only — recomputed server-side on every read (SUSPICIOUS_PRICE_THRESHOLD
    # rule from the original app), ignored if sent by the client.
    suspicious: bool = False


# ---------------------------------------------------------------- projects --
# Top-level grouping (e.g. "10DK") — holds many houses, see HouseState below.

class ProjectSummary(BaseModel):
    id: str
    name: str
    updated_at: str
    has_logo: bool = False


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1)


# ------------------------------------------------------------------ houses --
# One house = one full BOM (template → furniture list → price matching →
# quotation) — what this API used to call a "project" before the grouping
# above existed.

class HouseSummary(BaseModel):
    id: str
    name: str
    updated_at: str


class HouseCreate(BaseModel):
    project_id: str
    name: str = Field(min_length=1)


class HouseRename(BaseModel):
    name: str = Field(min_length=1)


class HouseState(BaseModel):
    id: str
    project_id: str
    name: str
    excel_filename: str | None = None
    has_template: bool
    sheet_names: list[str] = []
    target_sheet_name: str | None = None
    furniture_list: list[FurnitureItem] = []
    mapping_rows: list[MappingRow] = []
    alt_batch_info: dict[str, Any] | None = None
    baseline_furniture_value: float | None = None
    quotation_buckets: list[str] = []
    has_final_export: bool = False
    # User-set checklist toggles for the sidebar's workflow menu (คำนวณราคา /
    # ใบราคา) — manual, not derived from any other field. See WorkflowStatusUpdate.
    workflow_calc_done: bool = False
    workflow_quotation_done: bool = False
    updated_at: str


class WorkflowStatusUpdate(BaseModel):
    step: Literal["calc", "quotation"]
    done: bool


# ----------------------------------------------------------------- step 1 --

class TemplateUploadResponse(BaseModel):
    excel_filename: str
    sheet_names: list[str]


# ----------------------------------------------------------------- step 2 --

class FurnitureExtractResponse(BaseModel):
    items: list[FurnitureItem]
    warning: str | None = None


class FurnitureListReplace(BaseModel):
    items: list[FurnitureItem]


# ----------------------------------------------------------------- step 3 --

class MappingRowsReplace(BaseModel):
    rows: list[MappingRow]


class QuotationBucketMeta(BaseModel):
    id: int
    bucket_label: str
    filename: str


class MatchPricesResponse(BaseModel):
    mapping_rows: list[MappingRow]
    matched_buckets: list[str]
    alt_batch_info: dict[str, Any] | None = None
    warnings: list[str] = []
    pdf_files: list[QuotationBucketMeta] = []


class PreviewRequest(BaseModel):
    sheet_name: str
    rows: list[MappingRow]
    column_mapping: ColumnMappingIn = ColumnMappingIn()


class LoadingFactorRequest(BaseModel):
    sheet_name: str
    sum_of_item_costs: float
    protection_fee: float = 0
    management_fee: float = 0
    column_mapping: ColumnMappingIn = ColumnMappingIn()


class LoadingFactorResponse(BaseModel):
    loading_factor: float
    pct_increase: float
    anchor_cell: str
    updated: bool
    current_value: float | None = None


class BaselineUpdate(BaseModel):
    value: float | None = None


class SetMultiplierRequest(BaseModel):
    """
    Directly writes a number into the I or M anchor cell (the "+5%+VAT7%"
    multiplier and the 10DK profit multiplier respectively) — these are
    fixed business constants that must already live in the uploaded Excel
    template, unlike H (the Loading Factor), which is computed per-project
    from the ALT quotation's own numbers.
    """
    sheet_name: str
    column: Literal["i", "m"]
    value: float
    column_mapping: ColumnMappingIn = ColumnMappingIn()


class SetMultiplierResponse(BaseModel):
    anchor_cell: str
    updated: bool
    current_value: float | None = None


class ExportRequest(BaseModel):
    sheet_name: str
    rows: list[MappingRow]
    column_mapping: ColumnMappingIn = ColumnMappingIn()
    furniture_total: float = 0
    baseline_furniture_value: float | None = None


class ExportStatus(BaseModel):
    """Mirrors the original's stale-download check (_current_export_source_hash)."""
    has_export: bool
    is_stale: bool
    download_url: str | None = None


class RawTextSearchResult(BaseModel):
    bucket_label: str
    matches: list[str]


class QuotationTextsResponse(BaseModel):
    texts: dict[str, str]


class ExportVersionMeta(BaseModel):
    id: int
    filename: str
    created_at: str


# ------------------------------------------------------- client quotation --

class QuotationRow(BaseModel):
    item_no: int
    floor: str = ""
    room: str = ""
    item_name: str = ""
    quantity: float = 1
    dk_work_price: float | None = None
    actual_price_purchase: float | None = None
    # Customer-supplied item — never priced, rendered as a "Client's" row
    # (gray band, no item number, blank price cells) per the reference
    # template. Never set by the pricing pipeline; only by manual edit.
    is_client_owned: bool = False
    remark: str = ""
    # Server-computed display label ("1", "2", ... for rows priced under
    # 10DK's work; "A", "B", ... for rows priced under the purchase column;
    # "Client's" for customer-owned rows) — always recomputed from the
    # current row list right before use, so any value sent by the client is
    # ignored on the way back in.
    label: str = ""


class QuotationBuildRequest(BaseModel):
    sheet_name: str
    rows: list[MappingRow]
    column_mapping: ColumnMappingIn = ColumnMappingIn()


class QuotationPreview(BaseModel):
    rows: list[QuotationRow]
    warnings: list[str] = []
    dk_work_subtotal: float
    purchase_subtotal: float
    vat: float
    grand_total: float


class QuotationPdfRequest(BaseModel):
    client_name: str = ""
    project_name: str = ""
    quotation_date: str = ""
    rows: list[QuotationRow]
    # Deducted from the 10DK's-work grand total only (e.g. an already-paid
    # design deposit) — matches the reference template's "หักค่ามัดจำออกแบบ" row.
    deposit_deduction: float = 0
    # Free-text bullet lines rendered under a "Remarks:" heading at the very
    # end of the document, one per line (e.g. "ราคาดังกล่าว ไม่รวมฟูกที่นอน").
    remarks: str = ""
    # Small italic note shown next to the Grand Total row.
    grand_total_note: str = "(ไม่รวมรายการ TBC ค่าขนส่ง, ค่าประกอบและค่าติดตั้ง)"
