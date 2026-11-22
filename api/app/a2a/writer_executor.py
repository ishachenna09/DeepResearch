"""The writer as an A2A agent: same prompt, same audit contract, own process.

Why does the agent not audit its own output?
Because the audit is the thing we do not trust the model with. It stays in the
API, next to the registry it resolves against. The service's job is to write; the
caller's job is to check. That split is also what makes the agent reusable by
something that is not this app.

Why stream artifact chunks instead of one big artifact at the end?
It is the only way a caller keeps the "typing" experience. Each chunk is a
`TaskArtifactUpdateEvent` with `append=True`; the caller concatenates them. The
last chunk carries no text and the run's metadata (tokens, served model,
finish_reason, truncated) - which is how the API can keep recording the model
that actually produced the words even though the words were produced elsewhere.

Cancellation: `tasks/cancel` is honoured *between* chunks rather than by killing
the provider call. The flag is per task id because one executor instance serves
every request. A cancelled task stops appending; the text already sent stays.
"""
from __future__ import annotations

import logging

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part
from pydantic import ValidationError

from app.a2a.execution import fail, metadata_for, open_task
from app.a2a.payloads import AgentPayloadError, WriterTask, read_payload
from app.agents.nodes.writer import WriterRun

logger = logging.getLogger(__name__)

ARTIFACT_ID = "report"
ARTIFACT_NAME = "markdown"


class WriterExecutor(AgentExecutor):
    def __init__(self, run_factory=WriterRun) -> None:
        # Injected so tests can drive the event protocol without a provider.
        self.run_factory = run_factory
        self._cancelled: set[str] = set()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        try:
            await self._run(context, event_queue)
        finally:
            self._cancelled.discard(context.task_id)

    async def _run(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = await open_task(context, event_queue)
        task_id = context.task_id

        try:
            task: WriterTask = read_payload(context.message.parts, WriterTask)
        except (AgentPayloadError, ValidationError) as exc:
            await fail(updater, f"Invalid input: {exc}")
            return

        await updater.start_work()

        registry = [item.model_dump() for item in task.registry]
        run = self.run_factory(
            task.question,
            task.plan,
            registry,
            max_tokens=task.max_tokens,
            temperature=task.temperature,
        )
        first = True
        chars = 0
        try:
            async for delta in run.astream():
                if task_id in self._cancelled:
                    logger.info("writer agent: task %s cancelled after %s chars", task_id, chars)
                    return
                if not delta:
                    continue
                chars += len(delta)
                await updater.add_artifact(
                    [Part(text=delta)],
                    artifact_id=ARTIFACT_ID,
                    name=ARTIFACT_NAME,
                    # The first chunk creates the artifact; the rest append.
                    append=not first,
                    last_chunk=False,
                )
                first = False
        except Exception as exc:  # noqa: BLE001 - a failed run is a failed task
            logger.exception("writer agent: generation failed after %s chars", chars)
            await fail(updater, f"{type(exc).__name__}: {exc}")
            return

        if first:
            # The model produced nothing. An empty artifact would look like a
            # finished (if empty) report, so this is a failure, not a success.
            await fail(updater, "the model produced no text")
            return

        await updater.add_artifact(
            [],
            artifact_id=ARTIFACT_ID,
            name=ARTIFACT_NAME,
            append=True,
            last_chunk=True,
            metadata=metadata_for(
                tokens=run.tokens,
                model=run.model,
                finish_reason=run.finish_reason or "",
                truncated=bool(run.truncated),
                chars=chars,
                citations=len(registry),
            ),
        )
        await updater.complete()
        logger.info(
            "writer agent: task %s done - %s chars, %s tokens, finish=%s, truncated=%s, model=%s",
            context.task_id, chars, run.tokens, run.finish_reason,
            bool(run.truncated), run.model,
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        self._cancelled.add(context.task_id)
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()
