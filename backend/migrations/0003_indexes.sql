-- 0003_indexes.sql — remaining lookup indexes.

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_goals_user ON goals(user_id);
CREATE INDEX IF NOT EXISTS idx_goal_fields_goal ON goal_fields(goal_id);
CREATE INDEX IF NOT EXISTS idx_goal_entries_goal_date ON goal_entries(goal_id, date);
CREATE INDEX IF NOT EXISTS idx_goal_values_entry ON goal_values(entry_id);
CREATE INDEX IF NOT EXISTS idx_goal_values_field ON goal_values(field_id);
CREATE INDEX IF NOT EXISTS idx_goal_stages_goal ON goal_stages(goal_id);
CREATE INDEX IF NOT EXISTS idx_admin_audit_created ON admin_audit(created_at);
CREATE INDEX IF NOT EXISTS idx_admin_audit_target ON admin_audit(target_user_id);
