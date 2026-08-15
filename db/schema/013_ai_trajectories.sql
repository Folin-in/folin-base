create table if not exists ai_trajectories (
  id bigint generated always as identity primary key,
  user_id text,
  kind text not null,            -- analyze | verify | refresh_advice | reconcile_tips
  model text,                    -- model that produced the response (null on total failure)
  report_id text,                -- linked after the report row exists
  request jsonb not null default '{}',
  response_raw text,
  status text not null,          -- ok | error
  error text,
  latency_ms int,
  created_at text not null
);
create index if not exists ai_traj_user on ai_trajectories (user_id, created_at desc);
create index if not exists ai_traj_report on ai_trajectories (report_id);

-- Profile change history: advice correctness depends on profile state (diet,
-- allergies, language...), so every change is auditable. Append-only.
