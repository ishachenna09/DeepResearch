"""Generate a cited report in the background, streaming tokens to Redis.

Why background + stream instead of one blocking request?
A 900-word generation is 20-60 s of provider time. Holding the HTTP request open
for it is the same mistake the research runner avoids: proxies and load
balancers time out, and the browser cannot show progress. So the POST flips the
report to `generating` and returns; the browser then subscribes to the SSE
endpoint, which replays the Redis stream from the beginning.

Why does the runner own the DB session?
The request session dies with the response. This task outlives it, so it opens
its own `SessionLocal()` - same rule as `research_runner`.

Known limitation (shared with Phase 4, documented on purpose): `create_task` is
per-process, so a restart mid-generation loses the task. Startup marks orphaned
`generating` reports as `failed` (reconcile_stale_reports) and the user can
regenerate; Phase 10 replaces this with a real queue.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select, update

from app.agents.nodes.writer import (
    WriterRun,
    build_registry,
    citations_for,
    needs_repair,
    normalize,
    repair_citations,
    validate,
)
from app.core.config import settings
from app.core.streams import get_report_stream
from app.db import SessionLocal
from app.models.report import Report
from app.models.research import ResearchJob
from app.models.source import SourceNote

logger = logging.getLogger(__name__)

RUNNING: dict[uuid.UUID, asyncio.Task] = {}


def _score(validation: dict) -> tuple:
    """Rank two candidate drafts.

    Order matters: a finished draft beats a truncated one even if the truncated
    one happens to score better on precision (it is half a brief). Then
    precision (no invented sources), then coverage, then length.
    """
    return (
        not validation.get("truncated", False),
        validation.get("precision", 0.0),
        validation.get("coverage", 0.0),
        validation.get("words", 0),
    )


def is_generating(report_id: uuid.UUID) -> bool:
    task = RUNNING.get(report_id)
    return task is not None and not task.done()


def spawn_report(report_id: uuid.UUID) -> asyncio.Task:
    task = asyncio.create_task(_run(report_id), name=f"report-{report_id}")
    RUNNING[report_id] = task
    task.add_done_callback(lambda _t, rid=report_id: RUNNING.pop(rid, None))
    return task


async def _run(report_id: uuid.UUID) -> None:
    stream = get_report_stream()
    async with SessionLocal() as session:
        report = await session.get(Report, report_id)
        if report is None:
            logger.warning("report task for missing report %s", report_id)
            return

        job = await session.get(ResearchJob, report.job_id)
        if job is None:
            report.status = "failed"
            report.error = "Research job disappeared"
            await session.commit()
            await stream.append(report_id, "error", {"error": report.error})
            return

        notes = (
            (
                await session.execute(
                    select(SourceNote)
                    .where(SourceNote.job_id == job.id)
                    .order_by(SourceNote.created_at.asc())
                )
            )
            .scalars()
            .all()
        )

        registry = build_registry(notes)
        if not registry:
            report.status = "failed"
            report.error = "No notes to write from"
            await session.commit()
            await stream.append(report_id, "error", {"error": report.error})
            return

        await stream.append(
            report_id,
            "status",
            {"status": "generating", "sources": len(registry), "notes": len(notes)},
        )
        logger.info(
            "report %s: writing from %s notes (%s in registry), job %s",
            report_id, len(notes), len(registry), job.id,
        )

        run = WriterRun(job.question, job.plan, registry)
        chunks: list[str] = []
        try:
            async for text in run.astream():
                chunks.append(text)
                # JSON, not the raw text: SSE frames are newline-delimited, and
                # a chunk containing "\n" would split into multiple data lines.
                await stream.append(report_id, "token", {"text": text})
        except Exception as exc:  # noqa: BLE001 - record partial output, never crash
            partial = "".join(chunks)
            report.markdown = partial
            report.status = "failed"
            report.error = f"{type(exc).__name__}: {exc}"[:1000]
            report.tokens_used = run.tokens
            report.citations = citations_for(partial, registry)
            report.validation = validate(partial, registry)
            await session.commit()
            await stream.append(report_id, "error", {"error": report.error})
            logger.exception("report %s failed after %s chars", report_id, len(partial))
            return

        markdown = normalize("".join(chunks))
        validation = validate(markdown, registry)
        validation["truncated"] = run.truncated
        total_tokens = run.tokens

        # One bounded second attempt. Two triggers, both caught by the audit
        # rather than by luck:
        # - the draft cites nothing / invents source numbers (Gemini sometimes
        #   falls back to its native 【...】 grounding style);
        # - the provider hit the token cap, so the brief ends mid-sentence.
        # We keep whichever draft scores better, so a bad retry can never make
        # the result worse.
        validation["repaired"] = False
        if needs_repair(validation):
            reason = (
                "the draft was cut off at the token limit"
                if validation.get("truncated")
                else "the first draft had no valid citation markers"
            )
            await stream.append(report_id, "status", {"status": "repairing", "reason": reason})
            logger.warning(
                "report %s: draft needs another attempt (markers=%s precision=%s truncated=%s)",
                report_id, validation["markers"], validation["precision"],
                validation.get("truncated"),
            )
            try:
                repaired, repair_tokens, repair_model = await repair_citations(
                    job.question,
                    job.plan,
                    registry,
                    markdown,
                    max_tokens=settings.REPORT_RETRY_MAX_TOKENS,
                )
                repaired = normalize(repaired)
                total_tokens += repair_tokens
                repaired_validation = validate(repaired, registry)
                repaired_validation["truncated"] = False   # a non-streamed call reports no chunks
                if len(repaired) > 200 and _score(repaired_validation) > _score(validation):
                    markdown, validation = repaired, repaired_validation
                    if repair_model:
                        run.model = repair_model
                    await stream.append(report_id, "replace", {"text": markdown})
                validation["repaired"] = True
                logger.info(
                    "report %s: second attempt -> markers=%s precision=%s coverage=%s words=%s "
                    "served_by=%s",
                    report_id, validation["markers"], validation["precision"],
                    validation["coverage"], validation["words"], repair_model,
                )
            except Exception as exc:  # noqa: BLE001 - a failed retry must not kill the report
                logger.warning("report %s: retry failed (%s: %s)", report_id, type(exc).__name__, exc)

        citations = citations_for(markdown, registry)

        report.markdown = markdown
        report.citations = citations
        report.validation = validation
        report.tokens_used = total_tokens
        report.model = run.model
        report.status = "ready"
        report.error = None
        await session.commit()

        await stream.append(
            report_id,
            "done",
            {
                "status": "ready",
                "validation": validation,
                "citations": len(citations),
                "tokens_used": total_tokens,
            },
        )
        logger.info(
            "report %s ready: %s words, %s citations, precision=%s coverage=%s, %s tokens, "
            "finish=%s truncated=%s uncited=%s served_by=%s",
            report_id, validation["words"], len(citations),
            validation["precision"], validation["coverage"], total_tokens,
            run.finish_reason, validation.get("truncated"), validation["uncited"], run.model,
        )


async def cancel_all_reports() -> None:
    tasks = [t for t in RUNNING.values() if not t.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("cancelled %d in-flight report task(s)", len(tasks))


async def reconcile_stale_reports() -> int:
    """Startup: a `generating` report cannot be running - this process just booted."""
    async with SessionLocal() as session:
        result = await session.execute(
            update(Report)
            .where(Report.status == "generating")
            .values(
                status="failed",
                error="Interrupted by an API restart. Generate the report again.",
            )
        )
        await session.commit()
        count = result.rowcount or 0
    if count:
        logger.warning("marked %d orphaned 'generating' report(s) as failed", count)
    return count
