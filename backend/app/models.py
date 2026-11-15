from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

ProjectStatus = Literal["open", "closed", "archived"]
RepeatUnit = Literal["day", "week", "month"]
FrequencyUnit = Literal["day", "week", "month", "year"]
CompletionMode = Literal["carry", "wait", "skip"]
GoalKind = Literal["measured", "percent", "staged"]
GoalStatus = Literal["active", "archived"]
GoalCompare = Literal["latest", "average", "best"]

PROJECT_STATUSES: tuple[ProjectStatus, ...] = ("open", "closed", "archived")
REPEAT_UNITS: tuple[RepeatUnit, ...] = ("day", "week", "month")
FREQUENCY_UNITS: tuple[FrequencyUnit, ...] = ("day", "week", "month", "year")
COMPLETION_MODES: tuple[CompletionMode, ...] = ("carry", "wait", "skip")
GOAL_KINDS: tuple[GoalKind, ...] = ("measured", "percent", "staged")
GOAL_STATUSES: tuple[GoalStatus, ...] = ("active", "archived")
GOAL_COMPARE_MODES: tuple[GoalCompare, ...] = ("latest", "average", "best")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def today_iso() -> str:
    return date.today().isoformat()


@dataclass(slots=True)
class User:
    id: int | None
    email: str
    password_hash: str = ""
    is_admin: int = 0
    is_active: int = 1
    created_at: str = ""

    @classmethod
    def from_row(cls, row: object) -> "User":
        return cls(**dict(row))


@dataclass(slots=True)
class Project:
    id: int | None
    name: str
    status: str = "open"
    description: str = ""
    deadline: str | None = None
    created_at: str = ""
    updated_at: str = ""
    user_id: int | None = None

    @classmethod
    def from_row(cls, row: object) -> "Project":
        return cls(**dict(row))


@dataclass(slots=True)
class Schedule:
    id: int | None
    task_id: int | None
    repeat_count: int
    repeat_unit: str
    start_date: str
    end_date: str | None = None
    duration_count: int | None = None
    duration_unit: str | None = None
    slot_days: str | None = None

    @classmethod
    def from_row(cls, row: object) -> "Schedule":
        return cls(**dict(row))


@dataclass(slots=True)
class Task:
    id: int | None
    title: str
    description: str = ""
    project_id: int | None = None
    created_at: str = ""
    start_date: str = ""
    due_date: str | None = None
    completion_mode: str = "carry"
    schedule: Schedule | None = None
    user_id: int | None = None

    @classmethod
    def from_row(cls, row: object) -> "Task":
        return cls(**dict(row))


@dataclass(slots=True)
class TaskCompletion:
    id: int | None
    task_id: int
    date: str
    completed_at: str = ""

    @classmethod
    def from_row(cls, row: object) -> "TaskCompletion":
        return cls(**dict(row))


@dataclass(slots=True)
class TaskNote:
    id: int | None
    task_id: int
    date: str
    note: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: object) -> "TaskNote":
        return cls(**dict(row))


@dataclass(slots=True)
class Routine:
    id: int | None
    name: str
    frequency_count: int
    frequency_unit: str
    description: str = ""
    created_at: str = ""
    slot_days: str | None = None
    user_id: int | None = None

    @classmethod
    def from_row(cls, row: object) -> "Routine":
        return cls(**dict(row))


@dataclass(slots=True)
class RoutineCompletion:
    id: int | None
    routine_id: int
    date: str
    completed_at: str = ""

    @classmethod
    def from_row(cls, row: object) -> "RoutineCompletion":
        return cls(**dict(row))


@dataclass(slots=True)
class Goal:
    id: int | None
    user_id: int | None
    title: str
    description: str = ""
    kind: str = "measured"
    show_chart: int = 1
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: object) -> "Goal":
        return cls(**dict(row))


@dataclass(slots=True)
class GoalField:
    id: int | None
    goal_id: int | None
    name: str
    unit: str = ""
    target_value: float | None = None
    compare_mode: str = "latest"
    higher_is_better: int = 1
    position: int = 0

    @classmethod
    def from_row(cls, row: object) -> "GoalField":
        return cls(**dict(row))


@dataclass(slots=True)
class GoalEntry:
    id: int | None
    goal_id: int
    date: str
    note: str = ""
    created_at: str = ""

    @classmethod
    def from_row(cls, row: object) -> "GoalEntry":
        return cls(**dict(row))


@dataclass(slots=True)
class GoalValue:
    id: int | None
    entry_id: int
    field_id: int
    value: float

    @classmethod
    def from_row(cls, row: object) -> "GoalValue":
        return cls(**dict(row))


@dataclass(slots=True)
class GoalStage:
    id: int | None
    goal_id: int | None
    title: str
    position: int = 0
    done: int = 0
    completed_at: str | None = None

    @classmethod
    def from_row(cls, row: object) -> "GoalStage":
        return cls(**dict(row))