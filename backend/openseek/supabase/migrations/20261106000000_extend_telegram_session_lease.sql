-- Keep the participant's single Telegram user session locked for longer than
-- Vercel's configured 300-second function deadline, including after a timeout.
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
  if p_owner is null or length(p_owner) < 16 or p_lease_seconds < 1 or p_lease_seconds > 600 then
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

revoke all on function public.try_acquire_pool_telegram_session(text, double precision) from public;
grant execute on function public.try_acquire_pool_telegram_session(text, double precision) to service_role;
