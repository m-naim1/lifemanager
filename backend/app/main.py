from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import web
from .auth import hash_password, normalize_email
from .config import settings
from .d1 import D1Database
from .db import Database
from .models import User, now_iso
from .repositories import (
    AdminRepository,
    GoalRepository,
    ProjectRepository,
    RoutineRepository,
    TaskRepository,
    UserRepository,
)
from .routers import auth as auth_router
from .routers import goals, home, projects, routines, stats, tasks
from .templating import STATIC_DIR


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.db_driver == "d1":
        db = D1Database()
    else:
        db = Database(settings.db_path)
    await db.connect()
    app.state.db = db
    app.state.projects = ProjectRepository(db)
    app.state.tasks = TaskRepository(db)
    app.state.routines = RoutineRepository(db)
    app.state.goals = GoalRepository(db)
    app.state.users = UserRepository(db)
    app.state.admin = AdminRepository(db)
    # Bootstrap admin from env if provided (for first deploy).
    if settings.admin_email and settings.admin_password:
        users: UserRepository = app.state.users
        email = normalize_email(settings.admin_email)
        existing = await users.get_by_email(email)
        if existing is None:
            await users.create(
                User(
                    id=None,
                    email=email,
                    password_hash=hash_password(settings.admin_password),
                    is_admin=1,
                    is_active=1,
                    created_at=now_iso(),
                )
            )
    yield
    await db.close()


app = FastAPI(title="LifeManager", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(auth_router.router, prefix="/api/auth")
app.include_router(auth_router.admin_router, prefix="/api/admin")
app.include_router(home.router, prefix="/api/home")
app.include_router(projects.router, prefix="/api/projects")
app.include_router(tasks.router, prefix="/api/tasks")
app.include_router(routines.router, prefix="/api/routines")
app.include_router(goals.router, prefix="/api/goals")
app.include_router(stats.router, prefix="/api/stats")
app.include_router(web.router)


@app.exception_handler(web.WebRedirect)
async def web_redirect_handler(request: Request, exc: web.WebRedirect) -> Response:
    if request.headers.get("HX-Request", "").lower() == "true":
        return Response(status_code=204, headers={"HX-Redirect": exc.url})
    return RedirectResponse(exc.url, status_code=exc.status_code)


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "app": "lifemanager"}
