"""Redis Streams: the transport behind the report's SSE endpoint.

Why Streams and not pub/sub?
pub/sub is fire-and-forget. If the browser connects 200 ms late - or reconnects
after a laptop sleep - every token published in between is gone, and the user
sees a report that starts mid-sentence. A stream keeps entries until it expires
and lets a consumer resume from the last id it saw, which is exactly the
semantic of SSE's `Last-Event-ID`. Replay from `0-0` also means a second tab (or
a refresh) can watch the same generation from the beginning.

Why not an in-process asyncio.Queue?
Because it only works with one uvicorn worker and one process. The same code
path has to survive Phase 10's multi-worker container, and Redis is already a
dependency for checkpoints and the page cache.

Cost control: Redis Cloud's free tier is 30 MB shared. Chunks are ~20-60 bytes,
MAXLEN caps a pathological run at ~4000 entries, and the whole key expires after
REPORT_STREAM_TTL (1 h). The durable report is the Postgres row, not the stream.

Failure model: streaming is an optimisation, never a dependency. Every operation
swallows Redis errors (logged once). If Redis is down the report still generates
and the SSE endpoint falls back to emitting the finished markdown - i.e. the
user gets the report a few seconds later instead of not at all.
"""
from __future__ import annotations

import json
import logging
import uuid

import redis.asyncio as redis

from app.core.config import settings

logger = logging.getLogger(__name__)

PREFIX = "dr:report:"

# Longest blocking XREAD window. The SSE endpoint loops on this: a quiet stream
# wakes up every BLOCK_MS to check the database and emit a heartbeat comment.
BLOCK_MS = 8_000


def stream_key(report_id: uuid.UUID) -> str:
    return f"{PREFIX}{report_id}"


class ReportStream:
    def __init__(self) -> None:
        self._client: redis.Redis | None = None
        self._warned = False

    # ---------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        self._client = redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=5,
            # Must be LONGER than the blocking XREAD window. A blocking read is
            # a quiet socket by design, so a 5 s socket timeout against a 10 s
            # BLOCK raises TimeoutError and silently "disables" streaming on
            # every idle gap - which is most of a report's first 20 seconds.
            socket_timeout=BLOCK_MS // 1000 + 20,
            health_check_interval=30,
        )
        try:
            await self._client.ping()
            logger.info("report stream ready (ttl=%ss)", settings.REPORT_STREAM_TTL)
        except Exception as exc:  # noqa: BLE001 - degrade to non-streaming
            self._warn(f"report stream unreachable ({type(exc).__name__}: {exc})")

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._client = None

    @property
    def ready(self) -> bool:
        return self._client is not None

    def _warn(self, message: str) -> None:
        if not self._warned:
            logger.warning("%s - streaming disabled, reports will still generate", message)
            self._warned = True

    # ------------------------------------------------------------------ surface
    async def append(self, report_id: uuid.UUID, kind: str, data: dict | str) -> str | None:
        """Publish one event. Returns the Redis entry id (the SSE `id:`)."""
        if self._client is None:
            return None
        key = stream_key(report_id)
        payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        try:
            entry_id = await self._client.xadd(
                key,
                {"t": kind, "d": payload},
                maxlen=4000,
                approximate=True,
            )
            await self._client.expire(key, settings.REPORT_STREAM_TTL)
            return entry_id
        except Exception as exc:  # noqa: BLE001
            self._warn(f"stream XADD failed ({type(exc).__name__}: {exc})")
            return None

    async def read(
        self,
        report_id: uuid.UUID,
        last_id: str = "0-0",
        *,
        block_ms: int = BLOCK_MS,
        count: int = 200,
    ) -> list[tuple[str, str, str]]:
        """Events after `last_id` as (entry_id, kind, data). Empty on timeout."""
        if self._client is None:
            return []
        try:
            batches = await self._client.xread(
                {stream_key(report_id): last_id}, count=count, block=block_ms
            )
        except Exception as exc:  # noqa: BLE001
            self._warn(f"stream XREAD failed ({type(exc).__name__}: {exc})")
            return []
        if not batches:
            return []
        events: list[tuple[str, str, str]] = []
        for _key, entries in batches:
            for entry_id, fields in entries:
                events.append((entry_id, fields.get("t", "message"), fields.get("d", "")))
        return events

    async def drop(self, report_id: uuid.UUID) -> None:
        """Delete the stream (used when a report is regenerated)."""
        if self._client is None:
            return
        try:
            await self._client.delete(stream_key(report_id))
        except Exception:  # noqa: BLE001 - a stale stream is harmless, TTL clears it
            pass


_stream: ReportStream | None = None


async def init_report_stream() -> ReportStream:
    global _stream
    _stream = ReportStream()
    await _stream.connect()
    return _stream


async def close_report_stream() -> None:
    global _stream
    if _stream is not None:
        await _stream.close()
    _stream = None


def get_report_stream() -> ReportStream:
    if _stream is None:
        raise RuntimeError("Report stream not initialised - call init_report_stream() in lifespan")
    return _stream
