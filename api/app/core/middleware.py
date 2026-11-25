"""ASGI middleware for abuse controls and response hardening.

Why pure ASGI rather than Starlette's `BaseHTTPMiddleware`?
`BaseHTTPMiddleware` builds a second receive channel. That breaks
`request.is_disconnected()` - exactly what the report SSE endpoint relies on to
stop replaying a stream when the browser closes the tab. These middlewares only
need to inspect the scope and wrap `send`, so they can do that without touching
`receive`.
"""
from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings
from app.core.ratelimit import client_ip, get_rate_limiter

# Health must stay cheap and reachable even when an abusive client is over the
# global budget; docs are static and not part of the API surface to protect.
_RATE_LIMIT_EXEMPT_PREFIX = "/api/v1/health"
_RATE_LIMIT_EXEMPT_EXACT = {"/docs", "/redoc", "/openapi.json", "/"}

_CSP_EXEMPT = ("/docs", "/redoc", "/openapi.json")


class GlobalRateLimitMiddleware:
    """A backstop per client IP across `/api/v1`.

    Per-route limits know the user and the cost of the operation; this one only
    knows the address. It exists so a caller cannot avoid every bucket by
    hammering many cheap endpoints, and it runs before authentication so an
    unauthenticated flood never reaches the database.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not settings.RATE_LIMIT_ENABLED:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if (
            not path.startswith("/api/v1")
            or path.startswith(_RATE_LIMIT_EXEMPT_PREFIX)
            or path in _RATE_LIMIT_EXEMPT_EXACT
        ):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        decision = await get_rate_limiter().hit(
            "global_ip", client_ip(request), limit=300, window=60
        )
        if not decision.allowed:
            response = JSONResponse(
                status_code=429,
                content={"detail": "Global rate limit exceeded; retry shortly"},
                headers=decision.headers(),
            )
            await response(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in decision.headers().items():
                    if key.startswith("X-RateLimit"):
                        headers[key] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


class SecurityHeadersMiddleware:
    """Headers that cost nothing and remove whole classes of browser bugs.

    The CSP is for API responses (`default-src 'none'`), not for the React app:
    the frontend is served by Vite locally and Vercel in Phase 10, and each of
    those sets its own policy. `/docs` needs its CDN assets, so it is exempt.
    HSTS is only sent in production over HTTPS - sending it from a laptop would
    poison `localhost` for every other project on the machine.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        request = Request(scope)
        https = request.url.scheme == "https" or (
            request.headers.get("x-forwarded-proto", "").lower() == "https"
        )

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault(
                    "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
                )
                if not path.startswith(_CSP_EXEMPT):
                    headers.setdefault(
                        "Content-Security-Policy",
                        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
                    )
                if settings.APP_ENV == "prod" and https:
                    headers.setdefault(
                        "Strict-Transport-Security",
                        "max-age=31536000; includeSubDomains",
                    )
            await send(message)

        await self.app(scope, receive, send_with_headers)
