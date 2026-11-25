from datetime import datetime, timezone
import logging

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import delete, select, update

from app.core.config import settings
from app.core.deps import CurrentUser, SessionDep
from app.core.ratelimit import digest, enforce_rate_limit, ip_rate_limit
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_token,
    refresh_expiry,
    verify_password,
)
from app.models.user import RefreshToken, User
from app.schemas.auth import LoginIn, RegisterIn, TokenOut, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])

logger = logging.getLogger(__name__)

REFRESH_COOKIE = "DeepSearch_refresh"


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=token,
        httponly=True,
        secure=settings.APP_ENV == "prod",
        samesite="lax",
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        path="/",
    )


async def _issue_refresh_token(session: SessionDep, user: User, request: Request) -> str:
    token = generate_refresh_token()
    session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=refresh_expiry(),
            user_agent=request.headers.get("user-agent", "")[:300],
        )
    )
    await session.commit()
    return token


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=UserOut,
    # Open registration is a quota tap: every account can spend LLM tokens.
    dependencies=[ip_rate_limit("register_ip", limit=5, window=3600)],
)
async def register(payload: RegisterIn, request: Request, session: SessionDep) -> User:
    if settings.REGISTRATION_MODE == "invite":
        # A header, not a body field: the frontend can send it once from a
        # deploy-time config, and an existing client does not break on 422.
        supplied = request.headers.get("x-invite-code", "")
        if not settings.REGISTRATION_INVITE_CODE or supplied != settings.REGISTRATION_INVITE_CODE:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Registration is invite-only")

    email = payload.email.lower()
    existing = await session.scalar(select(User).where(User.email == email))
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

    user = User(
        email=email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        role="agent",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@router.post(
    "/login",
    response_model=TokenOut,
    dependencies=[ip_rate_limit("login_ip", limit=10, window=60)],
)
async def login(
    payload: LoginIn, request: Request, response: Response, session: SessionDep
) -> TokenOut:
    # Per-email limit alongside the per-IP one: credential stuffing rotates
    # addresses, but it must still guess one account's password many times.
    await enforce_rate_limit(
        "login_email", digest(payload.email.lower()), limit=20, window=3600
    )
    user = await session.scalar(select(User).where(User.email == payload.email.lower()))
    if user is None or not verify_password(user.hashed_password, payload.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account is deactivated")

    refresh_token = await _issue_refresh_token(session, user, request)
    _set_refresh_cookie(response, refresh_token)

    return TokenOut(
        access_token=create_access_token(str(user.id), user.role),
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MIN * 60,
    )


@router.post(
    "/refresh",
    response_model=TokenOut,
    dependencies=[ip_rate_limit("refresh_ip", limit=30, window=60)],
)
async def refresh(
    request: Request, response: Response, session: SessionDep
) -> TokenOut:
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No refresh token")

    stored = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(token))
    )
    now = datetime.now(timezone.utc)
    if stored is None or stored.expires_at < now:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")

    if stored.revoked_at is not None:
        # A rotated token came back. Either an attacker replayed a stolen token
        # or a stolen copy is racing the real client; both mean the family is
        # compromised. Revoke every live token for the user, not just this one.
        revoked = await session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.user_id == stored.user_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        await session.commit()
        logger.warning(
            "refresh token reuse detected: user=%s revoked=%s",
            stored.user_id,
            revoked.rowcount,
        )
        response.delete_cookie(REFRESH_COOKIE, path="/")
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Refresh token reuse detected; sign in again",
        )

    # Rotation: revoke old, issue new
    stored.revoked_at = now
    user = await session.get(User, stored.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User inactive")

    new_token = await _issue_refresh_token(session, user, request)
    _set_refresh_cookie(response, new_token)

    return TokenOut(
        access_token=create_access_token(str(user.id), user.role),
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MIN * 60,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request, response: Response, session: SessionDep
) -> Response:
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        await session.execute(
            delete(RefreshToken).where(RefreshToken.token_hash == hash_token(token))
        )
        await session.commit()
    response.delete_cookie(REFRESH_COOKIE, path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> User:
    return user
