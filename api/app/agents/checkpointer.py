import contextlib

from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from redisvl.exceptions import RedisSearchError

from app.core.config import settings

_stack = contextlib.AsyncExitStack()
_saver: AsyncRedisSaver | None = None


async def init_checkpointer() -> AsyncRedisSaver:
    """Open ONE long-lived Redis saver for the app's lifetime."""
    global _saver
    try:
        _saver = await _stack.enter_async_context(
            AsyncRedisSaver.from_conn_string(settings.REDIS_URL)
        )
    except RedisSearchError as exc:
        # RedisSaver builds RediSearch (FT.*) indices on startup. Upstash is a
        # restricted serverless Redis: it rejects FT.* and only speaks its own
        # SEARCH.* API, so the saver can never work there. Fail with a fix, not
        # a stack trace.
        raise RuntimeError(
            "Redis checkpointing requires RedisJSON + RediSearch modules.\n"
            "  Upstash does not support FT.* commands -> use a module-capable Redis:\n"
            "  Redis Cloud free tier, Redis 8+, or redis-stack-server (Docker).\n"
            f"  Current REDIS_URL host: {settings.REDIS_URL.split('@')[-1]}\n"
            f"  Upstream error: {exc}"
        ) from exc
    return _saver


async def close_checkpointer() -> None:
    await _stack.aclose()


def get_checkpointer() -> AsyncRedisSaver:
    if _saver is None:
        raise RuntimeError("Checkpointer not initialised — call init_checkpointer() in lifespan")
    return _saver
