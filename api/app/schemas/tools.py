"""Contracts for the MCP tool diagnostics endpoints (Phase 2)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class ToolInfo(BaseModel):
    name: str
    description: str
    args: list[str] = Field(default_factory=list)


class ToolsOut(BaseModel):
    transport: str = "mcp/stdio"
    tools: list[ToolInfo]


class SearchIn(BaseModel):
    query: str = Field(min_length=2, max_length=300)
    max_results: int = Field(default=5, ge=1, le=10)


class SearchOut(BaseModel):
    query: str
    transport: str = "mcp/stdio"
    ms: int
    results: list[dict]


class FetchIn(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    max_chars: int = Field(default=12_000, ge=1_000, le=40_000)


class FetchOut(BaseModel):
    url: str
    final_url: str | None = None
    title: str = ""
    http_status: int | None = None
    chars: int = 0
    truncated: bool = False
    injected: bool = False
    signals: list[dict] = Field(default_factory=list)
    transport: str = "mcp/stdio"
    ms: int = 0
