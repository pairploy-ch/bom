"""
FastAPI app — the REST surface over app/logic.py and app/db.py.

Route shape mirrors the original Streamlit app's 3 steps, plus the small
extra actions each step's UI exposed as buttons/inputs (group-by-room,
loading-factor calculator, baseline value, raw-text search, PDF viewing).
"""
from __future__ import annotations

import io
import json
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from . import db
from .config import CORS_ORIGINS
from .logic import (
    BUCKET_ORDER_TYPE,
    ColumnMapping,
    LogicError,
    _current_export_source_hash,
    compute_price_preview,
    extract_furniture_list,
    extract_text_from_pdf,
    flag_suspicious_prices,
    get_openai_client,
    group_items_by_room,
    load_workbook_from_bytes,
    match_prices_bucket,
    merge_bucket_results,
    read_anchor_value,
    write_mapping_to_excel,
)
from .schemas import (
    BaselineUpdate,
    ColumnMappingIn,
    ExportRequest,
    ExportStatus,
    FurnitureExtractResponse,
    FurnitureItem,
    FurnitureListReplace,
    LoadingFactorRequest,
    LoadingFactorResponse,
    MappingRow,
    MappingRowsReplace,
    MatchPricesResponse,
    PreviewRequest,
    ProjectCreate,
    ProjectState,
    ProjectSummary,
    QuotationBucketMeta,
    QuotationTextsResponse,
    RawTextSearchResult,
    TemplateUploadResponse,
)

app = FastAPI(title="Furniture BOM & Price Mapping API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


@app.exception_handler(LogicError)
def _logic_error_handler(_request, exc: LogicError):
    return Response(
        content=json.dumps({"detail": str(exc)}, ensure_ascii=False),
        status_code=400,
        media_type="application/json",
    )


def _col_map(m: ColumnMappingIn) -> ColumnMapping:
    return ColumnMapping(**m.model_dump())


def _get_project_or_404(project_id: str):
    row = db.get_project_row(project_id)
    if row is None:
        raise HTTPException(404, "Project not found")
    return row


def _project_state(project_id: str) -> ProjectState:
    row = _get_project_or_404(project_id)
    furniture = db.get_furniture_items(project_id)
    mapping = flag_suspicious_prices(db.get_mapping_rows(project_id))
    quotation_buckets = sorted({m["bucket_label"] for m in db.get_quotation_pdfs_meta(project_id)})
    return ProjectState(
        id=row["id"],
        name=row["name"],
        excel_filename=row["excel_filename"],
        has_template=row["excel_bytes"] is not None,
        sheet_names=json.loads(row["sheet_names"]) if row["sheet_names"] else [],
        target_sheet_name=row["target_sheet_name"],
        furniture_list=furniture,
        mapping_rows=mapping,
        alt_batch_info=json.loads(row["alt_batch_info"]) if row["alt_batch_info"] else None,
        baseline_furniture_value=row["baseline_furniture_value"],
        quotation_buckets=quotation_buckets,
        has_final_export=row["final_excel_bytes"] is not None,
        updated_at=row["updated_at"],
    )


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "openai_configured": get_openai_client() is not None}


# ---------------------------------------------------------------- projects --

@app.get("/api/projects", response_model=list[ProjectSummary])
def list_projects():
    return db.list_projects()


@app.post("/api/projects", response_model=ProjectSummary, status_code=201)
def create_project(body: ProjectCreate):
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "Project name is required.")
    if db.get_project_by_name(name) is not None:
        raise HTTPException(409, f"A project named '{name}' already exists.")
    return db.create_project(name)


@app.get("/api/projects/{project_id}", response_model=ProjectState)
def get_project(project_id: str):
    return _project_state(project_id)


@app.delete("/api/projects/{project_id}", status_code=204)
def delete_project(project_id: str):
    _get_project_or_404(project_id)
    db.delete_project(project_id)


# ------------------------------------------------------------------ step 1 --

@app.put("/api/projects/{project_id}/template", response_model=TemplateUploadResponse)
async def upload_template(project_id: str, file: UploadFile = File(...)):
    _get_project_or_404(project_id)
    file_bytes = await file.read()
    wb = load_workbook_from_bytes(file_bytes)  # validates + raises LogicError if not a real .xlsx
    sheet_names = wb.sheetnames
    db.update_excel_template(project_id, file.filename or "template.xlsx", file_bytes, sheet_names)
    return TemplateUploadResponse(excel_filename=file.filename or "template.xlsx", sheet_names=sheet_names)


@app.put("/api/projects/{project_id}/target-sheet")
def set_target_sheet(project_id: str, sheet_name: str):
    row = _get_project_or_404(project_id)
    sheet_names = json.loads(row["sheet_names"]) if row["sheet_names"] else []
    if sheet_name not in sheet_names:
        raise HTTPException(422, f"'{sheet_name}' is not a sheet in this workbook.")
    db.update_target_sheet(project_id, sheet_name)
    return {"target_sheet_name": sheet_name}


# ------------------------------------------------------------------ step 2 --

@app.post("/api/projects/{project_id}/furniture-list", response_model=FurnitureExtractResponse)
async def extract_furniture(project_id: str, file: UploadFile = File(...)):
    row = _get_project_or_404(project_id)
    if row["excel_bytes"] is None:
        raise HTTPException(400, "Complete Step 1 (upload the Excel template) first.")

    client = get_openai_client()
    if client is None:
        raise HTTPException(503, "OPENAI_API_KEY is not configured on the server.")

    file_bytes = await file.read()
    pdf_text, warning = extract_text_from_pdf(file_bytes)
    items = extract_furniture_list(client, pdf_text)
    for item in items:
        item.setdefault("verified", False)

    db.replace_furniture_items(project_id, items)
    return FurnitureExtractResponse(items=items, warning=warning)


@app.patch("/api/projects/{project_id}/furniture-list", response_model=list[FurnitureItem])
def update_furniture_list(project_id: str, body: FurnitureListReplace):
    _get_project_or_404(project_id)
    items = [i.model_dump() for i in body.items]
    db.replace_furniture_items(project_id, items)
    return db.get_furniture_items(project_id)


@app.post("/api/projects/{project_id}/furniture-list/group-by-room", response_model=list[FurnitureItem])
def group_furniture_by_room(project_id: str):
    _get_project_or_404(project_id)
    items = db.get_furniture_items(project_id)
    grouped = group_items_by_room(items)
    db.replace_furniture_items(project_id, grouped)
    return grouped


# ------------------------------------------------------------------ step 3 --

@app.post("/api/projects/{project_id}/quotations", response_model=MatchPricesResponse)
async def match_prices(
    project_id: str,
    sheet_name: str = Form(...),
    alt_pdfs: list[UploadFile] = File(default=[]),
    pmay_pdfs: list[UploadFile] = File(default=[]),
    othermaker_pdfs: list[UploadFile] = File(default=[]),
    purchase_pdfs: list[UploadFile] = File(default=[]),
):
    _get_project_or_404(project_id)
    furniture_list = db.get_furniture_items(project_id)
    if not furniture_list:
        raise HTTPException(400, "Complete Step 2 (extract the furniture list) first.")

    client = get_openai_client()
    if client is None:
        raise HTTPException(503, "OPENAI_API_KEY is not configured on the server.")

    clean_furniture_list = [item for item in furniture_list if (item.get("item_name") or "").strip()]
    indexed_furniture_list = [{**item, "index": i} for i, item in enumerate(clean_furniture_list)]

    buckets: list[tuple[str, list[UploadFile], str | None]] = [
        ("ALT", alt_pdfs, "ALT"),
        ("P'May", pmay_pdfs, "P'May"),
        ("OTHER_MAKER", othermaker_pdfs, "Other"),
        ("PURCHASE", purchase_pdfs, None),
    ]

    bucket_results: list[tuple[str, list[dict[str, Any]]]] = []
    warnings: list[str] = []
    alt_batch_info: dict[str, Any] | None = None
    matched_buckets: list[str] = []

    for bucket_label, files, fixed_supplier in buckets:
        if not files:
            continue

        combined_chunks = []
        pdf_bytes_list: list[tuple[str, bytes]] = []
        for pdf_file in files:
            raw = await pdf_file.read()
            pdf_bytes_list.append((pdf_file.filename or "quotation.pdf", raw))
            try:
                text, warn = extract_text_from_pdf(raw)
            except LogicError as e:
                warnings.append(f"{bucket_label} — {pdf_file.filename}: {e}")
                continue
            if warn:
                warnings.append(f"{bucket_label} — {pdf_file.filename}: {warn}")
            combined_chunks.append(f"===== Supplier document: {pdf_file.filename} =====\n{text}")

        if not combined_chunks:
            warnings.append(f"❌ Could not extract text from any {bucket_label} PDF(s).")
            continue

        bucket_text = "\n\n".join(combined_chunks)
        db.save_quotation_texts(project_id, {bucket_label: bucket_text})
        db.save_quotation_pdfs(project_id, bucket_label, pdf_bytes_list)

        mapped, alt_info = match_prices_bucket(client, indexed_furniture_list, bucket_text, fixed_supplier)
        bucket_results.append((bucket_label, mapped))
        matched_buckets.append(bucket_label)
        if alt_info:
            alt_batch_info = alt_info

    if not bucket_results:
        raise HTTPException(422, "ไม่สามารถจับคู่ราคาได้จากไฟล์ที่อัปโหลด กรุณาตรวจสอบไฟล์อีกครั้ง")

    final_mapping, merge_warning = merge_bucket_results(clean_furniture_list, bucket_results)
    if merge_warning:
        warnings.append(merge_warning)

    db.replace_mapping_rows(project_id, final_mapping)
    if alt_batch_info:
        db.update_alt_batch_info(project_id, alt_batch_info)
    db.update_target_sheet(project_id, sheet_name)

    return MatchPricesResponse(
        mapping_rows=flag_suspicious_prices(final_mapping),
        matched_buckets=matched_buckets,
        alt_batch_info=alt_batch_info,
        warnings=warnings,
        pdf_files=db.get_quotation_pdfs_meta(project_id),
    )


@app.patch("/api/projects/{project_id}/mapping", response_model=list[MappingRow])
def update_mapping_rows(project_id: str, body: MappingRowsReplace):
    _get_project_or_404(project_id)
    rows = [r.model_dump(exclude={"suspicious"}) for r in body.rows]
    db.replace_mapping_rows(project_id, rows)
    return flag_suspicious_prices(db.get_mapping_rows(project_id))


@app.get("/api/projects/{project_id}/quotations/texts", response_model=QuotationTextsResponse)
def get_quotation_texts(project_id: str):
    _get_project_or_404(project_id)
    return QuotationTextsResponse(texts=db.get_quotation_texts(project_id))


@app.get("/api/projects/{project_id}/quotations/pdfs", response_model=list[QuotationBucketMeta])
def get_quotation_pdfs(project_id: str):
    _get_project_or_404(project_id)
    return db.get_quotation_pdfs_meta(project_id)


@app.get("/api/projects/{project_id}/quotations/pdfs/{pdf_id}")
def get_quotation_pdf_file(project_id: str, pdf_id: int):
    _get_project_or_404(project_id)
    found = db.get_quotation_pdf_bytes(pdf_id)
    if found is None:
        raise HTTPException(404, "PDF not found")
    filename, pdf_bytes = found
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@app.post("/api/projects/{project_id}/quotations/search", response_model=list[RawTextSearchResult])
def search_quotation_text(project_id: str, query: str):
    _get_project_or_404(project_id)
    keywords = [w for w in query.split() if len(w) >= 2]
    if not keywords:
        return []
    results = []
    for bucket_label, text in db.get_quotation_texts(project_id).items():
        matches = [line for line in text.split("\n") if any(kw.lower() in line.lower() for kw in keywords)]
        if matches:
            results.append(RawTextSearchResult(bucket_label=bucket_label, matches=matches[:8]))
    return results


@app.post("/api/projects/{project_id}/preview")
def preview_prices(project_id: str, body: PreviewRequest):
    row = _get_project_or_404(project_id)
    if row["excel_bytes"] is None:
        raise HTTPException(400, "Upload the Excel template (Step 1) first.")
    rows = [r.model_dump(exclude={"suspicious"}) for r in body.rows]
    return compute_price_preview(row["excel_bytes"], rows, _col_map(body.column_mapping), body.sheet_name)


@app.post("/api/projects/{project_id}/loading-factor", response_model=LoadingFactorResponse)
def apply_loading_factor(project_id: str, body: LoadingFactorRequest):
    row = _get_project_or_404(project_id)
    if row["excel_bytes"] is None:
        raise HTTPException(400, "Upload the Excel template (Step 1) first.")
    if body.sum_of_item_costs <= 0:
        raise HTTPException(422, "sum_of_item_costs must be greater than 0.")

    col_map = _col_map(body.column_mapping)
    loading_factor = (body.sum_of_item_costs + body.protection_fee + body.management_fee) / body.sum_of_item_costs

    wb = load_workbook_from_bytes(row["excel_bytes"])
    if body.sheet_name not in wb.sheetnames:
        raise HTTPException(422, f"'{body.sheet_name}' is not a sheet in this workbook.")
    ws = wb[body.sheet_name]

    current_value = read_anchor_value(ws, col_map.formula_h_col, col_map.multiplier_anchor_row)
    new_value = round(loading_factor, 4)
    anchor_cell = f"{col_map.formula_h_col}{col_map.multiplier_anchor_row}"

    updated = current_value is None or abs(current_value - new_value) > 0.00005
    if updated:
        ws[anchor_cell] = new_value
        out = io.BytesIO()
        wb.save(out)
        db.update_excel_bytes(project_id, out.getvalue())

    return LoadingFactorResponse(
        loading_factor=loading_factor,
        pct_increase=(loading_factor - 1) * 100,
        anchor_cell=anchor_cell,
        updated=updated,
        current_value=new_value if updated else current_value,
    )


@app.patch("/api/projects/{project_id}/baseline")
def update_baseline(project_id: str, body: BaselineUpdate):
    _get_project_or_404(project_id)
    db.update_baseline_furniture_value(project_id, body.value)
    return {"baseline_furniture_value": body.value}


@app.post("/api/projects/{project_id}/export")
def export_excel(project_id: str, body: ExportRequest):
    row = _get_project_or_404(project_id)
    if row["excel_bytes"] is None:
        raise HTTPException(400, "Upload the Excel template (Step 1) first.")

    rows = [r.model_dump(exclude={"suspicious"}) for r in body.rows]
    result_bytes, warnings = write_mapping_to_excel(
        row["excel_bytes"],
        body.sheet_name,
        rows,
        _col_map(body.column_mapping),
        furniture_total=body.furniture_total,
        baseline_furniture_value=body.baseline_furniture_value,
    )
    source_hash = _current_export_source_hash(row["excel_bytes"], rows)
    db.update_final_export(project_id, result_bytes, source_hash)
    if body.baseline_furniture_value is not None:
        db.update_baseline_furniture_value(project_id, body.baseline_furniture_value)
    return {"warnings": warnings, "download_url": f"/api/projects/{project_id}/export/file"}


@app.post("/api/projects/{project_id}/export/status", response_model=ExportStatus)
def export_status(project_id: str, body: MappingRowsReplace):
    row = _get_project_or_404(project_id)
    if row["final_excel_bytes"] is None:
        return ExportStatus(has_export=False, is_stale=False)
    rows = [r.model_dump(exclude={"suspicious"}) for r in body.rows]
    current_hash = _current_export_source_hash(row["excel_bytes"], rows)
    return ExportStatus(
        has_export=True,
        is_stale=current_hash != row["final_excel_source_hash"],
        download_url=f"/api/projects/{project_id}/export/file",
    )


@app.get("/api/projects/{project_id}/export/file")
def download_export(project_id: str):
    row = _get_project_or_404(project_id)
    if row["final_excel_bytes"] is None:
        raise HTTPException(404, "No exported file yet — run export first.")
    filename = f"completed_{row['excel_filename'] or 'BOM.xlsx'}"
    return Response(
        content=row["final_excel_bytes"],
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# Exposed for reference by the frontend when building upload UI (which
# bucket maps to which order_type — mirrors BUCKET_ORDER_TYPE in the original).
@app.get("/api/meta/bucket-order-types")
def bucket_order_types():
    return BUCKET_ORDER_TYPE
