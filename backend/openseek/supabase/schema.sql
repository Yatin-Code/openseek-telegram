-- OpenSeek Vercel profile: rebuildable channel index in Supabase PostgreSQL.
--
-- Telegram is the canonical durable package store (docs/adr/0001). This schema holds
-- only the derived index, upload-status rows, and the retry queue. Supabase Storage is
-- temporary upload staging, never a second permanent package copy.
--
-- Apply with the service role. RLS is enabled with no policies so anon/authenticated
-- keys cannot read or write these tables; only the service role bypasses RLS.

create table if not exists public.pool_channel_tiles (
  fp text not null,
  profile text not null,
  k integer not null,
  pts_ms bigint not null,
  duration_ms bigint not null,
  n_tiles integer not null,
  size bigint not null,
  sha256 text not null,
  message_id bigint not null,
  created double precision not null,
  primary key (fp, profile, k)
);
create index if not exists pool_channel_tiles_fp on public.pool_channel_tiles (fp, profile, k);

create table if not exists public.pool_channel_state (
  channel_id text primary key,
  last_message_id bigint not null default 0,
  scanned_at double precision not null default 0
);

create table if not exists public.pool_hints (
  fp text not null,
  profile text not null,
  imdb text,
  season integer not null default 0,
  episode integer not null default 0,
  duration_ms bigint not null,
  size_bytes bigint,
  filename_norm text,
  n_tiles integer not null,
  grid integer not null,
  created double precision not null,
  verified integer not null default 0,
  primary key (fp, profile)
);
create index if not exists pool_hints_imdb_episode on public.pool_hints (imdb, season, episode);
create index if not exists pool_hints_filename on public.pool_hints (filename_norm);
create index if not exists pool_hints_duration on public.pool_hints (duration_ms);

create table if not exists public.pool_episode_packages (
  episode_key text not null,
  fp text not null,
  profile text not null,
  duration_ms bigint not null,
  updated double precision not null,
  primary key (episode_key, fp, profile)
);
create index if not exists pool_episode_lookup on public.pool_episode_packages (episode_key, duration_ms);

-- Read-only summary used by candidate ranking and package assembly. It is derived
-- entirely from pool_channel_tiles, so it is rebuilt by rebuild() like the tables.
create or replace view public.pool_packages as
select fp, profile, duration_ms, n_tiles, count(*)::integer as channel_tiles,
       max(created) as created
from public.pool_channel_tiles
group by fp, profile, duration_ms, n_tiles;

-- Vercel staging status. The object bytes live in Supabase Storage only until
-- Telegram accepts the package; this row never stores a second durable copy.
create table if not exists public.pool_uploads (
  upload_id text primary key,
  device_id text not null,
  fp text not null,
  profile text not null,
  duration_ms bigint not null,
  size_bytes bigint,
  imdb text,
  season integer not null default 0,
  episode integer not null default 0,
  filename_norm text,
  object_path text not null,
  state text not null,
  created double precision not null,
  updated double precision not null,
  expires double precision not null,
  error text
);
create index if not exists pool_uploads_state on public.pool_uploads (state, expires);

-- Telegram publication retries. The package is durable only after Telegram accepts it.
create table if not exists public.pool_publication_queue (
  upload_id text primary key,
  fp text not null,
  profile text not null,
  attempts integer not null default 0,
  next_attempt double precision not null default 0,
  state text not null,
  created double precision not null,
  updated double precision not null,
  error text
);
create index if not exists pool_publication_queue_next
  on public.pool_publication_queue (state, next_attempt);

alter table public.pool_channel_tiles enable row level security;
alter table public.pool_channel_state enable row level security;
alter table public.pool_hints enable row level security;
alter table public.pool_episode_packages enable row level security;
alter table public.pool_uploads enable row level security;
alter table public.pool_publication_queue enable row level security;
