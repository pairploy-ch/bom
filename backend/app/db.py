"""
Persistence layer — SQLite, normalized into real tables instead of the
original app's single `projects` table with `furniture_list` / `final_mapping`
dumped in as JSON blobs (see save_project_state() / load_project_state() in
the original app.py). This is what makes partial edits (one row, one field)
cheap, and is the actual fix for "state management" — a browser refresh (or
a totally different browser) just re-fetches from here; nothing ever lived
only in a Streamlit session.

Two levels of entity: a **project** (e.g. "10DK") groups many **houses**
(each house is one full BOM: template → furniture list → price matching →
quotation — what this file used to call a "project" before that grouping
existed, see _migrate_legacy_schema()).

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


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _migrate_legacy_schema(conn: sqlite3.Connection) -> None:
    """
    One-time upgrade for DB files created before the Project→House split:
    back then, the table now called `houses` was named `projects` and had no
    `project_id` column, and every house-scoped table (furniture_items,
    mapping_rows, quotation_texts, quotation_pdfs, export_versions) FK'd to
    it via a `project_id` column. Detects that old shape and rewrites it in
    place — a fresh DB (or one already migrated) hits neither branch below
    and this is a no-op.
    """
    if _table_exists(conn, "houses"):
        return  # already migrated (or created fresh in the final shape below)
    if not _table_exists(conn, "projects"):
        return  # fresh DB — the executescript in init_db() creates everything directly

    conn.execute("ALTER TABLE projects RENAME TO houses")

    now = _now()
    default_project_id = uuid.uuid4().hex
    conn.execute(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            logo_bytes BLOB,
            logo_content_type TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (default_project_id, "10DK", now, now),
    )

    conn.execute("ALTER TABLE houses ADD COLUMN project_id TEXT REFERENCES projects(id)")
    conn.execute("UPDATE houses SET project_id = ?", (default_project_id,))

    for table in ("furniture_items", "mapping_rows", "quotation_texts", "quotation_pdfs", "export_versions"):
        conn.execute(f"ALTER TABLE {table} RENAME COLUMN project_id TO house_id")


def init_db() -> None:
    with get_conn() as conn:
        _migrate_legacy_schema(conn)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                logo_bytes BLOB,
                logo_content_type TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS houses (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
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
            CREATE INDEX IF NOT EXISTS idx_houses_project ON houses(project_id, updated_at);

            CREATE TABLE IF NOT EXISTS furniture_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                house_id TEXT NOT NULL REFERENCES houses(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                room TEXT NOT NULL DEFAULT '',
                item_name TEXT NOT NULL DEFAULT '',
                quantity REAL NOT NULL DEFAULT 1,
                verified INTEGER NOT NULL DEFAULT 0,
                spec TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_furniture_house ON furniture_items(house_id, position);

            CREATE TABLE IF NOT EXISTS mapping_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                house_id TEXT NOT NULL REFERENCES houses(id) ON DELETE CASCADE,
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
            CREATE INDEX IF NOT EXISTS idx_mapping_house ON mapping_rows(house_id, position);

            CREATE TABLE IF NOT EXISTS quotation_texts (
                house_id TEXT NOT NULL REFERENCES houses(id) ON DELETE CASCADE,
                bucket_label TEXT NOT NULL,
                raw_text TEXT NOT NULL,
                PRIMARY KEY (house_id, bucket_label)
            );

            CREATE TABLE IF NOT EXISTS quotation_pdfs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                house_id TEXT NOT NULL REFERENCES houses(id) ON DELETE CASCADE,
                bucket_label TEXT NOT NULL,
                filename TEXT NOT NULL,
                pdf_bytes BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pdfs_house ON quotation_pdfs(house_id, bucket_label);

            -- One row per completed export, so a house can be "saved" across many
            -- timestamped versions and any past one re-downloaded later, instead of only
            -- ever keeping the single latest file (which is still tracked separately on
            -- `houses.final_excel_bytes` for the cheap stale-check).
            CREATE TABLE IF NOT EXISTS export_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                house_id TEXT NOT NULL REFERENCES houses(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                excel_bytes BLOB NOT NULL,
                source_hash TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_export_versions_house ON export_versions(house_id, created_at);

            -- Single global row (id=1) — the company logo used on every
            -- generated quotation PDF/Word doc, uploaded once and reused,
            -- not tied to any one project/house.
            CREATE TABLE IF NOT EXISTS app_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                company_logo_bytes BLOB,
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
                avatar_bytes BLOB NOT NULL,
                avatar_content_type TEXT NOT NULL
            );
            """.format(default_order_type=DEFAULT_ORDER_TYPE)
        )
        # Backfills columns added after a DB file may already exist on disk.
        _ensure_column(conn, "furniture_items", "spec", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "mapping_rows", "spec", "TEXT NOT NULL DEFAULT ''")


# ----------------------------------------------------------------- projects --
# Top-level grouping (e.g. "10DK") — holds many houses. See module docstring.

def create_project(name: str) -> dict[str, Any]:
    project_id = uuid.uuid4().hex
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
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


def get_project_row(project_id: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()


def get_project_by_name(name: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()


def delete_project(project_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


def set_project_logo(project_id: str, logo_bytes: bytes, content_type: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE projects SET logo_bytes = ?, logo_content_type = ?, updated_at = ? WHERE id = ?",
            (logo_bytes, content_type, _now(), project_id),
        )


def get_project_logo(project_id: str) -> tuple[bytes, str] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT logo_bytes, logo_content_type FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
    if row is None or row["logo_bytes"] is None:
        return None
    return (row["logo_bytes"], row["logo_content_type"] or "image/png")


# ------------------------------------------------------------------- houses --

def create_house(project_id: str, name: str) -> dict[str, Any]:
    house_id = uuid.uuid4().hex
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO houses (id, project_id, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (house_id, project_id, name, now, now),
        )
    return {"id": house_id, "project_id": project_id, "name": name, "created_at": now, "updated_at": now}


def touch_house(house_id: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE houses SET updated_at = ? WHERE id = ?", (_now(), house_id))


def list_houses(project_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, updated_at FROM houses WHERE project_id = ? ORDER BY updated_at DESC",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_house_row(house_id: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM houses WHERE id = ?", (house_id,)).fetchone()


def get_house_by_name(project_id: str, name: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM houses WHERE project_id = ? AND name = ?", (project_id, name)
        ).fetchone()


def delete_house(house_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM houses WHERE id = ?", (house_id,))


def reset_house(house_id: str) -> None:
    """
    Wipes everything under this house back to a just-created state — same
    id/name (so the URL and house-list entry stay put), but the template,
    furniture list, mapping rows, quotation texts/PDFs, and any exports are
    all cleared. The equivalent of the original app's "Reset All", which
    wiped the whole Streamlit session.
    """
    with get_conn() as conn:
        conn.execute("DELETE FROM furniture_items WHERE house_id = ?", (house_id,))
        conn.execute("DELETE FROM mapping_rows WHERE house_id = ?", (house_id,))
        conn.execute("DELETE FROM quotation_texts WHERE house_id = ?", (house_id,))
        conn.execute("DELETE FROM quotation_pdfs WHERE house_id = ?", (house_id,))
        conn.execute("DELETE FROM export_versions WHERE house_id = ?", (house_id,))
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
                updated_at = ?
            WHERE id = ?
            """,
            (_now(), house_id),
        )


def update_excel_template(house_id: str, filename: str, file_bytes: bytes, sheet_names: list[str]) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE houses
            SET excel_filename = ?, excel_bytes = ?, sheet_names = ?, updated_at = ?
            WHERE id = ?
            """,
            (filename, file_bytes, json.dumps(sheet_names), _now(), house_id),
        )


def update_excel_bytes(house_id: str, file_bytes: bytes) -> None:
    """Used after an in-place edit to the workbook itself (Loading Factor auto-apply)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE houses SET excel_bytes = ?, updated_at = ? WHERE id = ?",
            (file_bytes, _now(), house_id),
        )


def update_target_sheet(house_id: str, sheet_name: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE houses SET target_sheet_name = ?, updated_at = ? WHERE id = ?",
            (sheet_name, _now(), house_id),
        )


def update_alt_batch_info(house_id: str, alt_batch_info: dict[str, Any] | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE houses SET alt_batch_info = ?, updated_at = ? WHERE id = ?",
            (json.dumps(alt_batch_info) if alt_batch_info else None, _now(), house_id),
        )


def update_baseline_furniture_value(house_id: str, value: float | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE houses SET baseline_furniture_value = ?, updated_at = ? WHERE id = ?",
            (value, _now(), house_id),
        )


def update_final_export(house_id: str, file_bytes: bytes, source_hash: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE houses SET final_excel_bytes = ?, final_excel_source_hash = ?, updated_at = ? WHERE id = ?",
            (file_bytes, source_hash, _now(), house_id),
        )


# --------------------------------------------------------------- furniture --

def replace_furniture_items(house_id: str, items: list[dict[str, Any]]) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM furniture_items WHERE house_id = ?", (house_id,))
        conn.executemany(
            """
            INSERT INTO furniture_items (house_id, position, room, item_name, quantity, verified, spec)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    house_id,
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
        conn.execute("UPDATE houses SET updated_at = ? WHERE id = ?", (_now(), house_id))


def get_furniture_items(house_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT room, item_name, quantity, verified, spec FROM furniture_items "
            "WHERE house_id = ? ORDER BY position",
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
        conn.execute("DELETE FROM mapping_rows WHERE house_id = ?", (house_id,))
        conn.executemany(
            """
            INSERT INTO mapping_rows
                (house_id, position, room, item_name, quantity, unit_price,
                 alt_price, pmay_price, other_maker_price, supplier, order_type, spec)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                )
                for i, row in enumerate(rows)
            ],
        )
        conn.execute("UPDATE houses SET updated_at = ? WHERE id = ?", (_now(), house_id))


def get_mapping_rows(house_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT room, item_name, quantity, unit_price, alt_price, pmay_price,
                   other_maker_price, supplier, order_type, spec
            FROM mapping_rows WHERE house_id = ? ORDER BY position
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
                VALUES (?, ?, ?)
                ON CONFLICT(house_id, bucket_label) DO UPDATE SET raw_text = excluded.raw_text
                """,
                (house_id, bucket, text),
            )


def get_quotation_texts(house_id: str) -> dict[str, str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT bucket_label, raw_text FROM quotation_texts WHERE house_id = ?",
            (house_id,),
        ).fetchall()
    return {r["bucket_label"]: r["raw_text"] for r in rows}


def save_quotation_pdfs(house_id: str, bucket: str, files: list[tuple[str, bytes]]) -> None:
    """Replaces all previously-stored PDFs for this one bucket."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM quotation_pdfs WHERE house_id = ? AND bucket_label = ?",
            (house_id, bucket),
        )
        conn.executemany(
            "INSERT INTO quotation_pdfs (house_id, bucket_label, filename, pdf_bytes) VALUES (?, ?, ?, ?)",
            [(house_id, bucket, filename, data) for filename, data in files],
        )


def get_quotation_pdfs_meta(house_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, bucket_label, filename FROM quotation_pdfs WHERE house_id = ? ORDER BY bucket_label, id",
            (house_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_quotation_pdf_bytes(pdf_id: int) -> tuple[str, bytes] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT filename, pdf_bytes FROM quotation_pdfs WHERE id = ?", (pdf_id,)
        ).fetchone()
    return (row["filename"], row["pdf_bytes"]) if row else None


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
            VALUES (?, ?, ?, ?, ?)
            """,
            (house_id, filename, file_bytes, source_hash, now),
        )
        version_id = cur.lastrowid
    return {"id": version_id, "filename": filename, "created_at": now}


def list_export_versions(house_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, filename, created_at FROM export_versions WHERE house_id = ? ORDER BY created_at DESC",
            (house_id,),
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


# ------------------------------------------------------------- user avatars --

def set_user_avatar(user_id: str, avatar_bytes: bytes, content_type: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO user_avatars (user_id, avatar_bytes, avatar_content_type)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET avatar_bytes = excluded.avatar_bytes,
                                                avatar_content_type = excluded.avatar_content_type
            """,
            (user_id, avatar_bytes, content_type),
        )


def get_user_avatar(user_id: str) -> tuple[bytes, str] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT avatar_bytes, avatar_content_type FROM user_avatars WHERE user_id = ?", (user_id,)
        ).fetchone()
    if row is None:
        return None
    return (row["avatar_bytes"], row["avatar_content_type"] or "image/png")
