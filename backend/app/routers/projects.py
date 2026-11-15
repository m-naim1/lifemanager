from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import create_task_with_schedule, get_projects_repo, get_tasks_repo, require_user
from ..models import Project, User, now_iso
from ..repositories import ProjectRepository, TaskRepository
from ..schemas import ProjectCreate, ProjectUpdate, TaskCreate
from ..services import TaskService

router = APIRouter(tags=["projects"])


def _project_dict(project: Project, task_count: int = 0, done_count: int = 0) -> dict:
    return {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "status": project.status,
        "deadline": project.deadline,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "task_count": task_count,
        "done_count": done_count,
    }


@router.get("")
async def list_projects(
    projects: ProjectRepository = Depends(get_projects_repo),
    tasks: TaskRepository = Depends(get_tasks_repo),
    user: User = Depends(require_user),
    status: Literal["open", "closed", "archived"] | None = Query(default=None),
    include_completed: bool = Query(default=True),
) -> list[dict]:
    projects_list = await projects.list(user.id)
    all_tasks = await tasks.list(user.id)

    tasks_by_project: dict[int, list] = {}
    for t in all_tasks:
        if t.project_id is not None:
            tasks_by_project.setdefault(t.project_id, []).append(t)

    completions = await tasks.all_completions(user.id)
    done_ids = {c.task_id for c in completions}

    result = []
    for p in projects_list:
        if status is not None and p.status != status:
            continue
        if not include_completed and p.status in ("closed", "archived"):
            continue
        project_tasks = tasks_by_project.get(p.id, [])
        result.append(
            _project_dict(
                p,
                task_count=len(project_tasks),
                done_count=sum(1 for t in project_tasks if t.id in done_ids),
            )
        )
    return result


@router.post("")
async def create_project(
    payload: ProjectCreate,
    projects: ProjectRepository = Depends(get_projects_repo),
    user: User = Depends(require_user),
) -> dict:
    now = now_iso()
    created = await projects.create(
        Project(
            id=None,
            name=payload.name.strip(),
            description=payload.description.strip(),
            status="open",
            deadline=payload.deadline,
            created_at=now,
            updated_at=now,
            user_id=user.id,
        )
    )
    return _project_dict(created)


@router.get("/{project_id}")
async def get_project(
    project_id: int,
    projects: ProjectRepository = Depends(get_projects_repo),
    user: User = Depends(require_user),
) -> dict:
    project = await projects.get(project_id, user.id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return _project_dict(project)


@router.patch("/{project_id}")
async def update_project(
    project_id: int,
    payload: ProjectUpdate,
    projects: ProjectRepository = Depends(get_projects_repo),
    user: User = Depends(require_user),
) -> dict:
    project = await projects.get(project_id, user.id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    updates = payload.model_dump(exclude_unset=True)
    if updates.get("name") is not None:
        project.name = updates["name"].strip()
    if updates.get("description") is not None:
        project.description = updates["description"].strip()
    if "status" in updates:
        project.status = updates["status"]
    if "deadline" in updates:
        project.deadline = updates["deadline"]
    project.updated_at = now_iso()
    updated = await projects.update(project)
    return _project_dict(updated)


@router.delete("/{project_id}")
async def delete_project(
    project_id: int,
    projects: ProjectRepository = Depends(get_projects_repo),
    user: User = Depends(require_user),
) -> dict:
    if await projects.get(project_id, user.id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    await projects.delete(project_id, user.id)
    return {"ok": True, "id": project_id}


@router.get("/{project_id}/tasks")
async def project_tasks(
    project_id: int,
    projects: ProjectRepository = Depends(get_projects_repo),
    tasks: TaskRepository = Depends(get_tasks_repo),
    user: User = Depends(require_user),
) -> list[dict]:
    if await projects.get(project_id, user.id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    service = TaskService(tasks)
    return [t for t in await service.index(user.id) if t["project_id"] == project_id]


@router.post("/{project_id}/tasks")
async def create_project_task(
    project_id: int,
    payload: TaskCreate,
    projects: ProjectRepository = Depends(get_projects_repo),
    tasks: TaskRepository = Depends(get_tasks_repo),
    user: User = Depends(require_user),
) -> dict:
    if await projects.get(project_id, user.id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    created = await create_task_with_schedule(
        title=payload.title,
        description=payload.description,
        project_id=project_id,
        schedule_payload=payload.schedule,
        repo=tasks,
        start_date=payload.start_date,
        due_date=payload.due_date,
        completion_mode=payload.completion_mode,
        user_id=user.id,
    )
    service = TaskService(tasks)
    return await service.detail(created.id, user.id)
