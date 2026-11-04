from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.deps import CurrentUser, SessionDep
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

REFRESH_COOKIE = "supportiq_refresh"


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


@router.post("/register", status_code=status.HTTP_201_CREATED, response_model=UserOut)
async def register(payload: RegisterIn, session: SessionDep) -> User:
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


@router.post("/login", response_model=TokenOut)
async def login(
    payload: LoginIn, request: Request, response: Response, session: SessionDep
) -> TokenOut:
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


@router.post("/refresh", response_model=TokenOut)
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
    if stored is None or stored.revoked_at is not None or stored.expires_at < now:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")

    # Rotation: revoke old, issue new (reuse detection comes in Phase 2 hardening)
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
