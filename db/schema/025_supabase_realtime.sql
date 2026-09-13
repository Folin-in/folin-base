-- Enable Supabase Realtime publication for key admin tables
do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    begin
      alter publication supabase_realtime add table users, reports, analyze_jobs;
    exception when others then
      -- table already added or publication constraint
      null;
    end;
  end if;
end $$;
