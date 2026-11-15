from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .db import Database
from .models import (
    Goal,
    GoalEntry,
    GoalField,
    GoalStage,
    GoalValue,
    Project,
    Routine,
    RoutineCompletion,
    Schedule,
    Task,
    TaskCompletion,
    TaskNote,
    User,
    now_iso,
)


def _from_row(cls: type, row: object):
    return cls(**dict(row))


@dataclass
class UserRepository:
    db: Database

    async def count(self) -> int:
        cur = await self.db.conn.execute("SELECT COUNT(*) AS n FROM users")
        row = await cur.fetchone()
        return int(row["n"])

    async def get(self, user_id: int) -> User | None:
        cur = await self.db.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = await cur.fetchone()
        return _from_row(User, row) if row else None

    async def get_by_email(self, email: str) -> User | None:
        cur = await self.db.conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.strip().lower(),)
        )
        row = await cur.fetchone()
        return _from_row(User, row) if row else None

    async def create(self, user: User) -> User:
        cur = await self.db.conn.execute(
            "INSERT INTO users (email, password_hash, is_admin, is_active, created_at) VALUES (?, ?, ?, ?, ?)",
            (user.email.strip().lower(), user.password_hash, user.is_admin, user.is_active, user.created_at),
        )
        await self.db.conn.commit()
        created = await self.get(cur.lastrowid)
        # Claim pre-auth orphan rows (keeper task + routines) for the first user.
        if created is not None and await self.count() == 1:
            await self.db.conn.execute("UPDATE projects SET user_id = ? WHERE user_id IS NULL", (created.id,))
            await self.db.conn.execute("UPDATE tasks SET user_id = ? WHERE user_id IS NULL", (created.id,))
            await self.db.conn.execute("UPDATE routines SET user_id = ? WHERE user_id IS NULL", (created.id,))
            await self.db.conn.commit()
        return created

    async def list(self) -> list[User]:
        cur = await self.db.conn.execute("SELECT * FROM users ORDER BY id")
        return [_from_row(User, r) for r in await cur.fetchall()]

    async def update(self, user: User) -> User | None:
        await self.db.conn.execute(
            """
            UPDATE users
            SET email = ?, password_hash = ?, is_admin = ?, is_active = ?
            WHERE id = ?
            """,
            (
                user.email.strip().lower(),
                user.password_hash,
                user.is_admin,
                user.is_active,
                user.id,
            ),
        )
        await self.db.conn.commit()
        return await self.get(user.id)

    async def delete(self, user_id: int) -> None:
        await self.db.conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        await self.db.conn.commit()


@dataclass
class ProjectRepository:
    db: Database

    async def list(self, user_id: int) -> list[Project]:
        cur = await self.db.conn.execute(
            """
            SELECT * FROM projects WHERE user_id = ?
            ORDER BY CASE status
                        WHEN 'open' THEN 0
                        WHEN 'closed' THEN 1
                        ELSE 2
                      END, updated_at DESC
            """,
            (user_id,),
        )
        rows = await cur.fetchall()
        return [_from_row(Project, r) for r in rows]

    async def get(self, project_id: int, user_id: int) -> Project | None:
        cur = await self.db.conn.execute(
            "SELECT * FROM projects WHERE id = ? AND user_id = ?", (project_id, user_id)
        )
        row = await cur.fetchone()
        return _from_row(Project, row) if row else None

    async def create(self, project: Project) -> Project:
        cur = await self.db.conn.execute(
            """
            INSERT INTO projects (user_id, name, description, status, deadline, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project.user_id,
                project.name,
                project.description,
                project.status,
                project.deadline,
                project.created_at,
                project.updated_at,
            ),
        )
        await self.db.conn.commit()
        return await self.get(cur.lastrowid, project.user_id)

    async def update(self, project: Project) -> Project | None:
        await self.db.conn.execute(
            """
            UPDATE projects
            SET name = ?, description = ?, status = ?, deadline = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                project.name,
                project.description,
                project.status,
                project.deadline,
                project.updated_at,
                project.id,
                project.user_id,
            ),
        )
        await self.db.conn.commit()
        return await self.get(project.id, project.user_id)

    async def delete(self, project_id: int, user_id: int) -> None:
        await self.db.conn.execute(
            "DELETE FROM projects WHERE id = ? AND user_id = ?", (project_id, user_id)
        )
        await self.db.conn.commit()

    async def due_on(self, day: str, user_id: int) -> list[Project]:
        cur = await self.db.conn.execute(
            "SELECT * FROM projects WHERE status = 'open' AND deadline = ? AND user_id = ? ORDER BY name",
            (day, user_id),
        )
        rows = await cur.fetchall()
        return [_from_row(Project, r) for r in rows]


@dataclass
class TaskRepository:
    db: Database

    async def list(self, user_id: int, project_id: int | None = None) -> list[Task]:
        if project_id is None:
            sql = "SELECT * FROM tasks WHERE user_id = ? ORDER BY created_at DESC"
            params: tuple = (user_id,)
        else:
            sql = "SELECT * FROM tasks WHERE user_id = ? AND project_id = ? ORDER BY created_at DESC"
            params = (user_id, project_id)
        cur = await self.db.conn.execute(sql, params)
        rows = await cur.fetchall()
        return [_from_row(Task, r) for r in rows]

    async def get(self, task_id: int, user_id: int) -> Task | None:
        cur = await self.db.conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
        )
        row = await cur.fetchone()
        return _from_row(Task, row) if row else None

    async def create(self, task: Task) -> Task:
        cur = await self.db.conn.execute(
            """
            INSERT INTO tasks
                (user_id, title, description, project_id, created_at, start_date, due_date, completion_mode)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.user_id,
                task.title,
                task.description,
                task.project_id,
                task.created_at,
                task.start_date or task.created_at,
                task.due_date,
                task.completion_mode or "carry",
            ),
        )
        await self.db.conn.commit()
        return await self.get(cur.lastrowid, task.user_id)

    async def update(self, task: Task) -> Task | None:
        await self.db.conn.execute(
            """
            UPDATE tasks
            SET title = ?, description = ?, project_id = ?, start_date = ?, due_date = ?, completion_mode = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                task.title,
                task.description,
                task.project_id,
                task.start_date or task.created_at,
                task.due_date,
                task.completion_mode or "carry",
                task.id,
                task.user_id,
            ),
        )
        await self.db.conn.commit()
        return await self.get(task.id, task.user_id)

    async def delete(self, task_id: int, user_id: int) -> None:
        await self.db.conn.execute(
            "DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
        )
        await self.db.conn.commit()

    # --- schedules ---

    async def get_schedule(self, task_id: int) -> Schedule | None:
        cur = await self.db.conn.execute(
            "SELECT * FROM schedules WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        row = await cur.fetchone()
        return _from_row(Schedule, row) if row else None

    async def save_schedule(self, schedule: Schedule) -> Schedule:
        if schedule.id is None:
            cur = await self.db.conn.execute(
                """
                INSERT INTO schedules
                    (task_id, repeat_count, repeat_unit, start_date, end_date, duration_count, duration_unit, slot_days)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    schedule.task_id,
                    schedule.repeat_count,
                    schedule.repeat_unit,
                    schedule.start_date,
                    schedule.end_date,
                    schedule.duration_count,
                    schedule.duration_unit,
                    schedule.slot_days,
                ),
            )
            await self.db.conn.commit()
            return await self.get_schedule(schedule.task_id)
        await self.db.conn.execute(
            """
            UPDATE schedules
            SET repeat_count = ?, repeat_unit = ?, start_date = ?, end_date = ?,
                duration_count = ?, duration_unit = ?, slot_days = ?
            WHERE id = ?
            """,
            (
                schedule.repeat_count,
                schedule.repeat_unit,
                schedule.start_date,
                schedule.end_date,
                schedule.duration_count,
                schedule.duration_unit,
                schedule.slot_days,
                schedule.id,
            ),
        )
        await self.db.conn.commit()
        return await self.get_schedule(schedule.task_id)

    async def delete_schedule(self, task_id: int) -> None:
        await self.db.conn.execute("DELETE FROM schedules WHERE task_id = ?", (task_id,))
        await self.db.conn.commit()

    async def all_with_schedules(self, user_id: int) -> list[Task]:
        tasks = await self.list(user_id)
        cur = await self.db.conn.execute(
            "SELECT s.* FROM schedules s JOIN tasks t ON t.id = s.task_id WHERE t.user_id = ?",
            (user_id,),
        )
        rows = await cur.fetchall()
        schedules = [_from_row(Schedule, r) for r in rows]
        by_task = {t.id: t for t in tasks}
        for s in schedules:
            if s.task_id in by_task:
                by_task[s.task_id].schedule = s
        return tasks

    # --- completions ---

    async def all_completions(self, user_id: int) -> list[TaskCompletion]:
        cur = await self.db.conn.execute(
            "SELECT c.* FROM task_completions c JOIN tasks t ON t.id = c.task_id WHERE t.user_id = ? ORDER BY c.date",
            (user_id,),
        )
        rows = await cur.fetchall()
        return [_from_row(TaskCompletion, r) for r in rows]

    async def completions_for_task(self, task_id: int) -> list[TaskCompletion]:
        cur = await self.db.conn.execute(
            "SELECT * FROM task_completions WHERE task_id = ? ORDER BY date",
            (task_id,),
        )
        rows = await cur.fetchall()
        return [_from_row(TaskCompletion, r) for r in rows]

    async def completions_between(self, start: str, end: str, user_id: int) -> list[TaskCompletion]:
        cur = await self.db.conn.execute(
            "SELECT c.* FROM task_completions c JOIN tasks t ON t.id = c.task_id WHERE t.user_id = ? AND c.date BETWEEN ? AND ? ORDER BY c.date",
            (user_id, start, end),
        )
        rows = await cur.fetchall()
        return [_from_row(TaskCompletion, r) for r in rows]

    async def add_completion(self, task_id: int, day: str) -> TaskCompletion:
        cur = await self.db.conn.execute(
            "INSERT INTO task_completions (task_id, date, completed_at) VALUES (?, ?, ?)",
            (task_id, day, now_iso()),
        )
        await self.db.conn.commit()
        completion_id = cur.lastrowid
        cur = await self.db.conn.execute(
            "SELECT * FROM task_completions WHERE id = ?", (completion_id,)
        )
        row = await cur.fetchone()
        return _from_row(TaskCompletion, row)

    async def delete_completion(self, completion_id: int) -> None:
        await self.db.conn.execute(
            "DELETE FROM task_completions WHERE id = ?", (completion_id,)
        )
        await self.db.conn.commit()

    async def delete_latest_for_day(self, task_id: int, day: str) -> None:
        cur = await self.db.conn.execute(
            """
            SELECT id FROM task_completions
            WHERE task_id = ? AND date = ?
            ORDER BY id DESC LIMIT 1
            """,
            (task_id, day),
        )
        row = await cur.fetchone()
        if row is not None:
            await self.delete_completion(row["id"])

    # --- day notes ---

    async def note_for(self, task_id: int, day: str) -> TaskNote | None:
        cur = await self.db.conn.execute(
            "SELECT * FROM task_notes WHERE task_id = ? AND date = ?",
            (task_id, day),
        )
        row = await cur.fetchone()
        return _from_row(TaskNote, row) if row else None

    async def notes_for(self, task_id: int) -> list[TaskNote]:
        cur = await self.db.conn.execute(
            "SELECT * FROM task_notes WHERE task_id = ? ORDER BY date",
            (task_id,),
        )
        rows = await cur.fetchall()
        return [_from_row(TaskNote, r) for r in rows]

    async def notes_between(self, start: str, end: str, user_id: int) -> list[TaskNote]:
        cur = await self.db.conn.execute(
            "SELECT n.* FROM task_notes n JOIN tasks t ON t.id = n.task_id WHERE t.user_id = ? AND n.date BETWEEN ? AND ? ORDER BY n.date",
            (user_id, start, end),
        )
        rows = await cur.fetchall()
        return [_from_row(TaskNote, r) for r in rows]

    async def upsert_note(self, task_id: int, day: str, note: str) -> TaskNote:
        trimmed = note.strip()
        if not trimmed:
            await self.delete_note(task_id, day)
            return TaskNote(id=None, task_id=task_id, date=day, note="", updated_at="")
        await self.db.conn.execute(
            """
            INSERT INTO task_notes (task_id, date, note, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(task_id, date) DO UPDATE SET
                note = excluded.note,
                updated_at = excluded.updated_at
            """,
            (task_id, day, trimmed, now_iso()),
        )
        await self.db.conn.commit()
        return (await self.note_for(task_id, day)) or TaskNote(
            id=None, task_id=task_id, date=day, note=trimmed, updated_at=now_iso()
        )

    async def delete_note(self, task_id: int, day: str) -> None:
        await self.db.conn.execute(
            "DELETE FROM task_notes WHERE task_id = ? AND date = ?", (task_id, day)
        )
        await self.db.conn.commit()


@dataclass
class RoutineRepository:
    db: Database

    async def list(self, user_id: int) -> list[Routine]:
        cur = await self.db.conn.execute(
            "SELECT * FROM routines WHERE user_id = ? ORDER BY name", (user_id,)
        )
        rows = await cur.fetchall()
        return [_from_row(Routine, r) for r in rows]

    async def get(self, routine_id: int, user_id: int) -> Routine | None:
        cur = await self.db.conn.execute(
            "SELECT * FROM routines WHERE id = ? AND user_id = ?", (routine_id, user_id)
        )
        row = await cur.fetchone()
        return _from_row(Routine, row) if row else None

    async def create(self, routine: Routine) -> Routine:
        cur = await self.db.conn.execute(
            """
            INSERT INTO routines (user_id, name, description, frequency_count, frequency_unit, created_at, slot_days)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                routine.user_id,
                routine.name,
                routine.description,
                routine.frequency_count,
                routine.frequency_unit,
                routine.created_at,
                routine.slot_days,
            ),
        )
        await self.db.conn.commit()
        return await self.get(cur.lastrowid, routine.user_id)

    async def update(self, routine: Routine) -> Routine | None:
        await self.db.conn.execute(
            """
            UPDATE routines
            SET name = ?, description = ?, frequency_count = ?, frequency_unit = ?, slot_days = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                routine.name,
                routine.description,
                routine.frequency_count,
                routine.frequency_unit,
                routine.slot_days,
                routine.id,
                routine.user_id,
            ),
        )
        await self.db.conn.commit()
        return await self.get(routine.id, routine.user_id)

    async def delete(self, routine_id: int, user_id: int) -> None:
        await self.db.conn.execute(
            "DELETE FROM routines WHERE id = ? AND user_id = ?", (routine_id, user_id)
        )
        await self.db.conn.commit()

    async def all_completions(self, user_id: int) -> list[RoutineCompletion]:
        cur = await self.db.conn.execute(
            "SELECT c.* FROM routine_completions c JOIN routines r ON r.id = c.routine_id WHERE r.user_id = ? ORDER BY c.date",
            (user_id,),
        )
        rows = await cur.fetchall()
        return [_from_row(RoutineCompletion, r) for r in rows]

    async def completions_for_routine(self, routine_id: int) -> list[RoutineCompletion]:
        cur = await self.db.conn.execute(
            "SELECT * FROM routine_completions WHERE routine_id = ? ORDER BY date",
            (routine_id,),
        )
        rows = await cur.fetchall()
        return [_from_row(RoutineCompletion, r) for r in rows]

    async def completions_between(self, start: str, end: str, user_id: int) -> list[RoutineCompletion]:
        cur = await self.db.conn.execute(
            "SELECT c.* FROM routine_completions c JOIN routines r ON r.id = c.routine_id WHERE r.user_id = ? AND c.date BETWEEN ? AND ? ORDER BY c.date",
            (user_id, start, end),
        )
        rows = await cur.fetchall()
        return [_from_row(RoutineCompletion, r) for r in rows]

    async def add_completion(self, routine_id: int, day: str) -> RoutineCompletion:
        cur = await self.db.conn.execute(
            "INSERT INTO routine_completions (routine_id, date, completed_at) VALUES (?, ?, ?)",
            (routine_id, day, now_iso()),
        )
        await self.db.conn.commit()
        completion_id = cur.lastrowid
        cur = await self.db.conn.execute(
            "SELECT * FROM routine_completions WHERE id = ?", (completion_id,)
        )
        row = await cur.fetchone()
        return _from_row(RoutineCompletion, row)

    async def delete_completion(self, completion_id: int) -> None:
        await self.db.conn.execute(
            "DELETE FROM routine_completions WHERE id = ?", (completion_id,)
        )
        await self.db.conn.commit()

    async def delete_latest_for_day(self, routine_id: int, day: str) -> None:
        cur = await self.db.conn.execute(
            """
            SELECT id FROM routine_completions
            WHERE routine_id = ? AND date = ?
            ORDER BY id DESC LIMIT 1
            """,
            (routine_id, day),
        )
        row = await cur.fetchone()
        if row is not None:
            await self.delete_completion(row["id"])


@dataclass
class GoalRepository:
    db: Database

    async def list(self, user_id: int) -> list[Goal]:
        cur = await self.db.conn.execute(
            """
            SELECT * FROM goals WHERE user_id = ?
            ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,
                     updated_at DESC, id DESC
            """,
            (user_id,),
        )
        rows = await cur.fetchall()
        return [_from_row(Goal, r) for r in rows]

    async def get(self, goal_id: int, user_id: int) -> Goal | None:
        cur = await self.db.conn.execute(
            "SELECT * FROM goals WHERE id = ? AND user_id = ?", (goal_id, user_id)
        )
        row = await cur.fetchone()
        return _from_row(Goal, row) if row else None

    async def create(
        self,
        goal: Goal,
        fields: list[GoalField],
        stages: list[GoalStage] | None = None,
    ) -> Goal:
        cur = await self.db.conn.execute(
            """
            INSERT INTO goals (user_id, title, description, kind, show_chart, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                goal.user_id,
                goal.title,
                goal.description,
                goal.kind,
                goal.show_chart,
                goal.status,
                goal.created_at,
                goal.updated_at,
            ),
        )
        goal_id = cur.lastrowid
        for position, field in enumerate(fields):
            await self.db.conn.execute(
                """
                INSERT INTO goal_fields
                    (goal_id, name, unit, target_value, compare_mode, higher_is_better, position)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    goal_id,
                    field.name,
                    field.unit,
                    field.target_value,
                    field.compare_mode,
                    field.higher_is_better,
                    position,
                ),
            )
        for position, stage in enumerate(stages or []):
            await self.db.conn.execute(
                """
                INSERT INTO goal_stages (goal_id, title, position, done, completed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    goal_id,
                    stage.title,
                    position,
                    stage.done,
                    stage.completed_at,
                ),
            )
        await self.db.conn.commit()
        created = await self.get(goal_id, goal.user_id)
        assert created is not None
        return created

    async def update(
        self,
        goal: Goal,
        fields: list[GoalField] | None = None,
        stages: list[GoalStage] | None = None,
    ) -> Goal | None:
        await self.db.conn.execute(
            """
            UPDATE goals
            SET title = ?, description = ?, show_chart = ?, status = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                goal.title,
                goal.description,
                goal.show_chart,
                goal.status,
                goal.updated_at,
                goal.id,
                goal.user_id,
            ),
        )
        if fields is not None:
            existing = await self.fields_for(goal.id)
            existing_ids = {f.id for f in existing if f.id is not None}
            keep: set[int] = set()
            for position, field in enumerate(fields):
                if field.id is not None and field.id in existing_ids:
                    await self.db.conn.execute(
                        """
                        UPDATE goal_fields
                        SET name = ?, unit = ?, target_value = ?, compare_mode = ?,
                            higher_is_better = ?, position = ?
                        WHERE id = ? AND goal_id = ?
                        """,
                        (
                            field.name,
                            field.unit,
                            field.target_value,
                            field.compare_mode,
                            field.higher_is_better,
                            position,
                            field.id,
                            goal.id,
                        ),
                    )
                    keep.add(field.id)
                else:
                    await self.db.conn.execute(
                        """
                        INSERT INTO goal_fields
                            (goal_id, name, unit, target_value, compare_mode, higher_is_better, position)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            goal.id,
                            field.name,
                            field.unit,
                            field.target_value,
                            field.compare_mode,
                            field.higher_is_better,
                            position,
                        ),
                    )
            for field in existing:
                if field.id is not None and field.id not in keep:
                    await self.db.conn.execute(
                        "DELETE FROM goal_fields WHERE id = ? AND goal_id = ?",
                        (field.id, goal.id),
                    )
        if stages is not None:
            existing_stages = {s.id: s for s in await self.stages_for(goal.id) if s.id}
            keep_stages: set[int] = set()
            for position, stage in enumerate(stages):
                current = existing_stages.get(stage.id) if stage.id else None
                done = 1 if stage.done else 0
                completed_at = stage.completed_at
                if current is not None:
                    if done and not current.done:
                        completed_at = stage.completed_at or now_iso()
                    elif not done:
                        completed_at = None
                    elif current.completed_at:
                        completed_at = current.completed_at
                    else:
                        completed_at = now_iso()
                    await self.db.conn.execute(
                        """
                        UPDATE goal_stages
                        SET title = ?, position = ?, done = ?, completed_at = ?
                        WHERE id = ? AND goal_id = ?
                        """,
                        (stage.title, position, done, completed_at, stage.id, goal.id),
                    )
                    keep_stages.add(stage.id)
                else:
                    await self.db.conn.execute(
                        """
                        INSERT INTO goal_stages
                            (goal_id, title, position, done, completed_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            goal.id,
                            stage.title,
                            position,
                            done,
                            completed_at or (now_iso() if done else None),
                        ),
                    )
            for stage in existing_stages.values():
                if stage.id is not None and stage.id not in keep_stages:
                    await self.db.conn.execute(
                        "DELETE FROM goal_stages WHERE id = ? AND goal_id = ?",
                        (stage.id, goal.id),
                    )
        await self.db.conn.commit()
        return await self.get(goal.id, goal.user_id)

    async def touch(self, goal_id: int, when: str) -> None:
        await self.db.conn.execute(
            "UPDATE goals SET updated_at = ? WHERE id = ?", (when, goal_id)
        )
        await self.db.conn.commit()

    async def delete(self, goal_id: int, user_id: int) -> None:
        await self.db.conn.execute(
            "DELETE FROM goals WHERE id = ? AND user_id = ?", (goal_id, user_id)
        )
        await self.db.conn.commit()

    # --- fields ---

    async def fields_for(self, goal_id: int) -> list[GoalField]:
        cur = await self.db.conn.execute(
            "SELECT * FROM goal_fields WHERE goal_id = ? ORDER BY position, id",
            (goal_id,),
        )
        rows = await cur.fetchall()
        return [_from_row(GoalField, r) for r in rows]

    async def fields_for_user(self, user_id: int) -> list[GoalField]:
        cur = await self.db.conn.execute(
            """
            SELECT f.* FROM goal_fields f
            JOIN goals g ON g.id = f.goal_id
            WHERE g.user_id = ?
            ORDER BY f.goal_id, f.position, f.id
            """,
            (user_id,),
        )
        rows = await cur.fetchall()
        return [_from_row(GoalField, r) for r in rows]

    # --- stages ---

    async def stages_for(self, goal_id: int) -> list[GoalStage]:
        cur = await self.db.conn.execute(
            "SELECT * FROM goal_stages WHERE goal_id = ? ORDER BY position, id",
            (goal_id,),
        )
        rows = await cur.fetchall()
        return [_from_row(GoalStage, r) for r in rows]

    async def stages_for_user(self, user_id: int) -> list[GoalStage]:
        cur = await self.db.conn.execute(
            """
            SELECT s.* FROM goal_stages s
            JOIN goals g ON g.id = s.goal_id
            WHERE g.user_id = ?
            ORDER BY s.goal_id, s.position, s.id
            """,
            (user_id,),
        )
        rows = await cur.fetchall()
        return [_from_row(GoalStage, r) for r in rows]

    async def stage_for(self, stage_id: int, goal_id: int) -> GoalStage | None:
        cur = await self.db.conn.execute(
            "SELECT * FROM goal_stages WHERE id = ? AND goal_id = ?",
            (stage_id, goal_id),
        )
        row = await cur.fetchone()
        return _from_row(GoalStage, row) if row else None

    async def add_stage(self, goal_id: int, title: str) -> GoalStage:
        cur = await self.db.conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS next FROM goal_stages WHERE goal_id = ?",
            (goal_id,),
        )
        row = await cur.fetchone()
        position = int(row["next"]) if row else 0
        cur = await self.db.conn.execute(
            "INSERT INTO goal_stages (goal_id, title, position, done, completed_at) VALUES (?, ?, ?, 0, NULL)",
            (goal_id, title, position),
        )
        await self.db.conn.commit()
        created = await self.stage_for(cur.lastrowid, goal_id)
        assert created is not None
        return created

    async def update_stage(self, stage: GoalStage) -> GoalStage | None:
        await self.db.conn.execute(
            """
            UPDATE goal_stages
            SET title = ?, position = ?, done = ?, completed_at = ?
            WHERE id = ? AND goal_id = ?
            """,
            (
                stage.title,
                stage.position,
                stage.done,
                stage.completed_at,
                stage.id,
                stage.goal_id,
            ),
        )
        await self.db.conn.commit()
        return await self.stage_for(stage.id, stage.goal_id)

    async def delete_stage(self, stage_id: int, goal_id: int) -> None:
        await self.db.conn.execute(
            "DELETE FROM goal_stages WHERE id = ? AND goal_id = ?", (stage_id, goal_id)
        )
        await self.db.conn.commit()

    # --- entries ---

    async def entries_for(self, goal_id: int) -> list[GoalEntry]:
        cur = await self.db.conn.execute(
            "SELECT * FROM goal_entries WHERE goal_id = ? ORDER BY date, id",
            (goal_id,),
        )
        rows = await cur.fetchall()
        return [_from_row(GoalEntry, r) for r in rows]

    async def all_entries(self, user_id: int) -> list[GoalEntry]:
        cur = await self.db.conn.execute(
            """
            SELECT e.* FROM goal_entries e
            JOIN goals g ON g.id = e.goal_id
            WHERE g.user_id = ?
            ORDER BY e.goal_id, e.date, e.id
            """,
            (user_id,),
        )
        rows = await cur.fetchall()
        return [_from_row(GoalEntry, r) for r in rows]

    async def entry_for(self, entry_id: int, goal_id: int) -> GoalEntry | None:
        cur = await self.db.conn.execute(
            "SELECT * FROM goal_entries WHERE id = ? AND goal_id = ?",
            (entry_id, goal_id),
        )
        row = await cur.fetchone()
        return _from_row(GoalEntry, row) if row else None

    async def create_entry(
        self, entry: GoalEntry, values: dict[int, float]
    ) -> GoalEntry:
        cur = await self.db.conn.execute(
            """
            INSERT INTO goal_entries (goal_id, date, note, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (entry.goal_id, entry.date, entry.note, entry.created_at),
        )
        entry_id = cur.lastrowid
        for field_id, value in values.items():
            await self.db.conn.execute(
                "INSERT INTO goal_values (entry_id, field_id, value) VALUES (?, ?, ?)",
                (entry_id, field_id, value),
            )
        await self.db.conn.commit()
        created = await self.entry_for(entry_id, entry.goal_id)
        assert created is not None
        return created

    async def update_entry(
        self, entry: GoalEntry, values: dict[int, float]
    ) -> GoalEntry:
        await self.db.conn.execute(
            "UPDATE goal_entries SET date = ?, note = ? WHERE id = ? AND goal_id = ?",
            (entry.date, entry.note, entry.id, entry.goal_id),
        )
        await self.db.conn.execute(
            "DELETE FROM goal_values WHERE entry_id = ?", (entry.id,)
        )
        for field_id, value in values.items():
            await self.db.conn.execute(
                "INSERT INTO goal_values (entry_id, field_id, value) VALUES (?, ?, ?)",
                (entry.id, field_id, value),
            )
        await self.db.conn.commit()
        updated = await self.entry_for(entry.id, entry.goal_id)
        assert updated is not None
        return updated

    async def delete_entry(self, entry_id: int, goal_id: int) -> None:
        await self.db.conn.execute(
            "DELETE FROM goal_entries WHERE id = ? AND goal_id = ?", (entry_id, goal_id)
        )
        await self.db.conn.commit()

    # --- values ---

    async def values_for(self, goal_id: int) -> list[GoalValue]:
        cur = await self.db.conn.execute(
            """
            SELECT v.* FROM goal_values v
            JOIN goal_entries e ON e.id = v.entry_id
            WHERE e.goal_id = ?
            ORDER BY e.date, e.id, v.field_id
            """,
            (goal_id,),
        )
        rows = await cur.fetchall()
        return [_from_row(GoalValue, r) for r in rows]

    async def values_for_entry(self, entry_id: int) -> list[GoalValue]:
        cur = await self.db.conn.execute(
            "SELECT * FROM goal_values WHERE entry_id = ? ORDER BY field_id",
            (entry_id,),
        )
        rows = await cur.fetchall()
        return [_from_row(GoalValue, r) for r in rows]

    async def all_values(self, user_id: int) -> list[GoalValue]:
        cur = await self.db.conn.execute(
            """
            SELECT v.* FROM goal_values v
            JOIN goal_entries e ON e.id = v.entry_id
            JOIN goals g ON g.id = e.goal_id
            WHERE g.user_id = ?
            """,
            (user_id,),
        )
        rows = await cur.fetchall()
        return [_from_row(GoalValue, r) for r in rows]


@dataclass
class AdminRepository:
    db: Database

    async def _count(self, table: str) -> int:
        cur = await self.db.conn.execute(f"SELECT COUNT(*) AS n FROM {table}")
        row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def _count_since(self, table: str, since: str) -> int:
        cur = await self.db.conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE created_at >= ?", (since,)
        )
        row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def totals(self) -> dict:
        now = datetime.now()
        since_7 = (now - timedelta(days=7)).isoformat(timespec="seconds")
        since_30 = (now - timedelta(days=30)).isoformat(timespec="seconds")
        cur = await self.db.conn.execute(
            """
            SELECT
                COUNT(*) AS users,
                COALESCE(SUM(is_active), 0) AS active_users,
                COALESCE(SUM(is_admin), 0) AS admin_users
            FROM users
            """
        )
        row = await cur.fetchone()
        users = int(row["users"]) if row else 0
        active_users = int(row["active_users"]) if row else 0
        admin_users = int(row["admin_users"]) if row else 0
        return {
            "users": users,
            "active_users": active_users,
            "admin_users": admin_users,
            "disabled_users": users - active_users,
            "projects": await self._count("projects"),
            "tasks": await self._count("tasks"),
            "routines": await self._count("routines"),
            "goals": await self._count("goals"),
            "task_completions": await self._count("task_completions"),
            "routine_completions": await self._count("routine_completions"),
            "goal_stages": await self._count("goal_stages"),
            "new_users_7d": await self._count_since("users", since_7),
            "new_users_30d": await self._count_since("users", since_30),
        }

    async def user_stats(self) -> list[dict]:
        cur = await self.db.conn.execute(
            """
            SELECT u.id, u.email, u.is_admin, u.is_active, u.created_at,
                (SELECT COUNT(*) FROM projects p WHERE p.user_id = u.id) AS project_count,
                (SELECT COUNT(*) FROM tasks t WHERE t.user_id = u.id) AS task_count,
                (SELECT COUNT(*) FROM routines r WHERE r.user_id = u.id) AS routine_count,
                (SELECT COUNT(*) FROM goals g WHERE g.user_id = u.id) AS goal_count,
                (SELECT COUNT(*) FROM task_completions c
                    JOIN tasks t ON t.id = c.task_id WHERE t.user_id = u.id) AS task_completion_count,
                (SELECT COUNT(*) FROM routine_completions c
                    JOIN routines r ON r.id = c.routine_id WHERE r.user_id = u.id) AS routine_completion_count,
                max(
                    coalesce((SELECT max(updated_at) FROM projects WHERE user_id = u.id), ''),
                    coalesce((SELECT max(created_at) FROM tasks WHERE user_id = u.id), ''),
                    coalesce((SELECT max(created_at) FROM routines WHERE user_id = u.id), ''),
                    coalesce((SELECT max(updated_at) FROM goals WHERE user_id = u.id), ''),
                    coalesce(u.created_at, '')
                ) AS last_activity
            FROM users u
            ORDER BY u.created_at DESC, u.id DESC
            """
        )
        return [dict(row) for row in await cur.fetchall()]

    async def user_stats_for(self, user_id: int) -> dict | None:
        rows = await self.user_stats()
        for row in rows:
            if row["id"] == user_id:
                return row
        return None

    async def recent_content(self, user_id: int) -> dict:
        cur = await self.db.conn.execute(
            """
            SELECT id, name, status, deadline, updated_at
            FROM projects WHERE user_id = ?
            ORDER BY updated_at DESC, id DESC LIMIT 5
            """,
            (user_id,),
        )
        projects = [dict(row) for row in await cur.fetchall()]
        cur = await self.db.conn.execute(
            """
            SELECT id, title, due_date, created_at
            FROM tasks WHERE user_id = ?
            ORDER BY created_at DESC, id DESC LIMIT 5
            """,
            (user_id,),
        )
        tasks = [dict(row) for row in await cur.fetchall()]
        cur = await self.db.conn.execute(
            """
            SELECT id, title, kind, status, updated_at
            FROM goals WHERE user_id = ?
            ORDER BY updated_at DESC, id DESC LIMIT 5
            """,
            (user_id,),
        )
        goals = [dict(row) for row in await cur.fetchall()]
        return {"projects": projects, "tasks": tasks, "goals": goals}

    async def signup_series(self, days: int = 14) -> list[dict]:
        today = datetime.now().date()
        start = today - timedelta(days=days - 1)
        cur = await self.db.conn.execute(
            """
            SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n
            FROM users WHERE created_at >= ?
            GROUP BY day ORDER BY day
            """,
            (start.isoformat(),),
        )
        counts = {row["day"]: int(row["n"]) for row in await cur.fetchall()}
        return [
            {
                "date": (start + timedelta(days=offset)).isoformat(),
                "count": counts.get((start + timedelta(days=offset)).isoformat(), 0),
            }
            for offset in range(days)
        ]

    async def log(
        self,
        admin_user_id: int | None,
        action: str,
        target_user_id: int | None = None,
        detail: str = "",
    ) -> None:
        await self.db.conn.execute(
            """
            INSERT INTO admin_audit (admin_user_id, action, target_user_id, detail, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (admin_user_id, action, target_user_id, detail, now_iso()),
        )
        await self.db.conn.commit()

    async def recent_audit(self, limit: int = 20) -> list[dict]:
        cur = await self.db.conn.execute(
            """
            SELECT a.*, admin.email AS admin_email, target.email AS target_email
            FROM admin_audit a
            LEFT JOIN users admin ON admin.id = a.admin_user_id
            LEFT JOIN users target ON target.id = a.target_user_id
            ORDER BY a.id DESC LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def audit_for_user(self, user_id: int, limit: int = 10) -> list[dict]:
        cur = await self.db.conn.execute(
            """
            SELECT a.*, admin.email AS admin_email, target.email AS target_email
            FROM admin_audit a
            LEFT JOIN users admin ON admin.id = a.admin_user_id
            LEFT JOIN users target ON target.id = a.target_user_id
            WHERE a.target_user_id = ? OR a.admin_user_id = ?
            ORDER BY a.id DESC LIMIT ?
            """,
            (user_id, user_id, limit),
        )
        return [dict(row) for row in await cur.fetchall()]