alter table users add column if not exists is_blocked boolean not null default false;
alter table users add column if not exists blocked_at text;
alter table users add column if not exists blocked_reason text;
