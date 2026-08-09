"""
FastAPI app — the REST surface over app/logic.py and app/db.py.

Two levels of route: `/api/projects` (top-level grouping, e.g. "10DK") and
`/api/houses` (one full BOM workflow each — template → furniture list →
price matching → quotation). House routes mirror the original Streamlit
app's 3 steps, plus the small extra actions each step's UI exposed as
buttons/inputs (group-by-room, loading-factor calculator, baseline value,
raw-text search, PDF viewing).
"""
from __future__ import annotations

import io
import json
from typing import Any
from urllib.parse import quote

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
    assign_quotation_labels,
    build_quotation_rows,
    compute_price_preview,
    extract_furniture_list,
    extract_text_from_pdf,
    flag_suspicious_prices,
    generate_quotation_docx,
    generate_quotation_pdf,
    get_anthropic_client,
    get_openai_client,
    group_items_by_room,
    load_workbook_from_bytes,
    match_prices_bucket,
    merge_bucket_results,
    parse_exported_excel_for_quotation,
    read_anchor_value,
    write_mapping_to_excel,
)
from .schemas import (
    BaselineUpdate,
    ColumnMappingIn,
    ExportRequest,
    ExportStatus,
    ExportVersionMeta,
    FurnitureExtractResponse,
    FurnitureItem,
    FurnitureListReplace,
    HouseCreate,
    HouseState,
    HouseSummary,
    LoadingFactorRequest,
    LoadingFactorResponse,
    MappingRow,
    MappingRowsReplace,
    MatchPricesResponse,
    PreviewRequest,
    ProjectCreate,
    ProjectSummary,
    QuotationBucketMeta,
    QuotationBuildRequest,
    QuotationPdfRequest,
    QuotationPreview,
    QuotationTextsResponse,
    RawTextSearchResult,
    SetMultiplierRequest,
    SetMultiplierResponse,
    TemplateUploadResponse,
)

app = FastAPI(title="SSK The Cat Workspace API", version="1.0.0")

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


def _content_disposition(disposition: str, filename: str) -> str:
    """
    HTTP header values must be latin-1 — filenames here often contain Thai
    text (from the uploaded template's original name), which isn't. Falls
    back to an ASCII-only `filename=` for old clients and adds the RFC
    5987-encoded `filename*=` (what browsers actually use) for the rest.
    """
    ascii_filename = filename.encode("ascii", "ignore").decode() or "download.xlsx"
    return f"{disposition}; filename=\"{ascii_filename}\"; filename*=UTF-8''{quote(filename)}"


def _get_project_or_404(project_id: str):
    row = db.get_project_row(project_id)
    if row is None:
        raise HTTPException(404, "Project not found")
    return row


def _get_house_or_404(house_id: str):
    row = db.get_house_row(house_id)
    if row is None:
        raise HTTPException(404, "House not found")
    return row


def _house_state(house_id: str) -> HouseState:
    row = _get_house_or_404(house_id)
    furniture = db.get_furniture_items(house_id)
    mapping = flag_suspicious_prices(db.get_mapping_rows(house_id))
    quotation_buckets = sorted({m["bucket_label"] for m in db.get_quotation_pdfs_meta(house_id)})
    return HouseState(
        id=row["id"],
        project_id=row["project_id"],
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
    openai_ok = get_openai_client() is not None
    anthropic_ok = get_anthropic_client() is not None
    return {
        "status": "ok",
        "openai_configured": openai_ok,
        "anthropic_configured": anthropic_ok,
        # Step 2 (furniture-list extraction) prefers Claude when available.
        "extraction_provider": "anthropic" if anthropic_ok else ("openai" if openai_ok else None),
    }


# ---------------------------------------------------------------- projects --
# Top-level grouping (e.g. "10DK") — holds many houses (see below).

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
    created = db.create_project(name)
    return ProjectSummary(id=created["id"], name=created["name"], updated_at=created["updated_at"], has_logo=False)


@app.get("/api/projects/{project_id}", response_model=ProjectSummary)
def get_project(project_id: str):
    row = _get_project_or_404(project_id)
    return ProjectSummary(
        id=row["id"], name=row["name"], updated_at=row["updated_at"], has_logo=row["logo_bytes"] is not None
    )


@app.delete("/api/projects/{project_id}", status_code=204)
def delete_project(project_id: str):
    _get_project_or_404(project_id)
    db.delete_project(project_id)


@app.put("/api/projects/{project_id}/logo")
async def upload_project_logo(project_id: str, file: UploadFile = File(...)):
    _get_project_or_404(project_id)
    file_bytes = await file.read()
    content_type = file.content_type or "image/png"
    if not content_type.startswith("image/"):
        raise HTTPException(422, "Logo must be an image file (PNG/JPEG).")
    db.set_project_logo(project_id, file_bytes, content_type)
    return {"updated": True}


@app.get("/api/projects/{project_id}/logo")
def get_project_logo(project_id: str):
    _get_project_or_404(project_id)
    logo = db.get_project_logo(project_id)
    if logo is None:
        raise HTTPException(404, "No logo uploaded yet.")
    logo_bytes, content_type = logo
    return Response(content=logo_bytes, media_type=content_type)


# ------------------------------------------------------------------ houses --
# One house = one full BOM workflow (template → furniture list → price
# matching → quotation) — what this API used to call a "project" before the
# grouping above existed.

@app.get("/api/houses", response_model=list[HouseSummary])
def list_houses(project_id: str):
    _get_project_or_404(project_id)
    return db.list_houses(project_id)


@app.post("/api/houses", response_model=HouseSummary, status_code=201)
def create_house(body: HouseCreate):
    _get_project_or_404(body.project_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "House name is required.")
    if db.get_house_by_name(body.project_id, name) is not None:
        raise HTTPException(409, f"A house named '{name}' already exists in this project.")
    return db.create_house(body.project_id, name)


@app.get("/api/houses/{house_id}", response_model=HouseState)
def get_house(house_id: str):
    return _house_state(house_id)


@app.delete("/api/houses/{house_id}", status_code=204)
def delete_house(house_id: str):
    _get_house_or_404(house_id)
    db.delete_house(house_id)


@app.post("/api/houses/{house_id}/reset", response_model=HouseState)
def reset_house(house_id: str):
    _get_house_or_404(house_id)
    db.reset_house(house_id)
    return _house_state(house_id)


# ------------------------------------------------------------------ step 1 --

@app.put("/api/houses/{house_id}/template", response_model=TemplateUploadResponse)
async def upload_template(house_id: str, file: UploadFile = File(...)):
    _get_house_or_404(house_id)
    file_bytes = await file.read()
    wb = load_workbook_from_bytes(file_bytes)  # validates + raises LogicError if not a real .xlsx
    sheet_names = wb.sheetnames
    db.update_excel_template(house_id, file.filename or "template.xlsx", file_bytes, sheet_names)
    return TemplateUploadResponse(excel_filename=file.filename or "template.xlsx", sheet_names=sheet_names)


@app.put("/api/houses/{house_id}/target-sheet")
def set_target_sheet(house_id: str, sheet_name: str):
    row = _get_house_or_404(house_id)
    sheet_names = json.loads(row["sheet_names"]) if row["sheet_names"] else []
    if sheet_name not in sheet_names:
        raise HTTPException(422, f"'{sheet_name}' is not a sheet in this workbook.")
    db.update_target_sheet(house_id, sheet_name)
    return {"target_sheet_name": sheet_name}


# ------------------------------------------------------------------ step 2 --

@app.post("/api/houses/{house_id}/furniture-list", response_model=FurnitureExtractResponse)
async def extract_furniture(house_id: str, file: UploadFile = File(...)):
    row = _get_house_or_404(house_id)
    if row["excel_bytes"] is None:
        raise HTTPException(400, "Complete Step 1 (upload the Excel template) first.")

    if get_anthropic_client() is None and get_openai_client() is None:
        raise HTTPException(503, "No AI provider configured — set ANTHROPIC_API_KEY or OPENAI_API_KEY on the server.")

    file_bytes = await file.read()
    pdf_text, warning = extract_text_from_pdf(file_bytes)
    items = extract_furniture_list(pdf_text)
    for item in items:
        item.setdefault("verified", True)

    db.replace_furniture_items(house_id, items)
    return FurnitureExtractResponse(items=items, warning=warning)


@app.patch("/api/houses/{house_id}/furniture-list", response_model=list[FurnitureItem])
def update_furniture_list(house_id: str, body: FurnitureListReplace):
    _get_house_or_404(house_id)
    items = [i.model_dump() for i in body.items]
    db.replace_furniture_items(house_id, items)
    return db.get_furniture_items(house_id)


@app.post("/api/houses/{house_id}/furniture-list/group-by-room", response_model=list[FurnitureItem])
def group_furniture_by_room(house_id: str):
    _get_house_or_404(house_id)
    items = db.get_furniture_items(house_id)
    grouped = group_items_by_room(items)
    db.replace_furniture_items(house_id, grouped)
    return grouped


# ------------------------------------------------------------------ step 3 --

@app.post("/api/houses/{house_id}/quotations", response_model=MatchPricesResponse)
async def match_prices(
    house_id: str,
    sheet_name: str = Form(...),
    alt_pdfs: list[UploadFile] = File(default=[]),
    pmay_pdfs: list[UploadFile] = File(default=[]),
    othermaker_pdfs: list[UploadFile] = File(default=[]),
    purchase_pdfs: list[UploadFile] = File(default=[]),
):
    _get_house_or_404(house_id)
    furniture_list = db.get_furniture_items(house_id)
    if not furniture_list:
        raise HTTPException(400, "Complete Step 2 (extract the furniture list) first.")

    client = get_openai_client()
    if client is None:
        raise HTTPException(503, "OPENAI_API_KEY is not configured on the server.")

    # Only items still checked ("verified") in Step 2 are carried into Step 3 —
    # unchecking an item is how the user says "I don't want this one," so it's
    # cut out here rather than being matched/priced/exported.
    clean_furniture_list = [
        item for item in furniture_list if (item.get("item_name") or "").strip() and item.get("verified")
    ]
    if not clean_furniture_list:
        raise HTTPException(
            400,
            "ยังไม่ได้ติ๊กถูกรายการใดเลยใน Step 2 — กรุณาเลือก (ติ๊กถูก) อย่างน้อย 1 รายการก่อนจับคู่ราคา",
        )
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
        db.save_quotation_texts(house_id, {bucket_label: bucket_text})
        db.save_quotation_pdfs(house_id, bucket_label, pdf_bytes_list)

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

    db.replace_mapping_rows(house_id, final_mapping)
    if alt_batch_info:
        db.update_alt_batch_info(house_id, alt_batch_info)
    db.update_target_sheet(house_id, sheet_name)

    return MatchPricesResponse(
        mapping_rows=flag_suspicious_prices(final_mapping),
        matched_buckets=matched_buckets,
        alt_batch_info=alt_batch_info,
        warnings=warnings,
        pdf_files=db.get_quotation_pdfs_meta(house_id),
    )


@app.patch("/api/houses/{house_id}/mapping", response_model=list[MappingRow])
def update_mapping_rows(house_id: str, body: MappingRowsReplace):
    _get_house_or_404(house_id)
    rows = [r.model_dump(exclude={"suspicious"}) for r in body.rows]
    db.replace_mapping_rows(house_id, rows)
    return flag_suspicious_prices(db.get_mapping_rows(house_id))


@app.get("/api/houses/{house_id}/quotations/texts", response_model=QuotationTextsResponse)
def get_quotation_texts(house_id: str):
    _get_house_or_404(house_id)
    return QuotationTextsResponse(texts=db.get_quotation_texts(house_id))


@app.get("/api/houses/{house_id}/quotations/pdfs", response_model=list[QuotationBucketMeta])
def get_quotation_pdfs(house_id: str):
    _get_house_or_404(house_id)
    return db.get_quotation_pdfs_meta(house_id)


@app.get("/api/houses/{house_id}/quotations/pdfs/{pdf_id}")
def get_quotation_pdf_file(house_id: str, pdf_id: int):
    _get_house_or_404(house_id)
    found = db.get_quotation_pdf_bytes(pdf_id)
    if found is None:
        raise HTTPException(404, "PDF not found")
    filename, pdf_bytes = found
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": _content_disposition("inline", filename)},
    )


@app.post("/api/houses/{house_id}/quotations/search", response_model=list[RawTextSearchResult])
def search_quotation_text(house_id: str, query: str):
    _get_house_or_404(house_id)
    keywords = [w for w in query.split() if len(w) >= 2]
    if not keywords:
        return []
    results = []
    for bucket_label, text in db.get_quotation_texts(house_id).items():
        matches = [line for line in text.split("\n") if any(kw.lower() in line.lower() for kw in keywords)]
        if matches:
            results.append(RawTextSearchResult(bucket_label=bucket_label, matches=matches[:8]))
    return results


@app.post("/api/houses/{house_id}/preview")
def preview_prices(house_id: str, body: PreviewRequest):
    row = _get_house_or_404(house_id)
    if row["excel_bytes"] is None:
        raise HTTPException(400, "Upload the Excel template (Step 1) first.")
    rows = [r.model_dump(exclude={"suspicious"}) for r in body.rows]
    return compute_price_preview(row["excel_bytes"], rows, _col_map(body.column_mapping), body.sheet_name)


@app.post("/api/houses/{house_id}/loading-factor", response_model=LoadingFactorResponse)
def apply_loading_factor(house_id: str, body: LoadingFactorRequest):
    row = _get_house_or_404(house_id)
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
        db.update_excel_bytes(house_id, out.getvalue())

    return LoadingFactorResponse(
        loading_factor=loading_factor,
        pct_increase=(loading_factor - 1) * 100,
        anchor_cell=anchor_cell,
        updated=updated,
        current_value=new_value if updated else current_value,
    )


@app.put("/api/houses/{house_id}/multiplier", response_model=SetMultiplierResponse)
def set_multiplier(house_id: str, body: SetMultiplierRequest):
    """
    Writes a value directly into the I or M anchor cell — unlike Loading
    Factor (H), these have no per-house sub-formula; the caller already
    knows the number (e.g. computed from a %+VAT% pair, or typed directly).
    """
    row = _get_house_or_404(house_id)
    if row["excel_bytes"] is None:
        raise HTTPException(400, "Upload the Excel template (Step 1) first.")

    col_map = _col_map(body.column_mapping)
    col_letter = col_map.formula_i_col if body.column == "i" else col_map.formula_m_col

    wb = load_workbook_from_bytes(row["excel_bytes"])
    if body.sheet_name not in wb.sheetnames:
        raise HTTPException(422, f"'{body.sheet_name}' is not a sheet in this workbook.")
    ws = wb[body.sheet_name]

    current_value = read_anchor_value(ws, col_letter, col_map.multiplier_anchor_row)
    new_value = round(body.value, 4)
    anchor_cell = f"{col_letter}{col_map.multiplier_anchor_row}"

    updated = current_value is None or abs(current_value - new_value) > 0.00005
    if updated:
        ws[anchor_cell] = new_value
        out = io.BytesIO()
        wb.save(out)
        db.update_excel_bytes(house_id, out.getvalue())

    return SetMultiplierResponse(
        anchor_cell=anchor_cell,
        updated=updated,
        current_value=new_value if updated else current_value,
    )


@app.patch("/api/houses/{house_id}/baseline")
def update_baseline(house_id: str, body: BaselineUpdate):
    _get_house_or_404(house_id)
    db.update_baseline_furniture_value(house_id, body.value)
    return {"baseline_furniture_value": body.value}


@app.post("/api/houses/{house_id}/export")
def export_excel(house_id: str, body: ExportRequest):
    row = _get_house_or_404(house_id)
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
    db.update_final_export(house_id, result_bytes, source_hash)
    filename = f"completed_{row['excel_filename'] or 'BOM.xlsx'}"
    db.save_export_version(house_id, filename, result_bytes, source_hash)
    if body.baseline_furniture_value is not None:
        db.update_baseline_furniture_value(house_id, body.baseline_furniture_value)
    return {"warnings": warnings, "download_url": f"/api/houses/{house_id}/export/file"}


@app.post("/api/houses/{house_id}/export/status", response_model=ExportStatus)
def export_status(house_id: str, body: MappingRowsReplace):
    row = _get_house_or_404(house_id)
    if row["final_excel_bytes"] is None:
        return ExportStatus(has_export=False, is_stale=False)
    rows = [r.model_dump(exclude={"suspicious"}) for r in body.rows]
    current_hash = _current_export_source_hash(row["excel_bytes"], rows)
    return ExportStatus(
        has_export=True,
        is_stale=current_hash != row["final_excel_source_hash"],
        download_url=f"/api/houses/{house_id}/export/file",
    )


@app.get("/api/houses/{house_id}/export/file")
def download_export(house_id: str):
    row = _get_house_or_404(house_id)
    if row["final_excel_bytes"] is None:
        raise HTTPException(404, "No exported file yet — run export first.")
    filename = f"completed_{row['excel_filename'] or 'BOM.xlsx'}"
    return Response(
        content=row["final_excel_bytes"],
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": _content_disposition("attachment", filename)},
    )


# Every successful export/{house_id} call above also appends a timestamped
# row here, so past versions stay downloadable even after a newer export.
@app.get("/api/houses/{house_id}/exports", response_model=list[ExportVersionMeta])
def list_export_versions(house_id: str):
    _get_house_or_404(house_id)
    return db.list_export_versions(house_id)


@app.get("/api/houses/{house_id}/exports/{version_id}/file")
def download_export_version(house_id: str, version_id: int):
    _get_house_or_404(house_id)
    found = db.get_export_version_bytes(version_id)
    if found is None:
        raise HTTPException(404, "Export version not found")
    filename, file_bytes = found
    return Response(
        content=file_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": _content_disposition("attachment", filename)},
    )


# ------------------------------------------------------- client quotation --
# A distinct "quotation-doc" segment, since /houses/{id}/quotations* is
# already the Step-3 supplier-PDF-upload feature. Nothing here is persisted
# server-side — both paths (existing house vs. uploaded Excel) converge on
# the same transient QuotationRow list, only ever sent back to the client.

def _quotation_totals(rows: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    dk_work_subtotal = sum(
        (r.get("dk_work_price") or 0) * (r.get("quantity") or 0)
        for r in rows
        if not r.get("is_client_owned") and r.get("dk_work_price") is not None
    )
    purchase_subtotal = sum(
        (r.get("actual_price_purchase") or 0) * (r.get("quantity") or 0)
        for r in rows
        if not r.get("is_client_owned") and r.get("actual_price_purchase") is not None
    )
    vat = dk_work_subtotal * 0.07
    return dk_work_subtotal, purchase_subtotal, vat, dk_work_subtotal + vat


@app.post("/api/houses/{house_id}/quotation-doc/preview", response_model=QuotationPreview)
def preview_quotation_from_house(house_id: str, body: QuotationBuildRequest):
    row = _get_house_or_404(house_id)
    if row["excel_bytes"] is None:
        raise HTTPException(400, "Upload the Excel template (Step 1) first.")
    rows = [r.model_dump(exclude={"suspicious"}) for r in body.rows]
    preview = compute_price_preview(row["excel_bytes"], rows, _col_map(body.column_mapping), body.sheet_name)
    quotation_rows, row_warnings = build_quotation_rows(preview["rows"])
    dk_work_subtotal, purchase_subtotal, vat, grand_total = _quotation_totals(quotation_rows)
    return QuotationPreview(
        rows=quotation_rows,
        warnings=preview["warnings"] + row_warnings,
        dk_work_subtotal=dk_work_subtotal,
        purchase_subtotal=purchase_subtotal,
        vat=vat,
        grand_total=grand_total,
    )


@app.post("/api/quotation-doc/inspect-excel")
async def inspect_quotation_excel(file: UploadFile = File(...)):
    file_bytes = await file.read()
    wb = load_workbook_from_bytes(file_bytes)
    return {"sheet_names": wb.sheetnames}


@app.post("/api/quotation-doc/preview-from-excel", response_model=QuotationPreview)
async def preview_quotation_from_excel(file: UploadFile = File(...), sheet_name: str = Form(...)):
    file_bytes = await file.read()
    parsed_rows, parse_warnings = parse_exported_excel_for_quotation(file_bytes, sheet_name, ColumnMapping())
    quotation_rows, row_warnings = build_quotation_rows(parsed_rows)
    dk_work_subtotal, purchase_subtotal, vat, grand_total = _quotation_totals(quotation_rows)
    return QuotationPreview(
        rows=quotation_rows,
        warnings=parse_warnings + row_warnings,
        dk_work_subtotal=dk_work_subtotal,
        purchase_subtotal=purchase_subtotal,
        vat=vat,
        grand_total=grand_total,
    )


def _rows_with_fresh_labels(body_rows: list[Any]) -> list[dict[str, Any]]:
    """
    Recomputes each row's display label from scratch before rendering — the
    browser may have edited prices / toggled "Client's" since the row list
    was first built, which shifts which numeric/lettered sequence a row
    belongs to (see assign_quotation_labels).
    """
    rows = [r.model_dump() for r in body_rows]
    for row, label in zip(rows, assign_quotation_labels(rows)):
        row["label"] = label
    return rows


@app.post("/api/quotation-doc/pdf")
def download_quotation_pdf(body: QuotationPdfRequest):
    rows = _rows_with_fresh_labels(body.rows)
    logo = db.get_company_logo()
    pdf_bytes = generate_quotation_pdf(
        rows,
        body.client_name,
        body.project_name,
        body.quotation_date,
        logo_bytes=logo[0] if logo else None,
        deposit_deduction=body.deposit_deduction,
        remarks=body.remarks,
        grand_total_note=body.grand_total_note,
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": _content_disposition("attachment", "ใบเสนอราคา.pdf")},
    )


@app.post("/api/quotation-doc/docx")
def download_quotation_docx(body: QuotationPdfRequest):
    rows = _rows_with_fresh_labels(body.rows)
    logo = db.get_company_logo()
    docx_bytes = generate_quotation_docx(
        rows,
        body.client_name,
        body.project_name,
        body.quotation_date,
        logo_bytes=logo[0] if logo else None,
        deposit_deduction=body.deposit_deduction,
        remarks=body.remarks,
        grand_total_note=body.grand_total_note,
    )
    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": _content_disposition("attachment", "ใบเสนอราคา.docx")},
    )


@app.put("/api/quotation-doc/logo")
async def upload_company_logo(file: UploadFile = File(...)):
    """
    Uploaded once, reused on every quotation PDF/Word doc generated
    afterward — a single global asset, not tied to any one project/house.
    """
    file_bytes = await file.read()
    content_type = file.content_type or "image/png"
    if not content_type.startswith("image/"):
        raise HTTPException(422, "Logo must be an image file (PNG/JPEG).")
    db.set_company_logo(file_bytes, content_type)
    return {"updated": True}


@app.get("/api/quotation-doc/logo")
def get_company_logo():
    logo = db.get_company_logo()
    if logo is None:
        raise HTTPException(404, "No logo uploaded yet.")
    logo_bytes, content_type = logo
    return Response(content=logo_bytes, media_type=content_type)


# ------------------------------------------------------------------ profile --
# The profile menu's avatar picture. Display name itself is stored by the
# frontend directly in Supabase's own user_metadata (no backend involvement
# needed there) — this is only for the picture, which needs somewhere to
# live as bytes. Like every other route in this file, there's no check that
# the caller actually *is* user_id — the Next.js proxy.ts login gate is this
# app's chosen enforcement boundary, not this backend.

@app.put("/api/profile/{user_id}/avatar")
async def upload_user_avatar(user_id: str, file: UploadFile = File(...)):
    file_bytes = await file.read()
    content_type = file.content_type or "image/png"
    if not content_type.startswith("image/"):
        raise HTTPException(422, "Avatar must be an image file (PNG/JPEG).")
    db.set_user_avatar(user_id, file_bytes, content_type)
    return {"updated": True}


@app.get("/api/profile/{user_id}/avatar")
def get_user_avatar(user_id: str):
    avatar = db.get_user_avatar(user_id)
    if avatar is None:
        raise HTTPException(404, "No avatar uploaded yet.")
    avatar_bytes, content_type = avatar
    return Response(content=avatar_bytes, media_type=content_type)


# Exposed for reference by the frontend when building upload UI (which
# bucket maps to which order_type — mirrors BUCKET_ORDER_TYPE in the original).
@app.get("/api/meta/bucket-order-types")
def bucket_order_types():
    return BUCKET_ORDER_TYPE
