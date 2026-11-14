"""Smoke-test the MCP tool server exactly the way the API will consume it.

Run from the api/ directory (so `app` is importable):

    ..\\.venv\\Scripts\\python.exe ..\\scripts\\mcp_smoke_test.py

Checks, in order:
  1. the server starts over stdio and advertises its tools
  2. web_search returns real hits (Tavily key required)
  3. fetch_page extracts text and reports injection flags
  4. SSRF guard refuses localhost and the cloud-metadata address
"""
from __future__ import annotations

import asyncio
import json
import sys

from langchain_mcp_adapters.client import MultiServerMCPClient

SERVER = {
    "command": sys.executable,
    "args": ["-m", "app.mcp_tools.servers.web_tools"],
    "transport": "stdio",
}


def unwrap(result) -> dict:
    """MCP returns [{'type': 'text', 'text': '<json>'}]; decode the JSON payload.

    Note: MCP signals tool failures with isError=True instead of raising, so a
    rejected call comes back as a text block starting with "Error executing".
    """
    if isinstance(result, tuple):  # (content, artifact)
        result = result[0]
    if isinstance(result, list) and result:
        block = result[0]
        if isinstance(block, dict) and "text" in block:
            raw = block["text"]
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"raw": raw}
    if isinstance(result, dict):
        return result
    return {"raw": str(result)}


async def main() -> None:
    client = MultiServerMCPClient({"web": SERVER})

    tools = await client.get_tools()
    names = sorted(t.name for t in tools)
    print("1. tools advertised:", names)
    assert {"web_search", "fetch_page"} <= set(names), "missing tools"

    search = next(t for t in tools if t.name == "web_search")
    payload = unwrap(await search.ainvoke(
        {"query": "Upstash Redis RediSearch FT commands support", "max_results": 3}
    ))
    results = payload.get("results", []) if isinstance(payload, dict) else []
    print(f"2. web_search: {len(results)} hits")
    if results:
        print("   first:", json.dumps(
            {k: str(v)[:100] for k, v in results[0].items()}, indent=2)[:450])

    fetch = next(t for t in tools if t.name == "fetch_page")
    page = unwrap(await fetch.ainvoke({"url": "https://example.com"}))
    print("3. fetch_page:", {k: page.get(k) for k in
                             ("url", "title", "http_status", "chars", "injected")})

    print("4. SSRF guard:")
    blockers = ("non-public", "not allowed", "Error executing", "unsafe")
    for bad in ("http://127.0.0.1:8000/docs", "http://169.254.169.254/latest/meta-data/"):
        try:
            payload = unwrap(await fetch.ainvoke({"url": bad}))
        except Exception as exc:  # some adapter versions do raise
            print(f"   blocked {bad} -> {type(exc).__name__}: {str(exc)[:100]}")
            continue
        text = json.dumps(payload) if not isinstance(payload, str) else payload
        if any(b.lower() in text.lower() for b in blockers):
            print(f"   blocked {bad} -> {text[:130]}")
        else:
            print(f"   SECURITY BUG - fetched {bad}: {text[:130]}")

    print("\nMCP smoke test done")


if __name__ == "__main__":
    asyncio.run(main())
