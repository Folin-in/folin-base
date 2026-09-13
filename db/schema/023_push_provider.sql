-- Existing Expo registrations remain distinguishable during Android migration.
alter table push_tokens add column if not exists provider text not null default 'expo'
  check (provider in ('expo', 'fcm'));
