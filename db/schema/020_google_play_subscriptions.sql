create table if not exists google_play_account_links (
  account_hash text primary key,
  user_id text not null unique,
  created_at text not null,
  updated_at text not null
);

create table if not exists google_play_subscriptions (
  purchase_token text primary key,
  user_id text not null,
  product_id text not null,
  base_plan_id text,
  latest_order_id text,
  status text not null,
  auto_renewing boolean not null default false,
  acknowledged boolean not null default false,
  expires_at text,
  created_at text not null,
  updated_at text not null
);
create index if not exists google_play_subscriptions_user
  on google_play_subscriptions (user_id, updated_at desc);
create index if not exists google_play_subscriptions_order
  on google_play_subscriptions (latest_order_id)
  where latest_order_id is not null;

-- Pub/Sub may redeliver a notification. Recording its message id makes handling idempotent.
create table if not exists google_play_notifications (
  message_id text primary key,
  purchase_token text,
  notification_type int,
  processed_at text not null
);
