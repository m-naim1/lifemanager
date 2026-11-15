"""Service layer - orchestrates repositories + pure schedule logic.

Kept async and dependency-light; each service takes the repositories it needs
so it stays easy to test without touching HTTP.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from .auth import hash_password, normalize_email
from .models import (
    GOAL_COMPARE_MODES,
    GOAL_KINDS,
    GOAL_STATUSES,
    Goal,
    GoalEntry,
    GoalField,
    GoalStage,
    Routine,
    Task,
    User,
    now_iso,
    today_iso,
)
from .repositories import (
    AdminRepository,
    GoalRepository,
    ProjectRepository,
    RoutineRepository,
    TaskRepository,
    UserRepository,
)
from .schedule import (
    is_slot_day,
    matches_days,
    parse_slot_days,
    period_bounds,
)

HEATMAP_WEEKS = 52


@dataclass
class TaskService:
    tasks: TaskRepository

    async def index(self, user_id: int) -> list[dict]:
        """All tasks with schedule, done flag and completion history."""
        tasks = await self.tasks.all_with_schedules(user_id)
        completions = await self.tasks.all_completions(user_id)
        total_by_task: dict[int, int] = defaultdict(int)
        dates_by_task: dict[int, list[str]] = defaultdict(list)
        for c in completions:
            total_by_task[c.task_id] += 1
            dates_by_task[c.task_id].append(c.date)
        out = []
        for t in tasks:
            total = total_by_task.get(t.id, 0)
            item = {
                **self._task_dict(t),
                "done": t.schedule is None and total > 0,
                "completed_total": total,
                "completions": [
                    {"id": c.id, "date": c.date}
                    for c in completions
                    if c.task_id == t.id
                ],
            }
            out.append(item)
        return out

    async def detail(self, task_id: int, user_id: int) -> dict | None:
        task = await self.tasks.get(task_id, user_id)
        if task is None:
            return None
        for item in await self.index(user_id):
            if item["id"] == task_id:
                return item
        return None

    @staticmethod
    def _task_dict(task: Task) -> dict:
        schedule = task.schedule
        return {
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "project_id": task.project_id,
            "created_at": task.created_at,
            "start_date": task.start_date or task.created_at,
            "due_date": task.due_date,
            "completion_mode": task.completion_mode or "carry",
            "schedule": {
                "id": schedule.id,
                "repeat_count": schedule.repeat_count,
                "repeat_unit": schedule.repeat_unit,
                "start_date": schedule.start_date,
                "end_date": schedule.end_date,
                "duration_count": schedule.duration_count,
                "duration_unit": schedule.duration_unit,
                "slot_days": parse_slot_days(schedule.slot_days),
            }
            if schedule is not None
            else None,
        }


@dataclass
class TodayService:
    projects: ProjectRepository
    tasks: TaskRepository
    routines: RoutineRepository

    async def view(self, day: date, user_id: int) -> dict:
        iso = day.isoformat()

        projects_due = await self.projects.due_on(iso, user_id)

        tasks = await self.tasks.all_with_schedules(user_id)
        task_comps = await self.tasks.all_completions(user_id)
        total_by_task: dict[int, int] = defaultdict(int)
        dates_by_task: dict[int, list[str]] = defaultdict(list)
        for c in task_comps:
            total_by_task[c.task_id] += 1
            dates_by_task[c.task_id].append(c.date)

        notes = await self.tasks.notes_between(iso, iso, user_id)
        note_by_task = {n.task_id: n.note for n in notes}

        tasks_view: list[dict] = []
        overdue: list[dict] = []

        for t in tasks:
            schedule = t.schedule
            task_dict = TaskService._task_dict(t)
            base = {"task": task_dict}
            note = note_by_task.get(t.id)
            if note:
                base["note"] = note

            if schedule is None:
                # One-off task: due from its start date until completed.
                if total_by_task.get(t.id, 0) > 0:
                    continue
                start = date.fromisoformat(t.start_date or t.created_at)
                if day < start:
                    continue
                if t.due_date:
                    due = date.fromisoformat(t.due_date)
                    if day > due:
                        if t.completion_mode == "skip":
                            continue
                        if t.completion_mode == "wait":
                            overdue.append(
                                {**base, "progress": self._once_progress(iso, overdue=True)}
                            )
                            continue
                        # carry → still due, flagged as overdue
                        tasks_view.append(
                            {**base, "progress": self._once_progress(iso, overdue=True)}
                        )
                        continue
                tasks_view.append(
                    {**base, "progress": self._once_progress(iso, overdue=False)}
                )
                continue

            start = date.fromisoformat(schedule.start_date)
            end = date.fromisoformat(schedule.end_date) if schedule.end_date else None
            if day < start or (end is not None and day > end):
                continue
            slot_days = parse_slot_days(schedule.slot_days)
            slot_today = is_slot_day(
                day,
                schedule.repeat_count,
                schedule.repeat_unit,
                start,
                end,
                slot_days,
            )
            period = period_bounds(day, schedule.repeat_unit)
            completed = sum(
                1
                for d in dates_by_task.get(t.id, [])
                if period[0].isoformat() <= d <= period[1].isoformat()
            )
            if completed >= schedule.repeat_count:
                continue
            progress = {
                "kind": schedule.repeat_unit,
                "completed": completed,
                "target": schedule.repeat_count,
                "period_start": period[0].isoformat(),
                "period_end": period[1].isoformat(),
            }
            if slot_today:
                tasks_view.append({**base, "progress": {**progress, "overdue": False}})
                continue
            had_past_slot = self._past_slot_in_period(
                day,
                period[0],
                schedule.repeat_count,
                schedule.repeat_unit,
                start,
                end,
                slot_days,
            )
            if t.completion_mode == "carry":
                # Missed slot rolls forward inside the period until the target is met.
                if had_past_slot:
                    tasks_view.append({**base, "progress": {**progress, "overdue": True}})
                continue
            if t.completion_mode == "wait":
                # Missed slots accumulate in the Overdue section for the user to decide.
                if had_past_slot:
                    overdue.append({**base, "progress": {**progress, "overdue": True}})
                continue
            # skip: missed slots are silently dropped.

        routines_list = await self.routines.list(user_id)
        routine_comps = await self.routines.all_completions(user_id)
        dates_by_routine: dict[int, list[str]] = defaultdict(list)
        for c in routine_comps:
            dates_by_routine[c.routine_id].append(c.date)

        routines_view: list[dict] = []
        for r in routines_list:
            period = period_bounds(day, r.frequency_unit)
            completed = sum(
                1
                for d in dates_by_routine.get(r.id, [])
                if period[0].isoformat() <= d <= period[1].isoformat()
            )
            if completed >= r.frequency_count:
                continue
            if not matches_days(day, r.frequency_unit, parse_slot_days(r.slot_days)):
                continue
            routines_view.append(
                {
                    "routine": self._routine_dict(r),
                    "progress": {
                        "kind": r.frequency_unit,
                        "completed": completed,
                        "target": r.frequency_count,
                        "period_start": period[0].isoformat(),
                        "period_end": period[1].isoformat(),
                    },
                }
            )

        return {
            "date": iso,
            "projects_due_today": [
                {
                    "id": p.id,
                    "name": p.name,
                    "description": p.description,
                    "status": p.status,
                    "deadline": p.deadline,
                }
                for p in projects_due
            ],
            "tasks": tasks_view,
            "overdue": overdue,
            "routines": routines_view,
        }

    @staticmethod
    def _once_progress(iso: str, overdue: bool = False) -> dict:
        return {
            "kind": "once",
            "completed": 0,
            "target": 1,
            "period_start": iso,
            "period_end": iso,
            "overdue": overdue,
        }

    @staticmethod
    def _past_slot_in_period(
        today: date,
        period_start: date,
        count: int,
        unit: str,
        start: date,
        end: date | None,
        slot_days: list[int] | None,
    ) -> bool:
        d = today - timedelta(days=1)
        while d >= period_start:
            if is_slot_day(d, count, unit, start, end, slot_days):
                return True
            d -= timedelta(days=1)
        return False

    @staticmethod
    def _routine_dict(r: Routine) -> dict:
        return {
            "id": r.id,
            "name": r.name,
            "description": r.description,
            "frequency_count": r.frequency_count,
            "frequency_unit": r.frequency_unit,
            "created_at": r.created_at,
            "slot_days": parse_slot_days(r.slot_days),
        }


@dataclass
class HeatmapService:
    tasks: TaskRepository
    routines: RoutineRepository

    async def build(
        self, kind: Literal["tasks", "routines"], today: date, user_id: int
    ) -> dict:
        end = today
        # Align to a Sunday-start week so the final column is the current week.
        last_sat = end - timedelta(days=(end.weekday() + 2) % 7)
        start = last_sat - timedelta(days=(HEATMAP_WEEKS - 1) * 7)

        if kind == "tasks":
            rows = await self.tasks.completions_between(
                start.isoformat(), end.isoformat(), user_id
            )
        else:
            rows = await self.routines.completions_between(
                start.isoformat(), end.isoformat(), user_id
            )

        counts: dict[str, int] = defaultdict(int)
        for row in rows:
            counts[row.date] += 1

        max_count = max(counts.values(), default=0)
        total = sum(counts.values())
        weeks: list[list[dict]] = []
        for w in range(HEATMAP_WEEKS):
            column: list[dict] = []
            for e in range(7):
                day = start + timedelta(days=w * 7 + e)
                if day > today:
                    column.append({"date": day.isoformat(), "count": None})
                else:
                    column.append({"date": day.isoformat(), "count": counts.get(day.isoformat(), 0)})
            weeks.append(column)

        return {
            "kind": kind,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "weeks": weeks,
            "max": max_count,
            "total": total,
            "months": self._month_labels(start, HEATMAP_WEEKS * 7),
        }

    @staticmethod
    def _month_labels(start: date, total_days: int) -> list[dict]:
        labels: list[dict] = []
        for offset in range(0, total_days, 7):
            day = start + timedelta(days=offset)
            if day.day <= 7 and (not labels or labels[-1]["text"] != day.strftime("%b")):
                col = offset // 7
                if labels and labels[-1]["col"] == col:
                    continue
                labels.append({"col": col, "text": day.strftime("%b")})
        return labels


@dataclass
class CalendarService:
    tasks: TaskRepository
    routines: RoutineRepository

    async def build(
        self, kind: Literal["tasks", "routines"], year: int, month: int, user_id: int
    ) -> dict:
        """Per-day completion counts and (for tasks) day notes for a month."""
        p_start, p_end = period_bounds(date(year, month, 1), "month")

        if kind == "tasks":
            comps = await self.tasks.completions_between(
                p_start.isoformat(), p_end.isoformat(), user_id
            )
            notes_rows = await self.tasks.notes_between(
                p_start.isoformat(), p_end.isoformat(), user_id
            )
            tasks_list = await self.tasks.list(user_id)
            by_id = {t.id: t for t in tasks_list}
            notes: dict[str, list[dict]] = defaultdict(list)
            for n in notes_rows:
                task = by_id.get(n.task_id)
                if task is None:
                    continue
                notes[n.date].append(
                    {"task_id": task.id, "title": task.title, "note": n.note}
                )
            completions: dict[str, list[dict]] = defaultdict(list)
            for c in comps:
                task = by_id.get(c.task_id)
                if task is None:
                    continue
                completions[c.date].append({"task_id": task.id, "title": task.title})
        else:
            comps = await self.routines.completions_between(
                p_start.isoformat(), p_end.isoformat(), user_id
            )
            routines_list = await self.routines.list(user_id)
            by_id = {r.id: r for r in routines_list}
            notes = {}
            completions = defaultdict(list)
            for c in comps:
                routine = by_id.get(c.routine_id)
                if routine is None:
                    continue
                completions[c.date].append(
                    {"routine_id": routine.id, "title": routine.name}
                )

        day_counts: dict[str, int] = defaultdict(int)
        for c in comps:
            day_counts[c.date] += 1

        days: list[dict] = []
        d = p_start
        while d <= p_end:
            iso = d.isoformat()
            days.append(
                {
                    "date": iso,
                    "day_of_week": d.weekday(),
                    "count": day_counts.get(iso, 0),
                }
            )
            d += timedelta(days=1)

        return {
            "year": year,
            "month": month,
            "max": max(day_counts.values(), default=0),
            "days": days,
            "notes": {k: v for k, v in notes.items()} if kind == "tasks" else {},
            "completions": {k: v for k, v in completions.items()},
        }


class GoalValidationError(ValueError):
    """A goal/entry payload is malformed or inconsistent."""


class AdminValidationError(ValueError):
    """An admin action is invalid or would lock the deployment out."""


GOAL_TONES = ("blue", "emerald", "amber", "violet", "rose", "sky")


def _item_value(item: object, name: str, default=None):
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _format_number(value: float | None) -> str:
    if value is None:
        return "—"
    text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return text or "0"


@dataclass
class GoalService:
    goals: GoalRepository

    async def index(self, user_id: int) -> list[dict]:
        goals = await self.goals.list(user_id)
        if not goals:
            return []

        fields_by_goal: dict[int, list[GoalField]] = defaultdict(list)
        for field in await self.goals.fields_for_user(user_id):
            fields_by_goal[field.goal_id].append(field)

        entries_by_goal: dict[int, list[GoalEntry]] = defaultdict(list)
        for entry in await self.goals.all_entries(user_id):
            entries_by_goal[entry.goal_id].append(entry)

        stages_by_goal: dict[int, list[GoalStage]] = defaultdict(list)
        for stage in await self.goals.stages_for_user(user_id):
            stages_by_goal[stage.goal_id].append(stage)

        values_by_entry: dict[int, list] = defaultdict(list)
        for value in await self.goals.all_values(user_id):
            values_by_entry[value.entry_id].append(value)

        return [
            self._summary(
                goal,
                fields_by_goal.get(goal.id, []),
                entries_by_goal.get(goal.id, []),
                values_by_entry,
                stages_by_goal.get(goal.id, []),
            )
            for goal in goals
        ]

    async def detail(self, goal_id: int, user_id: int) -> dict | None:
        goal = await self.goals.get(goal_id, user_id)
        if goal is None:
            return None
        fields = await self.goals.fields_for(goal_id)
        entries = await self.goals.entries_for(goal_id)
        stages = await self.goals.stages_for(goal_id)
        values = await self.goals.values_for(goal_id)

        values_by_entry: dict[int, list] = defaultdict(list)
        for value in values:
            values_by_entry[value.entry_id].append(value)

        summary = self._summary(goal, fields, entries, values_by_entry, stages)
        field_by_id = {field.id: field for field in fields}
        entry_rows = []
        for entry in entries:
            entry_values = values_by_entry.get(entry.id, [])
            value_by_field = {v.field_id: v.value for v in entry_values}
            entry_rows.append(
                {
                    "id": entry.id,
                    "date": entry.date,
                    "note": entry.note,
                    "created_at": entry.created_at,
                    "value_by_field": value_by_field,
                    "values": [
                        {
                            "field_id": v.field_id,
                            "value": v.value,
                            "name": field_by_id[v.field_id].name
                            if v.field_id in field_by_id
                            else "",
                            "unit": field_by_id[v.field_id].unit
                            if v.field_id in field_by_id
                            else "",
                        }
                        for v in entry_values
                    ],
                }
            )

        charts = []
        if goal.show_chart and goal.kind == "measured":
            for index, field in enumerate(fields):
                points = self._field_points(field, entries, values_by_entry)
                charts.append(self._chart(field, points, index))

        stage_rows = [
            {
                "id": stage.id,
                "title": stage.title,
                "position": stage.position,
                "done": bool(stage.done),
                "completed_at": stage.completed_at,
            }
            for stage in stages
        ]
        return {
            **summary,
            "entries": entry_rows,
            "charts": charts,
            "stages": stage_rows,
        }

    # --- mutations ---

    async def create_goal(
        self,
        user_id: int,
        title: str,
        description: str = "",
        kind: str = "measured",
        show_chart: bool = True,
        fields_data: list | None = None,
        stages_data: list | None = None,
    ) -> dict:
        title = (title or "").strip()
        if not title:
            raise GoalValidationError("Title is required")
        kind = kind if kind in GOAL_KINDS else "measured"
        now = now_iso()
        stages: list[GoalStage] = []
        if kind == "percent":
            fields = [
                GoalField(
                    id=None,
                    goal_id=None,
                    name="Progress",
                    unit="%",
                    target_value=100.0,
                    compare_mode="latest",
                    higher_is_better=1,
                )
            ]
        elif kind == "staged":
            fields = []
            stages = self._normalize_stages(stages_data or [])
            if not stages:
                raise GoalValidationError("Add at least one stage")
        else:
            fields = self._normalize_fields(fields_data or [])
            if not fields:
                raise GoalValidationError("Add at least one metric")

        goal = await self.goals.create(
            Goal(
                id=None,
                user_id=user_id,
                title=title,
                description=(description or "").strip(),
                kind=kind,
                show_chart=1 if show_chart else 0,
                status="active",
                created_at=now,
                updated_at=now,
            ),
            fields,
            stages,
        )
        detail = await self.detail(goal.id, user_id)
        assert detail is not None
        return detail

    async def update_goal(
        self,
        goal: Goal,
        *,
        title: str,
        description: str,
        show_chart: bool,
        fields_data: list | None,
        stages_data: list | None = None,
        status: str | None = None,
    ) -> dict:
        title = (title or "").strip()
        if not title:
            raise GoalValidationError("Title is required")
        if status is not None and status not in GOAL_STATUSES:
            raise GoalValidationError("Invalid status")

        fields = None
        if goal.kind == "measured" and fields_data is not None:
            fields = self._normalize_fields(fields_data)
            if not fields:
                raise GoalValidationError("Add at least one metric")
        stages = None
        if goal.kind == "staged" and stages_data is not None:
            stages = self._normalize_stages(stages_data)
            if not stages:
                raise GoalValidationError("Add at least one stage")

        goal.title = title
        goal.description = (description or "").strip()
        goal.show_chart = 1 if show_chart else 0
        if status is not None:
            goal.status = status
        goal.updated_at = now_iso()
        updated = await self.goals.update(goal, fields, stages)
        if updated is None:
            raise GoalValidationError("Goal not found")
        detail = await self.detail(updated.id, goal.user_id)
        assert detail is not None
        return detail

    async def set_status(self, goal: Goal, status: str) -> dict | None:
        if status not in GOAL_STATUSES:
            raise GoalValidationError("Invalid status")
        goal.status = status
        goal.updated_at = now_iso()
        await self.goals.update(goal)
        return await self.detail(goal.id, goal.user_id)

    async def delete_goal(self, goal_id: int, user_id: int) -> None:
        await self.goals.delete(goal_id, user_id)

    async def add_entry(
        self,
        goal: Goal,
        day: str | None,
        note: str,
        raw_values: list,
    ) -> dict:
        fields = await self.goals.fields_for(goal.id)
        values = self._normalize_values(goal, fields, raw_values)
        entry = GoalEntry(
            id=None,
            goal_id=goal.id,
            date=self._parse_day(day),
            note=(note or "").strip(),
            created_at=now_iso(),
        )
        created = await self.goals.create_entry(entry, values)
        await self.goals.touch(goal.id, now_iso())
        detail = await self.detail(goal.id, goal.user_id)
        assert detail is not None
        return detail

    async def update_entry(
        self,
        goal: Goal,
        entry: GoalEntry,
        day: str | None,
        note: str | None,
        raw_values: list | None,
    ) -> dict:
        fields = await self.goals.fields_for(goal.id)
        if raw_values is not None:
            values = self._normalize_values(goal, fields, raw_values)
        else:
            values = {
                value.field_id: value.value
                for value in await self.goals.values_for_entry(entry.id)
            }
        entry.date = self._parse_day(day) if day else entry.date
        if note is not None:
            entry.note = note.strip()
        await self.goals.update_entry(entry, values)
        await self.goals.touch(goal.id, now_iso())
        detail = await self.detail(goal.id, goal.user_id)
        assert detail is not None
        return detail

    async def delete_entry(self, goal: Goal, entry_id: int) -> dict:
        await self.goals.delete_entry(entry_id, goal.id)
        await self.goals.touch(goal.id, now_iso())
        detail = await self.detail(goal.id, goal.user_id)
        assert detail is not None
        return detail

    async def add_stage(self, goal: Goal, title: str) -> dict:
        if goal.kind != "staged":
            raise GoalValidationError("This goal does not use stages")
        title = (title or "").strip()
        if not title:
            raise GoalValidationError("Stage title is required")
        await self.goals.add_stage(goal.id, title)
        await self.goals.touch(goal.id, now_iso())
        detail = await self.detail(goal.id, goal.user_id)
        assert detail is not None
        return detail

    async def toggle_stage(self, goal: Goal, stage_id: int) -> dict:
        if goal.kind != "staged":
            raise GoalValidationError("This goal does not use stages")
        stage = await self.goals.stage_for(stage_id, goal.id)
        if stage is None:
            raise GoalValidationError("Stage not found")
        stage.done = 0 if stage.done else 1
        stage.completed_at = now_iso() if stage.done else None
        await self.goals.update_stage(stage)
        await self.goals.touch(goal.id, now_iso())
        detail = await self.detail(goal.id, goal.user_id)
        assert detail is not None
        return detail

    async def update_stage(
        self,
        goal: Goal,
        stage: GoalStage,
        *,
        title: str | None = None,
        done: bool | None = None,
    ) -> dict:
        if title is not None:
            title = title.strip()
            if not title:
                raise GoalValidationError("Stage title is required")
            stage.title = title
        if done is not None:
            was_done = bool(stage.done)
            stage.done = 1 if done else 0
            if stage.done and not was_done:
                stage.completed_at = now_iso()
            elif not stage.done:
                stage.completed_at = None
        await self.goals.update_stage(stage)
        await self.goals.touch(goal.id, now_iso())
        detail = await self.detail(goal.id, goal.user_id)
        assert detail is not None
        return detail

    async def delete_stage(self, goal: Goal, stage_id: int) -> dict:
        await self.goals.delete_stage(stage_id, goal.id)
        await self.goals.touch(goal.id, now_iso())
        detail = await self.detail(goal.id, goal.user_id)
        assert detail is not None
        return detail

    # --- helpers ---

    def _summary(
        self,
        goal: Goal,
        fields: list[GoalField],
        entries: list[GoalEntry],
        values_by_entry: dict[int, list],
        stages: list[GoalStage] | None = None,
    ) -> dict:
        stages = stages or []
        field_summaries = [
            self._field_summary(field, index, entries, values_by_entry)
            for index, field in enumerate(fields)
        ]
        if goal.kind == "staged":
            stage_count = len(stages)
            stages_done = sum(1 for stage in stages if stage.done)
            target_count = stage_count
            at_target_count = stages_done
            percent = round(stages_done / stage_count * 100) if stage_count else 0
            achieved = stage_count > 0 and stages_done == stage_count
            latest_date = max(
                (
                    (stage.completed_at or "")[:10]
                    for stage in stages
                    if stage.completed_at
                ),
                default=(goal.created_at or "")[:10],
            )
        else:
            target_count = sum(
                1 for field in field_summaries if field["target_value"] is not None
            )
            at_target_count = sum(1 for field in field_summaries if field["at_target"])
            percents = [
                field["percent"]
                for field in field_summaries
                if field["percent"] is not None
            ]
            percent = round(sum(percents) / len(percents)) if percents else 0
            achieved = (
                bool(field_summaries)
                and field_summaries[0]["percent"] is not None
                and field_summaries[0]["percent"] >= 100
            ) if goal.kind == "percent" else (
                target_count > 0 and at_target_count == target_count
            )
            latest_date = entries[-1].date if entries else (goal.created_at or "")[:10]

        return {
            "id": goal.id,
            "title": goal.title,
            "description": goal.description,
            "kind": goal.kind,
            "status": goal.status,
            "show_chart": bool(goal.show_chart),
            "created_at": goal.created_at,
            "updated_at": goal.updated_at,
            "record_count": len(entries),
            "stage_count": len(stages),
            "stages_done": sum(1 for stage in stages if stage.done),
            "latest_date": latest_date,
            "percent": percent,
            "achieved": achieved,
            "target_count": target_count,
            "at_target_count": at_target_count,
            "fields": field_summaries,
            "stages": [
                {
                    "id": stage.id,
                    "title": stage.title,
                    "position": stage.position,
                    "done": bool(stage.done),
                    "completed_at": stage.completed_at,
                }
                for stage in stages
            ],
        }

    def _field_summary(
        self,
        field: GoalField,
        index: int,
        entries: list[GoalEntry],
        values_by_entry: dict[int, list],
    ) -> dict:
        points: list[tuple[str, int, float]] = []
        for entry in entries:
            for value in values_by_entry.get(entry.id, []):
                if value.field_id == field.id:
                    points.append((entry.date, entry.id or 0, float(value.value)))
        points.sort(key=lambda item: (item[0], item[1]))

        values = [item[2] for item in points]
        latest = values[-1] if values else None
        average = sum(values) / len(values) if values else None
        higher = bool(field.higher_is_better)
        best = (max(values) if higher else min(values)) if values else None
        current = {
            "latest": latest,
            "average": average,
            "best": best,
        }.get(field.compare_mode, latest)

        target = field.target_value
        at_target = False
        percent: int | None = None
        if target is not None and current is not None:
            if higher:
                at_target = current >= target
                percent = self._percent_higher(current, target)
            else:
                at_target = current <= target
                percent = self._percent_lower(current, target)

        return {
            "id": field.id,
            "name": field.name,
            "unit": field.unit,
            "target_value": target,
            "compare_mode": field.compare_mode,
            "higher_is_better": higher,
            "current": current,
            "latest": latest,
            "average": average,
            "best": best,
            "at_target": at_target,
            "percent": percent,
            "record_count": len(values),
            "tone": GOAL_TONES[index % len(GOAL_TONES)],
            "display_current": _format_number(current),
            "display_target": _format_number(target),
            "display_average": _format_number(average),
            "display_best": _format_number(best),
        }

    def _field_points(
        self,
        field: GoalField,
        entries: list[GoalEntry],
        values_by_entry: dict[int, list],
    ) -> list[dict]:
        points = []
        for entry in entries:
            for value in values_by_entry.get(entry.id, []):
                if value.field_id == field.id:
                    points.append({"date": entry.date, "value": float(value.value)})
        return points

    @staticmethod
    def _percent_higher(value: float, target: float) -> int:
        if target <= 0:
            return 100 if value >= target else 0
        return max(0, min(100, round(value / target * 100)))

    @staticmethod
    def _percent_lower(value: float, target: float) -> int:
        if target <= 0:
            return 100 if value <= target else 0
        if value <= 0:
            return 100
        return max(0, min(100, round(target / value * 100)))

    def _normalize_fields(self, fields_data: list) -> list[GoalField]:
        normalized: list[GoalField] = []
        for item in fields_data:
            name = str(_item_value(item, "name", "") or "").strip()
            if not name:
                raise GoalValidationError("Every metric needs a name")
            unit = str(_item_value(item, "unit", "") or "").strip()
            raw_target = _item_value(
                item, "target_value", _item_value(item, "target", None)
            )
            target: float | None = None
            if raw_target not in (None, ""):
                try:
                    target = float(raw_target)
                except (TypeError, ValueError) as exc:
                    raise GoalValidationError(f"Invalid target for {name}") from exc
                if not math.isfinite(target):
                    raise GoalValidationError(f"Invalid target for {name}")
            compare = str(
                _item_value(
                    item, "compare_mode", _item_value(item, "compare", "latest")
                )
                or "latest"
            )
            if compare not in GOAL_COMPARE_MODES:
                compare = "latest"
            direction = _item_value(
                item, "higher_is_better", _item_value(item, "direction", True)
            )
            if isinstance(direction, str):
                direction = direction.strip().lower() not in {
                    "",
                    "0",
                    "false",
                    "no",
                    "lower",
                }
            raw_id = _item_value(item, "id", None)
            field_id = None
            if raw_id not in (None, ""):
                try:
                    field_id = int(raw_id)
                except (TypeError, ValueError):
                    field_id = None
            normalized.append(
                GoalField(
                    id=field_id,
                    goal_id=None,
                    name=name,
                    unit=unit,
                    target_value=target,
                    compare_mode=compare,
                    higher_is_better=1 if direction else 0,
                    position=len(normalized),
                )
            )
        return normalized

    def _normalize_stages(self, stages_data: list) -> list[GoalStage]:
        normalized: list[GoalStage] = []
        for item in stages_data:
            title = str(_item_value(item, "title", "") or "").strip()
            raw_id = _item_value(item, "id", None)
            stage_id = None
            if raw_id not in (None, ""):
                try:
                    stage_id = int(raw_id)
                except (TypeError, ValueError):
                    stage_id = None
            if not title:
                if stage_id is None:
                    continue
                raise GoalValidationError("Every stage needs a title")
            done = _item_value(item, "done", False)
            if isinstance(done, str):
                done = done.strip().lower() in {"1", "true", "on", "yes", "done"}
            normalized.append(
                GoalStage(
                    id=stage_id,
                    goal_id=None,
                    title=title,
                    position=len(normalized),
                    done=1 if done else 0,
                )
            )
        return normalized

    def _normalize_values(
        self, goal: Goal, fields: list[GoalField], raw_values: list
    ) -> dict[int, float]:
        allowed = {field.id: field for field in fields}
        values: dict[int, float] = {}
        for item in raw_values:
            raw_field_id = _item_value(item, "field_id", None)
            raw_value = _item_value(item, "value", None)
            if raw_value in (None, ""):
                continue
            try:
                field_id = int(raw_field_id)
            except (TypeError, ValueError) as exc:
                raise GoalValidationError("Invalid metric") from exc
            field = allowed.get(field_id)
            if field is None:
                raise GoalValidationError("Invalid metric")
            try:
                value = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise GoalValidationError(f"Invalid value for {field.name}") from exc
            if not math.isfinite(value):
                raise GoalValidationError(f"Invalid value for {field.name}")
            if goal.kind == "percent" and not 0 <= value <= 100:
                raise GoalValidationError("Progress must be between 0 and 100")
            values[field_id] = value
        if not values:
            raise GoalValidationError("Enter at least one value")
        return values

    @staticmethod
    def _parse_day(day: str | None) -> str:
        raw = (day or "").strip() or today_iso()
        try:
            return date.fromisoformat(raw).isoformat()
        except ValueError as exc:
            raise GoalValidationError("Invalid date") from exc

    def _chart(self, field: GoalField, points: list[dict], index: int) -> dict:
        width, height = 560, 200
        left, right, top, bottom = 44, 18, 18, 30
        plot_w = width - left - right
        plot_h = height - top - bottom

        if not points:
            return {
                "field_id": field.id,
                "name": field.name,
                "unit": field.unit,
                "tone": GOAL_TONES[index % len(GOAL_TONES)],
                "has_data": False,
                "points": [],
                "avg_points": [],
                "path": "",
                "avg_path": "",
                "area": "",
                "target_y": None,
                "y_ticks": [],
                "x_labels": [],
            }

        values = [p["value"] for p in points]
        target = field.target_value
        domain = list(values)
        if target is not None:
            domain.append(target)
        y_min, y_max = min(domain), max(domain)
        if y_max == y_min:
            y_min -= 1
            y_max += 1
        else:
            padding = (y_max - y_min) * 0.15
            y_min -= padding
            y_max += padding

        days = [date.fromisoformat(p["date"]) for p in points]
        first_day, last_day = min(days), max(days)
        span = (last_day - first_day).days

        def x_for(day: date) -> float:
            if span == 0:
                return left + plot_w / 2
            return left + (day - first_day).days / span * plot_w

        def y_for(value: float) -> float:
            return top + (1 - (value - y_min) / (y_max - y_min)) * plot_h

        chart_points = [
            {
                "x": round(x_for(date.fromisoformat(p["date"])), 2),
                "y": round(y_for(p["value"]), 2),
                "date": p["date"],
                "value": p["value"],
                "label": f"{p['date']}: {_format_number(p['value'])}"
                + (f" {field.unit}" if field.unit else ""),
            }
            for p in points
        ]
        path = " ".join(
            f"{'M' if i == 0 else 'L'} {p['x']} {p['y']}"
            for i, p in enumerate(chart_points)
        )
        area = (
            f"M {chart_points[0]['x']} {top + plot_h} "
            + " ".join(f"L {p['x']} {p['y']}" for p in chart_points)
            + f" L {chart_points[-1]['x']} {top + plot_h} Z"
        )

        running = 0.0
        avg_points = []
        for i, p in enumerate(points, start=1):
            running += p["value"]
            avg_points.append(
                {
                    "x": chart_points[i - 1]["x"],
                    "y": round(y_for(running / i), 2),
                    "value": running / i,
                }
            )
        avg_path = " ".join(
            f"{'M' if i == 0 else 'L'} {p['x']} {p['y']}"
            for i, p in enumerate(avg_points)
        )

        tick_values = [y_max, (y_min + y_max) / 2, y_min]
        y_ticks = [
            {"y": round(y_for(value), 2), "label": _format_number(value)}
            for value in tick_values
        ]

        x_labels = []
        if span == 0:
            x_labels.append({"x": left + plot_w / 2, "label": first_day.strftime("%b %d")})
        else:
            x_labels.append({"x": left, "label": first_day.strftime("%b %d")})
            x_labels.append({"x": left + plot_w, "label": last_day.strftime("%b %d")})

        return {
            "field_id": field.id,
            "name": field.name,
            "unit": field.unit,
            "tone": GOAL_TONES[index % len(GOAL_TONES)],
            "has_data": True,
            "points": chart_points,
            "avg_points": avg_points if field.compare_mode == "average" else [],
            "path": path,
            "avg_path": avg_path if field.compare_mode == "average" else "",
            "area": area,
            "target_y": round(y_for(target), 2) if target is not None else None,
            "target_label": _format_number(target) if target is not None else "",
            "y_ticks": y_ticks,
            "x_labels": x_labels,
            "latest": values[-1],
        }


@dataclass
class AdminService:
    users: UserRepository
    admin: AdminRepository

    async def dashboard(
        self, q: str = "", role: str = "all", status: str = "all"
    ) -> dict:
        totals = await self.admin.totals()
        rows = await self.admin.user_stats()
        needle = q.strip().lower()
        filtered = []
        for row in rows:
            if needle and needle not in str(row["email"]).lower():
                continue
            if role == "admin" and not row["is_admin"]:
                continue
            if role == "member" and row["is_admin"]:
                continue
            if status == "active" and not row["is_active"]:
                continue
            if status == "disabled" and row["is_active"]:
                continue
            filtered.append(row)
        audit = await self.admin.recent_audit(15)
        series = await self.admin.signup_series(14)
        max_signup = max((item["count"] for item in series), default=0)
        return {
            "totals": totals,
            "users": filtered,
            "all_user_count": len(rows),
            "audit": audit,
            "signup_series": series,
            "max_signup": max_signup,
            "q": q,
            "role": role if role in ("admin", "member") else "all",
            "status": status if status in ("active", "disabled") else "all",
        }

    async def user_detail(self, user_id: int) -> dict | None:
        account = await self.admin.user_stats_for(user_id)
        if account is None:
            return None
        return {
            "account": account,
            "recent": await self.admin.recent_content(user_id),
            "audit": await self.admin.audit_for_user(user_id, 15),
        }

    async def create_user(
        self,
        admin_id: int,
        *,
        email: str,
        password: str,
        is_admin: bool = False,
        is_active: bool = True,
    ) -> dict:
        normalized = normalize_email(email)
        if "@" not in normalized:
            raise AdminValidationError("Enter a valid email address")
        if len(password) < 8:
            raise AdminValidationError("Password must be at least 8 characters")
        if await self.users.get_by_email(normalized) is not None:
            raise AdminValidationError("Email already registered")
        created = await self.users.create(
            User(
                id=None,
                email=normalized,
                password_hash=hash_password(password),
                is_admin=1 if is_admin else 0,
                is_active=1 if is_active else 0,
                created_at=now_iso(),
            )
        )
        await self.admin.log(admin_id, "user.create", created.id, normalized)
        detail = await self.user_detail(created.id)
        assert detail is not None
        return detail

    async def update_user(
        self,
        admin_id: int,
        target: User,
        *,
        email: str | None = None,
        password: str | None = None,
        is_admin: bool | None = None,
        is_active: bool | None = None,
    ) -> dict:
        changes: list[str] = []
        password_reset = False
        if email is not None:
            normalized = normalize_email(email)
            if "@" not in normalized:
                raise AdminValidationError("Enter a valid email address")
            existing = await self.users.get_by_email(normalized)
            if existing is not None and existing.id != target.id:
                raise AdminValidationError("Email already registered")
            if normalized != target.email:
                target.email = normalized
                changes.append("email")
        if password:
            if len(password) < 8:
                raise AdminValidationError("Password must be at least 8 characters")
            target.password_hash = hash_password(password)
            password_reset = True
        if is_admin is not None:
            next_admin = 1 if is_admin else 0
            if (
                target.id == admin_id
                and target.is_admin
                and not next_admin
                and await self._sole_admin(admin_id)
            ):
                raise AdminValidationError("Cannot demote the last admin account")
            if next_admin != target.is_admin:
                target.is_admin = next_admin
                changes.append("admin role")
        if is_active is not None:
            next_active = 1 if is_active else 0
            if target.id == admin_id and not next_active:
                raise AdminValidationError("Cannot disable yourself")
            if next_active != target.is_active:
                target.is_active = next_active
                changes.append("active status")
        if not changes and not password_reset:
            raise AdminValidationError("No changes to save")
        updated = await self.users.update(target)
        assert updated is not None
        if changes:
            await self.admin.log(
                admin_id, "user.update", target.id, ", ".join(changes)
            )
        if password_reset:
            await self.admin.log(
                admin_id, "user.reset_password", target.id, target.email
            )
        detail = await self.user_detail(target.id)
        assert detail is not None
        return detail

    async def reset_password(
        self, admin_id: int, target: User, password: str
    ) -> dict:
        if len(password) < 8:
            raise AdminValidationError("Password must be at least 8 characters")
        target.password_hash = hash_password(password)
        updated = await self.users.update(target)
        assert updated is not None
        await self.admin.log(admin_id, "user.reset_password", target.id, target.email)
        detail = await self.user_detail(target.id)
        assert detail is not None
        return detail

    async def toggle_admin(self, admin_id: int, target: User) -> dict:
        return await self.update_user(
            admin_id, target, is_admin=not bool(target.is_admin)
        )

    async def toggle_active(self, admin_id: int, target: User) -> dict:
        return await self.update_user(
            admin_id, target, is_active=not bool(target.is_active)
        )

    async def delete_user(self, admin_id: int, target: User) -> None:
        if target.id == admin_id:
            raise AdminValidationError("Cannot delete yourself")
        await self.admin.log(admin_id, "user.delete", target.id, target.email)
        await self.users.delete(target.id)

    async def _sole_admin(self, admin_id: int) -> bool:
        rows = await self.users.list()
        return not any(user.id != admin_id and user.is_admin for user in rows)