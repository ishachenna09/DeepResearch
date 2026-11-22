"""Small helpers shared by both executors.

The one non-obvious rule of an A2A executor: **enqueue a `Task` before any
status update**. `TaskUpdater.start_work()` publishes a `TaskStatusUpdateEvent`,
and the SDK's result aggregator rejects a status event for a task it has never
seen ("Agent should enqueue Task before TaskStatusUpdateEvent event"). That is
the protocol's way of saying the first event defines the task.
"""
from __future__ import annotations

from collections.abc import Sequence

from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, Task, TaskState, TaskStatus


async def open_task(context, event_queue: EventQueue) -> TaskUpdater:
    """Create the task, then return an updater bound to it."""
    await event_queue.enqueue_event(
        Task(
            id=context.task_id,
            context_id=context.context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
            history=[context.message] if context.message else [],
        )
    )
    return TaskUpdater(event_queue, context.task_id, context.context_id)


async def fail(updater: TaskUpdater, message: str) -> None:
    """Terminal failure with a human-readable reason in the task history."""
    await updater.failed(updater.new_agent_message([Part(text=message[:2000])]))


def metadata_for(*, tokens: int, model: str, **extra) -> dict:
    """Artifact metadata that survives the wire.

    Numbers become floats inside a protobuf `Struct`, so the reader coerces them
    back to int - see `app.a2a.client.read_reply`. Omitting `None` values keeps
    the JSON small and avoids a `"model": ""` that looks like a real answer.
    """
    meta = {"tokens": int(tokens), "model": model or ""}
    meta.update({k: v for k, v in extra.items() if v is not None})
    return meta


def joined_text(parts: Sequence[Part]) -> str:
    return "".join(p.text for p in parts if p.WhichOneof("content") == "text")
