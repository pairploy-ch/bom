# Supabase schema

`schema.sql` mirrors `backend/app/db.py`'s own `CREATE TABLE` statements —
the two levels of entity (`projects` grouping many `houses`), every
house-scoped table (`furniture_items`, `mapping_rows`, `quotation_texts`,
`quotation_pdfs`, `export_versions`), plus `app_settings` (company logo) and
`user_avatars` (profile pictures).

## This is the live database, not a draft

The backend connects here directly over Postgres (via `psycopg`, using
`DATABASE_URL`) — **not** through Supabase's REST API, and not with the
`service_role` key. `db.py`'s own `init_db()` applies this same DDL
idempotently on every backend startup, so running `schema.sql` by hand is
optional — it's kept here as a readable reference, and for applying it
manually before the first deploy if you'd rather not wait for the backend
to do it.

## Setup

1. Supabase dashboard → **Project Settings → Database → Connection string**
   → copy the **Transaction pooler** string (port `6543`) — not the direct
   connection. The pooler handles connection pooling on Supabase's side,
   which matters since the backend opens a fresh connection per request.
2. Fill in the password placeholder in that string with your database
   password (Project Settings → Database → reset if you don't have it).
3. Put the result in `backend/.env` as `DATABASE_URL=postgresql://...`.

That's it — no `schema.sql` step required, `init_db()` creates everything
on first startup.

## Notes

- **RLS is enabled with no policies** on every table. The backend's
  `DATABASE_URL` connection uses the `postgres` role, which owns these
  tables and bypasses RLS regardless — so this only serves its actual
  purpose: blocking the anon/public REST API from touching this data
  directly. Add policies later only if the Next.js app should query
  Supabase directly instead of through FastAPI.
- IDs (`projects.id`, `houses.id`) are app-generated `uuid4().hex` strings
  stored as `text`, not Postgres `uuid` — matches what `db.py` already
  generates, no conversion needed. Same reasoning for `created_at`/
  `updated_at` staying `text` (ISO strings) instead of `timestamptz`.
- **`excel_bytes` / `final_excel_bytes` / `pdf_bytes` / `avatar_bytes` /
  `logo_bytes` are `bytea`** — fine at this app's file sizes (Excel
  templates, PDFs, images); if that ever changes, moving those to a
  [Supabase Storage](https://supabase.com/docs/guides/storage) bucket and
  keeping just the object path in the row is the more scalable option.
