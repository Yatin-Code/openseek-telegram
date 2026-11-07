-- Revoked device tokens: a tombstone keeps "revoked" distinguishable from
-- "never valid" after the device row is deleted (H6, issue #57).
create table if not exists public.pool_revoked_tokens (
  token_hash text primary key,
  revoked double precision not null
);
alter table public.pool_revoked_tokens enable row level security;
