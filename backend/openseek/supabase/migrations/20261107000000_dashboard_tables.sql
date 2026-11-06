-- Dashboard state used by every profile that serves the browser dashboard.
-- These two tables exist in supabase/schema.sql, the documented copy path; the CLI
-- migration path must produce the same schema or a wizard-created node lacks them.

create table if not exists public.pool_owner_settings (
  k text primary key,
  v text not null
);

create table if not exists public.pool_generation_jobs (
  generation_id text primary key,
  state text not null,
  fp text,
  profile text,
  duration_ms bigint,
  tiles_done integer not null default 0,
  tiles_total integer,
  upload_id text,
  error text,
  package_sha256 text,
  created double precision not null,
  updated double precision not null
);
create index if not exists pool_generation_jobs_created
  on public.pool_generation_jobs (created desc);

alter table public.pool_owner_settings enable row level security;
alter table public.pool_generation_jobs enable row level security;
