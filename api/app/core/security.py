"""Password hashing (argon2) + JWT access tokens + opaque refresh tokens."""
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

from app.core.config import settings

_hasher = PasswordHasher()


# ── Passwords ─────────────────────────────────────────────
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(hashed: str, password: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except (VerifyMismatchError, VerificationError):
        return False


# ── Access token (JWT, short-lived, stateless) ───────────
def create_access_token(user_id: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "role": role,
        "scope": "access",
        "jti": secrets.token_urlsafe(16),
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MIN),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Raises jwt.PyJWTError on any invalid/expired token."""
    return jwt.decode(
        token,
        settings.SECRET_KEY,
        algorithms=[settings.JWT_ALGORITHM],
        options={"require": ["exp", "sub", "scope"]},
    )


# ── Refresh token (opaque, stored hashed in DB, rotating) ─
def generate_refresh_token() -> str:
    return "rt_" + secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def refresh_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)


def new_id() -> uuid.UUID:
    return uuid.uuid4()
