from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth import create_token, hash_password, normalize_email, user_public, verify_password
from ..config import settings
from ..deps import get_admin_repo, get_users_repo, require_admin, require_user
from ..models import User, now_iso
from ..repositories import AdminRepository, UserRepository
from ..schemas import (
    AdminUserCreate,
    AdminUserUpdate,
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
)
from ..services import AdminService, AdminValidationError

router = APIRouter(tags=["auth"])


@router.post("/register")
async def register(payload: RegisterRequest, users: UserRepository = Depends(get_users_repo)) -> dict:
    email = normalize_email(payload.email)
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email")
    if not settings.allow_signup and await users.count() > 0:
        raise HTTPException(status_code=403, detail="Signup is disabled")
    if await users.get_by_email(email) is not None:
        raise HTTPException(status_code=400, detail="Email already registered")
    first = await users.count() == 0
    created = await users.create(
        User(
            id=None,
            email=email,
            password_hash=hash_password(payload.password),
            is_admin=1 if first else 0,
            is_active=1,
            created_at=now_iso(),
        )
    )
    return {"token": create_token(created), "user": user_public(created)}


@router.post("/login")
async def login(payload: LoginRequest, users: UserRepository = Depends(get_users_repo)) -> dict:
    email = normalize_email(payload.email)
    user = await users.get_by_email(email)
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User is disabled")
    return {"token": create_token(user), "user": user_public(user)}


@router.get("/me")
async def me(user: User = Depends(require_user)) -> dict:
    return user_public(user)


@router.post("/logout")
async def logout() -> dict:
    # Stateless JWT: client discards the token.
    return {"ok": True}


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    user: User = Depends(require_user),
    users: UserRepository = Depends(get_users_repo),
) -> dict:
    fresh = await users.get(user.id)
    if fresh is None or not verify_password(payload.current_password, fresh.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    fresh.password_hash = hash_password(payload.new_password)
    await users.update(fresh)
    return {"ok": True}


admin_router = APIRouter(tags=["admin"])


def _admin_service(
    users: UserRepository, admin_repo: AdminRepository
) -> AdminService:
    return AdminService(users, admin_repo)


@admin_router.get("/overview")
async def admin_overview(
    _admin: User = Depends(require_admin),
    users: UserRepository = Depends(get_users_repo),
    admin_repo: AdminRepository = Depends(get_admin_repo),
) -> dict:
    return await _admin_service(users, admin_repo).dashboard()


@admin_router.get("/users")
async def list_users(
    _admin: User = Depends(require_admin),
    users: UserRepository = Depends(get_users_repo),
) -> list[dict]:
    return [user_public(u) for u in await users.list()]


@admin_router.get("/users/{user_id}")
async def get_user(
    user_id: int,
    _admin: User = Depends(require_admin),
    users: UserRepository = Depends(get_users_repo),
    admin_repo: AdminRepository = Depends(get_admin_repo),
) -> dict:
    detail = await _admin_service(users, admin_repo).user_detail(user_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="User not found")
    return detail


@admin_router.post("/users")
async def create_user(
    payload: AdminUserCreate,
    admin: User = Depends(require_admin),
    users: UserRepository = Depends(get_users_repo),
    admin_repo: AdminRepository = Depends(get_admin_repo),
) -> dict:
    try:
        detail = await _admin_service(users, admin_repo).create_user(
            admin.id,
            email=payload.email,
            password=payload.password,
            is_admin=payload.is_admin,
            is_active=payload.is_active,
        )
    except AdminValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return detail["account"]


@admin_router.patch("/users/{user_id}")
async def update_user(
    user_id: int,
    payload: AdminUserUpdate,
    admin: User = Depends(require_admin),
    users: UserRepository = Depends(get_users_repo),
    admin_repo: AdminRepository = Depends(get_admin_repo),
) -> dict:
    target = await users.get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        detail = await _admin_service(users, admin_repo).update_user(
            admin.id,
            target,
            email=payload.email,
            password=payload.password,
            is_admin=payload.is_admin,
            is_active=payload.is_active,
        )
    except AdminValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return detail["account"]


@admin_router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    admin: User = Depends(require_admin),
    users: UserRepository = Depends(get_users_repo),
    admin_repo: AdminRepository = Depends(get_admin_repo),
) -> dict:
    target = await users.get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        await _admin_service(users, admin_repo).delete_user(admin.id, target)
    except AdminValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "id": user_id}


@admin_router.get("/audit")
async def admin_audit(
    _admin: User = Depends(require_admin),
    admin_repo: AdminRepository = Depends(get_admin_repo),
    limit: int = 50,
) -> list[dict]:
    return await admin_repo.recent_audit(max(1, min(200, limit)))
