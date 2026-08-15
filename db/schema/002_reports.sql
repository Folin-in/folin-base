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
-- Supabase Auth mapping. auth.users owns the credential; this row owns everything
-- Folin knows about the person. Keeping our own user_id as the primary key means the
-- 15 tables that reference it (and their cascade FKs) need no migration at all.
