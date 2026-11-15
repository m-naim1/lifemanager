from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends

from ..deps import get_projects_repo, get_routines_repo, get_tasks_repo, require_user
from ..models import User
from ..repositories import ProjectRepository, RoutineRepository, TaskRepository
from ..services import TodayService

router = APIRouter(tags=["home"])


@router.get("/today")
async def today(
    projects: ProjectRepository = Depends(get_projects_repo),
    tasks: TaskRepository = Depends(get_tasks_repo),
    routines: RoutineRepository = Depends(get_routines_repo),
    user: User = Depends(require_user),
) -> dict:
    service = TodayService(projects, tasks, routines)
    return await service.view(date.today(), user.id)
