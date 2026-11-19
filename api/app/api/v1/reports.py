"""Report endpoints: generate, read, and stream the cited answer.

Why `fetch` + a Bearer header on the client instead of `EventSource`?
`EventSource` cannot send an `Authorization` header, so the usual workaround puts
the JWT in the query string - where it lands in access logs, proxy logs and
browser history. This endpoint is a normal authenticated GET; the frontend reads
the response body as a stream. That also lets reconnects send `Last-Event-ID`,
which `EventSource` would do automatically but without our auth.

Why replay from `0-0` when no `Last-Event-ID` is sent?
A second tab, or a refresh mid-generation, should see the whole report, not just
the tail. The stream is bounded (MAXLEN + TTL) so replay is cheap.

Failure model: if Redis is unavailable the stream is empty; the endpoint then
watches the database and emits a single `done` when the report is ready. The
user loses the live typing effect, not the report.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.core.deps import CurrentUser, SessionDep
from app.core.streams import BLOCK_MS, get_report_stream
from app.db import SessionLocal
from app.models.report import Report
from app.models.research import ResearchJob
from app.schemas.report import CitationOut, ReportOut, ReportValidationOut
from app.services.report_runner import is_generating, spawn_report

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research", tags=["reports"])

# Heartbeat/comments keep proxies from closing an idle stream while a slow
# model is still thinking (Gemini's first token can take 10-20 s).
_HEARTBEAT_SECONDS = BLOCK_MS // 1000
_READ_BLOCK_MS = BLOCK_MS


async def _owned_job(job_id: uuid.UUID, user, session) -> ResearchJob:
    job = await session.scalar(
        select(ResearchJob).where(ResearchJob.id == job_id, ResearchJob.user_id == user.id)
    )
    if job is None:
        # 404, not 403: never confirm that someone else's job id exists.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return job


async def _owned_job_and_report(
    job_id: uuid.UUID, user, session
) -> tuple[ResearchJob, Report | None]:
    """Ownership check + report lookup in ONE round trip.

    Every query costs ~450 ms against this deployment's Postgres (the database
    is in a different region from the client), so two sequential queries double
    the cost of a page load that shows nothing new. The outer join keeps the
    "job exists but has no report yet" case distinct from "no such job".
    """
    row = (
        await session.execute(
            select(ResearchJob, Report)
            .outerjoin(Report, Report.job_id == ResearchJob.id)
            .where(ResearchJob.id == job_id, ResearchJob.user_id == user.id)
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    job, report = row
    return job, report


def _to_out(report: Report) -> ReportOut:
    validation = ReportValidationOut(**report.validation) if report.validation else None
    citations = [CitationOut(**item) for item in (report.citations or [])]
    return ReportOut(
        report_id=report.id,
        job_id=report.job_id,
        status=report.status,          # type: ignore[arg-type]
        markdown=report.markdown or "",
        citations=citations,
        validation=validation,
        tokens_used=report.tokens_used or 0,
        model=report.model,
        error=report.error,
        created_at=report.created_at,
        updated_at=report.updated_at,
    )


@router.post("/{job_id}/report", response_model=ReportOut, status_code=status.HTTP_202_ACCEPTED)
async def generate_report(
    job_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> ReportOut:
    """Start (or restart) report generation. Idempotent while one is running."""
    job = await _owned_job(job_id, user, session)

    if job.status != "notes_ready":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Job is '{job.status}' - a report needs finished research (`notes_ready`)",
        )
    if (job.notes_count or 0) == 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "Job has no notes to write from")

    report = await session.scalar(select(Report).where(Report.job_id == job.id))

    if report is not None and report.status == "generating" and is_generating(report.id):
        # Already running: attach instead of starting a second generation.
        return _to_out(report)

    if report is None:
        report = Report(job_id=job.id, user_id=user.id, status="generating", markdown="")
        session.add(report)
        await session.commit()
        await session.refresh(report)
    else:
        # Regenerate: overwrite in place, and drop the previous token stream so a
        # late subscriber cannot replay the old report into the new one.
        try:
            await get_report_stream().drop(report.id)
        except RuntimeError:
            pass
        report.status = "generating"
        report.markdown = ""
        report.citations = None
        report.validation = None
        report.tokens_used = 0
        report.error = None
        await session.commit()
        await session.refresh(report)

    spawn_report(report.id)
    return _to_out(report)


@router.get("/{job_id}/report", response_model=ReportOut)
async def get_report(job_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> ReportOut:
    _job, report = await _owned_job_and_report(job_id, user, session)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No report yet for this job")
    return _to_out(report)


def _sse(event: str, data: str, event_id: str | None = None) -> str:
    head = f"id: {event_id}\n" if event_id else ""
    return f"{head}event: {event}\ndata: {data}\n\n"


@router.get("/{job_id}/report/stream")
async def stream_report(
    job_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """SSE: `status`, `token`, `done` / `error`, replayable via Last-Event-ID."""
    _job, report = await _owned_job_and_report(job_id, user, session)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No report yet for this job")

    report_id = report.id
    start = last_event_id if last_event_id else "0-0"

    async def events() -> AsyncIterator[str]:
        try:
            stream = get_report_stream()
        except RuntimeError:
            stream = None
        last = start
        # Flush headers immediately so proxies/browsers switch to streaming mode.
        yield ": connected\n\n"
        deadline = time.monotonic() + 900      # hard ceiling: 15 min
        while time.monotonic() < deadline:
            batch = await stream.read(report_id, last, block_ms=_READ_BLOCK_MS) if stream else []
            if batch:
                for entry_id, kind, data in batch:
                    last = entry_id
                    yield _sse(kind, data, entry_id)
                    if kind in ("done", "error"):
                        return
                continue

            # Nothing new: is the generation already over (stream expired, Redis
            # down, or the task died)? Then answer from the database and close.
            async with SessionLocal() as check:
                row = await check.get(Report, report_id)
                state = row.status if row is not None else "failed"
                if row is not None and state == "ready":
                    final = json.dumps(
                        {
                            "status": "ready",
                            "validation": row.validation,
                            "citations": len(row.citations or []),
                            "tokens_used": row.tokens_used,
                        }
                    )
                else:
                    final = json.dumps({"error": (row.error if row else "Report disappeared")})

            if state in ("ready", "failed"):
                # One last drain in case the terminal event was published between
                # our read and the database check.
                tail = await stream.read(report_id, last, block_ms=0) if stream else []
                for entry_id, kind, data in tail:
                    last = entry_id
                    yield _sse(kind, data, entry_id)
                    if kind in ("done", "error"):
                        return
                yield _sse("done" if state == "ready" else "error", final)
                return

            yield ": waiting\n\n"      # comment frame: keeps the connection warm

        yield _sse("error", json.dumps({"error": "Stream timed out"}))

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Tell nginx not to buffer: otherwise tokens arrive in one burst.
            "X-Accel-Buffering": "no",
        },
    )
