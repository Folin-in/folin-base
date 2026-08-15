create table if not exists quota_counters (
  id text primary key,
  user_id text not null,
  day text not null,
  bucket text not null,
  count int not null default 0
);

-- Payment / plan audit trail: append-only, one row per plan event (upgrade, downgrade,
-- expiry). Survives everything except account deletion (FK cascade below).
