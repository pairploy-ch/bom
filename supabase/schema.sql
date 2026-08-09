-- PM Workspace — Supabase (Postgres) schema.
--
-- Mirrors backend/app/db.py's CREATE TABLE statements exactly (same tables,
-- columns, relationships) — db.py's own init_db() also applies this DDL
-- idempotently on every backend startup, so running this file by hand is
-- optional, not required. It's kept here as a readable reference and for
-- applying it manually before the first deploy if you'd rather not wait for
-- the backend to do it.
--
-- How to apply (optional, see above):
--   1. Open your Supabase project's SQL Editor -> New query.
--   2. Paste this whole file, Run.
--
-- Two levels of entity: a project (e.g. "10DK") groups many houses (each
-- house is one full BOM: template -> furniture list -> price matching ->
-- quotation). IDs are app-generated uuid4 hex strings (not Postgres
-- `uuid`/`gen_random_uuid()`) and timestamps are ISO-format text (not
-- `timestamptz`) — both match exactly what backend/app/db.py already
-- writes, so no Python-side conversion is needed on either side.

-- ---------------------------------------------------------------- projects --
-- Top-level grouping (e.g. "10DK") — holds many houses.

create table if not exists projects (
    id                  text primary key,
    name                text not null unique,
    logo_bytes          bytea,
    logo_content_type   text,
    created_at          text not null,
    updated_at          text not null
);

-- ------------------------------------------------------------------ houses --
-- One house = one full BOM workflow (template -> furniture list -> price
-- matching -> quotation) — what this schema used to call a "project" before
-- the grouping above existed.

create table if not exists houses (
    id                          text primary key,
    project_id                  text not null references projects(id) on delete cascade,
    name                        text not null,
    excel_filename              text,
    excel_bytes                 bytea,
    sheet_names                 text,  -- JSON array
    target_sheet_name           text,
    alt_batch_info              text,  -- JSON object or NULL
    baseline_furniture_value    double precision,
    final_excel_bytes           bytea, -- last exported file (for the "stale download" check)
    final_excel_source_hash     text,
    created_at                  text not null,
    updated_at                  text not null
);

create index if not exists idx_houses_project on houses(project_id, updated_at);

-- -------------------------------------------------------------- furniture --

create table if not exists furniture_items (
    id          bigint generated always as identity primary key,
    house_id    text not null references houses(id) on delete cascade,
    position    integer not null,
    room        text not null default '',
    item_name   text not null default '',
    quantity    double precision not null default 1,
    verified    boolean not null default false,
    spec        text not null default ''
);

create index if not exists idx_furniture_house on furniture_items(house_id, position);

-- ---------------------------------------------------------------- mapping --

create table if not exists mapping_rows (
    id                  bigint generated always as identity primary key,
    house_id            text not null references houses(id) on delete cascade,
    position            integer not null,
    room                text not null default '',
    item_name           text not null default '',
    quantity            double precision not null default 1,
    unit_price          double precision not null default 0,
    alt_price           double precision not null default 0,
    pmay_price          double precision not null default 0,
    other_maker_price   double precision not null default 0,
    supplier            text not null default '',
    order_type          text not null default 'จัดซื้อ (ราคาจริง ไม่บวกกำไร)',
    spec                text not null default ''
);

create index if not exists idx_mapping_house on mapping_rows(house_id, position);

-- ------------------------------------------------------------- quotations --

create table if not exists quotation_texts (
    house_id        text not null references houses(id) on delete cascade,
    bucket_label    text not null,
    raw_text        text not null,
    primary key (house_id, bucket_label)
);

create table if not exists quotation_pdfs (
    id              bigint generated always as identity primary key,
    house_id        text not null references houses(id) on delete cascade,
    bucket_label    text not null,
    filename        text not null,
    pdf_bytes       bytea not null
);

create index if not exists idx_pdfs_house on quotation_pdfs(house_id, bucket_label);

-- ------------------------------------------------------------------ exports --
-- One row per completed export, so a house keeps a full, timestamped
-- history instead of only the latest file (still tracked separately on
-- houses.final_excel_bytes/final_excel_source_hash for the stale-check).

create table if not exists export_versions (
    id              bigint generated always as identity primary key,
    house_id        text not null references houses(id) on delete cascade,
    filename        text not null,
    excel_bytes     bytea not null,
    source_hash     text,
    created_at      text not null
);

create index if not exists idx_export_versions_house on export_versions(house_id, created_at);

-- ------------------------------------------------------------- app settings --
-- Single global row (id=1) — the company logo used on every generated
-- quotation PDF/Word doc, uploaded once and reused, not tied to any one
-- project/house.

create table if not exists app_settings (
    id                          integer primary key check (id = 1),
    company_logo_bytes          bytea,
    company_logo_content_type   text
);

-- ------------------------------------------------------------- user avatars --
-- Avatar images for the profile menu, keyed by Supabase Auth user id.
-- Display name lives in Supabase's own user_metadata, not here.

create table if not exists user_avatars (
    user_id                 text primary key,
    avatar_bytes             bytea not null,
    avatar_content_type      text not null
);

-- --------------------------------------------------------------------- RLS --
-- Supabase exposes every public-schema table over its REST API by default.
-- The backend connects with the `postgres` role (which owns these tables
-- and bypasses RLS), so RLS is enabled here with *no* policies attached —
-- that makes every table unreachable via the anon/public API key. If you
-- later want the Next.js frontend to query Supabase directly (instead of
-- through FastAPI) using the anon key, add explicit policies per table then.

alter table projects          enable row level security;
alter table houses            enable row level security;
alter table furniture_items   enable row level security;
alter table mapping_rows      enable row level security;
alter table quotation_texts   enable row level security;
alter table quotation_pdfs    enable row level security;
alter table export_versions   enable row level security;
alter table app_settings      enable row level security;
alter table user_avatars      enable row level security;
