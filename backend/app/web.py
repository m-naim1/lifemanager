"""Server-rendered UI: Jinja pages driven by htmx partials and Alpine state.

The JSON API under /api remains available (and is still used by the load
harness). This module is the browser-facing application.
"""

from __future__ import annotations

import calendar as calendar_mod
import json
import math
from collections import defaultdict
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import RedirectResponse

from .auth import (
    TOKEN_COOKIE,
    create_token,
    decode_token,
    hash_password,
    normalize_email,
    user_public,
    verify_password,
)
from .config import settings
from .deps import (
    create_task_with_schedule,
    get_projects_repo,
    get_routines_repo,
    get_tasks_repo,
    get_users_repo,
    set_schedule,
)
from .models import Project, Routine, User, now_iso, today_iso
from .repositories import ProjectRepository, RoutineRepository, TaskRepository, UserRepository
from .schedule import parse_slot_days, period_bounds
from .schemas import ScheduleCreate
from .services import (
    AdminService,
    AdminValidationError,
    CalendarService,
    GoalService,
    GoalValidationError,
    HeatmapService,
    TaskService,
    TodayService,
)
from .templating import format_day_long, month_label, templates

router = APIRouter(include_in_schema=False)

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
PERIOD_LABELS = {
    "day": "today",
    "week": "this week",
    "month": "this month",
    "year": "this year",
}


class WebRedirect(Exception):
    """A redirect raised from a dependency or route (handled in main.py)."""

    def __init__(self, url: str, status_code: int = 303) -> None:
        self.url = url
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Request / session helpers
# ---------------------------------------------------------------------------


def _safe_next(value: str | None, fallback: str) -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return fallback


def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        TOKEN_COOKIE,
        token,
        max_age=settings.jwt_expire_minutes * 60,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(TOKEN_COOKIE, path="/")


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def _redirect(request: Request, url: str, status_code: int = 303) -> Response:
    if _is_htmx(request):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=status_code)


def _page(request: Request, name: str, user: User | None, status_code: int = 200, **context):
    return templates.TemplateResponse(
        request,
        name,
        {
            "request": request,
            "user": user,
            "current_path": request.url.path,
            **context,
        },
        status_code=status_code,
    )


async def _current_user(request: Request) -> User | None:
    token = request.cookies.get(TOKEN_COOKIE)
    if not token:
        header = request.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:]
    if not token:
        return None
    try:
        payload = decode_token(token)
        user_id = int(payload.get("sub", 0))
    except (ValueError, TypeError):
        return None
    user = await request.app.state.users.get(user_id)
    if user is None or not user.is_active:
        return None
    return user


async def require_page_user(request: Request) -> User:
    user = await _current_user(request)
    if user is None:
        raise WebRedirect(f"/login?next={quote(request.url.path)}")
    return user


async def require_admin_page(request: Request) -> User:
    user = await require_page_user(request)
    if not user.is_admin:
        raise WebRedirect("/")
    return user


# ---------------------------------------------------------------------------
# View helpers
# ---------------------------------------------------------------------------


def _done(task: dict) -> bool:
    schedule = task.get("schedule")
    if not schedule:
        return bool(task.get("done"))
    end = schedule.get("end_date")
    return bool(task.get("completed_total")) and bool(end) and str(end) < today_iso()


def _overdue(task: dict) -> bool:
    if _done(task):
        return False
    due = task.get("due_date")
    if not due:
        return False
    if not task.get("schedule") and task.get("done"):
        return False
    return str(due) < today_iso()


def _toggled_today(task: dict, day: str) -> bool:
    if not task.get("schedule"):
        return bool(task.get("done"))
    return any(item.get("date") == day for item in task.get("completions", []))


def _days_label(days: list[int] | None, unit: str) -> str:
    if not days:
        return ""
    if unit == "month":
        return "on " + ", ".join(str(d) for d in days)
    return "on " + ", ".join(WEEKDAYS[d] for d in days if 0 <= d < 7)


def _schedule_label(task: dict) -> str:
    schedule = task.get("schedule")
    if not schedule:
        return "once"
    label = f"{schedule['repeat_count']}x/{schedule['repeat_unit']}"
    extra = _days_label(schedule.get("slot_days"), schedule.get("repeat_unit", ""))
    return f"{label} {extra}" if extra else label


def _frequency_label(routine: dict) -> str:
    label = f"{routine['frequency_count']}x/{routine['frequency_unit']}"
    extra = _days_label(routine.get("slot_days"), routine.get("frequency_unit", ""))
    return f"{label} {extra}" if extra else label


def _progress_label(progress: dict) -> str:
    if progress.get("kind") == "once":
        return "Due today"
    return f"{progress.get('completed', 0)}/{progress.get('target', 1)} this {progress.get('kind', '')}"


def _project_dict(project: Project, task_count: int = 0, done_count: int = 0) -> dict:
    progress = round((done_count / task_count) * 100) if task_count else 0
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
        "progress": progress,
        "overdue": project.status == "open"
        and bool(project.deadline)
        and str(project.deadline) < today_iso(),
    }


def _routine_dict(routine: Routine, dates: list[str]) -> dict:
    today = date.today()
    period = period_bounds(today, routine.frequency_unit)
    completed = sum(1 for d in dates if period[0].isoformat() <= d <= period[1].isoformat())
    return {
        "id": routine.id,
        "name": routine.name,
        "description": routine.description,
        "frequency_count": routine.frequency_count,
        "frequency_unit": routine.frequency_unit,
        "created_at": routine.created_at,
        "slot_days": parse_slot_days(routine.slot_days),
        "progress": {
            "kind": routine.frequency_unit,
            "completed": completed,
            "target": routine.frequency_count,
            "period_start": period[0].isoformat(),
            "period_end": period[1].isoformat(),
            "percent": min(round((completed / routine.frequency_count) * 100), 100)
            if routine.frequency_count
            else 0,
        },
        "period_label": PERIOD_LABELS.get(routine.frequency_unit, routine.frequency_unit),
    }


async def _home_context(request: Request, user: User) -> dict:
    day = date.today()
    today_data = await TodayService(
        request.app.state.projects,
        request.app.state.tasks,
        request.app.state.routines,
    ).view(day, user.id)

    heatmap = HeatmapService(request.app.state.tasks, request.app.state.routines)
    heat_tasks = await heatmap.build("tasks", day, user.id)
    heat_routines = await heatmap.build("routines", day, user.id)

    completed_today = 0
    for heat in (heat_tasks, heat_routines):
        for column in heat["weeks"]:
            for cell in column:
                if cell["date"] == today_data["date"] and cell["count"]:
                    completed_today += cell["count"]

    total_due = (
        len(today_data["tasks"])
        + len(today_data["routines"])
        + len(today_data["projects_due_today"])
    )
    goals = await GoalService(request.app.state.goals).index(user.id)
    active_goals = [goal for goal in goals if goal["status"] == "active"][:3]
    return {
        "today": today_data,
        "heat_tasks": heat_tasks,
        "heat_routines": heat_routines,
        "completed_today": completed_today,
        "total_due": total_due,
        "day_long": _day_long(today_data["date"]),
        "greeting": _greeting(),
        "today_iso": today_data["date"],
        "goals": active_goals,
    }


def _day_long(iso: str) -> str:
    return format_day_long(iso)


def _greeting() -> str:
    from .templating import greeting

    return greeting()


async def _tasks_context(
    request: Request, user: User, tab: str = "pending", q: str = ""
) -> dict:
    items = await TaskService(request.app.state.tasks).index(user.id)
    for item in items:
        item["_done"] = _done(item)
        item["_overdue"] = _overdue(item)
        item["_toggled"] = _toggled_today(item, today_iso())

    pending_count = sum(1 for item in items if not item["_done"])
    overdue_count = sum(1 for item in items if item["_overdue"])

    filtered = items
    if tab == "pending":
        filtered = [item for item in items if not item["_done"] and not item["_overdue"]]
    elif tab == "overdue":
        filtered = [item for item in items if item["_overdue"]]
    elif tab == "done":
        filtered = [item for item in items if item["_done"]]

    needle = q.strip().lower()
    if needle:
        filtered = [
            item
            for item in filtered
            if needle in item["title"].lower()
            or needle in (item.get("description") or "").lower()
        ]

    projects = await request.app.state.projects.list(user.id)
    return {
        "tasks": filtered,
        "pending_count": pending_count,
        "overdue_count": overdue_count,
        "tab": tab if tab in {"pending", "overdue", "done", "all"} else "pending",
        "q": q,
        "projects": projects,
        "project_names": {p.id: p.name for p in projects},
        "today_iso": today_iso(),
    }


async def _projects_context(request: Request, user: User, status: str = "open") -> dict:
    projects: ProjectRepository = request.app.state.projects
    tasks: TaskRepository = request.app.state.tasks

    project_rows = await projects.list(user.id)
    all_tasks = await tasks.list(user.id)
    completions = await tasks.all_completions(user.id)
    done_ids = {c.task_id for c in completions}

    task_count: dict[int, int] = defaultdict(int)
    done_count: dict[int, int] = defaultdict(int)
    for task in all_tasks:
        if task.project_id is None:
            continue
        task_count[task.project_id] += 1
        if task.id in done_ids:
            done_count[task.project_id] += 1

    items = [
        _project_dict(
            project,
            task_count=task_count.get(project.id, 0),
            done_count=done_count.get(project.id, 0),
        )
        for project in project_rows
        if project.status == status
    ]
    return {
        "projects": items,
        "status": status,
        "today_iso": today_iso(),
    }


async def _project_context(request: Request, user: User, project_id: int) -> dict | None:
    project = await request.app.state.projects.get(project_id, user.id)
    if project is None:
        return None
    items = await TaskService(request.app.state.tasks).index(user.id)
    project_tasks = [item for item in items if item["project_id"] == project_id]
    for item in project_tasks:
        item["_toggled"] = _toggled_today(item, today_iso())
        item["_overdue"] = _overdue(item)
    done_count = sum(
        1
        for item in project_tasks
        if item.get("done") or (item.get("schedule") and item.get("completed_total", 0) > 0)
    )
    data = _project_dict(project, task_count=len(project_tasks), done_count=done_count)
    projects = await request.app.state.projects.list(user.id)
    return {
        "project": data,
        "project_tasks": project_tasks,
        "done_count": done_count,
        "projects": projects,
        "today_iso": today_iso(),
    }


async def _routines_context(request: Request, user: User) -> dict:
    routines: RoutineRepository = request.app.state.routines
    rows = await routines.list(user.id)
    completions = await routines.all_completions(user.id)
    dates: dict[int, list[str]] = defaultdict(list)
    for completion in completions:
        dates[completion.routine_id].append(completion.date)
    items = [_routine_dict(routine, dates.get(routine.id, [])) for routine in rows]
    return {"routines": items, "today_iso": today_iso()}


async def _goals_context(request: Request, user: User) -> dict:
    service = GoalService(request.app.state.goals)
    return {"goals": await service.index(user.id), "today_iso": today_iso()}


async def _goal_detail_context(
    request: Request, user: User, goal_id: int
) -> dict | None:
    service = GoalService(request.app.state.goals)
    detail = await service.detail(goal_id, user.id)
    if detail is None:
        return None
    return {"goal": detail, "today_iso": today_iso()}


async def _goal_fragment(request: Request, user: User, goal_id: int, error: str = ""):
    context = await _goal_detail_context(request, user, goal_id)
    if context is None:
        return _redirect(request, "/goals")
    return _page(
        request,
        "partials/goal_content.html",
        user,
        error=error,
        **context,
    )


def _goal_builder_data(
    kind: str,
    show_chart: bool,
    fields: list[dict],
    stages: list[dict] | None = None,
) -> dict:
    return {
        "kind": kind,
        "showChart": bool(show_chart),
        "fields": [
            {
                "key": index,
                "id": field.get("id"),
                "name": field.get("name", ""),
                "unit": field.get("unit", ""),
                "target": field.get("target", ""),
                "compare": field.get("compare", "latest"),
                "direction": field.get("direction", "higher"),
            }
            for index, field in enumerate(fields)
        ],
        "stages": [
            {
                "key": index,
                "id": stage.get("id"),
                "title": stage.get("title", ""),
                "done": bool(stage.get("done")),
            }
            for index, stage in enumerate(stages or [])
        ],
    }


def _collect_goal_fields(
    field_id: list[str] | None,
    field_name: list[str] | None,
    field_unit: list[str] | None,
    field_target: list[str] | None,
    field_compare: list[str] | None,
    field_direction: list[str] | None,
) -> list[dict]:
    ids = field_id or []
    names = field_name or []
    units = field_unit or []
    targets = field_target or []
    compares = field_compare or []
    directions = field_direction or []
    rows: list[dict] = []
    for index, raw_name in enumerate(names):
        raw_id = ids[index].strip() if index < len(ids) else ""
        name = (raw_name or "").strip()
        if not name and not raw_id:
            continue
        rows.append(
            {
                "key": index,
                "id": raw_id or None,
                "name": name,
                "unit": (units[index] if index < len(units) else "").strip(),
                "target": (targets[index] if index < len(targets) else "").strip(),
                "compare": (
                    compares[index] if index < len(compares) else "latest"
                )
                or "latest",
                "direction": (
                    directions[index] if index < len(directions) else "higher"
                )
                or "higher",
            }
        )
    return rows


def _collect_goal_stages(
    stage_id: list[str] | None,
    stage_title: list[str] | None,
    stage_done: list[str] | None,
) -> list[dict]:
    ids = stage_id or []
    titles = stage_title or []
    dones = stage_done or []
    rows: list[dict] = []
    for index, raw_title in enumerate(titles):
        raw_id = ids[index].strip() if index < len(ids) else ""
        title = (raw_title or "").strip()
        if not title and not raw_id:
            continue
        done = (dones[index] if index < len(dones) else "0").strip().lower()
        rows.append(
            {
                "key": index,
                "id": raw_id or None,
                "title": title,
                "done": done in ("1", "true", "on", "yes", "done"),
            }
        )
    return rows


def _entry_values(field_id: list[int] | None, value: list[str] | None) -> list[dict]:
    values: list[dict] = []
    for index, raw_field_id in enumerate(field_id or []):
        raw_value = value[index].strip() if index < len(value or []) else ""
        if not raw_value:
            continue
        values.append({"field_id": raw_field_id, "value": raw_value})
    return values


def _date_level(count: int, maximum: int) -> int:
    if count <= 0:
        return 0
    if maximum <= 1:
        return min(count, 4)
    return max(1, min(4, math.ceil((count / maximum) * 4)))


async def _calendar_context(
    request: Request,
    user: User,
    kind: str,
    year: int,
    month: int,
    selected: str | None = None,
) -> dict:
    kind = "routines" if kind == "routines" else "tasks"
    data = await CalendarService(
        request.app.state.tasks, request.app.state.routines
    ).build(kind, year, month, user.id)  # type: ignore[arg-type]

    day_counts = {day["date"]: day["count"] for day in data["days"]}
    first = date(year, month, 1)
    last_day_number = calendar_mod.monthrange(year, month)[1]
    cells: list[dict | None] = [None] * first.weekday()
    for day_number in range(1, last_day_number + 1):
        iso = date(year, month, day_number).isoformat()
        count = day_counts.get(iso, 0)
        cells.append(
            {
                "date": iso,
                "day": day_number,
                "count": count,
                "level": _date_level(count, data["max"]),
                "has_notes": bool(data["notes"].get(iso)),
                "is_today": iso == today_iso(),
            }
        )
    while len(cells) % 7:
        cells.append(None)
    weeks = [cells[index : index + 7] for index in range(0, len(cells), 7)]

    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)

    return {
        "kind": kind,
        "year": year,
        "month": month,
        "month_label": month_label(year, month),
        "prev_year": prev_year,
        "prev_month": prev_month,
        "next_year": next_year,
        "next_month": next_month,
        "weeks": weeks,
        "weekdays": WEEKDAYS,
        "selected": selected,
        "today_iso": today_iso(),
        "day_panel": await _day_panel_context(
            request, user, kind, selected, year, month
        ),
    }


async def _day_panel_context(
    request: Request,
    user: User,
    kind: str,
    selected: str | None,
    year: int,
    month: int,
) -> dict:
    if not selected:
        return {"selected": None, "kind": kind, "year": year, "month": month}

    data = await CalendarService(
        request.app.state.tasks, request.app.state.routines
    ).build(kind, year, month, user.id)  # type: ignore[arg-type]
    notes = data["notes"].get(selected, []) if kind == "tasks" else []
    tasks_for_notes = []
    if kind == "tasks":
        tasks_for_notes = await TaskService(request.app.state.tasks).index(user.id)
    return {
        "selected": selected,
        "selected_long": format_day_long(selected),
        "kind": kind,
        "year": year,
        "month": month,
        "selected_completions": data["completions"].get(selected, []),
        "selected_notes": notes,
        "note_by_task": {note["task_id"]: note["note"] for note in notes},
        "tasks_for_notes": tasks_for_notes,
    }


async def _respond(
    request: Request,
    user: User,
    fragment: str,
    *,
    tab: str = "pending",
    q: str = "",
    project_id: str = "",
    calendar_date: str = "",
    calendar_kind: str = "tasks",
    next_url: str = "/",
):
    if fragment == "home":
        return _page(request, "partials/home_content.html", user, **await _home_context(request, user))
    if fragment == "tasks":
        return _page(
            request,
            "partials/tasks_panel.html",
            user,
            **await _tasks_context(request, user, tab, q),
        )
    if fragment == "routines":
        return _page(
            request,
            "partials/routines_grid.html",
            user,
            **await _routines_context(request, user),
        )
    if fragment == "goals":
        return _page(
            request,
            "partials/goals_grid.html",
            user,
            **await _goals_context(request, user),
        )
    if fragment == "project" and project_id.isdigit():
        context = await _project_context(request, user, int(project_id))
        if context is not None:
            return _page(request, "partials/project_content.html", user, **context)
    if fragment == "calendar_day" and calendar_date:
        panel = await _day_panel_context(
            request,
            user,
            calendar_kind,
            calendar_date,
            date.fromisoformat(calendar_date).year,
            date.fromisoformat(calendar_date).month,
        )
        return _page(request, "partials/day_panel.html", user, **panel)
    return _redirect(request, _safe_next(next_url, "/"))


# ---------------------------------------------------------------------------
# Auth pages
# ---------------------------------------------------------------------------


@router.get("/login")
async def login_page(request: Request, next: str = "/"):
    user = await _current_user(request)
    if user is not None:
        raise WebRedirect(_safe_next(next, "/"))
    has_users = await request.app.state.users.count() > 0
    return _page(
        request,
        "login.html",
        None,
        next_url=_safe_next(next, "/"),
        allow_signup=settings.allow_signup or not has_users,
        has_users=has_users,
    )


@router.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
):
    users: UserRepository = request.app.state.users
    user = await users.get_by_email(normalize_email(email))
    if user is None or not verify_password(password, user.password_hash):
        return _page(
            request,
            "login.html",
            None,
            status_code=400,
            error="Invalid email or password",
            next_url=_safe_next(next, "/"),
            allow_signup=settings.allow_signup,
            has_users=await users.count() > 0,
            email=email,
        )
    if not user.is_active:
        return _page(
            request,
            "login.html",
            None,
            status_code=400,
            error="This account is disabled",
            next_url=_safe_next(next, "/"),
            allow_signup=settings.allow_signup,
            has_users=True,
            email=email,
        )
    response = _redirect(request, _safe_next(next, "/"))
    _set_auth_cookie(response, create_token(user))
    return response


@router.get("/register")
async def register_page(request: Request, next: str = "/"):
    user = await _current_user(request)
    if user is not None:
        raise WebRedirect(_safe_next(next, "/"))
    users: UserRepository = request.app.state.users
    has_users = await users.count() > 0
    if not settings.allow_signup and has_users:
        return _page(
            request,
            "register.html",
            None,
            status_code=403,
            error="Signup is disabled",
            next_url=_safe_next(next, "/"),
            allow_signup=False,
        )
    return _page(
        request,
        "register.html",
        None,
        next_url=_safe_next(next, "/"),
        allow_signup=True,
    )


@router.post("/register")
async def register_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
):
    users: UserRepository = request.app.state.users
    normalized = normalize_email(email)
    has_users = await users.count() > 0
    error = ""
    if "@" not in normalized:
        error = "Enter a valid email address"
    elif len(password) < 8:
        error = "Password must be at least 8 characters"
    elif not settings.allow_signup and has_users:
        error = "Signup is disabled"
    elif await users.get_by_email(normalized) is not None:
        error = "Email already registered"

    if error:
        return _page(
            request,
            "register.html",
            None,
            status_code=400,
            error=error,
            next_url=_safe_next(next, "/"),
            allow_signup=settings.allow_signup or not has_users,
            email=email,
        )

    first = not has_users
    created = await users.create(
        User(
            id=None,
            email=normalized,
            password_hash=hash_password(password),
            is_admin=1 if first else 0,
            is_active=1,
            created_at=now_iso(),
        )
    )
    response = _redirect(request, _safe_next(next, "/"))
    _set_auth_cookie(response, create_token(created))
    return response


@router.post("/logout")
async def logout(request: Request):
    response = _redirect(request, "/login")
    _clear_auth_cookie(response)
    return response


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------


@router.get("/")
async def home_page(request: Request, user: User = Depends(require_page_user)):
    context = await _home_context(request, user)
    if _is_htmx(request):
        return _page(request, "partials/home_content.html", user, **context)
    return _page(request, "home.html", user, **context)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@router.get("/tasks")
async def tasks_page(
    request: Request,
    tab: str = "pending",
    q: str = "",
    user: User = Depends(require_page_user),
):
    context = await _tasks_context(request, user, tab, q)
    if _is_htmx(request):
        return _page(request, "partials/tasks_panel.html", user, **context)
    return _page(request, "tasks.html", user, **context)


def _task_schedule_payload(
    *,
    repeats: str,
    repeat_count: str,
    repeat_unit: str,
    slot_days: list[int] | None,
    end_date: str | None,
) -> ScheduleCreate | None:
    if repeats != "1":
        return None
    unit = repeat_unit if repeat_unit in ("day", "week", "month") else "week"
    if unit == "week":
        days = sorted({int(day) for day in (slot_days or []) if 0 <= int(day) <= 6})
    elif unit == "month":
        days = sorted({int(day) for day in (slot_days or []) if 1 <= int(day) <= 31})
    else:
        days = []
    try:
        count = int(repeat_count or 1)
    except ValueError:
        count = 1
    count = len(days) if days else max(1, count)
    return ScheduleCreate(
        repeat_count=count,
        repeat_unit=unit,
        end_date=end_date or None,
        slot_days=days or None,
    )


async def _task_modal_context(
    request: Request,
    user: User,
    task: dict | None,
    next_url: str,
    project_id: int | None = None,
) -> dict:
    projects = await request.app.state.projects.list(user.id)
    schedule = (task or {}).get("schedule") or {}
    slot_days = schedule.get("slot_days") or []
    return {
        "task": task,
        "projects": projects,
        "next_url": _safe_next(next_url, "/tasks"),
        "default_project_id": project_id
        if project_id is not None
        else (task or {}).get("project_id"),
        "today_iso": today_iso(),
        "repeat_count": schedule.get("repeat_count", 1),
        "repeat_unit": schedule.get("repeat_unit", "week"),
        "slot_days": slot_days,
        "slot_days_json": json.dumps(slot_days),
        "repeats": bool(schedule),
        "completion_mode": (task or {}).get("completion_mode", "carry"),
    }


@router.get("/tasks/new")
async def new_task_modal(
    request: Request,
    project_id: int | None = None,
    next: str = "/tasks",
    user: User = Depends(require_page_user),
):
    context = await _task_modal_context(request, user, None, next, project_id)
    return _page(request, "partials/task_modal.html", user, **context)


@router.get("/tasks/{task_id}/edit")
async def edit_task_modal(
    request: Request,
    task_id: int,
    next: str = "/tasks",
    user: User = Depends(require_page_user),
):
    task = await TaskService(request.app.state.tasks).detail(task_id, user.id)
    if task is None:
        raise WebRedirect("/tasks")
    context = await _task_modal_context(request, user, task, next)
    return _page(request, "partials/task_modal.html", user, **context)


@router.post("/tasks")
async def create_task_page(
    request: Request,
    title: str = Form(""),
    description: str = Form(""),
    project_id: str = Form(""),
    start_date: str = Form(""),
    due_date: str = Form(""),
    completion_mode: str = Form("carry"),
    repeats: str = Form("0"),
    repeat_count: str = Form("1"),
    repeat_unit: str = Form("week"),
    slot_days: list[int] | None = Form(None),
    next: str = Form("/tasks"),
    user: User = Depends(require_page_user),
):
    if not title.strip():
        context = await _task_modal_context(request, user, None, next)
        context["error"] = "Title is required"
        return _page(request, "partials/task_modal.html", user, **context)

    project_value = int(project_id) if project_id.isdigit() else None
    if project_value is not None and await request.app.state.projects.get(project_value, user.id) is None:
        project_value = None

    payload = _task_schedule_payload(
        repeats=repeats,
        repeat_count=repeat_count,
        repeat_unit=repeat_unit,
        slot_days=slot_days,
        end_date=due_date or None,
    )
    await create_task_with_schedule(
        title=title.strip(),
        description=description.strip(),
        project_id=project_value,
        schedule_payload=payload,
        repo=request.app.state.tasks,
        start_date=start_date or today_iso(),
        due_date=due_date or None,
        completion_mode=completion_mode if completion_mode in ("carry", "wait", "skip") else "carry",
        user_id=user.id,
    )
    return _redirect(request, _safe_next(next, "/tasks"))


@router.post("/tasks/{task_id}")
async def update_task_page(
    request: Request,
    task_id: int,
    title: str = Form(""),
    description: str = Form(""),
    project_id: str = Form(""),
    start_date: str = Form(""),
    due_date: str = Form(""),
    completion_mode: str = Form("carry"),
    repeats: str = Form("0"),
    repeat_count: str = Form("1"),
    repeat_unit: str = Form("week"),
    slot_days: list[int] | None = Form(None),
    next: str = Form("/tasks"),
    user: User = Depends(require_page_user),
):
    task = await request.app.state.tasks.get(task_id, user.id)
    if task is None:
        raise WebRedirect("/tasks")

    if not title.strip():
        detail = await TaskService(request.app.state.tasks).detail(task_id, user.id)
        context = await _task_modal_context(request, user, detail, next)
        context["error"] = "Title is required"
        return _page(request, "partials/task_modal.html", user, **context)

    project_value = int(project_id) if project_id.isdigit() else None
    if project_value is not None and await request.app.state.projects.get(project_value, user.id) is None:
        project_value = None

    task.title = title.strip()
    task.description = description.strip()
    task.project_id = project_value
    task.start_date = start_date or today_iso()
    task.due_date = due_date or None
    task.completion_mode = (
        completion_mode if completion_mode in ("carry", "wait", "skip") else "carry"
    )
    await request.app.state.tasks.update(task)

    payload = _task_schedule_payload(
        repeats=repeats,
        repeat_count=repeat_count,
        repeat_unit=repeat_unit,
        slot_days=slot_days,
        end_date=due_date or None,
    )
    await request.app.state.tasks.delete_schedule(task.id)
    if payload is not None:
        await set_schedule(request.app.state.tasks, task, payload)

    return _redirect(request, _safe_next(next, "/tasks"))


@router.post("/tasks/{task_id}/delete")
async def delete_task_page(
    request: Request,
    task_id: int,
    fragment: str = Form(""),
    tab: str = Form("pending"),
    q: str = Form(""),
    project_id: str = Form(""),
    next: str = Form("/tasks"),
    user: User = Depends(require_page_user),
):
    if await request.app.state.tasks.get(task_id, user.id) is not None:
        await request.app.state.tasks.delete(task_id, user.id)
    return await _respond(
        request,
        user,
        fragment,
        tab=tab,
        q=q,
        project_id=project_id,
        next_url=next,
    )


@router.post("/tasks/{task_id}/complete")
async def complete_task_page(
    request: Request,
    task_id: int,
    day: str = Form("", alias="date"),
    fragment: str = Form(""),
    tab: str = Form("pending"),
    q: str = Form(""),
    project_id: str = Form(""),
    next: str = Form("/"),
    user: User = Depends(require_page_user),
):
    if await request.app.state.tasks.get(task_id, user.id) is None:
        raise WebRedirect("/tasks")
    await request.app.state.tasks.add_completion(task_id, day or today_iso())
    return await _respond(
        request, user, fragment, tab=tab, q=q, project_id=project_id, next_url=next
    )


@router.post("/tasks/{task_id}/undo")
async def undo_task_page(
    request: Request,
    task_id: int,
    day: str = Form("", alias="date"),
    fragment: str = Form(""),
    tab: str = Form("pending"),
    q: str = Form(""),
    project_id: str = Form(""),
    next: str = Form("/"),
    user: User = Depends(require_page_user),
):
    if await request.app.state.tasks.get(task_id, user.id) is None:
        raise WebRedirect("/tasks")
    await request.app.state.tasks.delete_latest_for_day(task_id, day or today_iso())
    return await _respond(
        request, user, fragment, tab=tab, q=q, project_id=project_id, next_url=next
    )


@router.post("/tasks/{task_id}/toggle")
async def toggle_task_page(
    request: Request,
    task_id: int,
    day: str = Form("", alias="date"),
    fragment: str = Form(""),
    tab: str = Form("pending"),
    q: str = Form(""),
    project_id: str = Form(""),
    next: str = Form("/"),
    user: User = Depends(require_page_user),
):
    if await request.app.state.tasks.get(task_id, user.id) is None:
        raise WebRedirect("/tasks")
    target_day = day or today_iso()
    completions = await request.app.state.tasks.completions_for_task(task_id)
    if any(item.date == target_day for item in completions):
        await request.app.state.tasks.delete_latest_for_day(task_id, target_day)
    else:
        await request.app.state.tasks.add_completion(task_id, target_day)
    return await _respond(
        request, user, fragment, tab=tab, q=q, project_id=project_id, next_url=next
    )


@router.post("/tasks/{task_id}/notes/{day}")
async def save_note_page(
    request: Request,
    task_id: int,
    day: str,
    note: str = Form(""),
    fragment: str = Form(""),
    tab: str = Form("pending"),
    q: str = Form(""),
    project_id: str = Form(""),
    calendar_date: str = Form("", alias="calendar_date"),
    calendar_kind: str = Form("tasks", alias="kind"),
    next: str = Form("/"),
    user: User = Depends(require_page_user),
):
    if await request.app.state.tasks.get(task_id, user.id) is not None:
        await request.app.state.tasks.upsert_note(task_id, day, note)
    return await _respond(
        request,
        user,
        fragment,
        tab=tab,
        q=q,
        project_id=project_id,
        calendar_date=calendar_date or day,
        calendar_kind=calendar_kind,
        next_url=next,
    )


@router.post("/tasks/{task_id}/notes/{day}/delete")
async def delete_note_page(
    request: Request,
    task_id: int,
    day: str,
    fragment: str = Form(""),
    tab: str = Form("pending"),
    q: str = Form(""),
    project_id: str = Form(""),
    calendar_date: str = Form("", alias="calendar_date"),
    calendar_kind: str = Form("tasks", alias="kind"),
    next: str = Form("/"),
    user: User = Depends(require_page_user),
):
    if await request.app.state.tasks.get(task_id, user.id) is not None:
        await request.app.state.tasks.delete_note(task_id, day)
    return await _respond(
        request,
        user,
        fragment,
        tab=tab,
        q=q,
        project_id=project_id,
        calendar_date=calendar_date or day,
        calendar_kind=calendar_kind,
        next_url=next,
    )


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


@router.get("/projects")
async def projects_page(
    request: Request,
    status: str = "open",
    user: User = Depends(require_page_user),
):
    context = await _projects_context(request, user, status)
    return _page(request, "projects.html", user, **context)


@router.get("/projects/new")
async def new_project_modal(
    request: Request,
    next: str = "/projects",
    user: User = Depends(require_page_user),
):
    return _page(
        request,
        "partials/project_modal.html",
        user,
        project=None,
        next_url=_safe_next(next, "/projects"),
    )


@router.get("/projects/{project_id}/edit")
async def edit_project_modal(
    request: Request,
    project_id: int,
    next: str = "/projects",
    user: User = Depends(require_page_user),
):
    project = await request.app.state.projects.get(project_id, user.id)
    if project is None:
        raise WebRedirect("/projects")
    return _page(
        request,
        "partials/project_modal.html",
        user,
        project=_project_dict(project),
        next_url=_safe_next(next, "/projects"),
    )


@router.get("/projects/{project_id}")
async def project_detail_page(
    request: Request, project_id: int, user: User = Depends(require_page_user)
):
    context = await _project_context(request, user, project_id)
    if context is None:
        raise WebRedirect("/projects")
    return _page(request, "project_detail.html", user, **context)


@router.post("/projects")
async def create_project_page(
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    deadline: str = Form(""),
    next: str = Form("/projects?status=open"),
    user: User = Depends(require_page_user),
):
    if not name.strip():
        return _page(
            request,
            "partials/project_modal.html",
            user,
            status_code=200,
            project=None,
            error="Name is required",
            next_url=_safe_next(next, "/projects"),
        )
    now = now_iso()
    await request.app.state.projects.create(
        Project(
            id=None,
            name=name.strip(),
            description=description.strip(),
            status="open",
            deadline=deadline or None,
            created_at=now,
            updated_at=now,
            user_id=user.id,
        )
    )
    return _redirect(request, _safe_next(next, "/projects?status=open"))


@router.post("/projects/{project_id}")
async def update_project_page(
    request: Request,
    project_id: int,
    name: str = Form(""),
    description: str = Form(""),
    deadline: str = Form(""),
    next: str = Form("/projects?status=open"),
    user: User = Depends(require_page_user),
):
    project = await request.app.state.projects.get(project_id, user.id)
    if project is None:
        raise WebRedirect("/projects")
    if not name.strip():
        return _page(
            request,
            "partials/project_modal.html",
            user,
            project=_project_dict(project),
            error="Name is required",
            next_url=_safe_next(next, "/projects"),
        )
    project.name = name.strip()
    project.description = description.strip()
    project.deadline = deadline or None
    project.updated_at = now_iso()
    await request.app.state.projects.update(project)
    return _redirect(request, _safe_next(next, f"/projects/{project_id}"))


@router.post("/projects/{project_id}/status")
async def project_status_page(
    request: Request,
    project_id: int,
    status: str = Form("open"),
    next: str = Form("/projects"),
    user: User = Depends(require_page_user),
):
    project = await request.app.state.projects.get(project_id, user.id)
    if project is not None and status in ("open", "closed", "archived"):
        project.status = status
        project.updated_at = now_iso()
        await request.app.state.projects.update(project)
    return _redirect(request, _safe_next(next, "/projects"))


@router.post("/projects/{project_id}/delete")
async def delete_project_page(
    request: Request,
    project_id: int,
    next: str = Form("/projects?status=open"),
    user: User = Depends(require_page_user),
):
    if await request.app.state.projects.get(project_id, user.id) is not None:
        await request.app.state.projects.delete(project_id, user.id)
    return _redirect(request, _safe_next(next, "/projects?status=open"))


# ---------------------------------------------------------------------------
# Routines
# ---------------------------------------------------------------------------


@router.get("/routines")
async def routines_page(request: Request, user: User = Depends(require_page_user)):
    context = await _routines_context(request, user)
    return _page(request, "routines.html", user, **context)


@router.get("/routines/new")
async def new_routine_modal(
    request: Request,
    next: str = "/routines",
    user: User = Depends(require_page_user),
):
    return _page(
        request,
        "partials/routine_modal.html",
        user,
        routine=None,
        next_url=_safe_next(next, "/routines"),
        count=3,
        unit="day",
        slot_days=[],
        slot_days_json="[]",
    )


@router.get("/routines/{routine_id}/edit")
async def edit_routine_modal(
    request: Request,
    routine_id: int,
    next: str = "/routines",
    user: User = Depends(require_page_user),
):
    routine = await request.app.state.routines.get(routine_id, user.id)
    if routine is None:
        raise WebRedirect("/routines")
    days = parse_slot_days(routine.slot_days) or []
    return _page(
        request,
        "partials/routine_modal.html",
        user,
        routine={
            "id": routine.id,
            "name": routine.name,
            "description": routine.description,
            "frequency_count": routine.frequency_count,
            "frequency_unit": routine.frequency_unit,
            "slot_days": days,
        },
        next_url=_safe_next(next, "/routines"),
        count=routine.frequency_count,
        unit=routine.frequency_unit,
        slot_days=days,
        slot_days_json=json.dumps(days),
    )


def _routine_payload(
    count: str, unit: str, slot_days: list[int] | None
) -> tuple[int, str, str | None]:
    frequency_unit = unit if unit in ("day", "week", "month", "year") else "day"
    if frequency_unit == "week":
        days = sorted({int(day) for day in (slot_days or []) if 0 <= int(day) <= 6})
    elif frequency_unit == "month":
        days = sorted({int(day) for day in (slot_days or []) if 1 <= int(day) <= 31})
    else:
        days = []
    try:
        frequency_count = int(count or 1)
    except ValueError:
        frequency_count = 1
    frequency_count = len(days) if days else max(1, frequency_count)
    return frequency_count, frequency_unit, json.dumps(days) if days else None


@router.post("/routines")
async def create_routine_page(
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    frequency_count: str = Form("1"),
    frequency_unit: str = Form("day"),
    slot_days: list[int] | None = Form(None),
    next: str = Form("/routines"),
    user: User = Depends(require_page_user),
):
    if not name.strip():
        return _page(
            request,
            "partials/routine_modal.html",
            user,
            routine=None,
            error="Name is required",
            next_url=_safe_next(next, "/routines"),
            count=frequency_count,
            unit=frequency_unit,
            slot_days=slot_days or [],
            slot_days_json=json.dumps(slot_days or []),
        )
    count, unit, days = _routine_payload(frequency_count, frequency_unit, slot_days)
    await request.app.state.routines.create(
        Routine(
            id=None,
            name=name.strip(),
            description=description.strip(),
            frequency_count=count,
            frequency_unit=unit,
            created_at=now_iso(),
            slot_days=days,
            user_id=user.id,
        )
    )
    return _redirect(request, _safe_next(next, "/routines"))


@router.post("/routines/{routine_id}")
async def update_routine_page(
    request: Request,
    routine_id: int,
    name: str = Form(""),
    description: str = Form(""),
    frequency_count: str = Form("1"),
    frequency_unit: str = Form("day"),
    slot_days: list[int] | None = Form(None),
    next: str = Form("/routines"),
    user: User = Depends(require_page_user),
):
    routine = await request.app.state.routines.get(routine_id, user.id)
    if routine is None:
        raise WebRedirect("/routines")
    if not name.strip():
        days = slot_days or []
        return _page(
            request,
            "partials/routine_modal.html",
            user,
            routine=_routine_dict(routine, []),
            error="Name is required",
            next_url=_safe_next(next, "/routines"),
            count=frequency_count,
            unit=frequency_unit,
            slot_days=days,
            slot_days_json=json.dumps(days),
        )
    count, unit, days = _routine_payload(frequency_count, frequency_unit, slot_days)
    routine.name = name.strip()
    routine.description = description.strip()
    routine.frequency_count = count
    routine.frequency_unit = unit
    routine.slot_days = days
    await request.app.state.routines.update(routine)
    return _redirect(request, _safe_next(next, "/routines"))


@router.post("/routines/{routine_id}/delete")
async def delete_routine_page(
    request: Request,
    routine_id: int,
    fragment: str = Form(""),
    next: str = Form("/routines"),
    user: User = Depends(require_page_user),
):
    if await request.app.state.routines.get(routine_id, user.id) is not None:
        await request.app.state.routines.delete(routine_id, user.id)
    return await _respond(request, user, fragment, next_url=next)


@router.post("/routines/{routine_id}/complete")
async def complete_routine_page(
    request: Request,
    routine_id: int,
    day: str = Form("", alias="date"),
    fragment: str = Form(""),
    next: str = Form("/routines"),
    user: User = Depends(require_page_user),
):
    if await request.app.state.routines.get(routine_id, user.id) is None:
        raise WebRedirect("/routines")
    await request.app.state.routines.add_completion(routine_id, day or today_iso())
    return await _respond(request, user, fragment, next_url=next)


@router.post("/routines/{routine_id}/undo")
async def undo_routine_page(
    request: Request,
    routine_id: int,
    day: str = Form("", alias="date"),
    fragment: str = Form(""),
    next: str = Form("/routines"),
    user: User = Depends(require_page_user),
):
    if await request.app.state.routines.get(routine_id, user.id) is None:
        raise WebRedirect("/routines")
    await request.app.state.routines.delete_latest_for_day(routine_id, day or today_iso())
    return await _respond(request, user, fragment, next_url=next)


@router.post("/routines/{routine_id}/toggle")
async def toggle_routine_page(
    request: Request,
    routine_id: int,
    day: str = Form("", alias="date"),
    fragment: str = Form(""),
    next: str = Form("/routines"),
    user: User = Depends(require_page_user),
):
    if await request.app.state.routines.get(routine_id, user.id) is None:
        raise WebRedirect("/routines")
    target_day = day or today_iso()
    completions = await request.app.state.routines.completions_for_routine(routine_id)
    if any(item.date == target_day for item in completions):
        await request.app.state.routines.delete_latest_for_day(routine_id, target_day)
    else:
        await request.app.state.routines.add_completion(routine_id, target_day)
    return await _respond(request, user, fragment, next_url=next)


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------


@router.get("/goals")
async def goals_page(request: Request, user: User = Depends(require_page_user)):
    return _page(
        request,
        "goals.html",
        user,
        **await _goals_context(request, user),
    )


@router.get("/goals/new")
async def new_goal_modal(
    request: Request,
    next: str = "/goals",
    user: User = Depends(require_page_user),
):
    fields = [
        {
            "key": 0,
            "id": None,
            "name": "",
            "unit": "",
            "target": "",
            "compare": "latest",
            "direction": "higher",
        }
    ]
    return _page(
        request,
        "partials/goal_modal.html",
        user,
        goal=None,
        form={"title": "", "description": ""},
        builder=_goal_builder_data("measured", True, fields),
        next_url=_safe_next(next, "/goals"),
        error="",
    )


@router.get("/goals/{goal_id}")
async def goal_detail_page(
    request: Request,
    goal_id: int,
    user: User = Depends(require_page_user),
):
    context = await _goal_detail_context(request, user, goal_id)
    if context is None:
        raise WebRedirect("/goals")
    return _page(request, "goal_detail.html", user, **context)


@router.get("/goals/{goal_id}/edit")
async def edit_goal_modal(
    request: Request,
    goal_id: int,
    next: str = "",
    user: User = Depends(require_page_user),
):
    goal = await request.app.state.goals.get(goal_id, user.id)
    if goal is None:
        raise WebRedirect("/goals")
    rows = await request.app.state.goals.fields_for(goal_id)
    fields = [
        {
            "key": index,
            "id": field.id,
            "name": field.name,
            "unit": field.unit,
            "target": "" if field.target_value is None else field.target_value,
            "compare": field.compare_mode,
            "direction": "higher" if field.higher_is_better else "lower",
        }
        for index, field in enumerate(rows)
    ]
    stage_rows = await request.app.state.goals.stages_for(goal_id)
    stages = [
        {
            "key": index,
            "id": stage.id,
            "title": stage.title,
            "done": bool(stage.done),
        }
        for index, stage in enumerate(stage_rows)
    ]
    return _page(
        request,
        "partials/goal_modal.html",
        user,
        goal={"id": goal.id, "kind": goal.kind},
        form={"title": goal.title, "description": goal.description},
        builder=_goal_builder_data(
            goal.kind, bool(goal.show_chart), fields, stages
        ),
        next_url=_safe_next(next, f"/goals/{goal.id}"),
        error="",
    )


@router.post("/goals")
async def create_goal_page(
    request: Request,
    title: str = Form(""),
    description: str = Form(""),
    kind: str = Form("measured"),
    show_chart: str = Form(""),
    field_id: list[str] | None = Form(None),
    field_name: list[str] | None = Form(None),
    field_unit: list[str] | None = Form(None),
    field_target: list[str] | None = Form(None),
    field_compare: list[str] | None = Form(None),
    field_direction: list[str] | None = Form(None),
    stage_id: list[str] | None = Form(None),
    stage_title: list[str] | None = Form(None),
    stage_done: list[str] | None = Form(None),
    next: str = Form("/goals"),
    user: User = Depends(require_page_user),
):
    fields = _collect_goal_fields(
        field_id, field_name, field_unit, field_target, field_compare, field_direction
    )
    stages = _collect_goal_stages(stage_id, stage_title, stage_done)
    effective_kind = kind if kind in ("measured", "percent", "staged") else "measured"
    show = show_chart.strip().lower() in ("1", "on", "true", "yes")
    service = GoalService(request.app.state.goals)
    try:
        detail = await service.create_goal(
            user.id,
            title,
            description,
            effective_kind,
            show,
            fields if effective_kind == "measured" else [],
            stages if effective_kind == "staged" else [],
        )
    except GoalValidationError as exc:
        return _page(
            request,
            "partials/goal_modal.html",
            user,
            goal=None,
            form={"title": title, "description": description},
            builder=_goal_builder_data(
                effective_kind,
                show,
                fields if effective_kind == "measured" else [],
                stages if effective_kind == "staged" else [],
            ),
            next_url=_safe_next(next, "/goals"),
            error=str(exc),
        )
    return _redirect(request, f"/goals/{detail['id']}")


@router.post("/goals/{goal_id}")
async def update_goal_page(
    request: Request,
    goal_id: int,
    title: str = Form(""),
    description: str = Form(""),
    show_chart: str = Form(""),
    field_id: list[str] | None = Form(None),
    field_name: list[str] | None = Form(None),
    field_unit: list[str] | None = Form(None),
    field_target: list[str] | None = Form(None),
    field_compare: list[str] | None = Form(None),
    field_direction: list[str] | None = Form(None),
    stage_id: list[str] | None = Form(None),
    stage_title: list[str] | None = Form(None),
    stage_done: list[str] | None = Form(None),
    next: str = Form(""),
    user: User = Depends(require_page_user),
):
    goal = await request.app.state.goals.get(goal_id, user.id)
    if goal is None:
        raise WebRedirect("/goals")
    fields = _collect_goal_fields(
        field_id, field_name, field_unit, field_target, field_compare, field_direction
    )
    stages = _collect_goal_stages(stage_id, stage_title, stage_done)
    show = show_chart.strip().lower() in ("1", "on", "true", "yes")
    service = GoalService(request.app.state.goals)
    try:
        await service.update_goal(
            goal,
            title=title,
            description=description,
            show_chart=show,
            fields_data=fields if goal.kind == "measured" else None,
            stages_data=stages if goal.kind == "staged" else None,
        )
    except GoalValidationError as exc:
        return _page(
            request,
            "partials/goal_modal.html",
            user,
            goal={"id": goal.id, "kind": goal.kind},
            form={"title": title, "description": description},
            builder=_goal_builder_data(
                goal.kind,
                show,
                fields if goal.kind == "measured" else [],
                stages if goal.kind == "staged" else [],
            ),
            next_url=_safe_next(next, f"/goals/{goal.id}"),
            error=str(exc),
        )
    return _redirect(request, _safe_next(next, f"/goals/{goal.id}"))


@router.post("/goals/{goal_id}/delete")
async def delete_goal_page(
    request: Request,
    goal_id: int,
    fragment: str = Form(""),
    next: str = Form("/goals"),
    user: User = Depends(require_page_user),
):
    if await request.app.state.goals.get(goal_id, user.id) is not None:
        await GoalService(request.app.state.goals).delete_goal(goal_id, user.id)
    if fragment == "goals":
        return await _respond(request, user, "goals", next_url=next)
    return _redirect(request, _safe_next(next, "/goals"))


@router.post("/goals/{goal_id}/status")
async def goal_status_page(
    request: Request,
    goal_id: int,
    status: str = Form("active"),
    fragment: str = Form(""),
    next: str = Form(""),
    user: User = Depends(require_page_user),
):
    goal = await request.app.state.goals.get(goal_id, user.id)
    if goal is None:
        raise WebRedirect("/goals")
    try:
        await GoalService(request.app.state.goals).set_status(goal, status)
    except GoalValidationError as exc:
        return await _goal_fragment(request, user, goal_id, error=str(exc))
    if fragment == "goal":
        return await _goal_fragment(request, user, goal_id)
    if fragment == "goals":
        return await _respond(request, user, "goals", next_url=next)
    return _redirect(request, _safe_next(next, f"/goals/{goal_id}"))


@router.post("/goals/{goal_id}/stages")
async def add_goal_stage_page(
    request: Request,
    goal_id: int,
    title: str = Form(""),
    user: User = Depends(require_page_user),
):
    goal = await request.app.state.goals.get(goal_id, user.id)
    if goal is None:
        raise WebRedirect("/goals")
    try:
        await GoalService(request.app.state.goals).add_stage(goal, title)
    except GoalValidationError as exc:
        return await _goal_fragment(request, user, goal_id, error=str(exc))
    return await _goal_fragment(request, user, goal_id)


@router.post("/goals/{goal_id}/stages/{stage_id}/toggle")
async def toggle_goal_stage_page(
    request: Request,
    goal_id: int,
    stage_id: int,
    user: User = Depends(require_page_user),
):
    goal = await request.app.state.goals.get(goal_id, user.id)
    if goal is None:
        raise WebRedirect("/goals")
    try:
        await GoalService(request.app.state.goals).toggle_stage(goal, stage_id)
    except GoalValidationError as exc:
        return await _goal_fragment(request, user, goal_id, error=str(exc))
    return await _goal_fragment(request, user, goal_id)


@router.post("/goals/{goal_id}/stages/{stage_id}/delete")
async def delete_goal_stage_page(
    request: Request,
    goal_id: int,
    stage_id: int,
    user: User = Depends(require_page_user),
):
    goal = await request.app.state.goals.get(goal_id, user.id)
    if goal is None:
        raise WebRedirect("/goals")
    if await request.app.state.goals.stage_for(stage_id, goal_id) is not None:
        await GoalService(request.app.state.goals).delete_stage(goal, stage_id)
    return await _goal_fragment(request, user, goal_id)


@router.post("/goals/{goal_id}/entries")
async def create_goal_entry_page(
    request: Request,
    goal_id: int,
    date_value: str = Form("", alias="date"),
    note: str = Form(""),
    field_id: list[int] | None = Form(None),
    value: list[str] | None = Form(None),
    user: User = Depends(require_page_user),
):
    goal = await request.app.state.goals.get(goal_id, user.id)
    if goal is None:
        raise WebRedirect("/goals")
    try:
        await GoalService(request.app.state.goals).add_entry(
            goal, date_value, note, _entry_values(field_id, value)
        )
    except GoalValidationError as exc:
        return await _goal_fragment(request, user, goal_id, error=str(exc))
    return await _goal_fragment(request, user, goal_id)


@router.post("/goals/{goal_id}/entries/{entry_id}")
async def update_goal_entry_page(
    request: Request,
    goal_id: int,
    entry_id: int,
    date_value: str = Form("", alias="date"),
    note: str = Form(""),
    field_id: list[int] | None = Form(None),
    value: list[str] | None = Form(None),
    user: User = Depends(require_page_user),
):
    goal = await request.app.state.goals.get(goal_id, user.id)
    if goal is None:
        raise WebRedirect("/goals")
    entry = await request.app.state.goals.entry_for(entry_id, goal_id)
    if entry is None:
        raise WebRedirect(f"/goals/{goal_id}")
    try:
        await GoalService(request.app.state.goals).update_entry(
            goal, entry, date_value, note, _entry_values(field_id, value)
        )
    except GoalValidationError as exc:
        return await _goal_fragment(request, user, goal_id, error=str(exc))
    return await _goal_fragment(request, user, goal_id)


@router.post("/goals/{goal_id}/entries/{entry_id}/delete")
async def delete_goal_entry_page(
    request: Request,
    goal_id: int,
    entry_id: int,
    user: User = Depends(require_page_user),
):
    goal = await request.app.state.goals.get(goal_id, user.id)
    if goal is None:
        raise WebRedirect("/goals")
    if await request.app.state.goals.entry_for(entry_id, goal_id) is not None:
        await GoalService(request.app.state.goals).delete_entry(goal, entry_id)
    return await _goal_fragment(request, user, goal_id)


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


@router.get("/calendar")
async def calendar_page(
    request: Request,
    kind: str = "tasks",
    year: int | None = None,
    month: int | None = None,
    selected: str | None = None,
    partial: str = "",
    user: User = Depends(require_page_user),
):
    today = date.today()
    context = await _calendar_context(
        request,
        user,
        kind,
        year or today.year,
        month or today.month,
        selected,
    )
    if partial == "1" or _is_htmx(request):
        return _page(request, "partials/calendar_body.html", user, **context)
    return _page(request, "calendar.html", user, **context)


@router.get("/calendar/day")
async def calendar_day_panel(
    request: Request,
    kind: str = "tasks",
    date_str: str = "",
    user: User = Depends(require_page_user),
):
    from .templating import format_day_long

    selected = date_str or None
    year = date.fromisoformat(selected).year if selected else date.today().year
    month = date.fromisoformat(selected).month if selected else date.today().month
    panel = await _day_panel_context(request, user, kind, selected, year, month)
    if selected:
        panel["selected_long"] = format_day_long(selected)
    return _page(request, "partials/day_panel.html", user, **panel)


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


def _admin_url(path: str, *, error: str = "", message: str = "") -> str:
    pairs = []
    if error:
        pairs.append(f"error={quote(error)}")
    if message:
        pairs.append(f"message={quote(message)}")
    return path + (("?" + "&".join(pairs)) if pairs else "")


def _bool_form(value: str, default: bool = False) -> bool:
    raw = (value or "").strip().lower()
    if raw in ("1", "on", "true", "yes"):
        return True
    if raw in ("0", "off", "false", "no"):
        return False
    return default


def _admin_service(request: Request) -> AdminService:
    return AdminService(request.app.state.users, request.app.state.admin)


@router.get("/admin")
async def admin_page(
    request: Request,
    q: str = "",
    role: str = "all",
    status: str = "all",
    user: User = Depends(require_admin_page),
):
    context = await _admin_service(request).dashboard(q, role, status)
    return _page(
        request,
        "admin.html",
        user,
        error=request.query_params.get("error", ""),
        message=request.query_params.get("message", ""),
        **context,
    )


@router.get("/admin/users/new")
async def admin_new_user_modal(
    request: Request,
    next: str = "/admin",
    user: User = Depends(require_admin_page),
):
    return _page(
        request,
        "partials/admin_user_modal.html",
        user,
        mode="create",
        account=None,
        form={"email": "", "is_admin": "0", "is_active": "1"},
        next_url=_safe_next(next, "/admin"),
        error="",
    )


@router.get("/admin/users/{user_id}")
async def admin_user_page(
    request: Request, user_id: int, user: User = Depends(require_admin_page)
):
    context = await _admin_service(request).user_detail(user_id)
    if context is None:
        raise WebRedirect("/admin")
    return _page(
        request,
        "admin_user.html",
        user,
        error=request.query_params.get("error", ""),
        message=request.query_params.get("message", ""),
        **context,
    )


@router.get("/admin/users/{user_id}/edit")
async def admin_edit_user_modal(
    request: Request,
    user_id: int,
    next: str = "",
    user: User = Depends(require_admin_page),
):
    target = await request.app.state.users.get(user_id)
    if target is None:
        raise WebRedirect("/admin")
    return _page(
        request,
        "partials/admin_user_modal.html",
        user,
        mode="edit",
        account={"id": target.id, "email": target.email},
        form={
            "email": target.email,
            "is_admin": "1" if target.is_admin else "0",
            "is_active": "1" if target.is_active else "0",
        },
        next_url=_safe_next(next, f"/admin/users/{target.id}"),
        error="",
    )


@router.get("/admin/users/{user_id}/reset-password")
async def admin_reset_password_modal(
    request: Request,
    user_id: int,
    next: str = "",
    user: User = Depends(require_admin_page),
):
    target = await request.app.state.users.get(user_id)
    if target is None:
        raise WebRedirect("/admin")
    return _page(
        request,
        "partials/admin_password_modal.html",
        user,
        account={"id": target.id, "email": target.email},
        next_url=_safe_next(next, f"/admin/users/{target.id}"),
        error="",
    )


@router.post("/admin/users")
async def admin_create_user(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    is_admin: str = Form("0"),
    is_active: str = Form("1"),
    next: str = Form("/admin"),
    user: User = Depends(require_admin_page),
):
    try:
        detail = await _admin_service(request).create_user(
            user.id,
            email=email,
            password=password,
            is_admin=_bool_form(is_admin),
            is_active=_bool_form(is_active, default=True),
        )
    except AdminValidationError as exc:
        return _page(
            request,
            "partials/admin_user_modal.html",
            user,
            mode="create",
            account=None,
            form={"email": email, "is_admin": is_admin, "is_active": is_active},
            next_url=_safe_next(next, "/admin"),
            error=str(exc),
        )
    account = detail["account"]
    return _redirect(
        request,
        _admin_url(f"/admin/users/{account['id']}", message="User created"),
    )


@router.post("/admin/users/{user_id}")
async def admin_update_user(
    request: Request,
    user_id: int,
    email: str = Form(""),
    is_admin: str = Form("0"),
    is_active: str = Form("1"),
    next: str = Form(""),
    user: User = Depends(require_admin_page),
):
    target = await request.app.state.users.get(user_id)
    if target is None:
        raise WebRedirect("/admin")
    try:
        await _admin_service(request).update_user(
            user.id,
            target,
            email=email,
            is_admin=_bool_form(is_admin),
            is_active=_bool_form(is_active, default=True),
        )
    except AdminValidationError as exc:
        return _page(
            request,
            "partials/admin_user_modal.html",
            user,
            mode="edit",
            account={"id": target.id, "email": target.email},
            form={"email": email, "is_admin": is_admin, "is_active": is_active},
            next_url=_safe_next(next, f"/admin/users/{target.id}"),
            error=str(exc),
        )
    return _redirect(
        request,
        _admin_url(
            _safe_next(next, f"/admin/users/{target.id}"), message="User updated"
        ),
    )


@router.post("/admin/users/{user_id}/reset-password")
async def admin_reset_user_password(
    request: Request,
    user_id: int,
    password: str = Form(""),
    confirm: str = Form(""),
    next: str = Form(""),
    user: User = Depends(require_admin_page),
):
    target = await request.app.state.users.get(user_id)
    if target is None:
        raise WebRedirect("/admin")
    error = ""
    if password != confirm:
        error = "Passwords do not match"
    else:
        try:
            await _admin_service(request).reset_password(user.id, target, password)
        except AdminValidationError as exc:
            error = str(exc)
    if error:
        return _page(
            request,
            "partials/admin_password_modal.html",
            user,
            account={"id": target.id, "email": target.email},
            next_url=_safe_next(next, f"/admin/users/{target.id}"),
            error=error,
        )
    return _redirect(
        request,
        _admin_url(
            _safe_next(next, f"/admin/users/{target.id}"),
            message="Password reset",
        ),
    )


@router.post("/admin/users/{user_id}/toggle-admin")
async def admin_toggle_admin(
    request: Request,
    user_id: int,
    next: str = Form("/admin"),
    user: User = Depends(require_admin_page),
):
    target = await request.app.state.users.get(user_id)
    if target is None:
        return _redirect(request, _admin_url("/admin", error="User not found"))
    try:
        await _admin_service(request).toggle_admin(user.id, target)
    except AdminValidationError as exc:
        return _redirect(request, _admin_url("/admin", error=str(exc)))
    return _redirect(
        request,
        _admin_url(_safe_next(next, "/admin"), message="Role updated"),
    )


@router.post("/admin/users/{user_id}/toggle-active")
async def admin_toggle_active(
    request: Request,
    user_id: int,
    next: str = Form("/admin"),
    user: User = Depends(require_admin_page),
):
    target = await request.app.state.users.get(user_id)
    if target is None:
        return _redirect(request, _admin_url("/admin", error="User not found"))
    try:
        await _admin_service(request).toggle_active(user.id, target)
    except AdminValidationError as exc:
        return _redirect(request, _admin_url("/admin", error=str(exc)))
    return _redirect(
        request,
        _admin_url(_safe_next(next, "/admin"), message="Access updated"),
    )


@router.post("/admin/users/{user_id}/delete")
async def admin_delete_user(
    request: Request,
    user_id: int,
    next: str = Form("/admin"),
    user: User = Depends(require_admin_page),
):
    target = await request.app.state.users.get(user_id)
    if target is None:
        return _redirect(request, _admin_url("/admin", error="User not found"))
    try:
        await _admin_service(request).delete_user(user.id, target)
    except AdminValidationError as exc:
        return _redirect(
            request, _admin_url(f"/admin/users/{user_id}", error=str(exc))
        )
    return _redirect(
        request,
        _admin_url(_safe_next(next, "/admin"), message="User deleted"),
    )
