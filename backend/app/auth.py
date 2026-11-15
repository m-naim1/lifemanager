"""Local email/password auth: PBKDF2 hashing + HS256 JWT.

PBKDF2-SHA256 keeps password hashing dependency-light and portable.
Hash format: pbkdf2$<iterations>$<salt_hex>$<hash_hex>
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from fastapi import HTTPException, status

from .config import settings
from .models import User

_ITERATIONS = 200_000

# Name of the HttpOnly cookie used by the server-rendered UI. The JSON API
# continues to accept `Authorization: Bearer <token>` as well.
TOKEN_COOKIE = "lm_token"


def normalize_email(email: str) -> str:
    return email.strip().lower()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, it_s, salt_hex, hash_hex = stored.split("$")
        if scheme != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(it_s)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_json(payload: dict) -> str:
    return _b64url(json.dumps(payload, separators=(",", ":")).encode())


def create_token(user: User) -> str:
    now = int(time.time())
    header = _b64url_json({"alg": "HS256", "typ": "JWT"})
    body = _b64url_json(
        {
            "sub": str(user.id),
            "email": user.email,
            "admin": bool(user.is_admin),
            "iat": now,
            "exp": now + settings.jwt_expire_minutes * 60,
        }
    )
    sig = hmac.new(settings.jwt_secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{_b64url(sig)}"


def decode_token(token: str) -> dict:
    try:
        header_b, body_b, sig_b = token.split(".")
        expected = hmac.new(
            settings.jwt_secret.encode(), f"{header_b}.{body_b}".encode(), hashlib.sha256
        ).digest()
        actual = base64.urlsafe_b64decode(sig_b + "=" * (-len(sig_b) % 4))
        if not hmac.compare_digest(expected, actual):
            raise ValueError("bad signature")
        payload = json.loads(base64.urlsafe_b64decode(body_b + "=" * (-len(body_b) % 4)))
        if payload.get("exp", 0) < int(time.time()):
            raise ValueError("expired")
        return payload
    except Exception as exc:
        raise ValueError("invalid token") from exc


def user_public(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "is_admin": bool(user.is_admin),
        "is_active": bool(user.is_active),
        "created_at": user.created_at,
    }
