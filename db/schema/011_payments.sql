-- Legacy Razorpay payment records are retained for financial reporting and migration.
-- New Android purchases use google_play_subscriptions.
create table if not exists payments (
  order_id text primary key,       -- razorpay order id
  user_id text not null,
  amount_inr int not null,         -- server-decided, never from the client
  currency text not null default 'INR',
  status text not null,            -- created | paid | failed
  razorpay_payment_id text unique,
  created_at text not null,
  paid_at text
);
create index if not exists payments_user on payments (user_id, created_at desc);

-- Accuracy feedback: one row per (report, biomarker) - biomarker_index null = whole report.
-- id encodes the uniqueness key so upserts are a plain on-conflict.
