create table if not exists analyze_jobs (
  job_id text primary key,
  user_id text not null,
  member_id text not null default 'self',
  filename text not null default '',
  mime_type text not null default '',
  status text not null default 'pending',
  created_at text not null default '',
  started_at text,
  completed_at text,
  report_id text,
  xp_earned int,
  new_badges jsonb,
  error text,
  request_payload jsonb
);
alter table analyze_jobs add column if not exists request_payload jsonb;
create index if not exists jobs_user on analyze_jobs (user_id);
-- The API checks for an active job for a friendly error, while this index closes the
-- concurrent-request race between that check and insert.
create unique index if not exists jobs_one_running_per_user on analyze_jobs (user_id)
  where status in ('pending', 'processing');
