// Mirrors backend/app/schemas.py — kept in one file since the two can't
// share types across the Python/TypeScript boundary directly.

export type OrderType =
  | "สั่งผลิต"
  | "จัดซื้อ (บวกกำไร 10DK)"
  | "จัดซื้อ (ราคาจริง ไม่บวกกำไร)";

export const ORDER_TYPES: OrderType[] = [
  "สั่งผลิต",
  "จัดซื้อ (บวกกำไร 10DK)",
  "จัดซื้อ (ราคาจริง ไม่บวกกำไร)",
];

export interface ColumnMapping {
  supplier_col: string;
  room_col: string;
  item_col: string;
  qty_col: string;
  custom_made_price_col: string;
  purchased_price_col: string;
  start_row: number;
  generate_formulas: boolean;
  multiplier_anchor_row: number;
  formula_h_col: string;
  formula_i_col: string;
  formula_k_col: string;
  formula_j_col: string;
  formula_l_col: string;
  formula_m_col: string;
  formula_additional_item_col: string;
  formula_purchase_compare_col: string;
}

export const DEFAULT_COLUMN_MAPPING: ColumnMapping = {
  supplier_col: "A",
  room_col: "B",
  item_col: "C",
  qty_col: "D",
  custom_made_price_col: "E",
  purchased_price_col: "L",
  start_row: 5,
  generate_formulas: true,
  multiplier_anchor_row: 5,
  formula_h_col: "F",
  formula_i_col: "G",
  formula_k_col: "I",
  formula_j_col: "H",
  formula_l_col: "J",
  formula_m_col: "K",
  formula_additional_item_col: "M",
  formula_purchase_compare_col: "N",
};

export interface FurnitureItem {
  room: string;
  item_name: string;
  quantity: number;
  // Checked = kept going into Step 3's price matching; unchecked = cut out there.
  verified: boolean;
  // Free-text size/material, e.g. "180x200cm, ไม้วีเนียร์" — optional.
  spec: string;
}

export interface MappingRow {
  room: string;
  item_name: string;
  quantity: number;
  unit_price: number;
  alt_price: number;
  pmay_price: number;
  other_maker_price: number;
  supplier: string;
  order_type: OrderType;
  spec: string;
  suspicious?: boolean;
}

// Top-level grouping (e.g. "10DK") — holds many houses, see HouseState below.
export interface ProjectSummary {
  id: string;
  name: string;
  updated_at: string;
  has_logo: boolean;
}

// One house = one full BOM (template -> furniture list -> price matching ->
// quotation) — what this app used to call a "project" before the grouping
// above existed.
export interface HouseSummary {
  id: string;
  name: string;
  updated_at: string;
}

export interface HouseState {
  id: string;
  project_id: string;
  name: string;
  excel_filename: string | null;
  has_template: boolean;
  sheet_names: string[];
  target_sheet_name: string | null;
  furniture_list: FurnitureItem[];
  mapping_rows: MappingRow[];
  alt_batch_info: AltBatchInfo | null;
  baseline_furniture_value: number | null;
  quotation_buckets: string[];
  has_final_export: boolean;
  // User-set checklist toggles for the sidebar's workflow menu — manual, not
  // derived from any other field (the quotation page never persists a
  // "generated" state, it's recomputed fresh from mapping_rows every visit).
  workflow_calc_done: boolean;
  workflow_quotation_done: boolean;
  updated_at: string;
}

export type WorkflowStep = "calc" | "quotation";

export interface AltBatchInfo {
  sum_of_item_costs?: number;
  protection_fee?: number;
  management_fee?: number;
}

export interface TemplateUploadResponse {
  excel_filename: string;
  sheet_names: string[];
}

export interface FurnitureExtractResponse {
  items: FurnitureItem[];
  warning: string | null;
}

export interface QuotationBucketMeta {
  id: number;
  bucket_label: string;
  filename: string;
}

export interface MatchPricesResponse {
  mapping_rows: MappingRow[];
  matched_buckets: string[];
  alt_batch_info: AltBatchInfo | null;
  warnings: string[];
  pdf_files: QuotationBucketMeta[];
}

// One row from POST /preview — fields present depend on order_type, mirroring
// the original's Excel-style preview table exactly (same column set).
export interface PreviewRow {
  room: string;
  item_name: string;
  quantity: number;
  order_type: OrderType;
  supplier: string;
  unit_price: number;
  auto_note: string;
  g_cost?: number;
  h_loading?: number;
  i_plus_vat?: number;
  j_pmay?: number;
  k_other?: number;
  l_chosen?: number;
  m_10dk_price?: number;
  n_actual_price?: number;
  line_total: number;
}

export interface PreviewResponse {
  rows: PreviewRow[];
  warnings: string[];
}

export interface LoadingFactorResponse {
  loading_factor: number;
  pct_increase: number;
  anchor_cell: string;
  updated: boolean;
  current_value: number | null;
}

export type MultiplierColumn = "i" | "m";

export interface SetMultiplierResponse {
  anchor_cell: string;
  updated: boolean;
  current_value: number | null;
}

export interface ExportResponse {
  warnings: string[];
  download_url: string;
}

export interface ExportStatus {
  has_export: boolean;
  is_stale: boolean;
  download_url: string | null;
}

export interface RawTextSearchResult {
  bucket_label: string;
  matches: string[];
}

export interface ExportVersionMeta {
  id: number;
  filename: string;
  created_at: string;
}

// ------------------------------------------------------- client quotation --

export interface QuotationRow {
  item_no: number;
  floor: string;
  room: string;
  item_name: string;
  quantity: number;
  dk_work_price: number | null;
  actual_price_purchase: number | null;
  is_client_owned: boolean;
  remark: string;
  // Server-computed display label ("1"/"2"/... under 10DK's work, "A"/"B"/...
  // under the purchase column, "Client's" for customer-owned rows) — also
  // recomputed live on the frontend as rows are edited, see assignQuotationLabels.
  label: string;
}

export interface QuotationPreview {
  rows: QuotationRow[];
  warnings: string[];
  dk_work_subtotal: number;
  purchase_subtotal: number;
  vat: number;
  grand_total: number;
}

export const BUCKET_LABELS = ["ALT", "P'May", "OTHER_MAKER", "PURCHASE"] as const;
export type BucketLabel = (typeof BUCKET_LABELS)[number];

export const BUCKET_TITLES: Record<BucketLabel, { title: string; hint: string }> = {
  ALT: { title: "🏭 ALT (สั่งผลิต)", hint: "" },
  "P'May": { title: "🏭 P'May (สั่งผลิต)", hint: "" },
  OTHER_MAKER: { title: "🏭 Other (สั่งผลิต — เจ้าที่ 3)", hint: "แข่งราคากับ ALT/P'May เข้า MAX เดียวกัน" },
  PURCHASE: { title: "🛒 เบิกจ่ายตามจริง (จัดซื้อ)", hint: "ร้านทั่วไป เช่น SB, Index, IKEA" },
};
