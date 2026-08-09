import type {
  ColumnMapping,
  ExportResponse,
  ExportStatus,
  ExportVersionMeta,
  FurnitureExtractResponse,
  FurnitureItem,
  HouseState,
  HouseSummary,
  LoadingFactorResponse,
  MappingRow,
  MatchPricesResponse,
  MultiplierColumn,
  PreviewResponse,
  ProjectSummary,
  QuotationBucketMeta,
  QuotationPreview,
  QuotationRow,
  RawTextSearchResult,
  SetMultiplierResponse,
  TemplateUploadResponse,
} from "./types";

const BASE = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8010/api";

export class ApiError extends Error {}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, init);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      // response wasn't JSON — keep statusText
    }
    throw new ApiError(detail || `Request failed (${res.status})`);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

function json(body: unknown): RequestInit {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

async function requestBlob(path: string, init: RequestInit): Promise<Blob> {
  const res = await fetch(`${BASE}${path}`, init);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      // response wasn't JSON — keep statusText
    }
    throw new ApiError(detail || `Request failed (${res.status})`);
  }
  return res.blob();
}

// -------------------------------------------------------------- projects --
// Top-level grouping (e.g. "10DK") — holds many houses (see below).

export const api = {
  health: () => request<{ status: string; openai_configured: boolean }>("/health"),

  listProjects: () => request<ProjectSummary[]>("/projects"),

  createProject: (name: string) => request<ProjectSummary>("/projects", json({ name })),

  getProjectSummary: (id: string) => request<ProjectSummary>(`/projects/${id}`),

  deleteProject: (id: string) => request<void>(`/projects/${id}`, { method: "DELETE" }),

  uploadProjectLogo: (id: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<{ updated: boolean }>(`/projects/${id}/logo`, { method: "PUT", body: fd });
  },

  // Cache-busted so a freshly-uploaded logo shows up immediately instead of
  // the browser reusing a cached image at the same URL.
  projectLogoUrl: (id: string) => `${BASE}/projects/${id}/logo?t=${Date.now()}`,

  // ---------------------------------------------------------------- houses --
  // One house = one full BOM (template -> furniture list -> price matching ->
  // quotation) — what this app used to call a "project" before the grouping
  // above existed.

  listHouses: (projectId: string) => request<HouseSummary[]>(`/houses?project_id=${projectId}`),

  createHouse: (projectId: string, name: string) =>
    request<HouseSummary>("/houses", json({ project_id: projectId, name })),

  getHouse: (id: string) => request<HouseState>(`/houses/${id}`),

  deleteHouse: (id: string) => request<void>(`/houses/${id}`, { method: "DELETE" }),

  resetHouse: (id: string) => request<HouseState>(`/houses/${id}/reset`, { method: "POST" }),

  // ---------------------------------------------------------------- step 1 --

  uploadTemplate: (id: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<TemplateUploadResponse>(`/houses/${id}/template`, { method: "PUT", body: fd });
  },

  setTargetSheet: (id: string, sheetName: string) =>
    request<{ target_sheet_name: string }>(
      `/houses/${id}/target-sheet?sheet_name=${encodeURIComponent(sheetName)}`,
      { method: "PUT" }
    ),

  // ---------------------------------------------------------------- step 2 --

  extractFurnitureList: (id: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<FurnitureExtractResponse>(`/houses/${id}/furniture-list`, { method: "POST", body: fd });
  },

  updateFurnitureList: (id: string, items: FurnitureItem[]) =>
    request<FurnitureItem[]>(`/houses/${id}/furniture-list`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }),
    }),

  groupFurnitureByRoom: (id: string) =>
    request<FurnitureItem[]>(`/houses/${id}/furniture-list/group-by-room`, { method: "POST" }),

  // ---------------------------------------------------------------- step 3 --

  matchPrices: (
    id: string,
    sheetName: string,
    files: { alt: File[]; pmay: File[]; other: File[]; purchase: File[] }
  ) => {
    const fd = new FormData();
    fd.append("sheet_name", sheetName);
    files.alt.forEach((f) => fd.append("alt_pdfs", f));
    files.pmay.forEach((f) => fd.append("pmay_pdfs", f));
    files.other.forEach((f) => fd.append("othermaker_pdfs", f));
    files.purchase.forEach((f) => fd.append("purchase_pdfs", f));
    return request<MatchPricesResponse>(`/houses/${id}/quotations`, { method: "POST", body: fd });
  },

  updateMappingRows: (id: string, rows: MappingRow[]) =>
    request<MappingRow[]>(`/houses/${id}/mapping`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rows }),
    }),

  getQuotationTexts: (id: string) => request<{ texts: Record<string, string> }>(`/houses/${id}/quotations/texts`),

  getQuotationPdfs: (id: string) => request<QuotationBucketMeta[]>(`/houses/${id}/quotations/pdfs`),

  quotationPdfUrl: (id: string, pdfId: number) => `${BASE}/houses/${id}/quotations/pdfs/${pdfId}`,

  searchQuotationText: (id: string, query: string) =>
    request<RawTextSearchResult[]>(`/houses/${id}/quotations/search?query=${encodeURIComponent(query)}`, {
      method: "POST",
    }),

  preview: (id: string, sheetName: string, rows: MappingRow[], columnMapping: ColumnMapping) =>
    request<PreviewResponse>(
      `/houses/${id}/preview`,
      json({ sheet_name: sheetName, rows, column_mapping: columnMapping })
    ),

  applyLoadingFactor: (
    id: string,
    sheetName: string,
    sumOfItemCosts: number,
    protectionFee: number,
    managementFee: number,
    columnMapping: ColumnMapping
  ) =>
    request<LoadingFactorResponse>(
      `/houses/${id}/loading-factor`,
      json({
        sheet_name: sheetName,
        sum_of_item_costs: sumOfItemCosts,
        protection_fee: protectionFee,
        management_fee: managementFee,
        column_mapping: columnMapping,
      })
    ),

  setMultiplier: (
    id: string,
    sheetName: string,
    column: MultiplierColumn,
    value: number,
    columnMapping: ColumnMapping
  ) =>
    request<SetMultiplierResponse>(`/houses/${id}/multiplier`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sheet_name: sheetName, column, value, column_mapping: columnMapping }),
    }),

  updateBaseline: (id: string, value: number | null) =>
    request<{ baseline_furniture_value: number | null }>(`/houses/${id}/baseline`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    }),

  exportExcel: (
    id: string,
    sheetName: string,
    rows: MappingRow[],
    columnMapping: ColumnMapping,
    furnitureTotal: number,
    baselineFurnitureValue: number | null
  ) =>
    request<ExportResponse>(
      `/houses/${id}/export`,
      json({
        sheet_name: sheetName,
        rows,
        column_mapping: columnMapping,
        furniture_total: furnitureTotal,
        baseline_furniture_value: baselineFurnitureValue,
      })
    ),

  exportStatus: (id: string, rows: MappingRow[]) =>
    request<ExportStatus>(
      `/houses/${id}/export/status`,
      json({ rows })
    ),

  exportFileUrl: (id: string) => `${BASE}/houses/${id}/export/file`,

  listExportVersions: (id: string) => request<ExportVersionMeta[]>(`/houses/${id}/exports`),

  exportVersionFileUrl: (id: string, versionId: number) => `${BASE}/houses/${id}/exports/${versionId}/file`,

  // --------------------------------------------------------- quotation doc --

  previewQuotationFromHouse: (id: string, sheetName: string, rows: MappingRow[], columnMapping: ColumnMapping) =>
    request<QuotationPreview>(
      `/houses/${id}/quotation-doc/preview`,
      json({ sheet_name: sheetName, rows, column_mapping: columnMapping })
    ),

  inspectQuotationExcel: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<{ sheet_names: string[] }>("/quotation-doc/inspect-excel", { method: "POST", body: fd });
  },

  previewQuotationFromExcel: (file: File, sheetName: string) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("sheet_name", sheetName);
    return request<QuotationPreview>("/quotation-doc/preview-from-excel", { method: "POST", body: fd });
  },

  downloadQuotationPdf: (payload: {
    client_name: string;
    project_name: string;
    quotation_date: string;
    rows: QuotationRow[];
    deposit_deduction: number;
    remarks: string;
    grand_total_note: string;
  }) => requestBlob("/quotation-doc/pdf", json(payload)),

  downloadQuotationDocx: (payload: {
    client_name: string;
    project_name: string;
    quotation_date: string;
    rows: QuotationRow[];
    deposit_deduction: number;
    remarks: string;
    grand_total_note: string;
  }) => requestBlob("/quotation-doc/docx", json(payload)),

  uploadCompanyLogo: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<{ updated: boolean }>("/quotation-doc/logo", { method: "PUT", body: fd });
  },

  // Cache-busted so a freshly-uploaded logo shows up immediately instead of
  // the browser reusing a cached image at the same URL.
  companyLogoUrl: () => `${BASE}/quotation-doc/logo?t=${Date.now()}`,

  // ------------------------------------------------------------- profile --

  uploadUserAvatar: (userId: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<{ updated: boolean }>(`/profile/${userId}/avatar`, { method: "PUT", body: fd });
  },

  userAvatarUrl: (userId: string) => `${BASE}/profile/${userId}/avatar?t=${Date.now()}`,
};
