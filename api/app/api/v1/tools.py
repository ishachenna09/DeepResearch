"""MCP tool diagnostics + direct tool access (Phase 2).

These endpoints are the "can the API use its own MCP servers?" proof. Phase 3
stops calling them from the UI and calls `get_toolbox().call(...)` from graph
nodes instead - the transport code below is shared.
"""
from __future__ import annotations

import logging
import time
from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, status

from app.agents.mcp_client import MCPToolError, get_toolbox
from app.core.deps import CurrentUser, require_role
from app.core.ratelimit import user_rate_limit
from app.models.user import User
from app.schemas.tools import (
    FetchIn,
    FetchOut,
    SearchIn,
    SearchOut,
    ToolInfo,
    ToolsOut,
)

router = APIRouter(prefix="/tools", tags=["tools"])

logger = logging.getLogger(__name__)

AdminUser = Annotated[User, Depends(require_role("admin"))]

# A tool failure caused by the *input* (SSRF guard, bad content type) is 400,
# not 502 - the upstream did nothing wrong.
_CLIENT_ERROR_MARKERS = (
    "not allowed",
    "non-public",
    "unsafe",
    "credentials in URL",
    "unsupported content-type",
    "cannot resolve",
    "invalid port",
    "invalid host",
    "missing host",
    "control characters",
    "too large",
    "too many redirects",
    "redirect without",
)


def _toolbox_or_503():
    try:
        toolbox = get_toolbox()
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    if not toolbox.is_ready:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "MCP toolbox is not running - check the API log, then "
            "run: python ..\\scripts\\mcp_smoke_test.py",
        )
    return toolbox


def _as_http_error(exc: MCPToolError) -> HTTPException:
    message = str(exc).removeprefix("Error executing tool ").strip()
    if any(marker in message for marker in _CLIENT_ERROR_MARKERS):
        # The input was the problem (SSRF guard, bad URL, bad content type).
        # Echo it: the caller can fix the URL.
        return HTTPException(status.HTTP_400_BAD_REQUEST, message[:300])
    # An upstream failure can carry a traceback, a local path or a provider
    # payload. Log it; do not hand it to the caller.
    logger.warning("MCP tool failure: %s", message[:500])
    return HTTPException(
        status.HTTP_502_BAD_GATEWAY,
        "Upstream tool failed; the request was not retried",
    )


@router.get("", response_model=ToolsOut)
async def list_tools(user: CurrentUser) -> ToolsOut:
    """List the tools the MCP server advertises (names, descriptions, args)."""
    toolbox = _toolbox_or_503()
    return ToolsOut(tools=[ToolInfo(**item) for item in toolbox.describe()])


@router.post(
    "/search",
    response_model=SearchOut,
    dependencies=[user_rate_limit("tools_search", limit=30, window=3600)],
)
async def run_search(payload: SearchIn, user: CurrentUser) -> SearchOut:
    """Call `web_search` over MCP and return ranked hits."""
    toolbox = _toolbox_or_503()
    started = time.perf_counter()
    try:
        payload_out = await toolbox.call(
            "web_search", {"query": payload.query, "max_results": payload.max_results}
        )
    except MCPToolError as exc:
        raise _as_http_error(exc) from exc
    return SearchOut(
        query=payload.query,
        results=payload_out.get("results", []),
        ms=int((time.perf_counter() - started) * 1000),
    )


@router.post(
    "/fetch",
    response_model=FetchOut,
    dependencies=[user_rate_limit("tools_fetch", limit=60, window=3600)],
)
async def run_fetch(payload: FetchIn, user: CurrentUser) -> FetchOut:
    """Call `fetch_page` over MCP: SSRF guard + extraction + injection scan."""
    # Host only: query strings can carry tokens or private search terms.
    logger.info("fetch requested by user=%s host=%s", user.id, urlparse(payload.url).hostname)
    toolbox = _toolbox_or_503()
    started = time.perf_counter()
    try:
        page = await toolbox.call(
            "fetch_page", {"url": payload.url, "max_chars": payload.max_chars}
        )
    except MCPToolError as exc:
        raise _as_http_error(exc) from exc
    return FetchOut(
        **{k: page.get(k) for k in FetchOut.model_fields if k in page},
        ms=int((time.perf_counter() - started) * 1000),
    )


@router.get("/selftest")
async def selftest(admin: AdminUser) -> dict:
    """One call that proves the whole Phase 2 surface works.

    search -> fetch -> SSRF rejection. Used by the Phase 2 demo and by the
    Phase 9 load test as a cheap end-to-end probe.

    Admin-only: it spends Tavily quota and hits two external services, so it is
    a diagnostic, not a feature every account should be able to trigger.
    """
    toolbox = _toolbox_or_503()
    checks: list[dict] = []

    started = time.perf_counter()
    hits = await toolbox.call("web_search", {"query": "Model Context Protocol", "max_results": 2})
    checks.append(
        {
            "check": "web_search",
            "ok": bool(hits.get("results")),
            "detail": f"{len(hits.get('results', []))} hits",
            "ms": int((time.perf_counter() - started) * 1000),
        }
    )

    started = time.perf_counter()
    page = await toolbox.call("fetch_page", {"url": "https://example.com"})
    checks.append(
        {
            "check": "fetch_page",
            "ok": page.get("http_status") == 200 and page.get("chars", 0) > 0,
            "detail": f"{page.get('chars', 0)} chars, injected={page.get('injected')}",
            "ms": int((time.perf_counter() - started) * 1000),
        }
    )

    started = time.perf_counter()
    blocked, reason = False, ""
    try:
        await toolbox.call("fetch_page", {"url": "http://169.254.169.254/latest/meta-data/"})
    except MCPToolError as exc:
        blocked, reason = True, str(exc)[:120]
    checks.append(
        {
            "check": "ssrf_guard",
            "ok": blocked,
            "detail": reason or "NOT BLOCKED - security bug",
            "ms": int((time.perf_counter() - started) * 1000),
        }
    )

    return {
        "ok": all(check["ok"] for check in checks),
        "transport": "mcp/stdio",
        "tools": toolbox.tool_names,
        "checks": checks,
    }
