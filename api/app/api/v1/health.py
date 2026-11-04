from fastapi import APIRouter
from sqlalchemy import text

from app.core.config import settings
from app.core.deps import SessionDep

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(session: SessionDep) -> dict:
    """Liveness: DB + Redis reachable."""
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

    status["overall"] = (
        "healthy" if all(v == "ok" for v in status.values()) else "degraded"
    )
    return status


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
