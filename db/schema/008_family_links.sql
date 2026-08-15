create table if not exists family_links (
  a text not null,
  b text not null,
  relationship text not null default 'family',
  created_at text not null default '',
  primary key (a, b)
);
-- ponytail: no TTL cleanup - day-keyed rows are inert once the day passes; add pg_cron if the table ever matters
