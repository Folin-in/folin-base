create table if not exists engagement_events (
  id bigint generated always as identity primary key,
  user_id text not null,
  type text not null,
  report_id text,
  ref text,
  at text not null
);
create index if not exists engagement_events_user on engagement_events (user_id, at);

-- Foreign keys (idempotent): child rows die with their user; a pending invite to a
-- deleted invitee just unlinks. reports.member_id has NO FK on purpose - 'self' is a
-- virtual member that never exists in the members table.
