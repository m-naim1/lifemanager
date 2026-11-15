-- 0002_multiuser.sql — multi-user support.
-- Adds user ownership columns (goals already had user_id in 0001), the
-- carry/schedule task columns added later, backfills, and the per-user indexes.

ALTER TABLE projects ADD COLUMN user_id INTEGER;
ALTER TABLE tasks ADD COLUMN user_id INTEGER;
ALTER TABLE routines ADD COLUMN user_id INTEGER;

ALTER TABLE tasks ADD COLUMN start_date TEXT;
ALTER TABLE tasks ADD COLUMN due_date TEXT;
ALTER TABLE tasks ADD COLUMN completion_mode TEXT NOT NULL DEFAULT 'carry';

ALTER TABLE schedules ADD COLUMN slot_days TEXT;
ALTER TABLE routines ADD COLUMN slot_days TEXT;

-- Backfill task start dates from creation date (idempotent).
UPDATE tasks SET start_date = COALESCE(start_date, substr(created_at, 1, 10));

CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_routines_user ON routines(user_id);
