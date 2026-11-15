from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    deadline: str | None = None  # YYYY-MM-DD


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    status: Literal["open", "closed", "archived"] | None = None
    deadline: str | None = None


class ScheduleCreate(BaseModel):
    repeat_count: int = Field(ge=1, le=1000)
    repeat_unit: Literal["day", "week", "month"]
    end_date: str | None = None
    duration_count: int | None = Field(default=None, ge=1, le=2000)
    duration_unit: Literal["day", "week", "month", "year"] | None = None
    slot_days: list[int] | None = None  # weekdays 0-6 or month days 1-31


class ScheduleUpdate(ScheduleCreate):
    pass


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    project_id: int | None = None
    schedule: ScheduleCreate | None = None
    start_date: str | None = None  # YYYY-MM-DD, defaults to today
    due_date: str | None = None  # YYYY-MM-DD
    completion_mode: Literal["carry", "wait", "skip"] = "carry"


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    project_id: int | None = None
    start_date: str | None = None
    due_date: str | None = None
    completion_mode: Literal["carry", "wait", "skip"] | None = None


class CompletionCreate(BaseModel):
    date: str | None = None  # YYYY-MM-DD, defaults to today


class NoteUpdate(BaseModel):
    note: str = ""


class RoutineCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    frequency_count: int = Field(ge=1, le=1000)
    frequency_unit: Literal["day", "week", "month", "year"]
    slot_days: list[int] | None = None


class RoutineUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    frequency_count: int | None = Field(default=None, ge=1, le=1000)
    frequency_unit: Literal["day", "week", "month", "year"] | None = None
    slot_days: list[int] | None = None


class GoalFieldInput(BaseModel):
    id: int | None = None
    name: str = Field(min_length=1, max_length=80)
    unit: str = Field(default="", max_length=20)
    target_value: float | None = None
    compare_mode: Literal["latest", "average", "best"] = "latest"
    higher_is_better: bool = True


class GoalStageInput(BaseModel):
    id: int | None = None
    title: str = ""
    done: bool = False


class GoalCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    kind: Literal["measured", "percent", "staged"] = "measured"
    show_chart: bool = True
    fields: list[GoalFieldInput] = Field(default_factory=list)
    stages: list[GoalStageInput] = Field(default_factory=list)


class GoalUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    show_chart: bool | None = None
    status: Literal["active", "archived"] | None = None
    fields: list[GoalFieldInput] | None = None
    stages: list[GoalStageInput] | None = None


class GoalStageCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class GoalStageUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    done: bool | None = None


class GoalValueInput(BaseModel):
    field_id: int
    value: float


class GoalEntryCreate(BaseModel):
    date: str | None = None
    note: str = ""
    values: list[GoalValueInput] = Field(default_factory=list)


class GoalEntryUpdate(BaseModel):
    date: str | None = None
    note: str | None = None
    values: list[GoalValueInput] | None = None


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=200)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=200)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=8, max_length=200)


class AdminUserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=200)
    is_admin: bool = False
    is_active: bool = True


class AdminUserUpdate(BaseModel):
    email: str | None = Field(default=None, min_length=3, max_length=320)
    password: str | None = Field(default=None, min_length=8, max_length=200)
    is_admin: bool | None = None
    is_active: bool | None = None