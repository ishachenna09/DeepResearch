"""Deterministic tests for the fixed-window rate limiter (Phase 9).

What is worth testing is not Redis (a fake stands in) but the decisions:
- exactly `limit` requests pass, the next one is 429,
- identities are isolated,
- the window actually resets,
- a disabled limiter never blocks,
- a dead Redis degrades to memory instead of failing open *or* closed,
- the HTTP surface carries `Retry-After`.

No socket, no database, no clock waiting: the limiter takes a clock function.

Run from api/:
    ..\\.venv\\Scripts\\python.exe -m pytest tests -q
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.core import ratelimit
from app.core.config import settings
from app.core.ratelimit import RateLimiter, digest, enforce_rate_limit


class Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def clear_memory():
    ratelimit._MEMORY.clear()
    yield
    ratelimit._MEMORY.clear()


def run(coro):
    return asyncio.run(coro)


# ── counting ───────────────────────────────────────────────────────────────


def test_allows_exactly_limit_then_blocks():
    async def scenario():
        clock = Clock()
        limiter = RateLimiter(enabled=True, clock=clock)
        allowed = [
            await limiter.hit("login", "user-a", limit=3, window=60)
            for _ in range(3)
        ]
        blocked = await limiter.hit("login", "user-a", limit=3, window=60)
        return allowed, blocked

    allowed, blocked = run(scenario())
    assert [d.allowed for d in allowed] == [True, True, True]
    assert [d.remaining for d in allowed] == [2, 1, 0]
    assert blocked.allowed is False
    assert blocked.remaining == 0
    # Reset is the end of the current minute; the +1 keeps Retry-After honest.
    assert 1 <= blocked.retry_after <= 61


def test_identities_are_isolated():
    async def scenario():
        clock = Clock()
        limiter = RateLimiter(enabled=True, clock=clock)
        for _ in range(2):
            await limiter.hit("export", "user-a", limit=2, window=60)
        return (
            await limiter.hit("export", "user-a", limit=2, window=60),
            await limiter.hit("export", "user-b", limit=2, window=60),
        )

    blocked, other = run(scenario())
    assert blocked.allowed is False
    assert other.allowed is True


def test_scopes_are_isolated_for_the_same_identity():
    async def scenario():
        clock = Clock()
        limiter = RateLimiter(enabled=True, clock=clock)
        await limiter.hit("search", "user-a", limit=1, window=60)
        return (
            await limiter.hit("search", "user-a", limit=1, window=60),
            await limiter.hit("fetch", "user-a", limit=1, window=60),
        )

    search_again, fetch = run(scenario())
    assert search_again.allowed is False
    assert fetch.allowed is True


def test_window_resets():
    async def scenario():
        clock = Clock()
        limiter = RateLimiter(enabled=True, clock=clock)
        await limiter.hit("create", "user-a", limit=1, window=60)
        blocked = await limiter.hit("create", "user-a", limit=1, window=60)
        clock.advance(61)
        reset = await limiter.hit("create", "user-a", limit=1, window=60)
        return blocked, reset

    blocked, reset = run(scenario())
    assert blocked.allowed is False
    assert reset.allowed is True


def test_multiplier_scales_the_budget():
    async def scenario():
        clock = Clock()
        limiter = RateLimiter(enabled=True, multiplier=10.0, clock=clock)
        results = [
            await limiter.hit("list", "user-a", limit=2, window=60)
            for _ in range(20)
        ]
        return results

    results = run(scenario())
    assert all(d.allowed for d in results)
    assert results[-1].remaining == 0


def test_disabled_limiter_never_blocks():
    async def scenario():
        limiter = RateLimiter(enabled=False)
        return [
            await limiter.hit("anything", "user-a", limit=1, window=60)
            for _ in range(5)
        ]

    results = run(scenario())
    assert all(d.allowed for d in results)


# ── Redis path and degraded mode ───────────────────────────────────────────


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.expires: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def expire(self, key: str, ttl: int) -> None:
        self.expires[key] = ttl


class BrokenRedis:
    async def incr(self, key: str) -> int:
        raise ConnectionError("redis is down")

    async def expire(self, key: str, ttl: int) -> None:  # pragma: no cover
        raise AssertionError("expire must not be reached when incr fails")


def test_redis_path_counts_and_sets_ttl_once():
    async def scenario():
        clock = Clock()
        limiter = RateLimiter(enabled=True, clock=clock)
        limiter._client = FakeRedis()
        first = await limiter.hit("login", "user-a", limit=2, window=60)
        second = await limiter.hit("login", "user-a", limit=2, window=60)
        return first, second, limiter._client

    first, second, client = run(scenario())
    assert (first.allowed, second.allowed) == (True, True)
    assert len(client.values) == 1
    # One EXPIRE on the first hit; re-setting it on every hit would extend the
    # window forever under steady traffic.
    assert len(client.expires) == 1
    assert list(client.expires.values()) == [120]


def test_redis_failure_falls_back_to_memory():
    async def scenario():
        clock = Clock()
        limiter = RateLimiter(enabled=True, clock=clock)
        limiter._client = BrokenRedis()
        return [
            await limiter.hit("login", "user-a", limit=1, window=60)
            for _ in range(2)
        ], limiter

    (first, second), limiter = run(scenario())
    assert first.allowed is True
    assert second.allowed is False  # degraded is weaker, not unlimited
    assert limiter._warned is True


# ── HTTP surface ───────────────────────────────────────────────────────────


def test_enforce_rate_limit_raises_429_with_retry_after(monkeypatch):
    clock = Clock()
    limiter = RateLimiter(enabled=True, clock=clock)
    monkeypatch.setattr(ratelimit, "_limiter", limiter)

    async def scenario():
        await enforce_rate_limit("burst", "user-a", limit=1, window=60)
        with pytest.raises(HTTPException) as caught:
            await enforce_rate_limit("burst", "user-a", limit=1, window=60)
        return caught.value

    exc = run(scenario())
    assert exc.status_code == 429
    assert exc.headers["Retry-After"].isdigit()
    assert exc.headers["X-RateLimit-Limit"] == "1"
    assert exc.headers["X-RateLimit-Remaining"] == "0"


def _request(headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "headers": raw,
            "client": ("203.0.113.9", 5000),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def test_client_ip_ignores_forwarded_header_by_default(monkeypatch):
    monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", False)
    assert ratelimit.client_ip(_request({"x-forwarded-for": "1.2.3.4"})) == "203.0.113.9"


def test_client_ip_uses_first_hop_when_proxy_is_trusted(monkeypatch):
    monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", True)
    request = _request({"x-forwarded-for": "1.2.3.4, 10.0.0.1"})
    assert ratelimit.client_ip(request) == "1.2.3.4"


def test_digest_is_stable_and_does_not_leak_the_identity():
    assert digest("user-a") == digest("user-a")
    assert digest("user-a") != digest("user-b")
    assert "user-a" not in digest("user-a")
    assert len(digest("user-a")) == 16
