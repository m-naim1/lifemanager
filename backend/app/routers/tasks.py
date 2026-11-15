from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..deps import (
    create_task_with_schedule,
    get_projects_repo,
    get_tasks_repo,
    require_user,
    set_schedule,
)
from ..models import User
from ..repositories import ProjectRepository, TaskRepository
from ..services import TaskService

router = APIRouter(tags=["tasks"])


@router.get("")
async def list_tasks(
    tasks: TaskRepository = Depends(get_tasks_repo),
    user: User = Depends(require_user),
) -> list[dict]:
    return await TaskService(tasks).index(user.id)


@router.post("")
async def create_task(
    payload: dict,
    tasks: TaskRepository = Depends(get_tasks_repo),
    projects: ProjectRepository = Depends(get_projects_repo),
    user: User = Depends(require_user),
) -> dict:
    from ..schemas import TaskCreate as _TC

    data = _TC(**payload)
    if data.project_id is not None and await projects.get(data.project_id, user.id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    created = await create_task_with_schedule(
        title=data.title,
        description=data.description,
        project_id=data.project_id,
        schedule_payload=data.schedule,
        repo=tasks,
        start_date=data.start_date,
        due_date=data.due_date,
        completion_mode=data.completion_mode,
        user_id=user.id,
    )
    return await TaskService(tasks).detail(created.id, user.id)


@router.get("/{task_id}")
async def get_task(
    task_id: int,
    tasks: TaskRepository = Depends(get_tasks_repo),
    user: User = Depends(require_user),
) -> dict:
    result = await TaskService(tasks).detail(task_id, user.id)
    if result is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return result


@router.patch("/{task_id}")
async def update_task(
    task_id: int,
    payload: dict,
    tasks: TaskRepository = Depends(get_tasks_repo),
    projects: ProjectRepository = Depends(get_projects_repo),
    user: User = Depends(require_user),
) -> dict:
    from ..schemas import TaskUpdate as _TU

    data = _TU(**payload)
    task = await tasks.get(task_id, user.id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    updates = data.model_dump(exclude_unset=True)
    if updates.get("title") is not None:
        task.title = updates["title"].strip()
    if updates.get("description") is not None:
        task.description = updates["description"].strip()
    if "project_id" in updates:
        if updates["project_id"] is not None and await projects.get(updates["project_id"], user.id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
        task.project_id = updates["project_id"]
    if "start_date" in updates:
        task.start_date = updates["start_date"]
    if "due_date" in updates:
        task.due_date = updates["due_date"]
    if "completion_mode" in updates:
        task.completion_mode = updates["completion_mode"]
    await tasks.update(task)
    return await TaskService(tasks).detail(task_id, user.id)


@router.delete("/{task_id}")
async def delete_task(
    task_id: int,
    tasks: TaskRepository = Depends(get_tasks_repo),
    user: User = Depends(require_user),
) -> dict:
    if await tasks.get(task_id, user.id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await tasks.delete(task_id, user.id)
    return {"ok": True, "id": task_id}


@router.put("/{task_id}/schedule")
async def upsert_schedule(
    task_id: int,
    payload: dict,
    tasks: TaskRepository = Depends(get_tasks_repo),
    user: User = Depends(require_user),
) -> dict:
    from ..schemas import ScheduleUpdate as _SU

    data = _SU(**payload)
    task = await tasks.get(task_id, user.id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    schedule = await set_schedule(tasks, task, data)
    return {
        "id": schedule.id,
        "task_id": task_id,
        "repeat_count": schedule.repeat_count,
        "repeat_unit": schedule.repeat_unit,
        "start_date": schedule.start_date,
        "end_date": schedule.end_date,
        "duration_count": schedule.duration_count,
        "duration_unit": schedule.duration_unit,
    }


@router.delete("/{task_id}/schedule")
async def remove_schedule(
    task_id: int,
    tasks: TaskRepository = Depends(get_tasks_repo),
    user: User = Depends(require_user),
) -> dict:
    if await tasks.get(task_id, user.id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await tasks.delete_schedule(task_id)
    return {"ok": True, "id": task_id}


@router.post("/{task_id}/complete")
async def complete_task(
    task_id: int,
    payload: dict | None = None,
    tasks: TaskRepository = Depends(get_tasks_repo),
    user: User = Depends(require_user),
) -> dict:
    from ..models import today_iso
    from ..schemas import CompletionCreate as _CC

    if await tasks.get(task_id, user.id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    data = _CC(**(payload or {}))
    day = data.date if data.date else today_iso()
    completion = await tasks.add_completion(task_id, day)
    return {
        "id": completion.id,
        "task_id": task_id,
        "date": completion.date,
        "completed_at": completion.completed_at,
    }


@router.get("/{task_id}/completions")
async def task_completions(
    task_id: int,
    tasks: TaskRepository = Depends(get_tasks_repo),
    user: User = Depends(require_user),
) -> list[dict]:
    if await tasks.get(task_id, user.id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    rows = await tasks.completions_for_task(task_id)
    return [
        {"id": c.id, "date": c.date, "completed_at": c.completed_at} for c in rows
    ]


@router.delete("/{task_id}/completions/latest")
async def undo_latest_task_completion(
    task_id: int,
    tasks: TaskRepository = Depends(get_tasks_repo),
    user: User = Depends(require_user),
    day: str | None = None,
) -> dict:
    from ..models import today_iso

    if await tasks.get(task_id, user.id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    target = day or today_iso()
    await tasks.delete_latest_for_day(task_id, target)
    return {"ok": True}


@router.delete("/{task_id}/completions/{completion_id}")
async def delete_task_completion(
    task_id: int,
    completion_id: int,
    tasks: TaskRepository = Depends(get_tasks_repo),
    user: User = Depends(require_user),
) -> dict:
    if await tasks.get(task_id, user.id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await tasks.delete_completion(completion_id)
    return {"ok": True}


# --- day notes (per-day descriptions) ---


@router.get("/{task_id}/notes")
async def list_notes(
    task_id: int,
    tasks: TaskRepository = Depends(get_tasks_repo),
    user: User = Depends(require_user),
) -> list[dict]:
    if await tasks.get(task_id, user.id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    rows = await tasks.notes_for(task_id)
    return [
        {"id": n.id, "date": n.date, "note": n.note, "updated_at": n.updated_at}
        for n in rows
    ]


@router.put("/{task_id}/notes/{day}")
async def upsert_note(
    task_id: int,
    day: str,
    payload: dict,
    tasks: TaskRepository = Depends(get_tasks_repo),
    user: User = Depends(require_user),
) -> dict:
    from ..schemas import NoteUpdate as _NU

    data = _NU(**payload)
    if await tasks.get(task_id, user.id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    note = await tasks.upsert_note(task_id, day, data.note)
    return {
        "id": note.id,
        "task_id": task_id,
        "date": day,
        "note": note.note,
        "updated_at": note.updated_at,
    }


@router.delete("/{task_id}/notes/{day}")
async def delete_note(
    task_id: int,
    day: str,
    tasks: TaskRepository = Depends(get_tasks_repo),
    user: User = Depends(require_user),
) -> dict:
    if await tasks.get(task_id, user.id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await tasks.delete_note(task_id, day)
    return {"ok": True}
