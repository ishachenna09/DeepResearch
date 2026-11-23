"""Admin overview: the whole deployment, in one read-only endpoint.

Why one endpoint instead of `/admin/users`, `/admin/jobs`, `/admin/cost`?
The dashboard needs all of them at once; five round trips to the same database
is five chances for a half-rendered page. One query set, one snapshot, one
`generated_at` that means something.

Why do the aggregates include failed rows?
A failure burned tokens and notes just like a success. A dashboard that only
counts `ready` reports makes instability invisible - and the failure count is
the number an operator actually needs.

Why is this admin-only?
Costs and other users' questions are not a normal user's business. `require_role`
answers 403 before any query runs; the same rule protects `/usage?scope=all`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.config import settings
from app.core.deps import SessionDep, require_role
from app.core.pricing import estimate_usd, pricing_metadata
from app.models.report import Report
from app.models.research import ResearchJob
from app.models.user import User

router = APIRouter(prefix="/admin", tags=["admin"])

AdminUser = Annotated[User, Depends(require_role("admin"))]


@router.get("/overview")
async def overview(
    session: SessionDep,
    _admin: AdminUser,
    days: int = Query(default=30, ge=1, le=365),
) -> dict:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    users_total = int(await session.scalar(select(func.count(User.id))) or 0)
    users_active = int(
        await session.scalar(select(func.count(User.id)).where(User.is_active.is_(True))) or 0
    )
    users_new = int(
        await session.scalar(
            select(func.count(User.id)).where(User.created_at >= cutoff)
        )
        or 0
    )

    # Jobs: status split, notes and graph tokens (planner + extraction + critic).
    job_rows = (
        await session.execute(
            select(
                ResearchJob.status,
                func.count(ResearchJob.id),
                func.coalesce(func.sum(ResearchJob.notes_count), 0),
                func.coalesce(func.sum(ResearchJob.tokens_used), 0),
            )
            .where(ResearchJob.created_at >= cutoff)
            .group_by(ResearchJob.status)
        )
    ).all()
    jobs_by_status = {row[0]: int(row[1]) for row in job_rows}
    job_count = sum(jobs_by_status.values())
    notes = sum(int(row[2]) for row in job_rows)
    research_tokens = sum(int(row[3]) for row in job_rows)

    # Reports: status split plus the writer's tokens.
    report_rows = (
        await session.execute(
            select(
                Report.status,
                func.count(Report.id),
                func.coalesce(func.sum(Report.tokens_used), 0),
            )
            .where(Report.created_at >= cutoff)
            .group_by(Report.status)
        )
    ).all()
    reports_by_status = {row[0]: int(row[1]) for row in report_rows}
    report_count = sum(reports_by_status.values())
    report_tokens = sum(int(row[2]) for row in report_rows)

    # Per-model: ready/failed counts make a provider regression visible next to
    # the token count, instead of only in a log.
    model_rows = (
        await session.execute(
            select(
                Report.model,
                func.count(Report.id),
                func.count().filter(Report.status == "ready"),
                func.count().filter(Report.status == "failed"),
                func.coalesce(func.sum(Report.tokens_used), 0),
            )
            .where(Report.created_at >= cutoff)
            .group_by(Report.model)
        )
    ).all()
    by_model = []
    report_usd = 0.0
    for model, count, ready, failed, tokens in model_rows:
        usd = estimate_usd(int(tokens), model)
        report_usd += usd
        by_model.append(
            {
                "model": model or "unknown",
                "reports": int(count),
                "ready": int(ready),
                "failed": int(failed),
                "tokens": int(tokens),
                "estimated_usd": usd,
            }
        )
    by_model.sort(key=lambda row: row["tokens"], reverse=True)

    # One bucket per day across both tables, so the chart has no holes.
    job_days = {
        str(day): (int(count), int(tokens))
        for day, count, tokens in (
            await session.execute(
                select(
                    func.date(ResearchJob.created_at),
                    func.count(ResearchJob.id),
                    func.coalesce(func.sum(ResearchJob.tokens_used), 0),
                )
                .where(ResearchJob.created_at >= cutoff)
                .group_by(func.date(ResearchJob.created_at))
            )
        ).all()
    }
    report_days = {
        str(day): (int(count), int(tokens))
        for day, count, tokens in (
            await session.execute(
                select(
                    func.date(Report.created_at),
                    func.count(Report.id),
                    func.coalesce(func.sum(Report.tokens_used), 0),
                )
                .where(Report.created_at >= cutoff)
                .group_by(func.date(Report.created_at))
            )
        ).all()
    }
    by_day = []
    for day in sorted(set(job_days) | set(report_days)):
        jobs_on_day, job_tokens = job_days.get(day, (0, 0))
        reports_on_day, report_tokens_on_day = report_days.get(day, (0, 0))
        by_day.append(
            {
                "day": day,
                "jobs": jobs_on_day,
                "reports": reports_on_day,
                "tokens": job_tokens + report_tokens_on_day,
            }
        )

    # Top users: tokens are the measure, because that is what a heavy account
    # costs. Merge the two aggregate queries in Python rather than a double
    # outer join that multiplies counts.
    job_users = {
        row[0]: (int(row[1]), int(row[2]), int(row[3]))
        for row in (
            await session.execute(
                select(
                    ResearchJob.user_id,
                    func.count(ResearchJob.id),
                    func.coalesce(func.sum(ResearchJob.notes_count), 0),
                    func.coalesce(func.sum(ResearchJob.tokens_used), 0),
                )
                .where(ResearchJob.created_at >= cutoff)
                .group_by(ResearchJob.user_id)
            )
        ).all()
    }
    report_users = {
        row[0]: (int(row[1]), int(row[2]))
        for row in (
            await session.execute(
                select(
                    Report.user_id,
                    func.count(Report.id),
                    func.coalesce(func.sum(Report.tokens_used), 0),
                )
                .where(Report.created_at >= cutoff)
                .group_by(Report.user_id)
            )
        ).all()
    }
    user_ids = set(job_users) | set(report_users)
    emails = (
        dict(
            (
                await session.execute(
                    select(User.id, User.email).where(User.id.in_(user_ids))
                )
            ).all()
        )
        if user_ids
        else {}
    )
    top_users = []
    for user_id in user_ids:
        jobs, notes_for_user, job_tokens = job_users.get(user_id, (0, 0, 0))
        reports, report_tokens_for_user = report_users.get(user_id, (0, 0))
        top_users.append(
            {
                "user_id": str(user_id),
                "email": emails.get(user_id, "(deleted user)"),
                "jobs": jobs,
                "reports": reports,
                "notes": notes_for_user,
                "tokens": job_tokens + report_tokens_for_user,
            }
        )
    top_users.sort(key=lambda row: row["tokens"], reverse=True)
    top_users = top_users[:8]

    failed_jobs = (
        await session.execute(
            select(ResearchJob.id, ResearchJob.question, ResearchJob.error, ResearchJob.created_at)
            .where(
                ResearchJob.created_at >= cutoff,
                ResearchJob.status.in_(("failed", "rejected")),
            )
            .order_by(ResearchJob.created_at.desc())
            .limit(5)
        )
    ).all()
    failed_reports = (
        await session.execute(
            select(
                Report.id,
                Report.job_id,
                ResearchJob.question,
                Report.error,
                Report.created_at,
            )
            .join(ResearchJob, Report.job_id == ResearchJob.id)
            .where(Report.status == "failed", Report.created_at >= cutoff)
            .order_by(Report.created_at.desc())
            .limit(5)
        )
    ).all()
    recent_failures = [
        {
            "kind": "job",
            "id": str(row[0]),
            "job_id": str(row[0]),
            "question": row[1],
            "error": row[2],
            "created_at": row[3].isoformat(),
        }
        for row in failed_jobs
    ] + [
        {
            "kind": "report",
            "id": str(row[0]),
            "job_id": str(row[1]),
            "question": row[2],
            "error": row[3],
            "created_at": row[4].isoformat(),
        }
        for row in failed_reports
    ]
    recent_failures.sort(key=lambda row: row["created_at"], reverse=True)

    research_usd = estimate_usd(research_tokens, settings.LLM_PRIMARY_MODEL)
    ready_reports = reports_by_status.get("ready", 0)
    return {
        "generated_at": now.isoformat(),
        "window": {"days": days, "from": cutoff.isoformat(), "to": now.isoformat()},
        "users": {"total": users_total, "active": users_active, "new": users_new},
        "jobs": {
            "total": job_count,
            "by_status": jobs_by_status,
            "failed": jobs_by_status.get("failed", 0),
            "notes": notes,
            "tokens": research_tokens,
        },
        "reports": {
            "total": report_count,
            "by_status": reports_by_status,
            "ready": ready_reports,
            "failed": reports_by_status.get("failed", 0),
            "success_rate": (
                round(ready_reports / report_count, 4) if report_count else None
            ),
            "tokens": report_tokens,
        },
        "tokens": {
            "research": research_tokens,
            "report": report_tokens,
            "total": research_tokens + report_tokens,
        },
        "cost": {
            "estimated_usd": round(research_usd + report_usd, 6),
            "research_usd": research_usd,
            "report_usd": round(report_usd, 6),
            "research_tokens_priced_as": settings.LLM_PRIMARY_MODEL,
            "pricing": pricing_metadata(),
        },
        "by_day": by_day,
        "by_model": by_model,
        "top_users": top_users,
        "recent_failures": recent_failures[:5],
    }

