"""Task store: in-process for `ListTasks`, Redis for the tasks a caller can name.

Why not `InMemoryTaskStore` alone?
An A2A task is a handle a client can come back to (`GetTask`). The writer holds
one open for a minute; a restart in between would turn "check on my task" into
"no such task" even though the work finished. Redis with a TTL keeps that handle
across a restart without pretending a task is a durable document - the report row
in Postgres is the durable artifact, this is a short-lived receipt.

Why inherit from the SDK's in-memory store instead of implementing `TaskStore`?
`list()` needs paging (`ListTasksRequest`/`ListTasksResponse`) and we do not use
it; inheriting it keeps `ListTasks` honest *within a process* while this class
owns the three methods that matter. The limitation is documented, not hidden.
"""
from __future__ import annotations

import json
import logging

import redis.asyncio as redis
from google.protobuf.json_format import MessageToDict, ParseDict

from a2a.server.tasks import InMemoryTaskStore
from a2a.types import Task

from app.core.config import settings

logger = logging.getLogger(__name__)

PREFIX = "dr:a2a:task:"


class RedisTaskStore(InMemoryTaskStore):
    def __init__(self, namespace: str, ttl: int | None = None) -> None:
        super().__init__()
        self.namespace = namespace
        self.ttl = ttl if ttl is not None else settings.A2A_TASK_TTL
        self._client: redis.Redis | None = None
        self._warned = False

    def _key(self, task_id: str) -> str:
        return f"{PREFIX}{self.namespace}:{task_id}"

    async def _redis(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.Redis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
        return self._client

    async def save(self, task: Task, context) -> None:
        # Always keep the in-process copy: it is what ListTasks reads, and it is
        # the fallback when Redis is unreachable.
        await super().save(task, context)
        try:
            client = await self._redis()
            await client.setex(
                self._key(task.id), self.ttl, json.dumps(MessageToDict(task))
            )
        except Exception as exc:  # noqa: BLE001 - a dead Redis must not fail the task
            if not self._warned:
                self._warned = True
                logger.warning("a2a task store: redis unavailable (%s); task handles are process-local", exc)

    async def get(self, task_id: str, context) -> Task | None:
        try:
            client = await self._redis()
            raw = await client.get(self._key(task_id))
            if raw:
                return ParseDict(json.loads(raw), Task())
        except Exception as exc:  # noqa: BLE001
            if not self._warned:
                self._warned = True
                logger.warning("a2a task store: redis read failed (%s)", exc)
        return await super().get(task_id, context)

    async def delete(self, task_id: str, context) -> None:
        await super().delete(task_id, context)
        try:
            client = await self._redis()
            await client.delete(self._key(task_id))
        except Exception:  # noqa: BLE001
            pass

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
