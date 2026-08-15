create table if not exists family_invites (
  id text primary key,
  inviter_id text not null,
  inviter_name text not null default '',
  invitee_email text not null,
  invitee_user_id text,
  relationship text not null default 'family',
  status text not null default 'pending',
  created_at text not null default '',
  accepted_at text,
  declined_at text,
  resend_email_id text
);
create index if not exists invites_inviter on family_invites (inviter_id, status);
create index if not exists invites_email on family_invites (invitee_email, status);
