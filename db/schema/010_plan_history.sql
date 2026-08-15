create table if not exists plan_history (
  id bigint generated always as identity primary key,
  user_id text not null,
  action text not null,          -- upgraded | downgraded | expired
  plan text not null,            -- resulting plan
  amount_inr int,                -- charged amount (null for downgrades/expiry)
  starts_at text not null,
  expires_at text,               -- pro runs 90 days; null for free
  created_at text not null
);
create index if not exists plan_history_user on plan_history (user_id, created_at desc);

-- Razorpay orders. The amount is written when the order is created and re-checked at
-- verification, so a tampered client can never buy Pro for less. razorpay_payment_id is
-- unique: replaying a captured payment to re-activate Pro hits the constraint, not a check.
