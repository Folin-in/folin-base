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
