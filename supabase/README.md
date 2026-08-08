# Supabase schema

`schema.sql` is a structural port of the backend's current SQLite schema
(`backend/app/db.py`) to Postgres — same 5 tables, same relationships
(`ON DELETE CASCADE` from every child table back to `projects`), just with
Postgres-native types (`uuid`, `jsonb`, `timestamptz`, `bytea`, identity
columns instead of `AUTOINCREMENT`).

## Setup

1. Create a project at [supabase.com/dashboard](https://supabase.com/dashboard).
2. **SQL Editor → New query**, paste the contents of `schema.sql`, **Run**.
3. From **Project Settings → API**, copy the Project URL and the
   `service_role` key (not `anon` — the backend needs to bypass RLS) into
   `backend/.env`.

## Notes

- **RLS is enabled with no policies** on every table, so the tables are
  unreachable through the anon/public API key — only the `service_role` key
  (server-side only, never ship it to the frontend) can read/write. Add
  policies later only if the Next.js app should query Supabase directly.
- **`excel_bytes` / `final_excel_bytes` / `pdf_bytes` are `bytea`** to match
  the original SQLite `BLOB` columns exactly. Postgres/PostgREST round-trips
  `bytea` as hex-encoded text, which roughly doubles transfer size — fine for
  the Excel-sized files here, but if PDFs grow large, moving those to a
  [Supabase Storage](https://supabase.com/docs/guides/storage) bucket and
  keeping just the object path in the row is the more scalable option.
- Nothing in `backend/app/` reads from this yet — it still runs on SQLite
  (`backend/bom_backend.db`). Once you've added the keys, say so and the
  persistence layer (`backend/app/db.py`) can be swapped to talk to this
  instead, keeping the same function signatures so nothing above it changes.
