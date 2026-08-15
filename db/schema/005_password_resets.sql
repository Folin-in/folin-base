create table if not exists password_resets (
  user_id text primary key,
  email text not null,
  code_hash text not null,
  expires_at timestamptz not null,
  consumed boolean not null default false,
  created_at timestamptz,
  consumed_at timestamptz
);
