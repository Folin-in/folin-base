-- Interpretive comments printed by the lab itself ("Doctor's Note: ...", "Comment: ...").
-- Kept apart from `concerns`, which is Folin's own wording - these are the lab's words and
-- are reproduced verbatim.
alter table reports add column if not exists lab_notes jsonb not null default '[]';
