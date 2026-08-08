import type {
  ColumnMapping,
  ExportResponse,
  ExportStatus,
  ExportVersionMeta,
  FurnitureExtractResponse,
  FurnitureItem,
  LoadingFactorResponse,
  MappingRow,
  MatchPricesResponse,
  MultiplierColumn,
  PreviewResponse,
  ProjectState,
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

export const api = {
  health: () => request<{ status: string; openai_configured: boolean }>("/health"),

  listProjects: () => request<ProjectSummary[]>("/projects"),

  createProject: (name: string) => request<ProjectSummary>("/projects", json({ name })),

  getProject: (id: string) => request<ProjectState>(`/projects/${id}`),

  deleteProject: (id: string) => request<void>(`/projects/${id}`, { method: "DELETE" }),

  resetProject: (id: string) => request<ProjectState>(`/projects/${id}/reset`, { method: "POST" }),

  // ---------------------------------------------------------------- step 1 --

  uploadTemplate: (id: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<TemplateUploadResponse>(`/projects/${id}/template`, { method: "PUT", body: fd });
  },

  setTargetSheet: (id: string, sheetName: string) =>
    request<{ target_sheet_name: string }>(
      `/projects/${id}/target-sheet?sheet_name=${encodeURIComponent(sheetName)}`,
      { method: "PUT" }
    ),

  // ---------------------------------------------------------------- step 2 --

  extractFurnitureList: (id: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<FurnitureExtractResponse>(`/projects/${id}/furniture-list`, { method: "POST", body: fd });
  },

  updateFurnitureList: (id: string, items: FurnitureItem[]) =>
    request<FurnitureItem[]>(`/projects/${id}/furniture-list`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }),
    }),

  groupFurnitureByRoom: (id: string) =>
    request<FurnitureItem[]>(`/projects/${id}/furniture-list/group-by-room`, { method: "POST" }),

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
    return request<MatchPricesResponse>(`/projects/${id}/quotations`, { method: "POST", body: fd });
  },

  updateMappingRows: (id: string, rows: MappingRow[]) =>
    request<MappingRow[]>(`/projects/${id}/mapping`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rows }),
    }),

  getQuotationTexts: (id: string) => request<{ texts: Record<string, string> }>(`/projects/${id}/quotations/texts`),

  getQuotationPdfs: (id: string) => request<QuotationBucketMeta[]>(`/projects/${id}/quotations/pdfs`),

  quotationPdfUrl: (id: string, pdfId: number) => `${BASE}/projects/${id}/quotations/pdfs/${pdfId}`,

  searchQuotationText: (id: string, query: string) =>
    request<RawTextSearchResult[]>(`/projects/${id}/quotations/search?query=${encodeURIComponent(query)}`, {
      method: "POST",
    }),

  preview: (id: string, sheetName: string, rows: MappingRow[], columnMapping: ColumnMapping) =>
    request<PreviewResponse>(
      `/projects/${id}/preview`,
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
      `/projects/${id}/loading-factor`,
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
    request<SetMultiplierResponse>(`/projects/${id}/multiplier`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sheet_name: sheetName, column, value, column_mapping: columnMapping }),
    }),

  updateBaseline: (id: string, value: number | null) =>
    request<{ baseline_furniture_value: number | null }>(`/projects/${id}/baseline`, {
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
      `/projects/${id}/export`,
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
      `/projects/${id}/export/status`,
      json({ rows })
    ),

  exportFileUrl: (id: string) => `${BASE}/projects/${id}/export/file`,

  listExportVersions: (id: string) => request<ExportVersionMeta[]>(`/projects/${id}/exports`),

  exportVersionFileUrl: (id: string, versionId: number) => `${BASE}/projects/${id}/exports/${versionId}/file`,

  // --------------------------------------------------------- quotation doc --

  previewQuotationFromProject: (id: string, sheetName: string, rows: MappingRow[], columnMapping: ColumnMapping) =>
    request<QuotationPreview>(
      `/projects/${id}/quotation-doc/preview`,
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
};
