create table if not exists early_access_signups (
  id uuid primary key default gen_random_uuid(),
  email text not null unique,
  status text not null default 'pending'
    check (status in ('pending', 'confirmed', 'unsubscribed', 'bounced', 'complained')),
  confirmation_token_hash text unique,
  confirmation_expires_at timestamptz,
  confirmation_sent_at timestamptz,
  confirmed_at timestamptz,
  resend_email_id text,
  last_delivery_status text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists early_access_signups_status
  on early_access_signups (status, created_at);
create index if not exists early_access_signups_resend_email
  on early_access_signups (resend_email_id) where resend_email_id is not null;

-- Shared counters make public-form limits consistent across multiple API replicas.
-- Keys contain HMACed IPs, never raw visitor addresses.
create table if not exists early_access_limits (
  key text primary key,
  count int not null default 0,
  expires_at timestamptz not null
);
create index if not exists early_access_limits_expiry on early_access_limits (expires_at);
