create table if not exists profile_edits (
  id bigint generated always as identity primary key,
  user_id text not null,
  field text not null,
  old_value jsonb,
  new_value jsonb,
  changed_at text not null
);
create index if not exists profile_edits_user on profile_edits (user_id, changed_at desc);

-- Audit trail for user edits to a report (biomarker corrections, renames).
-- Append-only: the AI's original value survives every edit, so an admin can always
-- reconstruct what was shown vs what the user changed it to, and when.
