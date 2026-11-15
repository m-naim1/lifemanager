"""Raw SQL migrations for the D1 driver.

Migration files live in `backend/migrations/*.sql` and are applied in
alphabetical order on startup when `LIFEMANAGER_DB_DRIVER=d1`. Applied files
are tracked in the `_schema_migrations` table, so re-running is safe and only
new files are applied. Each statement runs separately because D1 auto-commits
every statement (no cross-statement transaction).

The same files work with `wrangler d1 migrations apply lifemanager --remote`
(the wrangler config points `migrations_dir` at the same folder). Note that
wrangler keeps its own tracking table, so pick one mechanism and stick with
it; running both would try to apply the same files twice.

When running locally with SQLite (`LIFEMANAGER_DB_DRIVER=sqlite`), the schema
bootstrap in `app.db` handles creation/upgrades and this runner is not used.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import settings
from .models import now_iso

DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def migrations_dir() -> Path:
    if settings.migrations_dir:
        return Path(settings.migrations_dir)
    return DEFAULT_MIGRATIONS_DIR


def discover_migration_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.glob("*.sql") if path.is_file())


def split_statements(sql_text: str) -> list[str]:
    """Split a migration file into individual statements.

    Migration files are simple (single-line comments, no semicolons inside
    string literals), so splitting on ';' after stripping comments is enough.
    """
    without_comments = re.sub(r"--[^\n]*", "", sql_text)
    return [s.strip() for s in without_comments.split(";") if s.strip()]


async def apply_pending_migrations(conn, directory: Path | None = None) -> list[str]:
    """Apply all not-yet-applied migration files. Returns the names applied."""
    folder = directory or migrations_dir()
    if not folder.is_dir():
        raise FileNotFoundError(
            f"migrations directory not found: {folder} "
            "(set LIFEMANAGER_MIGRATIONS_DIR to override)"
        )

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    cursor = await conn.execute("SELECT name FROM _schema_migrations")
    applied = {row["name"] for row in await cursor.fetchall()}

    applied_now: list[str] = []
    for path in discover_migration_files(folder):
        if path.name in applied:
            continue
        for statement in split_statements(path.read_text(encoding="utf-8")):
            await conn.execute(statement)
        await conn.execute(
            "INSERT INTO _schema_migrations (name, applied_at) VALUES (?, ?)",
            (path.name, now_iso()),
        )
        applied_now.append(path.name)
    return applied_now
