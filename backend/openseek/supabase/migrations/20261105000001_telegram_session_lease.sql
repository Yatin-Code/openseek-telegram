-- Serialize the one primary Telethon authorization used by a participant's
-- concurrent Vercel function instances. A dead invocation loses its lease.
create table if not exists public.pool_telegram_session_lock (
  lock_id text primary key,
  owner text not null,
  lease_until double precision not null
);

alter table public.pool_telegram_session_lock enable row level security;

create or replace function public.try_acquire_pool_telegram_session(
  p_owner text,
  p_lease_seconds double precision
)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
  now_s double precision := extract(epoch from clock_timestamp());
  changed integer;
begin
  if p_owner is null or length(p_owner) < 16 or p_lease_seconds < 1 or p_lease_seconds > 120 then
    return false;
  end if;

  insert into public.pool_telegram_session_lock(lock_id, owner, lease_until)
  values ('primary', p_owner, now_s + p_lease_seconds)
  on conflict (lock_id) do update
    set owner = excluded.owner, lease_until = excluded.lease_until
    where public.pool_telegram_session_lock.lease_until <= now_s
       or public.pool_telegram_session_lock.owner = p_owner;
  get diagnostics changed = row_count;
  return changed = 1;
end;
$$;

create or replace function public.release_pool_telegram_session(p_owner text)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
  changed integer;
begin
  delete from public.pool_telegram_session_lock
   where lock_id = 'primary' and owner = p_owner;
  get diagnostics changed = row_count;
  return changed = 1;
end;
$$;

revoke all on function public.try_acquire_pool_telegram_session(text, double precision) from public;
revoke all on function public.release_pool_telegram_session(text) from public;
grant execute on function public.try_acquire_pool_telegram_session(text, double precision) to service_role;
grant execute on function public.release_pool_telegram_session(text) to service_role;
