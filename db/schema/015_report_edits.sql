create table if not exists report_edits (
  id bigint generated always as identity primary key,
  user_id text not null,
  report_id uuid not null,
  field text not null,           -- 'biomarker:<name>' | 'title'
  old_value jsonb not null,      -- e.g. {"value": "12", "status": "low"}
  new_value jsonb not null,
  edited_at text not null
);
create index if not exists report_edits_report on report_edits (report_id, edited_at desc);

-- Engagement events: behaviour signals the gamification engine scores (profile
-- completed, explanation read, follow-up logged). Append-only; farming is limited
-- at scoring time (EVENT_ONCE / EVENT_DAILY_CAP in gamification.py).
