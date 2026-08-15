create table if not exists refresh_tokens (
  token_hash text primary key,
  user_id text not null,
  family_id text not null,
  used boolean not null default false,
  revoked_at timestamptz,
  created_at timestamptz not null,
  expires_at timestamptz not null
);
create index if not exists refresh_user on refresh_tokens (user_id);
create index if not exists refresh_family on refresh_tokens (family_id);
