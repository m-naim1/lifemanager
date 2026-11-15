from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..deps import get_goals_repo, require_user
from ..models import User
from ..repositories import GoalRepository
from ..schemas import (
    GoalCreate,
    GoalEntryCreate,
    GoalEntryUpdate,
    GoalStageCreate,
    GoalStageUpdate,
    GoalUpdate,
)
from ..services import GoalService, GoalValidationError

router = APIRouter(tags=["goals"])


def _service(goals: GoalRepository) -> GoalService:
    return GoalService(goals)


@router.get("")
async def list_goals(
    goals: GoalRepository = Depends(get_goals_repo),
    user: User = Depends(require_user),
) -> list[dict]:
    return await _service(goals).index(user.id)


@router.post("")
async def create_goal(
    payload: GoalCreate,
    goals: GoalRepository = Depends(get_goals_repo),
    user: User = Depends(require_user),
) -> dict:
    try:
        return await _service(goals).create_goal(
            user.id,
            payload.title,
            payload.description,
            payload.kind,
            payload.show_chart,
            payload.fields,
            payload.stages,
        )
    except GoalValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{goal_id}")
async def get_goal(
    goal_id: int,
    goals: GoalRepository = Depends(get_goals_repo),
    user: User = Depends(require_user),
) -> dict:
    result = await _service(goals).detail(goal_id, user.id)
    if result is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    return result


@router.patch("/{goal_id}")
async def update_goal(
    goal_id: int,
    payload: GoalUpdate,
    goals: GoalRepository = Depends(get_goals_repo),
    user: User = Depends(require_user),
) -> dict:
    repo = _service(goals)
    goal = await goals.get(goal_id, user.id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    try:
        return await repo.update_goal(
            goal,
            title=payload.title if payload.title is not None else goal.title,
            description=payload.description
            if payload.description is not None
            else goal.description,
            show_chart=payload.show_chart
            if payload.show_chart is not None
            else bool(goal.show_chart),
            fields_data=payload.fields,
            stages_data=payload.stages,
            status=payload.status,
        )
    except GoalValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{goal_id}")
async def delete_goal(
    goal_id: int,
    goals: GoalRepository = Depends(get_goals_repo),
    user: User = Depends(require_user),
) -> dict:
    if await goals.get(goal_id, user.id) is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    await _service(goals).delete_goal(goal_id, user.id)
    return {"ok": True, "id": goal_id}


@router.post("/{goal_id}/stages")
async def add_stage(
    goal_id: int,
    payload: GoalStageCreate,
    goals: GoalRepository = Depends(get_goals_repo),
    user: User = Depends(require_user),
) -> dict:
    goal = await goals.get(goal_id, user.id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    try:
        return await _service(goals).add_stage(goal, payload.title)
    except GoalValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/{goal_id}/stages/{stage_id}")
async def update_stage(
    goal_id: int,
    stage_id: int,
    payload: GoalStageUpdate,
    goals: GoalRepository = Depends(get_goals_repo),
    user: User = Depends(require_user),
) -> dict:
    goal = await goals.get(goal_id, user.id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    stage = await goals.stage_for(stage_id, goal_id)
    if stage is None:
        raise HTTPException(status_code=404, detail="Stage not found")
    try:
        return await _service(goals).update_stage(
            goal, stage, title=payload.title, done=payload.done
        )
    except GoalValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{goal_id}/stages/{stage_id}/toggle")
async def toggle_stage(
    goal_id: int,
    stage_id: int,
    goals: GoalRepository = Depends(get_goals_repo),
    user: User = Depends(require_user),
) -> dict:
    goal = await goals.get(goal_id, user.id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    try:
        return await _service(goals).toggle_stage(goal, stage_id)
    except GoalValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{goal_id}/stages/{stage_id}")
async def delete_stage(
    goal_id: int,
    stage_id: int,
    goals: GoalRepository = Depends(get_goals_repo),
    user: User = Depends(require_user),
) -> dict:
    goal = await goals.get(goal_id, user.id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    if await goals.stage_for(stage_id, goal_id) is None:
        raise HTTPException(status_code=404, detail="Stage not found")
    return await _service(goals).delete_stage(goal, stage_id)


@router.get("/{goal_id}/entries")
async def list_entries(
    goal_id: int,
    goals: GoalRepository = Depends(get_goals_repo),
    user: User = Depends(require_user),
) -> list[dict]:
    result = await _service(goals).detail(goal_id, user.id)
    if result is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    return result["entries"]


@router.post("/{goal_id}/entries")
async def create_entry(
    goal_id: int,
    payload: GoalEntryCreate,
    goals: GoalRepository = Depends(get_goals_repo),
    user: User = Depends(require_user),
) -> dict:
    goal = await goals.get(goal_id, user.id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    try:
        return await _service(goals).add_entry(
            goal, payload.date, payload.note, payload.values
        )
    except GoalValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/{goal_id}/entries/{entry_id}")
async def update_entry(
    goal_id: int,
    entry_id: int,
    payload: GoalEntryUpdate,
    goals: GoalRepository = Depends(get_goals_repo),
    user: User = Depends(require_user),
) -> dict:
    goal = await goals.get(goal_id, user.id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    entry = await goals.entry_for(entry_id, goal_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Entry not found")
    try:
        return await _service(goals).update_entry(
            goal, entry, payload.date, payload.note, payload.values
        )
    except GoalValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{goal_id}/entries/{entry_id}")
async def delete_entry(
    goal_id: int,
    entry_id: int,
    goals: GoalRepository = Depends(get_goals_repo),
    user: User = Depends(require_user),
) -> dict:
    goal = await goals.get(goal_id, user.id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    if await goals.entry_for(entry_id, goal_id) is None:
        raise HTTPException(status_code=404, detail="Entry not found")
    await _service(goals).delete_entry(goal, entry_id)
    return {"ok": True, "id": entry_id}
