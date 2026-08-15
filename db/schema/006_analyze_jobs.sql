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
  error text
);
create index if not exists jobs_user on analyze_jobs (user_id);
