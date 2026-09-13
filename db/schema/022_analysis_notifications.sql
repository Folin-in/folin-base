-- Outbox committed atomically with successful uploads. No medical content.
create table if not exists analysis_notifications (
  report_id uuid primary key references reports(id) on delete cascade,
  user_id text not null references users(user_id) on delete cascade,
  created_at timestamptz not null default clock_timestamp(),
  next_attempt_at timestamptz not null default clock_timestamp()
);
alter table analysis_notifications add column if not exists next_attempt_at timestamptz not null default clock_timestamp();
create index if not exists analysis_notifications_created on analysis_notifications(created_at);
alter table analysis_notifications enable row level security;
