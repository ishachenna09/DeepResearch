"""Per-user and per-IP rate limits, backed by Redis.

Why a fixed window instead of SlowAPI or a token bucket?
- SlowAPI is convenient but its default storage is per-process memory, which is
  wrong behind two uvicorn workers; its Redis backend adds the `limits`
  dependency and hides the failure policy. We already have a Redis client and
  only need one primitive: `INCR` a window-scoped key.
- A fixed window can allow a 2x burst at the boundary (10 just before the
  minute, 10 just after). For abuse control that is acceptable; a sliding log
  costs memory per identity, and a token bucket has more state to get wrong.
- The key includes the window index (`now // window`), so expiry is a TTL on an
  immutable key - no read-modify-write race at the reset boundary.

Failure model: Redis is an optimisation here too. If it is unreachable, the
limiter falls back to a per-process counter and logs once. That is weaker with
multiple workers, but it is not "no limit", and it keeps authentication and
health available during a Redis incident.
"""
from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import redis.asyncio as redis
from fastapi import Depends, HTTPException, Request, status

from app.core.config import settings
from app.core.deps import get_current_user
from app.models.user import User

logger = logging.getLogger(__name__)

PREFIX = "dr:rl:"

# Fallback counters: key -> (count, reset_at). Only touched when Redis fails.
_MEMORY: dict[str, tuple[int, float]] = {}


def digest(value: str) -> str:
    """A short, irreversible identity for a Redis key or a log line."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def client_ip(request: Request) -> str:
    """The address to limit.

    Behind Caddy the TCP peer is the proxy, so `X-Forwarded-For` is the only
    useful value - but a client can forge it. Trust it only when the deployment
    says a proxy overwrites it, and take the first hop (the client).
    """
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int
    reset_at: int
    scope: str

    def headers(self) -> dict[str, str]:
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(self.reset_at),
            "Retry-After": str(self.retry_after),
        }


class RateLimiter:
    def __init__(
        self,
        *,
        enabled: bool = True,
        multiplier: float = 1.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.enabled = enabled
        self.multiplier = multiplier
        self._clock = clock
        self._client: redis.Redis | None = None
        self._warned = False

    # ---------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        if not self.enabled:
            return
        self._client = redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
            health_check_interval=30,
        )
        try:
            await self._client.ping()
            logger.info("rate limiter ready (redis)")
        except Exception as exc:  # noqa: BLE001 - degrade to per-process counters
            self._warn(f"rate limiter Redis unreachable ({type(exc).__name__}: {exc})")

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._client = None

    def _warn(self, message: str) -> None:
        if not self._warned:
            logger.warning("%s - using per-process counters", message)
            self._warned = True

    # ------------------------------------------------------------------ surface
    async def hit(
        self, scope: str, identity: str, *, limit: int, window: int
    ) -> RateDecision:
        """Count one hit and decide whether it is allowed."""
        now = self._clock()
        if not self.enabled:
            # Disabled means disabled: tests and the load baseline should not
            # have to reason about a counter that is still ticking.
            return RateDecision(
                allowed=True,
                limit=limit,
                remaining=limit,
                retry_after=0,
                reset_at=int(now) + window,
                scope=scope,
            )

        effective = max(1, int(limit * self.multiplier))
        index = int(now // window)
        key = f"{PREFIX}{scope}:{digest(identity)}:{index}"

        count = 0
        if self._client is not None:
            try:
                count = int(await self._client.incr(key))
                if count == 1:
                    # window * 2 so the key survives a clock skew at the edge
                    # but cannot accumulate forever.
                    await self._client.expire(key, window * 2)
            except Exception as exc:  # noqa: BLE001
                self._warn(f"rate limiter INCR failed ({type(exc).__name__}: {exc})")
                count = self._memory_hit(key, now, window)
        else:
            count = self._memory_hit(key, now, window)

        reset_at = int((index + 1) * window)
        retry_after = max(1, reset_at - int(now) + 1)
        return RateDecision(
            allowed=count <= effective,
            limit=effective,
            remaining=max(0, effective - count),
            retry_after=retry_after,
            reset_at=reset_at,
            scope=scope,
        )

    def _memory_hit(self, key: str, now: float, window: int) -> int:
        if len(_MEMORY) > 10_000:
            for stale, (_, reset) in list(_MEMORY.items()):
                if reset <= now:
                    _MEMORY.pop(stale, None)
        count, reset = _MEMORY.get(key, (0, now + window))
        if reset <= now:
            count, reset = 0, now + window
        count += 1
        _MEMORY[key] = (count, reset)
        return count


_limiter: RateLimiter | None = None


async def init_rate_limiter() -> RateLimiter:
    global _limiter
    _limiter = RateLimiter(
        enabled=settings.RATE_LIMIT_ENABLED,
        multiplier=settings.RATE_LIMIT_MULTIPLIER,
    )
    await _limiter.connect()
    return _limiter


async def close_rate_limiter() -> None:
    global _limiter
    if _limiter is not None:
        await _limiter.close()
    _limiter = None


def get_rate_limiter() -> RateLimiter:
    # Import-time (or an ASGI test without lifespan): do not touch Redis. The
    # production app always initialises one in `lifespan`.
    return _limiter or RateLimiter(enabled=False)


async def enforce_rate_limit(
    scope: str, identity: str, *, limit: int, window: int
) -> RateDecision:
    decision = await get_rate_limiter().hit(scope, identity, limit=limit, window=window)
    if not decision.allowed:
        logger.warning(
            "rate limit: scope=%s identity=%s limit=%s window=%ss",
            scope,
            digest(identity),
            decision.limit,
            window,
        )
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded for {scope}; retry in {decision.retry_after}s",
            headers=decision.headers(),
        )
    return decision


# --------------------------------------------------------------------- deps
# A dependency is the right seam: limits live next to the route they protect,
# FastAPI caches `get_current_user` so the user lookup is not repeated, and a
# route that forgets a dependency is visible in review next to the handler.
def user_rate_limit(scope: str, *, limit: int, window: int):
    async def dependency(request: Request, user: User = Depends(get_current_user)) -> None:
        await enforce_rate_limit(scope, str(user.id), limit=limit, window=window)

    return Depends(dependency)


def ip_rate_limit(scope: str, *, limit: int, window: int):
    async def dependency(request: Request) -> None:
        await enforce_rate_limit(scope, client_ip(request), limit=limit, window=window)

    return Depends(dependency)
