"""The critic as an A2A agent.

It deliberately returns a *verdict*, not a decision. `sufficient`, `gaps` and
`reason` go back to the orchestrator, which applies `decide_stop` - rounds,
budget, no-progress. A remote agent that could also decide when to stop could
spend the user's quota on its own judgement; keeping the brakes local means a
misbehaving critic can only ever propose one more round, and the local policy
still refuses.

`max_gaps` is honoured here as well as in the orchestrator: a model that ignores
"at most 3" should not be able to put 12 questions on the wire.
"""
from __future__ import annotations

import logging

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import Part
from pydantic import ValidationError

from app.a2a.execution import fail, metadata_for, open_task
from app.a2a.payloads import AgentPayloadError, CriticTask, CriticVerdict, read_payload
from app.agents.nodes.critic import review_coverage

logger = logging.getLogger(__name__)

ARTIFACT_ID = "review"
ARTIFACT_NAME = "coverage-review"


class CriticExecutor(AgentExecutor):
    def __init__(self, review_fn=review_coverage) -> None:
        self.review_fn = review_fn

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = await open_task(context, event_queue)

        try:
            task: CriticTask = read_payload(context.message.parts, CriticTask)
        except (AgentPayloadError, ValidationError) as exc:
            await fail(updater, f"Invalid input: {exc}")
            return

        await updater.start_work()
        review, tokens, model, error = await self.review_fn(
            task.question, task.sub_questions, task.digest
        )
        if review is None:
            await fail(updater, f"critic produced no verdict: {error or 'unknown error'}")
            return

        verdict = CriticVerdict(
            sufficient=review.sufficient,
            gaps=[g for g in review.gaps if g and g.strip()][: task.max_gaps],
            reason=review.reason,
        )
        await updater.add_artifact(
            [Part(text=verdict.model_dump_json())],
            artifact_id=ARTIFACT_ID,
            name=ARTIFACT_NAME,
            append=False,
            last_chunk=True,
            metadata=metadata_for(tokens=tokens, model=model, gaps=len(verdict.gaps)),
        )
        await updater.complete()
        logger.info(
            "critic agent: task %s sufficient=%s gaps=%d tokens=%s served=%s",
            context.task_id, verdict.sufficient, len(verdict.gaps), tokens, model or "-",
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        from a2a.server.tasks import TaskUpdater

        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()
