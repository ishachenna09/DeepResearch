from __future__ import annotations

import asyncio

from fastapi import APIRouter
from sqlalchemy import text

from app.core.config import settings
from app.core.deps import SessionDep

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(session: SessionDep) -> dict:
    """Liveness: DB + Redis reachable; plus the agents when they are remote."""
    import redis.asyncio as aioredis

    status: dict[str, str] = {"api": "ok"}
    try:
        await session.execute(text("SELECT 1"))
        status["postgres"] = "ok"
    except Exception:
        status["postgres"] = "down"

    try:
        client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        await client.ping()
        await client.aclose()
        status["redis"] = "ok"
    except Exception:
        status["redis"] = "down"

    if settings.AGENTS_MODE == "a2a":
        # In a2a mode a report cannot be written without both services, so a
        # dead agent belongs in the liveness answer. In local mode there is no
        # socket to probe and adding one would be a false alarm.
        from app.a2a.client import agent_health

        status.update(await agent_health())

    status["overall"] = (
        "healthy"
        if all(value.startswith("ok") for value in status.values())
        else "degraded"
    )
    return status


@router.get("/health/agents")
async def agents_health() -> dict:
    """Card discovery for the two A2A services.

    A separate endpoint from `/healthz` on purpose: this one is a diagnostic
    (which agent, which card) and costs two network round trips, so it should
    not be on the path a load balancer polls every few seconds.
    """
    if settings.AGENTS_MODE != "a2a":
        # Reporting a remote agent as "down" when the mode is local would be a
        # false alarm: there is no port and no card to find.
        return {
            "mode": "local",
            "writer": "in-process",
            "critic": "in-process",
            "status": "ok",
        }

    from app.a2a.client import agent_health

    cards = await agent_health()
    healthy = all(value.startswith("ok") for value in cards.values())
    return {"mode": "a2a", **cards, "status": "ok" if healthy else "degraded"}


@router.get("/health/langfuse")
async def langfuse_health() -> dict:
    """Is tracing actually connected? Three states, no false green.

    `disabled` means keys are absent by configuration. `error` means keys are
    present but the host rejected them or is unreachable - the difference matters
    at 3 a.m. `auth_check()` is a network round trip, so this endpoint is a
    diagnostic like `/health/agents`, not part of the liveness path.
    """
    from app.core.observability import get_langfuse

    client = get_langfuse()
    if client is None:
        return {"status": "disabled", "detail": "LANGFUSE_PUBLIC_KEY/SECRET_KEY not set"}
    try:
        ok = await asyncio.to_thread(client.auth_check)
    except Exception as exc:  # noqa: BLE001 - report, do not fail the request
        return {"status": "error", "host": settings.LANGFUSE_HOST, "detail": str(exc)[:300]}
    return {
        "status": "ok" if ok else "error",
        "host": settings.LANGFUSE_HOST,
        "detail": "authenticated" if ok else "keys rejected by the host",
    }


@router.get("/health/llm")
async def llm_health() -> dict:
    """Calls the LiteLLM proxy via its OpenAI-compatible /v1 API.

    Why OpenAI SDK and not litellm.acompletion()? The proxy exposes virtual
    model names (support-chat) that only exist server-side; the SDK routes by
    provider prefix (gemini/..., groq/...) and can't resolve them. The proxy
    handles the Gemini -> Groq fallback internally and reports the model that
    actually served the request.
    """
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        base_url=f"{settings.LITELLM_BASE_URL}/v1",
        api_key=settings.LITELLM_MASTER_KEY,
    )
    try:
        resp = await client.chat.completions.create(
            model=settings.LLM_PRIMARY_MODEL,
            messages=[{"role": "user", "content": "Reply with exactly: OK"}],
            # 128, not 10: reasoning models (gpt-oss) spend tokens before answering,
            # and tiny caps can return empty content on the fallback path.
            max_tokens=128,
            timeout=30,
        )
        return {
            "status": "ok",
            "served_by": resp.model,  # upstream model that actually answered
            "reply": resp.choices[0].message.content,
        }
    except Exception as exc:  # all fallbacks exhausted
        return {"status": "error", "detail": str(exc)[:300]}
    finally:
        await client.close()
