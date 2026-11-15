from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends

from ..deps import get_routines_repo, get_tasks_repo, require_user
from ..models import User
from ..repositories import RoutineRepository, TaskRepository
from ..services import CalendarService, HeatmapService

router = APIRouter(tags=["stats"])


@router.get("/heatmap")
async def heatmap(
    type: Literal["tasks", "routines"] = "tasks",
    tasks: TaskRepository = Depends(get_tasks_repo),
    routines: RoutineRepository = Depends(get_routines_repo),
    user: User = Depends(require_user),
) -> dict:
    service = HeatmapService(tasks, routines)
    return await service.build(type, date.today(), user.id)


@router.get("/calendar")
async def calendar(
    type: Literal["tasks", "routines"] = "tasks",
    year: int | None = None,
    month: int | None = None,
    tasks: TaskRepository = Depends(get_tasks_repo),
    routines: RoutineRepository = Depends(get_routines_repo),
    user: User = Depends(require_user),
) -> dict:
    today = date.today()
    y = year if year is not None else today.year
    m = month if month is not None else today.month
    service = CalendarService(tasks, routines)
    return await service.build(type, y, m, user.id)
