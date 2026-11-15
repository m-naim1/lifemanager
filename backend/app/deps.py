from __future__ import annotations

import json
from datetime import date

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .auth import TOKEN_COOKIE, decode_token
from .models import Schedule, Task, User, today_iso
from .repositories import (
    AdminRepository,
    GoalRepository,
    ProjectRepository,
    RoutineRepository,
    TaskRepository,
    UserRepository,
)
from .schedule import add_duration
from .schemas import ScheduleCreate

_bearer = HTTPBearer(auto_error=False)


def get_projects_repo(request: Request) -> ProjectRepository:
    return request.app.state.projects


def get_tasks_repo(request: Request) -> TaskRepository:
    return request.app.state.tasks


def get_routines_repo(request: Request) -> RoutineRepository:
    return request.app.state.routines


def get_users_repo(request: Request) -> UserRepository:
    return request.app.state.users


def get_goals_repo(request: Request) -> GoalRepository:
    return request.app.state.goals


def get_admin_repo(request: Request) -> AdminRepository:
    return request.app.state.admin


async def require_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> User:
    token = creds.credentials if creds is not None and creds.credentials else None
    if not token:
        # The server-rendered UI keeps the same JWT in an HttpOnly cookie.
        token = request.cookies.get(TOKEN_COOKIE)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        payload = decode_token(token)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    repo: UserRepository = request.app.state.users
    user = await repo.get(int(payload["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User inactive")
    return user


async def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")
    return user


async def create_task_with_schedule(
    *,
    title: str,
    description: str,
    project_id: int | None,
    schedule_payload: ScheduleCreate | None,
    repo: TaskRepository,
    start_date: str | None = None,
    due_date: str | None = None,
    completion_mode: str = "carry",
    user_id: int | None = None,
) -> Task:
    """Create a task and attach a schedule if requested."""
    created_at = today_iso()
    task = Task(
        id=None,
        title=title,
        description=description,
        project_id=project_id,
        created_at=created_at,
        start_date=start_date or created_at,
        due_date=due_date,
        completion_mode=completion_mode or "carry",
        user_id=user_id,
    )
    created = await repo.create(task)
    if schedule_payload is not None:
        await set_schedule(repo, created, schedule_payload)
    return created


async def set_schedule(
    repo: TaskRepository, task: Task, payload: ScheduleCreate
) -> Schedule:
    start_date = task.start_date or today_iso()
    end_date: str | None = payload.end_date
    if end_date is None and payload.duration_count is not None and payload.duration_unit is not None:
        start = date.fromisoformat(start_date)
        end = add_duration(start, payload.duration_count, payload.duration_unit)
        end_date = end.isoformat()
    slot_days = json.dumps(payload.slot_days) if payload.slot_days else None
    schedule = Schedule(
        id=None,
        task_id=task.id,
        repeat_count=payload.repeat_count,
        repeat_unit=payload.repeat_unit,
        start_date=start_date,
        end_date=end_date,
        duration_count=payload.duration_count,
        duration_unit=payload.duration_unit,
        slot_days=slot_days,
    )
    return await repo.save_schedule(schedule)