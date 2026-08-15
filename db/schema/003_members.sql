create table if not exists members (
  member_id text primary key,
  user_id text not null,
  name text not null default '',
  relation text not null default 'family',
  age int,
  gender text,
  allergies jsonb not null default '[]',
  created_at text not null default ''
);
create index if not exists members_user on members (user_id);
