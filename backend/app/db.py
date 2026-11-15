from __future__ import annotations

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    deadline TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    project_id INTEGER,
    created_at TEXT NOT NULL,
    start_date TEXT,
    due_date TEXT,
    completion_mode TEXT NOT NULL DEFAULT 'carry',
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    repeat_count INTEGER NOT NULL,
    repeat_unit TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT,
    duration_count INTEGER,
    duration_unit TEXT,
    slot_days TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_completions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    UNIQUE (task_id, date)
);

CREATE TABLE IF NOT EXISTS routines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    frequency_count INTEGER NOT NULL,
    frequency_unit TEXT NOT NULL,
    created_at TEXT NOT NULL,
    slot_days TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS routine_completions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    routine_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    FOREIGN KEY (routine_id) REFERENCES routines(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'measured',
    show_chart INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS goal_fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    target_value REAL,
    compare_mode TEXT NOT NULL DEFAULT 'latest',
    higher_is_better INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (goal_id) REFERENCES goals(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS goal_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (goal_id) REFERENCES goals(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS goal_values (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL,
    field_id INTEGER NOT NULL,
    value REAL NOT NULL,
    FOREIGN KEY (entry_id) REFERENCES goal_entries(id) ON DELETE CASCADE,
    FOREIGN KEY (field_id) REFERENCES goal_fields(id) ON DELETE CASCADE,
    UNIQUE (entry_id, field_id)
);

CREATE TABLE IF NOT EXISTS goal_stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    FOREIGN KEY (goal_id) REFERENCES goals(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS admin_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_user_id INTEGER,
    action TEXT NOT NULL,
    target_user_id INTEGER,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (admin_user_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (target_user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_task_completions_date ON task_completions(date);
CREATE INDEX IF NOT EXISTS idx_routine_completions_date ON routine_completions(date);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_task_notes_date ON task_notes(date);
CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_routines_user ON routines(user_id);
CREATE INDEX IF NOT EXISTS idx_goals_user ON goals(user_id);
CREATE INDEX IF NOT EXISTS idx_goal_fields_goal ON goal_fields(goal_id);
CREATE INDEX IF NOT EXISTS idx_goal_entries_goal_date ON goal_entries(goal_id, date);
CREATE INDEX IF NOT EXISTS idx_goal_values_entry ON goal_values(entry_id);
CREATE INDEX IF NOT EXISTS idx_goal_values_field ON goal_values(field_id);
CREATE INDEX IF NOT EXISTS idx_goal_stages_goal ON goal_stages(goal_id);
CREATE INDEX IF NOT EXISTS idx_admin_audit_created ON admin_audit(created_at);
CREATE INDEX IF NOT EXISTS idx_admin_audit_target ON admin_audit(target_user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email);
"""

# Additive migrations for databases created before these columns existed.
# "duplicate column name" errors are expected and ignored.
MIGRATIONS = [
    "ALTER TABLE tasks ADD COLUMN start_date TEXT",
    "ALTER TABLE tasks ADD COLUMN due_date TEXT",
    "ALTER TABLE tasks ADD COLUMN completion_mode TEXT NOT NULL DEFAULT 'carry'",
    "ALTER TABLE schedules ADD COLUMN slot_days TEXT",
    "ALTER TABLE routines ADD COLUMN slot_days TEXT",
    "ALTER TABLE projects ADD COLUMN user_id INTEGER",
    "ALTER TABLE tasks ADD COLUMN user_id INTEGER",
    "ALTER TABLE routines ADD COLUMN user_id INTEGER",
    "CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_routines_user ON routines(user_id)",
    "UPDATE tasks SET start_date = COALESCE(start_date, substr(created_at, 1, 10))",
]


class Database:
    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database not connected")
        return self._conn

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        # Run base tables first (without user indexes, which fail on legacy
        # DBs whose tables predate the user_id columns), then migrations,
        # then indexes. Split executescript so one bad statement can't abort
        # the rest.
        statements = [s.strip() for s in SCHEMA.split(";") if s.strip()]
        for stmt in statements:
            if "idx_projects_user" in stmt or "idx_tasks_user" in stmt or "idx_routines_user" in stmt:
                continue  # created after migrations below
            try:
                await self._conn.execute(stmt)
            except Exception as exc:
                if "duplicate column name" not in str(exc) and "already exists" not in str(exc):
                    raise
        for stmt in MIGRATIONS:
            try:
                await self._conn.execute(stmt)
            except Exception as exc:
                msg = str(exc)
                if "duplicate column name" not in msg and "already exists" not in msg:
                    raise
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None