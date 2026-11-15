from __future__ import annotations

import json
from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from ..deps import get_routines_repo, require_user
from ..models import Routine, User, now_iso, today_iso
from ..repositories import RoutineRepository
from ..schedule import period_bounds
from ..schemas import CompletionCreate, RoutineCreate, RoutineUpdate

router = APIRouter(tags=["routines"])


def _routine_dict(r: Routine) -> dict:
    days = None
    if r.slot_days:
        try:
            parsed = json.loads(r.slot_days)
            if isinstance(parsed, list):
                days = [int(d) for d in parsed]
        except (ValueError, TypeError):
            days = None
    return {
        "id": r.id,
        "name": r.name,
        "description": r.description,
        "frequency_count": r.frequency_count,
        "frequency_unit": r.frequency_unit,
        "created_at": r.created_at,
        "slot_days": days,
    }


def _with_progress(
    r: Routine,
    ratios: dict[int, list[str]] | None = None,
) -> dict:
    item = _routine_dict(r)
    today = date.today()
    period = period_bounds(today, r.frequency_unit)
    dates = (ratios or {}).get(r.id, [])
    completed = sum(
        1
        for d in dates
        if period[0].isoformat() <= d <= period[1].isoformat()
    )
    item["progress"] = {
        "kind": r.frequency_unit,
        "completed": completed,
        "target": r.frequency_count,
        "period_start": period[0].isoformat(),
        "period_end": period[1].isoformat(),
    }
    return item


async def _progress_index(routines: RoutineRepository, user_id: int) -> dict[int, list[str]]:
    all_comps = await routines.all_completions(user_id)
    ratios: dict[int, list[str]] = defaultdict(list)
    for c in all_comps:
        ratios[c.routine_id].append(c.date)
    return ratios


@router.get("")
async def list_routines(
    routines: RoutineRepository = Depends(get_routines_repo),
    user: User = Depends(require_user),
) -> list[dict]:
    rows = await routines.list(user.id)
    if not rows:
        return [_routine_dict(r) for r in rows]

    ratios = await _progress_index(routines, user.id)
    return [_with_progress(r, ratios) for r in rows]


@router.post("")
async def create_routine(
    payload: RoutineCreate,
    routines: RoutineRepository = Depends(get_routines_repo),
    user: User = Depends(require_user),
) -> dict:
    created = await routines.create(
        Routine(
            id=None,
            name=payload.name.strip(),
            description=payload.description.strip(),
            frequency_count=payload.frequency_count,
            frequency_unit=payload.frequency_unit,
            created_at=now_iso(),
            slot_days=json.dumps(payload.slot_days) if payload.slot_days else None,
            user_id=user.id,
        )
    )
    return _with_progress(created)


@router.patch("/{routine_id}")
async def update_routine(
    routine_id: int,
    payload: RoutineUpdate,
    routines: RoutineRepository = Depends(get_routines_repo),
    user: User = Depends(require_user),
) -> dict:
    routine = await routines.get(routine_id, user.id)
    if routine is None:
        raise HTTPException(status_code=404, detail="Routine not found")
    updates = payload.model_dump(exclude_unset=True)
    if updates.get("name") is not None:
        routine.name = updates["name"].strip()
    if updates.get("description") is not None:
        routine.description = updates["description"].strip()
    if updates.get("frequency_count") is not None:
        routine.frequency_count = updates["frequency_count"]
    if updates.get("frequency_unit") is not None:
        routine.frequency_unit = updates["frequency_unit"]
    if "slot_days" in updates:
        routine.slot_days = (
            json.dumps(updates["slot_days"]) if updates["slot_days"] else None
        )
    updated = await routines.update(routine)
    ratios = await _progress_index(routines, user.id)
    return _with_progress(updated, ratios)


@router.delete("/{routine_id}")
async def delete_routine(
    routine_id: int,
    routines: RoutineRepository = Depends(get_routines_repo),
    user: User = Depends(require_user),
) -> dict:
    if await routines.get(routine_id, user.id) is None:
        raise HTTPException(status_code=404, detail="Routine not found")
    await routines.delete(routine_id, user.id)
    return {"ok": True, "id": routine_id}


@router.post("/{routine_id}/complete")
async def complete_routine(
    routine_id: int,
    payload: CompletionCreate | None = None,
    routines: RoutineRepository = Depends(get_routines_repo),
    user: User = Depends(require_user),
) -> dict:
    if await routines.get(routine_id, user.id) is None:
        raise HTTPException(status_code=404, detail="Routine not found")
    day = payload.date if payload is not None and payload.date else today_iso()
    completion = await routines.add_completion(routine_id, day)
    return {
        "id": completion.id,
        "routine_id": routine_id,
        "date": completion.date,
        "completed_at": completion.completed_at,
    }


@router.get("/{routine_id}/completions")
async def routine_completions(
    routine_id: int,
    routines: RoutineRepository = Depends(get_routines_repo),
    user: User = Depends(require_user),
) -> list[dict]:
    if await routines.get(routine_id, user.id) is None:
        raise HTTPException(status_code=404, detail="Routine not found")
    rows = await routines.completions_for_routine(routine_id)
    return [
        {"id": c.id, "date": c.date, "completed_at": c.completed_at} for c in rows
    ]


@router.delete("/{routine_id}/completions/latest")
async def undo_latest_routine_completion(
    routine_id: int,
    routines: RoutineRepository = Depends(get_routines_repo),
    user: User = Depends(require_user),
    day: str | None = None,
) -> dict:
    if await routines.get(routine_id, user.id) is None:
        raise HTTPException(status_code=404, detail="Routine not found")
    target = day or today_iso()
    await routines.delete_latest_for_day(routine_id, target)
    return {"ok": True, "day": target}


@router.delete("/{routine_id}/completions/{completion_id}")
async def delete_routine_completion(
    routine_id: int,
    completion_id: int,
    routines: RoutineRepository = Depends(get_routines_repo),
    user: User = Depends(require_user),
) -> dict:
    if await routines.get(routine_id, user.id) is None:
        raise HTTPException(status_code=404, detail="Routine not found")
    await routines.delete_completion(completion_id)
    return {"ok": True}
