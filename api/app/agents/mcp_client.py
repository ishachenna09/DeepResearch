"""MCP client: one long-lived stdio session per process.

Why a persistent session instead of `client.get_tools()` per request?
- `MultiServerMCPClient.get_tools()` opens a NEW ClientSession (i.e. spawns a
  NEW python process) for every tool invocation. One process per search is
  ~1-2 s of overhead and, in Phase 3's fan-out, a process storm.
- Here we start the server once, at app startup, and keep the session in an
  AsyncExitStack for the app's lifetime. Phase 6 swaps `stdio` for
  `streamable_http` by changing SERVER_SPEC only.

Failure model: if the child process dies, calls raise MCPToolError and
`restart()` re-spawns it; the API itself stays up.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from app.core.config import settings

logger = logging.getLogger(__name__)

# api/ directory - the child process needs it on sys.path to import `app.*`
API_DIR = str(Path(__file__).resolve().parents[2])

SERVER_SPEC: dict[str, dict[str, Any]] = {
    "web": {
        "command": sys.executable,
        "args": ["-m", settings.MCP_WEB_TOOL_MODULE],
        "cwd": API_DIR,
        "transport": "stdio",
        "env": {"PYTHONUNBUFFERED": "1"},
    }
}


class MCPToolError(RuntimeError):
    """A tool call reached the server but failed (bad URL, upstream error)."""


def _maybe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def unwrap_mcp_result(result: Any) -> Any:
    """Normalise an MCP tool result into plain Python.

    LangChain adapters return [{'type': 'text', 'text': '<json>'}] (sometimes a
    (content, artifact) tuple). Tool *errors* arrive as content too - the block
    text starts with "Error executing tool ..." - never as an exception.
    """
    if isinstance(result, tuple) and result:
        result = result[0]
    if isinstance(result, list) and result:
        block = result[0]
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            return _maybe_json(block["text"])
        return block
    if isinstance(result, str):
        return _maybe_json(result)
    return result


class MCPToolbox:
    """Holds the live MCP session and the LangChain tools bound to it."""

    def __init__(self, max_concurrency: int = 8) -> None:
        self._stack: AsyncExitStack | None = None
        self._client: MultiServerMCPClient | None = None
        self._tools: dict[str, BaseTool] = {}
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(max_concurrency)  # one stdio pipe must not be flooded

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        async with self._lock:
            if self._tools:
                return
            stack = AsyncExitStack()
            client = MultiServerMCPClient(SERVER_SPEC)
            session = await stack.enter_async_context(client.session("web"))
            tools = await load_mcp_tools(session)
            self._stack, self._client = stack, client
            self._tools = {tool.name: tool for tool in tools}

    async def stop(self) -> None:
        async with self._lock:
            if self._stack is not None:
                await self._stack.aclose()
            self._stack = None
            self._client = None
            self._tools = {}

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    # ------------------------------------------------------------------ surface
    @property
    def is_ready(self) -> bool:
        return bool(self._tools)

    @property
    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    def get_tool(self, name: str) -> BaseTool:
        tool = self._tools.get(name)
        if tool is None:
            raise MCPToolError(
                f"unknown tool {name!r}; available: {self.tool_names or 'none (not started)'}"
            )
        return tool

    def describe(self) -> list[dict]:
        return [
            {
                "name": tool.name,
                "description": (tool.description or "").strip().split("\n")[0][:200],
                "args": sorted((tool.args or {}).keys()),
            }
            for tool in self._tools.values()
        ]

    async def call(self, name: str, args: dict, timeout: float = 45.0) -> Any:
        """Invoke a tool, unwrap the transport, and turn content-errors into raises."""
        tool = self.get_tool(name)
        async with self._sem:
            try:
                async with asyncio.timeout(timeout):
                    raw = await tool.ainvoke(args)
            except TimeoutError as exc:
                raise MCPToolError(f"{name} timed out after {timeout}s") from exc
            except Exception as exc:  # transport died (child process crashed)
                raise MCPToolError(f"{name} transport error: {type(exc).__name__}: {exc}") from exc

        payload = unwrap_mcp_result(raw)
        if isinstance(payload, str) and payload.startswith("Error executing tool"):
            raise MCPToolError(payload)
        if isinstance(payload, dict) and isinstance(payload.get("raw"), str):
            if payload["raw"].startswith("Error executing tool"):
                raise MCPToolError(payload["raw"])
        return payload


_toolbox: MCPToolbox | None = None


async def init_toolbox() -> MCPToolbox:
    """Start the MCP child process. Soft-fail: a broken tool server must not
    take auth and the research graph down with it - /tools/* returns 503."""
    global _toolbox
    _toolbox = MCPToolbox()
    try:
        await _toolbox.start()
        logger.info("MCP toolbox ready: %s", _toolbox.tool_names)
    except Exception as exc:  # noqa: BLE001 - degrade, do not crash the API
        logger.warning("MCP toolbox unavailable (%s: %s)", type(exc).__name__, exc)
    return _toolbox


async def close_toolbox() -> None:
    global _toolbox
    if _toolbox is not None:
        await _toolbox.stop()
        _toolbox = None


def get_toolbox() -> MCPToolbox:
    if _toolbox is None:
        raise RuntimeError("MCP toolbox not initialised - call init_toolbox() in lifespan")
    return _toolbox
