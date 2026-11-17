"""Run the approved research graph in the background.

Why not just `await graph.ainvoke(...)` inside the approve endpoint?
A run is search x N branches + page fetches + extraction + embeddings: tens of
seconds to minutes. Holding the HTTP request open would time out the browser,
the proxy, and (on EC2) the load balancer. So the endpoint flips the job to
`researching`, returns immediately, and the client polls GET /research/{id}.

Why its own DB session?
The request session is closed when the response is sent. This task outlives the
request, so it opens a `SessionLocal()` of its own - the one rule that matters
when moving work out of a request lifecycle.

Known limitation (deliberate, documented): `asyncio.create_task` is per-process.
A restart mid-run loses the task, so startup marks orphaned `researching` jobs as
`failed` (see reconcile_stale_jobs). Phase 10 replaces this with a real queue
(ARQ/Celery + SQS) so runs survive deploys; the interface here stays the same.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from langgraph.types import Command
from sqlalchemy import func, select, update

from app.agents.graph import get_graph
from app.db import SessionLocal
from app.models.research import ResearchJob
from app.models.source import SourceNote

logger = logging.getLogger(__name__)

# Strong references: asyncio only keeps weak refs to tasks, so a task with no
# reference can be garbage-collected mid-flight.
RUNNING: dict[uuid.UUID, asyncio.Task] = {}


def is_running(job_id: uuid.UUID) -> bool:
    task = RUNNING.get(job_id)
    return task is not None and not task.done()


def spawn_research(job_id: uuid.UUID, resume: dict) -> asyncio.Task:
    task = asyncio.create_task(_run(job_id, resume), name=f"research-{job_id}")
    RUNNING[job_id] = task
    task.add_done_callback(lambda _t, jid=job_id: RUNNING.pop(jid, None))
    return task


async def _run(job_id: uuid.UUID, resume: dict) -> None:
    async with SessionLocal() as session:
        job = await session.get(ResearchJob, job_id)
        if job is None:
            logger.warning("research task for missing job %s", job_id)
            return

        thread_id = job.thread_id
        job.status = "researching"
        job.error = None
        await session.commit()

        try:
            result = await get_graph().ainvoke(
                Command(resume=resume), config={"configurable": {"thread_id": thread_id}}
            )
        except Exception as exc:  # noqa: BLE001 - record it on the job, never crash the loop
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"[:1000]
            await session.commit()
            logger.exception("research job %s failed", job_id)
            return

        stats = result.get("stats") or {}
        stored = await session.scalar(
            select(func.count()).select_from(SourceNote).where(SourceNote.job_id == job_id)
        )
        job.notes_count = int(stored or 0)
        job.sources = result.get("sources") or []
        if not job.plan and result.get("plan"):
            job.plan = result["plan"]
        job.status = "notes_ready"
        job.error = (
            "No notes extracted: every branch failed or returned nothing relevant"
            if job.notes_count == 0
            else None
        )
        await session.commit()
        logger.info("research job %s done: %s notes | %s", job_id, job.notes_count, stats)


async def cancel_all() -> None:
    """Lifespan shutdown: stop in-flight runs instead of leaking them."""
    tasks = [t for t in RUNNING.values() if not t.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("cancelled %d in-flight research task(s)", len(tasks))


async def reconcile_stale_jobs() -> int:
    """Startup: a job in `researching` cannot be running - this process just booted."""
    async with SessionLocal() as session:
        result = await session.execute(
            update(ResearchJob)
            .where(ResearchJob.status == "researching")
            .values(
                status="failed",
                error="Interrupted by an API restart. Re-approve the plan to retry.",
            )
        )
        await session.commit()
        count = result.rowcount or 0
    if count:
        logger.warning("marked %d orphaned 'researching' job(s) as failed", count)
    return count
