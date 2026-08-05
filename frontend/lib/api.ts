import type {
  ColumnMapping,
  ExportResponse,
  ExportStatus,
  FurnitureExtractResponse,
  FurnitureItem,
  LoadingFactorResponse,
  MappingRow,
  MatchPricesResponse,
  PreviewRow,
  ProjectState,
  ProjectSummary,
  QuotationBucketMeta,
  RawTextSearchResult,
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

// -------------------------------------------------------------- projects --

export const api = {
  health: () => request<{ status: string; openai_configured: boolean }>("/health"),

  listProjects: () => request<ProjectSummary[]>("/projects"),

  createProject: (name: string) => request<ProjectSummary>("/projects", json({ name })),

  getProject: (id: string) => request<ProjectState>(`/projects/${id}`),

  deleteProject: (id: string) => request<void>(`/projects/${id}`, { method: "DELETE" }),

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
    request<PreviewRow[]>(
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
};
