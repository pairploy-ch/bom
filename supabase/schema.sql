-- SSK The Cat Workspace — Supabase (Postgres) schema.
--
-- This is a straight structural port of backend/app/db.py's SQLite schema
-- (same tables, same columns, same relationships) so the backend's logic —
-- create_project(), replace_furniture_items(), replace_mapping_rows(), etc.
-- — can be repointed here later without changing its shape of data.
--
-- How to apply:
--   1. Create a project at https://supabase.com/dashboard.
--   2. Open SQL Editor -> New query, paste this whole file, Run.
--      (Or, with the Supabase CLI linked to the project: `supabase db push`.)
--   3. Grab the Project URL + service_role key from Project Settings -> API
--      and drop them into backend/.env when you're ready to switch the
--      backend from SQLite to this database.

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------- projects --

create table if not exists projects (
    id                          uuid primary key default gen_random_uuid(),
    name                        text not null unique,
    excel_filename              text,
    excel_bytes                 bytea,
    sheet_names                 jsonb,
    target_sheet_name           text,
    alt_batch_info              jsonb,
    baseline_furniture_value    double precision,
    final_excel_bytes           bytea,
    final_excel_source_hash     text,
    created_at                  timestamptz not null default now(),
    updated_at                  timestamptz not null default now()
);

-- -------------------------------------------------------------- furniture --

create table if not exists furniture_items (
    id          bigint generated always as identity primary key,
    project_id  uuid not null references projects(id) on delete cascade,
    position    integer not null,
    room        text not null default '',
    item_name   text not null default '',
    quantity    double precision not null default 1,
    verified    boolean not null default true,
    spec        text not null default ''
);

create index if not exists idx_furniture_project on furniture_items(project_id, position);

-- ---------------------------------------------------------------- mapping --

create table if not exists mapping_rows (
    id                  bigint generated always as identity primary key,
    project_id          uuid not null references projects(id) on delete cascade,
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

create index if not exists idx_mapping_project on mapping_rows(project_id, position);

-- ------------------------------------------------------------- quotations --

create table if not exists quotation_texts (
    project_id      uuid not null references projects(id) on delete cascade,
    bucket_label    text not null,
    raw_text        text not null,
    primary key (project_id, bucket_label)
);

create table if not exists quotation_pdfs (
    id              bigint generated always as identity primary key,
    project_id      uuid not null references projects(id) on delete cascade,
    bucket_label    text not null,
    filename        text not null,
    pdf_bytes       bytea not null
);

create index if not exists idx_pdfs_project on quotation_pdfs(project_id, bucket_label);

-- ------------------------------------------------------------------ exports --
-- One row per completed export, so a project keeps a full, timestamped
-- history instead of only the latest file (still tracked separately on
-- projects.final_excel_bytes/final_excel_source_hash for the stale-check).

create table if not exists export_versions (
    id              bigint generated always as identity primary key,
    project_id      uuid not null references projects(id) on delete cascade,
    filename        text not null,
    excel_bytes     bytea not null,
    source_hash     text,
    created_at      timestamptz not null default now()
);

create index if not exists idx_export_versions_project on export_versions(project_id, created_at);

-- --------------------------------------------------------------------- RLS --
-- Supabase exposes every public-schema table over its REST API by default.
-- The backend is meant to talk to Postgres with the service_role key (which
-- bypasses RLS), so RLS is enabled here with *no* policies attached — that
-- makes every table unreachable via the anon/public API key. If you later
-- want the Next.js frontend to query Supabase directly (instead of through
-- FastAPI) using the anon key, add explicit policies per table then.

alter table projects         enable row level security;
alter table furniture_items  enable row level security;
alter table mapping_rows     enable row level security;
alter table quotation_texts  enable row level security;
alter table quotation_pdfs   enable row level security;
alter table export_versions  enable row level security;
