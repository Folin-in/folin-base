create table if not exists plan_history (
  id bigint generated always as identity primary key,
  user_id text not null,
  action text not null,          -- upgraded | downgraded | expired | renewed | revoked
  plan text not null,            -- resulting plan
  amount_inr int,                -- charged amount (null for downgrades/expiry)
  starts_at text not null,
  expires_at text,               -- pro runs 90 days; null for free
  created_at text not null
);
create index if not exists plan_history_user on plan_history (user_id, created_at desc);

-- Historical payment data remains in its provider-specific payment table. New Google
-- Play subscription state is stored in 020_google_play_subscriptions.sql.
