"""Security regression tests (Phase 9).

What this file pins:
- JWT handling: algorithm pinning, expiry, required claims.
- Response hardening: security headers, HSTS only in production, Host checking.
- CORS: only the configured frontend is echoed.
- The auth matrix: every non-public route answers 401 without a token. This is
  the test that fails when someone adds a route and forgets `CurrentUser`.
- Error hygiene: an upstream failure is a generic 502, not a traceback.
- Ownership helpers: a missing/foreign row is a 404, never a 403 that confirms
  the id exists.

No database and no provider: the ASGI app is called with httpx's ASGITransport,
and the DB-backed ownership check is exercised through a fake session. The live
IDOR/refresh-reuse matrix lives in `scripts/verify_phase9.ps1`.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import jwt
import pytest
from fastapi import HTTPException

from app.api.v1 import library, reports
from app.api.v1.tools import _as_http_error
from app.agents.mcp_client import MCPToolError
from app.core.config import settings
from app.core.deps import require_role
from app.core.security import create_access_token, decode_access_token
from app.main import app

PUBLIC_PATHS = {
    "/",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/api/v1/auth/register",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/api/v1/auth/logout",       # idempotent cookie cleanup, intentionally open
    "/api/v1/healthz",
    "/api/v1/health/agents",     # cheap diagnostics; rate-limited by IP
    "/api/v1/health/langfuse",
}


def run(coro):
    return asyncio.run(coro)


def _client(**kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=kwargs.pop("base_url", "http://testserver"),
        **kwargs,
    )


# ── JWT ────────────────────────────────────────────────────────────────────


def test_access_token_roundtrips_required_claims():
    token = create_access_token("11111111-1111-1111-1111-111111111111", "agent")
    payload = decode_access_token(token)
    assert payload["scope"] == "access"
    assert payload["role"] == "agent"
    assert payload["sub"].startswith("1111")


def test_expired_token_is_rejected():
    payload = {
        "sub": str(uuid.uuid4()),
        "role": "agent",
        "scope": "access",
        "exp": datetime.now(timezone.utc) - timedelta(seconds=1),
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(token)


def test_alg_none_is_rejected():
    payload = {
        "sub": str(uuid.uuid4()),
        "role": "admin",
        "scope": "access",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    token = jwt.encode(payload, key="", algorithm="none")
    with pytest.raises(jwt.InvalidAlgorithmError):
        decode_access_token(token)


def test_a_token_signed_with_another_algorithm_is_rejected():
    payload = {
        "sub": str(uuid.uuid4()),
        "role": "admin",
        "scope": "access",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    other = "HS512" if settings.JWT_ALGORITHM != "HS512" else "HS256"
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=other)
    with pytest.raises(jwt.InvalidAlgorithmError):
        decode_access_token(token)


def test_missing_scope_claim_is_rejected():
    payload = {
        "sub": str(uuid.uuid4()),
        "role": "agent",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    with pytest.raises(jwt.MissingRequiredClaimError):
        decode_access_token(token)


# ── headers, host, CORS ────────────────────────────────────────────────────


def test_security_headers_on_every_response():
    async def scenario():
        async with _client() as client:
            return await client.get("/")

    response = run(scenario())
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "geolocation=()" in response.headers["permissions-policy"]
    assert "default-src 'none'" in response.headers["content-security-policy"]


def test_hsts_is_not_sent_from_a_laptop(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "local")

    async def scenario():
        async with _client() as client:
            return await client.get("/")

    assert "strict-transport-security" not in run(scenario()).headers


def test_hsts_is_sent_in_production_over_https(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "prod")

    async def scenario():
        async with _client() as client:
            return await client.get("/", headers={"x-forwarded-proto": "https"})

    response = run(scenario())
    assert response.headers["strict-transport-security"].startswith("max-age=")


def test_forged_host_header_is_rejected():
    async def scenario():
        async with _client(base_url="http://evil.example") as client:
            return await client.get("/api/v1/healthz")

    assert run(scenario()).status_code == 400


def test_cors_echoes_only_the_configured_frontend():
    async def scenario():
        async with _client() as client:
            allowed = await client.options(
                "/api/v1/research",
                headers={
                    "Origin": settings.FRONTEND_URL,
                    "Access-Control-Request-Method": "GET",
                },
            )
            denied = await client.options(
                "/api/v1/research",
                headers={
                    "Origin": "https://evil.example",
                    "Access-Control-Request-Method": "GET",
                },
            )
            return allowed, denied

    allowed, denied = run(scenario())
    assert allowed.headers.get("access-control-allow-origin") == settings.FRONTEND_URL
    assert "access-control-allow-origin" not in denied.headers


# ── the auth matrix ────────────────────────────────────────────────────────


def test_every_non_public_route_requires_a_token():
    """A new route without auth fails here instead of in production."""
    operations: list[tuple[str, str]] = []
    for path, item in app.openapi()["paths"].items():
        if path in PUBLIC_PATHS:
            continue
        for method in ("get", "post", "put", "patch", "delete"):
            if method in item:
                operations.append((method.upper(), path))

    assert len(operations) >= 15, "the matrix is suspiciously small"

    async def scenario():
        statuses: dict[str, int] = {}
        async with _client() as client:
            for method, path in operations:
                url = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)
                response = await client.request(
                    method, url, json={} if method in ("POST", "PUT", "PATCH") else None
                )
                statuses[f"{method} {path}"] = response.status_code
        return statuses

    statuses = run(scenario())
    unexpected = {key: code for key, code in statuses.items() if code != 401}
    assert not unexpected, f"routes that did not answer 401 without a token: {unexpected}"


# ── RBAC + error hygiene ───────────────────────────────────────────────────


def test_require_role_rejects_agent_and_allows_admin():
    async def scenario():
        checker = require_role("admin")
        with pytest.raises(HTTPException) as caught:
            await checker(SimpleNamespace(role="agent"))
        admin = await checker(SimpleNamespace(role="admin"))
        return caught.value, admin

    exc, admin = run(scenario())
    assert exc.status_code == 403
    assert admin.role == "admin"


def test_upstream_tool_failure_is_generic_502():
    exc = _as_http_error(
        MCPToolError(
            "Error executing tool fetch_page: Traceback (most recent call last): "
            r"C:\projects\secret\config.py token=abc"
        )
    )
    assert exc.status_code == 502
    assert "Traceback" not in exc.detail
    assert "C:\\" not in exc.detail
    assert "token" not in exc.detail


def test_client_side_tool_failure_keeps_the_actionable_message():
    exc = _as_http_error(
        MCPToolError("Error executing tool fetch_page: non-public address: 10.0.0.1")
    )
    assert exc.status_code == 400
    assert "non-public" in exc.detail


# ── ownership helpers ──────────────────────────────────────────────────────


class _NoRows:
    async def scalar(self, *_args, **_kwargs):
        return None

    async def execute(self, *_args, **_kwargs):
        return self

    def first(self):
        return None


def test_missing_job_is_404_not_403():
    async def scenario():
        with pytest.raises(HTTPException) as caught:
            await reports._owned_job(uuid.uuid4(), SimpleNamespace(id=uuid.uuid4()), _NoRows())
        return caught.value

    exc = run(scenario())
    assert exc.status_code == 404
    assert exc.detail == "Job not found"


def test_foreign_report_export_is_404():
    async def scenario():
        with pytest.raises(HTTPException) as caught:
            await library._owned_report(
                uuid.uuid4(), SimpleNamespace(id=uuid.uuid4()), _NoRows()
            )
        return caught.value

    exc = run(scenario())
    assert exc.status_code == 404
    assert exc.detail == "Report not found"
