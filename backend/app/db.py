"""
Persistence layer — Postgres (Supabase), normalized into real tables instead
of the original app's single `projects` table with `furniture_list` /
`final_mapping` dumped in as JSON blobs (see save_project_state() /
load_project_state() in the original app.py). This is what makes partial
edits (one row, one field) cheap, and is the actual fix for "state
management" — a browser refresh (or a totally different browser) just
re-fetches from here; nothing ever lived only in a Streamlit session.

Two levels of entity: a **project** (e.g. "10DK") groups many **houses**
(each house is one full BOM: template → furniture list → price matching →
quotation — what this file used to call a "project" before that grouping
existed).

Originally SQLite (a local file); moved to Postgres so the backend can run
as a stateless container (no persistent disk needed) — see
supabase/README.md for connection setup. IDs stay app-generated
`uuid4().hex` strings and timestamps stay ISO-format text (not Postgres
`uuid`/`timestamptz`) specifically so this module's own logic didn't need
to change during that move, only the connection/placeholder plumbing did.

A fresh connection is opened per call (mirrors the original SQLite version's
own pattern) — simple, and avoids any cross-thread connection-reuse
pitfalls under FastAPI's async request handling. DATABASE_URL should be
Supabase's "Transaction pooler" string so pooling still happens, just on
Supabase's side instead of held open in this process.
"""
from __future__ import annotations

import contextlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from .config import DATABASE_URL

DEFAULT_ORDER_TYPE = "จัดซื้อ (ราคาจริง ไม่บวกกำไร)"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_bytea(row: dict[str, Any] | None, *columns: str) -> dict[str, Any] | None:
    """
    `SELECT *` rows (get_project_row/get_house_row) get handed straight to
    callers (main.py) that do things like `Response(content=row["x"])` or
    feed it to openpyxl/hashlib — those expect plain `bytes`. Every
    single-column BLOB getter in this file already returns `bytes(...)`
    explicitly; this does the same for the handful of BYTEA columns that
    ride along in a full-row dict instead.
    """
    if row is None:
        return None
    for col in columns:
        if row.get(col) is not None:
            row[col] = bytes(row[col])
    return row


def _lock_house(conn: psycopg.Connection, house_id: str) -> None:
    """
    Serializes the "delete all rows for this house, then bulk-reinsert"
    functions below against each other. Without this, two overlapping calls
    for the same house (e.g. rapid-fire auto-save from two fields' onBlur
    landing close together) can interleave under READ COMMITTED: both
    DELETEs see the same pre-existing rows, both INSERTs then succeed, and
    the table ends up with two full copies instead of one — silent row
    duplication, not an error. pg_advisory_xact_lock blocks a second caller
    for the same house_id until the first one's transaction commits, and
    releases automatically at commit/rollback (matches get_conn()'s
    per-call connection/transaction lifetime). hashtext() collisions across
    different house_ids only cost extra waiting, never incorrect results.
    """
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (house_id,))


@contextlib.contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    # prepare_threshold=None disables psycopg3's automatic server-side
    # prepared statements. DATABASE_URL points at Supabase's "Transaction
    # pooler" (PgBouncer, transaction mode) — it hands out a shared backend
    # connection per transaction, so a prepared statement name like "_pg3_0"
    # from one request can still be registered on that backend connection
    # when a totally unrelated request lands on it next, raising
    # psycopg.errors.DuplicatePreparedStatement. executemany() is what
    # triggers auto-prepare, which is why this only showed up on bulk
    # inserts (e.g. uploading multiple PDFs at once).
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, prepare_threshold=None)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                logo_bytes BYTEA,
                logo_content_type TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS houses (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                excel_filename TEXT,
                excel_bytes BYTEA,
                sheet_names TEXT,             -- JSON array
                target_sheet_name TEXT,
                alt_batch_info TEXT,          -- JSON object or NULL
                baseline_furniture_value DOUBLE PRECISION,
                final_excel_bytes BYTEA,      -- last exported file (for the "stale download" check)
                final_excel_source_hash TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_houses_project ON houses(project_id, updated_at);

            -- Manual "done" toggles for the sidebar's workflow checklist
            -- (คำนวณราคา / ใบราคา) — user-set, not derived from any other
            -- column, since neither step has a reliable auto-detected
            -- "finished" signal.
            ALTER TABLE houses ADD COLUMN IF NOT EXISTS workflow_calc_done BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE houses ADD COLUMN IF NOT EXISTS workflow_quotation_done BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE houses ADD COLUMN IF NOT EXISTS workflow_contract_done BOOLEAN NOT NULL DEFAULT FALSE;

            -- Scalar fill-in-the-blank fields for the "ทำสัญญา" contract
            -- form tab — one JSON object per house, same shape as
            -- ContractDetails in schemas.py. Kept as a single blob (like
            -- alt_batch_info) since it's a fixed set of scalar fields, not
            -- a repeating list.
            ALTER TABLE houses ADD COLUMN IF NOT EXISTS contract_details TEXT;

            -- Saved "ใบราคา" (quotation preview) state — client/project/date
            -- fields + the user's in-place edited rows, one JSON object per
            -- house (shape: QuotationDetails in schemas.py). Previously the
            -- quotation page never persisted anything (regenerated fresh
            -- from mapping_rows on every visit), so in-table edits were lost
            -- on reload — this is the explicit "บันทึก" checkpoint for that.
            ALTER TABLE houses ADD COLUMN IF NOT EXISTS quotation_details TEXT;

            CREATE TABLE IF NOT EXISTS furniture_items (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                house_id TEXT NOT NULL REFERENCES houses(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                room TEXT NOT NULL DEFAULT '',
                item_name TEXT NOT NULL DEFAULT '',
                quantity DOUBLE PRECISION NOT NULL DEFAULT 1,
                verified BOOLEAN NOT NULL DEFAULT FALSE,
                spec TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_furniture_house ON furniture_items(house_id, position);

            CREATE TABLE IF NOT EXISTS mapping_rows (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                house_id TEXT NOT NULL REFERENCES houses(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                room TEXT NOT NULL DEFAULT '',
                item_name TEXT NOT NULL DEFAULT '',
                quantity DOUBLE PRECISION NOT NULL DEFAULT 1,
                unit_price DOUBLE PRECISION NOT NULL DEFAULT 0,
                alt_price DOUBLE PRECISION NOT NULL DEFAULT 0,
                pmay_price DOUBLE PRECISION NOT NULL DEFAULT 0,
                other_maker_price DOUBLE PRECISION NOT NULL DEFAULT 0,
                supplier TEXT NOT NULL DEFAULT '',
                order_type TEXT NOT NULL DEFAULT '{default_order_type}',
                spec TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_mapping_house ON mapping_rows(house_id, position);
            -- Extra descriptive text AI found in the supplier's quotation PDF
            -- beyond the item name — distinct from `spec` (ขนาด, from Step 2).
            ALTER TABLE mapping_rows ADD COLUMN IF NOT EXISTS quotation_spec TEXT NOT NULL DEFAULT '';

            CREATE TABLE IF NOT EXISTS quotation_texts (
                house_id TEXT NOT NULL REFERENCES houses(id) ON DELETE CASCADE,
                bucket_label TEXT NOT NULL,
                raw_text TEXT NOT NULL,
                PRIMARY KEY (house_id, bucket_label)
            );

            CREATE TABLE IF NOT EXISTS quotation_pdfs (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                house_id TEXT NOT NULL REFERENCES houses(id) ON DELETE CASCADE,
                bucket_label TEXT NOT NULL,
                filename TEXT NOT NULL,
                pdf_bytes BYTEA NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pdfs_house ON quotation_pdfs(house_id, bucket_label);

            -- Ordered pages for the "ทำสัญญา" contract's "เอกสารแนบ" tab —
            -- each row is one flattened PNG (pasted screenshot + drawn
            -- annotations, merged client-side before upload), analogous to
            -- quotation_pdfs above but ordered by `position` (page order in
            -- the final document) instead of grouped by supplier bucket.
            CREATE TABLE IF NOT EXISTS contract_attachments (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                house_id TEXT NOT NULL REFERENCES houses(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                image_bytes BYTEA NOT NULL,
                content_type TEXT NOT NULL DEFAULT 'image/png'
            );
            CREATE INDEX IF NOT EXISTS idx_contract_attachments_house ON contract_attachments(house_id, position);

            -- Per-page metadata driving the auto "หมายเหตุ" footnote printed
            -- bottom-right on this page in the combined contract PDF (see
            -- logic.py's _ATTACHMENT_REMARKS / generate_contract_pdf).
            -- attachment_type is one of '', 'plan', 'perspective',
            -- 'furniture_list'; item_range/reference_note only apply to
            -- 'furniture_list' (the "ลำดับที่ 1-27" / "(10)" blanks in that
            -- type's footnote).
            ALTER TABLE contract_attachments ADD COLUMN IF NOT EXISTS attachment_type TEXT NOT NULL DEFAULT '';
            ALTER TABLE contract_attachments ADD COLUMN IF NOT EXISTS floor TEXT NOT NULL DEFAULT '';
            ALTER TABLE contract_attachments ADD COLUMN IF NOT EXISTS zone TEXT NOT NULL DEFAULT '';
            ALTER TABLE contract_attachments ADD COLUMN IF NOT EXISTS item_range TEXT NOT NULL DEFAULT '';
            ALTER TABLE contract_attachments ADD COLUMN IF NOT EXISTS reference_note TEXT NOT NULL DEFAULT '';

            -- One row per completed export, so a house can be "saved" across many
            -- timestamped versions and any past one re-downloaded later, instead of only
            -- ever keeping the single latest file (which is still tracked separately on
            -- `houses.final_excel_bytes` for the cheap stale-check).
            CREATE TABLE IF NOT EXISTS export_versions (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                house_id TEXT NOT NULL REFERENCES houses(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                excel_bytes BYTEA NOT NULL,
                source_hash TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_export_versions_house ON export_versions(house_id, created_at);

            -- Single global row (id=1) — the company logo used on every
            -- generated quotation PDF/Word doc, uploaded once and reused,
            -- not tied to any one project/house.
            CREATE TABLE IF NOT EXISTS app_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                company_logo_bytes BYTEA,
                company_logo_content_type TEXT
            );

            -- Avatar images for the profile menu, keyed by Supabase Auth user
            -- id (a UUID string). Display name itself lives in Supabase's own
            -- user_metadata, not here — this table only holds what Supabase
            -- Auth has no place for: the picture. Like every other table in
            -- this file there's no server-side check that the caller *is*
            -- this user_id — enforcement is the Next.js proxy.ts login gate,
            -- matching this app's frontend-only auth decision.
            CREATE TABLE IF NOT EXISTS user_avatars (
                user_id TEXT PRIMARY KEY,
                avatar_bytes BYTEA NOT NULL,
                avatar_content_type TEXT NOT NULL
            );
            """.format(default_order_type=DEFAULT_ORDER_TYPE)
        )


# ----------------------------------------------------------------- projects --
# Top-level grouping (e.g. "10DK") — holds many houses. See module docstring.

def create_project(name: str) -> dict[str, Any]:
    project_id = uuid.uuid4().hex
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) VALUES (%s, %s, %s, %s)",
            (project_id, name, now, now),
        )
    return {"id": project_id, "name": name, "created_at": now, "updated_at": now}


def list_projects() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, updated_at, (logo_bytes IS NOT NULL) AS has_logo "
            "FROM projects ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_project_row(project_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone()
    return _coerce_bytea(row, "logo_bytes")


def get_project_by_name(name: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM projects WHERE name = %s", (name,)).fetchone()


def delete_project(project_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM projects WHERE id = %s", (project_id,))


def set_project_logo(project_id: str, logo_bytes: bytes, content_type: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE projects SET logo_bytes = %s, logo_content_type = %s, updated_at = %s WHERE id = %s",
            (logo_bytes, content_type, _now(), project_id),
        )


def get_project_logo(project_id: str) -> tuple[bytes, str] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT logo_bytes, logo_content_type FROM projects WHERE id = %s", (project_id,)
        ).fetchone()
    if row is None or row["logo_bytes"] is None:
        return None
    return (bytes(row["logo_bytes"]), row["logo_content_type"] or "image/png")


# ------------------------------------------------------------------- houses --

def create_house(project_id: str, name: str) -> dict[str, Any]:
    house_id = uuid.uuid4().hex
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO houses (id, project_id, name, created_at, updated_at) VALUES (%s, %s, %s, %s, %s)",
            (house_id, project_id, name, now, now),
        )
    return {"id": house_id, "project_id": project_id, "name": name, "created_at": now, "updated_at": now}


def touch_house(house_id: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE houses SET updated_at = %s WHERE id = %s", (_now(), house_id))


def rename_house(house_id: str, name: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE houses SET name = %s, updated_at = %s WHERE id = %s",
            (name, _now(), house_id),
        )


def list_houses(project_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, updated_at FROM houses WHERE project_id = %s ORDER BY updated_at DESC",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_house_row(house_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM houses WHERE id = %s", (house_id,)).fetchone()
    return _coerce_bytea(row, "excel_bytes", "final_excel_bytes")


def get_house_by_name(project_id: str, name: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM houses WHERE project_id = %s AND name = %s", (project_id, name)
        ).fetchone()


def delete_house(house_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM houses WHERE id = %s", (house_id,))


def reset_house(house_id: str) -> None:
    """
    Wipes everything under this house back to a just-created state — same
    id/name (so the URL and house-list entry stay put), but the template,
    furniture list, mapping rows, quotation texts/PDFs, and any exports are
    all cleared. The equivalent of the original app's "Reset All", which
    wiped the whole Streamlit session.
    """
    with get_conn() as conn:
        conn.execute("DELETE FROM furniture_items WHERE house_id = %s", (house_id,))
        conn.execute("DELETE FROM mapping_rows WHERE house_id = %s", (house_id,))
        conn.execute("DELETE FROM quotation_texts WHERE house_id = %s", (house_id,))
        conn.execute("DELETE FROM quotation_pdfs WHERE house_id = %s", (house_id,))
        conn.execute("DELETE FROM export_versions WHERE house_id = %s", (house_id,))
        conn.execute("DELETE FROM contract_attachments WHERE house_id = %s", (house_id,))
        conn.execute(
            """
            UPDATE houses
            SET excel_filename = NULL,
                excel_bytes = NULL,
                sheet_names = NULL,
                target_sheet_name = NULL,
                alt_batch_info = NULL,
                baseline_furniture_value = NULL,
                final_excel_bytes = NULL,
                final_excel_source_hash = NULL,
                contract_details = NULL,
                workflow_contract_done = FALSE,
                quotation_details = NULL,
                updated_at = %s
            WHERE id = %s
            """,
            (_now(), house_id),
        )


def update_excel_template(house_id: str, filename: str, file_bytes: bytes, sheet_names: list[str]) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE houses
            SET excel_filename = %s, excel_bytes = %s, sheet_names = %s, updated_at = %s
            WHERE id = %s
            """,
            (filename, file_bytes, json.dumps(sheet_names), _now(), house_id),
        )


def update_excel_bytes(house_id: str, file_bytes: bytes) -> None:
    """Used after an in-place edit to the workbook itself (Loading Factor auto-apply)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE houses SET excel_bytes = %s, updated_at = %s WHERE id = %s",
            (file_bytes, _now(), house_id),
        )


def update_target_sheet(house_id: str, sheet_name: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE houses SET target_sheet_name = %s, updated_at = %s WHERE id = %s",
            (sheet_name, _now(), house_id),
        )


def update_alt_batch_info(house_id: str, alt_batch_info: dict[str, Any] | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE houses SET alt_batch_info = %s, updated_at = %s WHERE id = %s",
            (json.dumps(alt_batch_info) if alt_batch_info else None, _now(), house_id),
        )


def update_baseline_furniture_value(house_id: str, value: float | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE houses SET baseline_furniture_value = %s, updated_at = %s WHERE id = %s",
            (value, _now(), house_id),
        )


def update_final_export(house_id: str, file_bytes: bytes, source_hash: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE houses SET final_excel_bytes = %s, final_excel_source_hash = %s, updated_at = %s WHERE id = %s",
            (file_bytes, source_hash, _now(), house_id),
        )


_WORKFLOW_STEP_COLUMNS = {
    "calc": "workflow_calc_done",
    "quotation": "workflow_quotation_done",
    "contract": "workflow_contract_done",
}


def set_workflow_step_done(house_id: str, step: str, done: bool) -> None:
    column = _WORKFLOW_STEP_COLUMNS[step]
    with get_conn() as conn:
        conn.execute(f"UPDATE houses SET {column} = %s, updated_at = %s WHERE id = %s", (done, _now(), house_id))


def update_contract_details(house_id: str, details: dict[str, Any]) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE houses SET contract_details = %s, updated_at = %s WHERE id = %s",
            (json.dumps(details), _now(), house_id),
        )


def update_quotation_details(house_id: str, details: dict[str, Any]) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE houses SET quotation_details = %s, updated_at = %s WHERE id = %s",
            (json.dumps(details), _now(), house_id),
        )


# --------------------------------------------------------------- furniture --

def replace_furniture_items(house_id: str, items: list[dict[str, Any]]) -> None:
    with get_conn() as conn:
        _lock_house(conn, house_id)
        conn.execute("DELETE FROM furniture_items WHERE house_id = %s", (house_id,))
        conn.cursor().executemany(
            """
            INSERT INTO furniture_items (house_id, position, room, item_name, quantity, verified, spec)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    house_id,
                    i,
                    str(item.get("room") or ""),
                    str(item.get("item_name") or ""),
                    float(item.get("quantity") or 0),
                    bool(item.get("verified")),
                    str(item.get("spec") or ""),
                )
                for i, item in enumerate(items)
            ],
        )
        conn.execute("UPDATE houses SET updated_at = %s WHERE id = %s", (_now(), house_id))


def get_furniture_items(house_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT room, item_name, quantity, verified, spec FROM furniture_items "
            "WHERE house_id = %s ORDER BY position",
            (house_id,),
        ).fetchall()
    return [
        {
            "room": r["room"],
            "item_name": r["item_name"],
            "quantity": r["quantity"],
            "verified": bool(r["verified"]),
            "spec": r["spec"],
        }
        for r in rows
    ]


# ----------------------------------------------------------------- mapping --

def replace_mapping_rows(house_id: str, rows: list[dict[str, Any]]) -> None:
    with get_conn() as conn:
        _lock_house(conn, house_id)
        conn.execute("DELETE FROM mapping_rows WHERE house_id = %s", (house_id,))
        conn.cursor().executemany(
            """
            INSERT INTO mapping_rows
                (house_id, position, room, item_name, quantity, unit_price,
                 alt_price, pmay_price, other_maker_price, supplier, order_type, spec, quotation_spec)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    house_id,
                    i,
                    str(row.get("room") or ""),
                    str(row.get("item_name") or ""),
                    float(row.get("quantity") or 0),
                    float(row.get("unit_price") or 0),
                    float(row.get("alt_price") or 0),
                    float(row.get("pmay_price") or 0),
                    float(row.get("other_maker_price") or 0),
                    str(row.get("supplier") or ""),
                    str(row.get("order_type") or DEFAULT_ORDER_TYPE),
                    str(row.get("spec") or ""),
                    str(row.get("quotation_spec") or ""),
                )
                for i, row in enumerate(rows)
            ],
        )
        conn.execute("UPDATE houses SET updated_at = %s WHERE id = %s", (_now(), house_id))


def get_mapping_rows(house_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT room, item_name, quantity, unit_price, alt_price, pmay_price,
                   other_maker_price, supplier, order_type, spec, quotation_spec
            FROM mapping_rows WHERE house_id = %s ORDER BY position
            """,
            (house_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------- quotations --

def save_quotation_texts(house_id: str, texts_by_bucket: dict[str, str]) -> None:
    with get_conn() as conn:
        for bucket, text in texts_by_bucket.items():
            conn.execute(
                """
                INSERT INTO quotation_texts (house_id, bucket_label, raw_text)
                VALUES (%s, %s, %s)
                ON CONFLICT (house_id, bucket_label) DO UPDATE SET raw_text = excluded.raw_text
                """,
                (house_id, bucket, text),
            )


def get_quotation_texts(house_id: str) -> dict[str, str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT bucket_label, raw_text FROM quotation_texts WHERE house_id = %s",
            (house_id,),
        ).fetchall()
    return {r["bucket_label"]: r["raw_text"] for r in rows}


def save_quotation_pdfs(house_id: str, bucket: str, files: list[tuple[str, bytes]]) -> None:
    """Replaces all previously-stored PDFs for this one bucket."""
    with get_conn() as conn:
        _lock_house(conn, house_id)
        conn.execute(
            "DELETE FROM quotation_pdfs WHERE house_id = %s AND bucket_label = %s",
            (house_id, bucket),
        )
        conn.cursor().executemany(
            "INSERT INTO quotation_pdfs (house_id, bucket_label, filename, pdf_bytes) VALUES (%s, %s, %s, %s)",
            [(house_id, bucket, filename, data) for filename, data in files],
        )


def get_quotation_pdfs_meta(house_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, bucket_label, filename FROM quotation_pdfs WHERE house_id = %s ORDER BY bucket_label, id",
            (house_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_quotation_pdf_bytes(pdf_id: int) -> tuple[str, bytes] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT filename, pdf_bytes FROM quotation_pdfs WHERE id = %s", (pdf_id,)
        ).fetchone()
    return (row["filename"], bytes(row["pdf_bytes"])) if row else None


# --------------------------------------------------------- contract (สัญญา) --

def replace_contract_attachments(house_id: str, items: list[dict[str, Any]]) -> None:
    """Replaces all attachment pages for this house — each item is
    {title, image_bytes, content_type, attachment_type, floor, zone,
    item_range, reference_note}, in the display order they should appear in
    the final contract PDF."""
    with get_conn() as conn:
        _lock_house(conn, house_id)
        conn.execute("DELETE FROM contract_attachments WHERE house_id = %s", (house_id,))
        conn.cursor().executemany(
            """
            INSERT INTO contract_attachments
                (house_id, position, title, image_bytes, content_type,
                 attachment_type, floor, zone, item_range, reference_note)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    house_id,
                    i,
                    str(item.get("title") or ""),
                    item["image_bytes"],
                    str(item.get("content_type") or "image/png"),
                    str(item.get("attachment_type") or ""),
                    str(item.get("floor") or ""),
                    str(item.get("zone") or ""),
                    str(item.get("item_range") or ""),
                    str(item.get("reference_note") or ""),
                )
                for i, item in enumerate(items)
            ],
        )
        conn.execute("UPDATE houses SET updated_at = %s WHERE id = %s", (_now(), house_id))


def get_contract_attachments_meta(house_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, position, title, attachment_type, floor, zone, item_range, reference_note
            FROM contract_attachments WHERE house_id = %s ORDER BY position
            """,
            (house_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_contract_attachment_bytes(attachment_id: int) -> tuple[bytes, str] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT image_bytes, content_type FROM contract_attachments WHERE id = %s", (attachment_id,)
        ).fetchone()
    if row is None:
        return None
    return (bytes(row["image_bytes"]), row["content_type"] or "image/png")


def get_contract_attachments_full(house_id: str) -> list[dict[str, Any]]:
    """Ordered attachment rows (title, image_bytes, and remark metadata) for
    building the combined contract PDF."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT title, image_bytes, attachment_type, floor, zone, item_range, reference_note
            FROM contract_attachments WHERE house_id = %s ORDER BY position
            """,
            (house_id,),
        ).fetchall()
    return [
        {
            "title": r["title"],
            "image_bytes": bytes(r["image_bytes"]),
            "attachment_type": r["attachment_type"],
            "floor": r["floor"],
            "zone": r["zone"],
            "item_range": r["item_range"],
            "reference_note": r["reference_note"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------- exports --
# History of every completed export, so a house can be "saved" across many
# timestamped versions and any past one re-downloaded later, instead of only
# ever keeping the single latest file (which is still tracked separately on
# `houses.final_excel_bytes` for the cheap stale-download check).

def save_export_version(house_id: str, filename: str, file_bytes: bytes, source_hash: str) -> dict[str, Any]:
    now = _now()
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO export_versions (house_id, filename, excel_bytes, source_hash, created_at)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (house_id, filename, file_bytes, source_hash, now),
        )
        version_id = cur.fetchone()["id"]
    return {"id": version_id, "filename": filename, "created_at": now}


def list_export_versions(house_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, filename, created_at FROM export_versions WHERE house_id = %s ORDER BY created_at DESC",
            (house_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_export_version_bytes(version_id: int) -> tuple[str, bytes] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT filename, excel_bytes FROM export_versions WHERE id = %s", (version_id,)
        ).fetchone()
    return (row["filename"], bytes(row["excel_bytes"])) if row else None


# ------------------------------------------------------------- app settings --

def set_company_logo(logo_bytes: bytes, content_type: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (id, company_logo_bytes, company_logo_content_type)
            VALUES (1, %s, %s)
            ON CONFLICT (id) DO UPDATE SET company_logo_bytes = excluded.company_logo_bytes,
                                            company_logo_content_type = excluded.company_logo_content_type
            """,
            (logo_bytes, content_type),
        )


def get_company_logo() -> tuple[bytes, str] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT company_logo_bytes, company_logo_content_type FROM app_settings WHERE id = 1"
        ).fetchone()
    if row is None or row["company_logo_bytes"] is None:
        return None
    return (bytes(row["company_logo_bytes"]), row["company_logo_content_type"] or "image/png")


# ------------------------------------------------------------- user avatars --

def set_user_avatar(user_id: str, avatar_bytes: bytes, content_type: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO user_avatars (user_id, avatar_bytes, avatar_content_type)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET avatar_bytes = excluded.avatar_bytes,
                                                 avatar_content_type = excluded.avatar_content_type
            """,
            (user_id, avatar_bytes, content_type),
        )


def get_user_avatar(user_id: str) -> tuple[bytes, str] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT avatar_bytes, avatar_content_type FROM user_avatars WHERE user_id = %s", (user_id,)
        ).fetchone()
    if row is None:
        return None
    return (bytes(row["avatar_bytes"]), row["avatar_content_type"] or "image/png")
