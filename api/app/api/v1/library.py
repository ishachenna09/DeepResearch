"""The report library: list what the caller has written, export one as a file.

Why a separate `/reports` resource when `/research/{id}/report` already exists?
The job path answers "what is the report *for this job*"; the library answers
"what have I written recently", across jobs, with the fields a list needs
(question, audit, tokens, model) and without shipping every markdown body. Two
questions, two payload shapes - folding the list into the job endpoint would
mean a query with no id to scope it.

Exports are streamed from the same authenticated route instead of a signed URL:
there is no bucket to sign for (see `services/report_export.py`), and a URL
would put a credential in browser history and proxy logs. The JWT stays in the
`Authorization` header where the rest of the API expects it.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import func, select

from app.core.deps import CurrentUser, SessionDep
from app.models.report import Report
from app.models.research import ResearchJob
from app.schemas.report import ReportPageOut, ReportSummaryOut
from app.services.report_export import (
    build_context,
    export_filename,
    render_markdown,
    render_pdf,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("", response_model=ReportPageOut)
async def list_reports(
    user: CurrentUser,
    session: SessionDep,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None, max_length=200, description="search the question"),
    report_status: Literal["generating", "ready", "failed"] | None = Query(
        default=None, alias="status"
    ),
    sort: Literal["recent", "oldest", "tokens"] = Query(default="recent"),
) -> ReportPageOut:
    """Owner-scoped, newest first by default. `total` is the pre-pagination count."""
    filters = [Report.user_id == user.id]
    if report_status is not None:
        filters.append(Report.status == report_status)
    if q and q.strip():
        # `%` and `_` are wildcards in ILIKE; a search box should treat them as
        # text. The explicit ESCAPE keeps one user's input from matching
        # everything.
        needle = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        filters.append(ResearchJob.question.ilike(f"%{needle}%", escape="\\"))

    order = {
        "recent": Report.updated_at.desc(),
        "oldest": Report.updated_at.asc(),
        "tokens": Report.tokens_used.desc(),
    }[sort]

    total = await session.scalar(
        select(func.count())
        .select_from(Report)
        .join(ResearchJob, Report.job_id == ResearchJob.id)
        .where(*filters)
    )
    rows = (
        await session.execute(
            select(Report, ResearchJob.question)
            .join(ResearchJob, Report.job_id == ResearchJob.id)
            .where(*filters)
            .order_by(order)
            .limit(limit)
            .offset(offset)
        )
    ).all()

    items = []
    for report, question in rows:
        validation = report.validation or {}
        items.append(
            ReportSummaryOut(
                report_id=report.id,
                job_id=report.job_id,
                question=question,
                status=report.status,  # type: ignore[arg-type]
                words=int(validation.get("words") or 0),
                markers=int(validation.get("markers") or 0),
                precision=float(validation.get("precision") or 0.0),
                coverage=float(validation.get("coverage") or 0.0),
                citations=len(report.citations or []),
                tokens_used=report.tokens_used or 0,
                model=report.model,
                error=report.error,
                created_at=report.created_at,
                updated_at=report.updated_at,
            )
        )
    return ReportPageOut(total=int(total or 0), limit=limit, offset=offset, items=items)


async def _owned_report(
    report_id: uuid.UUID, user, session
) -> tuple[Report, ResearchJob]:
    row = (
        await session.execute(
            select(Report, ResearchJob)
            .join(ResearchJob, Report.job_id == ResearchJob.id)
            .where(Report.id == report_id, Report.user_id == user.id)
        )
    ).first()
    if row is None:
        # 404, not 403: never confirm that another user's report id exists.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    return row


def _attachment(filename: str) -> dict[str, str]:
    return {
        "Content-Disposition": f'attachment; filename="{filename}"',
        # Regenerating overwrites the row, so the same URL can return different
        # bytes. A cached export would be a stale answer with the current title.
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }


def _ready(report: Report) -> None:
    """No partial files: a generating or failed report is not an answer."""
    if report.status != "ready":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Report is '{report.status}' - export needs a ready report",
        )


@router.get("/{report_id}/export.md")
async def export_markdown(
    report_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Response:
    report, job = await _owned_report(report_id, user, session)
    _ready(report)
    ctx = build_context(report, job.question)
    return Response(
        content=render_markdown(ctx),
        media_type="text/markdown; charset=utf-8",
        headers=_attachment(export_filename(ctx, "md")),
    )


@router.get("/{report_id}/export.pdf")
async def export_pdf(
    report_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Response:
    report, job = await _owned_report(report_id, user, session)
    _ready(report)
    ctx = build_context(report, job.question)
    try:
        # ReportLab is synchronous and a table-heavy report takes ~0.2 s; run it
        # off the event loop so one export cannot stall every other request.
        data = await asyncio.to_thread(render_pdf, ctx)
    except Exception as exc:  # noqa: BLE001 - a renderer bug is a 500, not a hang
        logger.exception("PDF export failed for report %s", report.id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"PDF export failed: {type(exc).__name__}",
        ) from exc
    return Response(
        content=data,
        media_type="application/pdf",
        headers=_attachment(export_filename(ctx, "pdf")),
    )
