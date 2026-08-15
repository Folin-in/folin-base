create table if not exists report_feedback (
  id text primary key,               -- "<report_id>:<biomarker_index|report>"
  user_id text not null,
  report_id uuid not null,
  biomarker_index int,
  biomarker_name text,
  correct boolean not null,
  issues jsonb not null default '[]',
  note text not null default '',
  created_at text not null,
  updated_at text not null
);
create index if not exists report_feedback_report on report_feedback (report_id);

-- Agent trajectories: one row per Gemini call (analyze, verify, refresh, reconcile)
-- with the exact request context, model that answered (after failover), raw response
-- text and latency. The raw response is the debugging ground truth when an
-- extraction looks wrong.
