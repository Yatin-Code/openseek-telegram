-- Device tokens and pairing invitations must survive disposable Vercel instances.
-- Each participant applies this only to their own Supabase project.
create table if not exists public.pool_devices (
  device_id text primary key,
  name text not null,
  token_hash text not null unique,
  role text not null default 'guest' check (role in ('owner', 'guest')),
  created double precision not null,
  last_seen double precision not null,
  expires double precision,
  invite_id text,
  contribute boolean not null default false
);
create index if not exists pool_devices_role_expires on public.pool_devices (role, expires);
alter table public.pool_devices enable row level security;

create table if not exists public.pool_invites (
  pair_id text primary key,
  label text not null default '',
  role text not null default 'guest' check (role in ('owner', 'guest')),
  salt text not null,
  iterations integer not null,
  k_hex text not null,
  created double precision not null,
  expires double precision not null,
  max_uses integer not null default 1,
  uses integer not null default 0,
  attempts integer not null default 0,
  revoked double precision,
  device_days double precision,
  tag text not null,
  contribute boolean not null default false
);
create index if not exists pool_invites_tag_expires on public.pool_invites (tag, expires desc);
alter table public.pool_invites enable row level security;

create or replace function public.bump_pool_invite_attempts(p_pair_id text)
returns boolean language plpgsql security definer set search_path = pg_catalog as $$
declare
  changed integer;
begin
  update public.pool_invites set attempts = attempts + 1
   where pair_id = p_pair_id and attempts < 5;
  get diagnostics changed = row_count;
  return changed = 1;
end;
$$;

create or replace function public.use_pool_invite(
  p_pair_id text, p_now double precision, p_max_attempts integer
)
returns boolean language plpgsql security definer set search_path = pg_catalog as $$
declare
  changed integer;
begin
  update public.pool_invites set uses = uses + 1
   where pair_id = p_pair_id and revoked is null and uses < max_uses
     and attempts < p_max_attempts and expires > p_now;
  get diagnostics changed = row_count;
  return changed = 1;
end;
$$;

create or replace function public.revoke_pool_invite(p_pair_id text, p_now double precision)
returns boolean language plpgsql security definer set search_path = pg_catalog as $$
declare
  changed integer;
begin
  update public.pool_invites set revoked = p_now
   where pair_id = p_pair_id and revoked is null;
  get diagnostics changed = row_count;
  return changed = 1;
end;
$$;

revoke all on function public.bump_pool_invite_attempts(text) from public;
revoke all on function public.use_pool_invite(text, double precision, integer) from public;
revoke all on function public.revoke_pool_invite(text, double precision) from public;
grant execute on function public.bump_pool_invite_attempts(text) to service_role;
grant execute on function public.use_pool_invite(text, double precision, integer) to service_role;
grant execute on function public.revoke_pool_invite(text, double precision) to service_role;
