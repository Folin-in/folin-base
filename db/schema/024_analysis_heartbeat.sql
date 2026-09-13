-- A renewable lease distinguishes a slow live worker from a crashed one.
alter table analyze_jobs add column if not exists heartbeat_at text;
