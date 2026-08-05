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


class FurnitureItem(BaseModel):
    room: str = ""
    item_name: str = ""
    quantity: float = 1
    verified: bool = False


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
    # Response-only — recomputed server-side on every read (SUSPICIOUS_PRICE_THRESHOLD
    # rule from the original app), ignored if sent by the client.
    suspicious: bool = False


# ---------------------------------------------------------------- projects --

class ProjectSummary(BaseModel):
    id: str
    name: str
    updated_at: str


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1)


class ProjectState(BaseModel):
    id: str
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
    updated_at: str


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
