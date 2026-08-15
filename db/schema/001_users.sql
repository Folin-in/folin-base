create table if not exists users (
  user_id text primary key,
  email text unique not null,
  username text unique,
  name text not null default '',
  password_hash text,
  auth_provider text not null default 'local',
  picture text,
  health_profile jsonb not null default '{}',
  preferences jsonb not null default '{}',
  token_version int not null default 0,
  created_at text not null default ''
);
alter table users add column if not exists google_id text;
alter table users add column if not exists apple_id text;
alter table users add column if not exists supabase_uid uuid unique;
alter table users add column if not exists consented_at text;
alter table users add column if not exists consent_version text;
