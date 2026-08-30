create table if not exists push_tokens (
  id uuid primary key default gen_random_uuid(),
  user_id text not null references users (user_id) on delete cascade,
  token text not null unique,
  platform text not null check (platform in ('android', 'ios')),
  timezone text not null default 'UTC',
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists push_tokens_active_user on push_tokens (user_id) where active;

create table if not exists push_deliveries (
  id uuid primary key default gen_random_uuid(),
  push_token_id uuid not null references push_tokens (id) on delete cascade,
  user_id text not null references users (user_id) on delete cascade,
  reminder_key text not null,
  cadence_key text not null,
  status text not null,
  ticket_id text,
  error text,
  created_at timestamptz not null default now(),
  receipt_checked_at timestamptz,
  unique (push_token_id, reminder_key, cadence_key)
);
create index if not exists push_deliveries_receipts
  on push_deliveries (created_at) where status = 'ticketed' and receipt_checked_at is null;
