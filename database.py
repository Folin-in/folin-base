"""Supabase Postgres via asyncpg. Schema is created at startup (idempotent).
JSONB columns hold the nested structures (biomarkers, suggestions, profiles...).
"""
import json
import logging

import asyncpg

import config

logger = logging.getLogger(__name__)

pool: asyncpg.Pool | None = None

SCHEMA = """
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
create table if not exists reports (
  id uuid primary key default gen_random_uuid(),
  user_id text not null,
  filename text not null default '',
  mime_type text not null default '',
  storage_path text,
  uploaded_at text not null default '',
  report_type text not null default 'lab',
  title text,
  report_date text,
  patient_name text,
  lab_name text,
  summary text not null default '',
  overall_assessment text not null default '',
  positives jsonb not null default '[]',
  concerns jsonb not null default '[]',
  suggestions jsonb not null default '[]',
  biomarkers jsonb not null default '[]',
  health_score int not null default 0,
  normal_count int not null default 0,
  attention_count int not null default 0,
  total_count int not null default 0,
  chief_complaint text,
  exam_findings jsonb not null default '[]',
  diagnoses jsonb not null default '[]',
  referrals jsonb not null default '[]',
  medications jsonb not null default '[]',
  modality text,
  body_area text,
  findings jsonb not null default '[]',
  impression text,
  admission_date text,
  discharge_date text,
  procedures jsonb not null default '[]',
  follow_up text
);
create index if not exists reports_user_uploaded on reports (user_id, uploaded_at desc);
alter table reports add column if not exists member_id text not null default 'self';
alter table reports add column if not exists name_mismatch boolean not null default false;
alter table reports add column if not exists file_hash text;
alter table reports add column if not exists deleted_at text;
alter table reports add column if not exists analysis_meta jsonb;
alter table users add column if not exists google_id text;
alter table users add column if not exists apple_id text;
-- Supabase Auth mapping. auth.users owns the credential; this row owns everything
-- Folin knows about the person. Keeping our own user_id as the primary key means the
-- 15 tables that reference it (and their cascade FKs) need no migration at all.
alter table users add column if not exists supabase_uid uuid unique;
alter table users add column if not exists consented_at text;
alter table users add column if not exists consent_version text;
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
create table if not exists refresh_tokens (
  token_hash text primary key,
  user_id text not null,
  family_id text not null,
  used boolean not null default false,
  revoked_at timestamptz,
  created_at timestamptz not null,
  expires_at timestamptz not null
);
create index if not exists refresh_user on refresh_tokens (user_id);
create index if not exists refresh_family on refresh_tokens (family_id);
create table if not exists password_resets (
  user_id text primary key,
  email text not null,
  code_hash text not null,
  expires_at timestamptz not null,
  consumed boolean not null default false,
  created_at timestamptz,
  consumed_at timestamptz
);
create table if not exists analyze_jobs (
  job_id text primary key,
  user_id text not null,
  member_id text not null default 'self',
  filename text not null default '',
  mime_type text not null default '',
  status text not null default 'pending',
  created_at text not null default '',
  started_at text,
  completed_at text,
  report_id text,
  xp_earned int,
  new_badges jsonb,
  error text
);
create index if not exists jobs_user on analyze_jobs (user_id);
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
create table if not exists family_links (
  a text not null,
  b text not null,
  relationship text not null default 'family',
  created_at text not null default '',
  primary key (a, b)
);
-- ponytail: no TTL cleanup - day-keyed rows are inert once the day passes; add pg_cron if the table ever matters
create table if not exists quota_counters (
  id text primary key,
  user_id text not null,
  day text not null,
  bucket text not null,
  count int not null default 0
);

-- Payment / plan audit trail: append-only, one row per plan event (upgrade, downgrade,
-- expiry). Survives everything except account deletion (FK cascade below).
create table if not exists plan_history (
  id bigint generated always as identity primary key,
  user_id text not null,
  action text not null,          -- upgraded | downgraded | expired
  plan text not null,            -- resulting plan
  amount_inr int,                -- charged amount (null for downgrades/expiry)
  starts_at text not null,
  expires_at text,               -- pro runs 90 days; null for free
  created_at text not null
);
create index if not exists plan_history_user on plan_history (user_id, created_at desc);

-- Razorpay orders. The amount is written when the order is created and re-checked at
-- verification, so a tampered client can never buy Pro for less. razorpay_payment_id is
-- unique: replaying a captured payment to re-activate Pro hits the constraint, not a check.
create table if not exists payments (
  order_id text primary key,       -- razorpay order id
  user_id text not null,
  amount_inr int not null,         -- server-decided, never from the client
  currency text not null default 'INR',
  status text not null,            -- created | paid | failed
  razorpay_payment_id text unique,
  created_at text not null,
  paid_at text
);
create index if not exists payments_user on payments (user_id, created_at desc);

-- Accuracy feedback: one row per (report, biomarker) - biomarker_index null = whole report.
-- id encodes the uniqueness key so upserts are a plain on-conflict.
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
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'reports_user_fk') then
    alter table reports add constraint reports_user_fk
      foreign key (user_id) references users (user_id) on delete cascade;
    alter table members add constraint members_user_fk
      foreign key (user_id) references users (user_id) on delete cascade;
    alter table refresh_tokens add constraint refresh_user_fk
      foreign key (user_id) references users (user_id) on delete cascade;
    alter table password_resets add constraint resets_user_fk
      foreign key (user_id) references users (user_id) on delete cascade;
    alter table analyze_jobs add constraint jobs_user_fk
      foreign key (user_id) references users (user_id) on delete cascade;
    alter table quota_counters add constraint quota_user_fk
      foreign key (user_id) references users (user_id) on delete cascade;
    alter table family_invites add constraint invites_inviter_fk
      foreign key (inviter_id) references users (user_id) on delete cascade;
    alter table family_invites add constraint invites_invitee_fk
      foreign key (invitee_user_id) references users (user_id) on delete set null;
    alter table family_links add constraint links_a_fk
      foreign key (a) references users (user_id) on delete cascade;
    alter table family_links add constraint links_b_fk
      foreign key (b) references users (user_id) on delete cascade;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'plan_history_user_fk') then
    alter table plan_history add constraint plan_history_user_fk
      foreign key (user_id) references users (user_id) on delete cascade;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'report_edits_user_fk') then
    alter table report_edits add constraint report_edits_user_fk
      foreign key (user_id) references users (user_id) on delete cascade;
    alter table report_edits add constraint report_edits_report_fk
      foreign key (report_id) references reports (id) on delete cascade;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'ai_traj_user_fk') then
    alter table ai_trajectories add constraint ai_traj_user_fk
      foreign key (user_id) references users (user_id) on delete cascade;
    alter table profile_edits add constraint profile_edits_user_fk
      foreign key (user_id) references users (user_id) on delete cascade;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'engagement_events_user_fk') then
    alter table engagement_events add constraint engagement_events_user_fk
      foreign key (user_id) references users (user_id) on delete cascade;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'report_feedback_user_fk') then
    alter table report_feedback add constraint report_feedback_user_fk
      foreign key (user_id) references users (user_id) on delete cascade;
    alter table report_feedback add constraint report_feedback_report_fk
      foreign key (report_id) references reports (id) on delete cascade;
  end if;
end $$;
"""


async def _init_conn(conn: asyncpg.Connection):
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def connect():
    global pool
    # statement_cache_size=0: required behind Supabase's pgbouncer transaction pooler
    pool = await asyncpg.create_pool(
        config.SUPABASE_DB_URL, init=_init_conn, statement_cache_size=0, min_size=1, max_size=10
    )
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)
    logger.info("Postgres connected, schema ensured")


async def close():
    if pool:
        await pool.close()


async def fetch(sql: str, *args) -> list[dict]:
    return [dict(r) for r in await pool.fetch(sql, *args)]


async def fetchrow(sql: str, *args) -> dict | None:
    r = await pool.fetchrow(sql, *args)
    return dict(r) if r else None


async def fetchval(sql: str, *args):
    return await pool.fetchval(sql, *args)


async def execute(sql: str, *args) -> str:
    return await pool.execute(sql, *args)
