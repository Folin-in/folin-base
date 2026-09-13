-- Temporary document payload, like analyze_jobs; erased on every terminal outcome.
-- Inserted in the same transaction as the report, never served to clients.
create table if not exists verification_jobs (
    report_id uuid primary key references reports(id) on delete cascade,
    user_id text not null references users(user_id) on delete cascade,
    status text not null default 'pending' check (status in ('pending', 'processing', 'completed', 'unavailable')),
    payload jsonb,
    created_at timestamptz not null default clock_timestamp(),
    started_at timestamptz,
    completed_at timestamptz,
    claim_token text,
    attempts integer not null default 0
);
create index if not exists verification_jobs_pending_idx
    on verification_jobs(created_at) where status in ('pending', 'processing');
alter table verification_jobs enable row level security;
