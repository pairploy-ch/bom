"""
Persistence layer — SQLite, normalized into real tables instead of the
original app's single `projects` table with `furniture_list` / `final_mapping`
dumped in as JSON blobs (see save_project_state() / load_project_state() in
the original app.py). This is what makes partial edits (one row, one field)
cheap, and is the actual fix for "state management" — a browser refresh (or
a totally different browser) just re-fetches from here; nothing ever lived
only in a Streamlit session.

A fresh sqlite3 connection is opened per call (mirrors the original's own
`_get_db_connection()` pattern) — simple, and avoids any cross-thread
connection-reuse pitfalls under FastAPI's async request handling.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import DB_PATH

DEFAULT_ORDER_TYPE = "จัดซื้อ (ราคาจริง ไม่บวกกำไร)"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextlib.contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Adds `column` to `table` if it's missing — lets older DB files on disk
    pick up new fields without needing a real migration tool."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                excel_filename TEXT,
                excel_bytes BLOB,
                sheet_names TEXT,             -- JSON array
                target_sheet_name TEXT,
                alt_batch_info TEXT,          -- JSON object or NULL
                baseline_furniture_value REAL,
                final_excel_bytes BLOB,       -- last exported file (for the "stale download" check)
                final_excel_source_hash TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS furniture_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                room TEXT NOT NULL DEFAULT '',
                item_name TEXT NOT NULL DEFAULT '',
                quantity REAL NOT NULL DEFAULT 1,
                verified INTEGER NOT NULL DEFAULT 0,
                spec TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_furniture_project ON furniture_items(project_id, position);

            CREATE TABLE IF NOT EXISTS mapping_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                room TEXT NOT NULL DEFAULT '',
                item_name TEXT NOT NULL DEFAULT '',
                quantity REAL NOT NULL DEFAULT 1,
                unit_price REAL NOT NULL DEFAULT 0,
                alt_price REAL NOT NULL DEFAULT 0,
                pmay_price REAL NOT NULL DEFAULT 0,
                other_maker_price REAL NOT NULL DEFAULT 0,
                supplier TEXT NOT NULL DEFAULT '',
                order_type TEXT NOT NULL DEFAULT '{default_order_type}',
                spec TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_mapping_project ON mapping_rows(project_id, position);

            CREATE TABLE IF NOT EXISTS quotation_texts (
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                bucket_label TEXT NOT NULL,
                raw_text TEXT NOT NULL,
                PRIMARY KEY (project_id, bucket_label)
            );

            CREATE TABLE IF NOT EXISTS quotation_pdfs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                bucket_label TEXT NOT NULL,
                filename TEXT NOT NULL,
                pdf_bytes BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pdfs_project ON quotation_pdfs(project_id, bucket_label);

            -- One row per completed export, so past versions stay downloadable
            -- (kept separate from projects.final_excel_bytes, which is just a
            -- convenience pointer to the LATEST version for the stale-check).
            CREATE TABLE IF NOT EXISTS export_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                excel_bytes BLOB NOT NULL,
                source_hash TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_export_versions_project ON export_versions(project_id, created_at);

            -- Single global row (id=1) — the company logo used on every
            -- generated quotation PDF/Word doc, uploaded once and reused,
            -- not tied to any one project.
            CREATE TABLE IF NOT EXISTS app_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                company_logo_bytes BLOB,
                company_logo_content_type TEXT
            );
            """.format(default_order_type=DEFAULT_ORDER_TYPE)
        )
        # Backfills columns added after a DB file may already exist on disk.
        _ensure_column(conn, "furniture_items", "spec", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "mapping_rows", "spec", "TEXT NOT NULL DEFAULT ''")


# ---------------------------------------------------------------- projects --

def create_project(name: str) -> dict[str, Any]:
    project_id = uuid.uuid4().hex
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (project_id, name, now, now),
        )
    return {"id": project_id, "name": name, "created_at": now, "updated_at": now}


def touch_project(project_id: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (_now(), project_id))


def list_projects() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, updated_at FROM projects ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_project_row(project_id: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()


def get_project_by_name(name: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()


def delete_project(project_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


def reset_project(project_id: str) -> None:
    """
    Wipes everything under this project back to a just-created state — same
    id/name (so the URL and project-list entry stay put), but the template,
    furniture list, mapping rows, quotation texts/PDFs, and any exports are
    all cleared. The equivalent of the original app's "Reset All", which
    wiped the whole Streamlit session.
    """
    with get_conn() as conn:
        conn.execute("DELETE FROM furniture_items WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM mapping_rows WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM quotation_texts WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM quotation_pdfs WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM export_versions WHERE project_id = ?", (project_id,))
        conn.execute(
            """
            UPDATE projects
            SET excel_filename = NULL,
                excel_bytes = NULL,
                sheet_names = NULL,
                target_sheet_name = NULL,
                alt_batch_info = NULL,
                baseline_furniture_value = NULL,
                final_excel_bytes = NULL,
                final_excel_source_hash = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (_now(), project_id),
        )


def update_excel_template(project_id: str, filename: str, file_bytes: bytes, sheet_names: list[str]) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE projects
            SET excel_filename = ?, excel_bytes = ?, sheet_names = ?, updated_at = ?
            WHERE id = ?
            """,
            (filename, file_bytes, json.dumps(sheet_names), _now(), project_id),
        )


def update_excel_bytes(project_id: str, file_bytes: bytes) -> None:
    """Used after an in-place edit to the workbook itself (Loading Factor auto-apply)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE projects SET excel_bytes = ?, updated_at = ? WHERE id = ?",
            (file_bytes, _now(), project_id),
        )


def update_target_sheet(project_id: str, sheet_name: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE projects SET target_sheet_name = ?, updated_at = ? WHERE id = ?",
            (sheet_name, _now(), project_id),
        )


def update_alt_batch_info(project_id: str, alt_batch_info: dict[str, Any] | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE projects SET alt_batch_info = ?, updated_at = ? WHERE id = ?",
            (json.dumps(alt_batch_info) if alt_batch_info else None, _now(), project_id),
        )


def update_baseline_furniture_value(project_id: str, value: float | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE projects SET baseline_furniture_value = ?, updated_at = ? WHERE id = ?",
            (value, _now(), project_id),
        )


def update_final_export(project_id: str, file_bytes: bytes, source_hash: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE projects SET final_excel_bytes = ?, final_excel_source_hash = ?, updated_at = ? WHERE id = ?",
            (file_bytes, source_hash, _now(), project_id),
        )


# --------------------------------------------------------------- furniture --

def replace_furniture_items(project_id: str, items: list[dict[str, Any]]) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM furniture_items WHERE project_id = ?", (project_id,))
        conn.executemany(
            """
            INSERT INTO furniture_items (project_id, position, room, item_name, quantity, verified, spec)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    project_id,
                    i,
                    str(item.get("room") or ""),
                    str(item.get("item_name") or ""),
                    float(item.get("quantity") or 0),
                    1 if item.get("verified") else 0,
                    str(item.get("spec") or ""),
                )
                for i, item in enumerate(items)
            ],
        )
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (_now(), project_id))


def get_furniture_items(project_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT room, item_name, quantity, verified, spec FROM furniture_items "
            "WHERE project_id = ? ORDER BY position",
            (project_id,),
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

def replace_mapping_rows(project_id: str, rows: list[dict[str, Any]]) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM mapping_rows WHERE project_id = ?", (project_id,))
        conn.executemany(
            """
            INSERT INTO mapping_rows
                (project_id, position, room, item_name, quantity, unit_price,
                 alt_price, pmay_price, other_maker_price, supplier, order_type, spec)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    project_id,
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
                )
                for i, row in enumerate(rows)
            ],
        )
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (_now(), project_id))


def get_mapping_rows(project_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT room, item_name, quantity, unit_price, alt_price, pmay_price,
                   other_maker_price, supplier, order_type, spec
            FROM mapping_rows WHERE project_id = ? ORDER BY position
            """,
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------- quotations --

def save_quotation_texts(project_id: str, texts_by_bucket: dict[str, str]) -> None:
    with get_conn() as conn:
        for bucket, text in texts_by_bucket.items():
            conn.execute(
                """
                INSERT INTO quotation_texts (project_id, bucket_label, raw_text)
                VALUES (?, ?, ?)
                ON CONFLICT(project_id, bucket_label) DO UPDATE SET raw_text = excluded.raw_text
                """,
                (project_id, bucket, text),
            )


def get_quotation_texts(project_id: str) -> dict[str, str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT bucket_label, raw_text FROM quotation_texts WHERE project_id = ?",
            (project_id,),
        ).fetchall()
    return {r["bucket_label"]: r["raw_text"] for r in rows}


def save_quotation_pdfs(project_id: str, bucket: str, files: list[tuple[str, bytes]]) -> None:
    """Replaces all previously-stored PDFs for this one bucket."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM quotation_pdfs WHERE project_id = ? AND bucket_label = ?",
            (project_id, bucket),
        )
        conn.executemany(
            "INSERT INTO quotation_pdfs (project_id, bucket_label, filename, pdf_bytes) VALUES (?, ?, ?, ?)",
            [(project_id, bucket, filename, data) for filename, data in files],
        )


def get_quotation_pdfs_meta(project_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, bucket_label, filename FROM quotation_pdfs WHERE project_id = ? ORDER BY bucket_label, id",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_quotation_pdf_bytes(pdf_id: int) -> tuple[str, bytes] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT filename, pdf_bytes FROM quotation_pdfs WHERE id = ?", (pdf_id,)
        ).fetchone()
    return (row["filename"], row["pdf_bytes"]) if row else None


# ---------------------------------------------------------------- exports --
# History of every completed export, so a project can be "saved" across many
# timestamped versions and any past one re-downloaded later, instead of only
# ever keeping the single latest file (which is still tracked separately on
# `projects.final_excel_bytes` for the cheap stale-download check).

def save_export_version(project_id: str, filename: str, file_bytes: bytes, source_hash: str) -> dict[str, Any]:
    now = _now()
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO export_versions (project_id, filename, excel_bytes, source_hash, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (project_id, filename, file_bytes, source_hash, now),
        )
        version_id = cur.lastrowid
    return {"id": version_id, "filename": filename, "created_at": now}


def list_export_versions(project_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, filename, created_at FROM export_versions WHERE project_id = ? ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_export_version_bytes(version_id: int) -> tuple[str, bytes] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT filename, excel_bytes FROM export_versions WHERE id = ?", (version_id,)
        ).fetchone()
    return (row["filename"], row["excel_bytes"]) if row else None


# ------------------------------------------------------------- app settings --

def set_company_logo(logo_bytes: bytes, content_type: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (id, company_logo_bytes, company_logo_content_type)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET company_logo_bytes = excluded.company_logo_bytes,
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
    return (row["company_logo_bytes"], row["company_logo_content_type"] or "image/png")
