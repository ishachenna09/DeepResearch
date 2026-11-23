"""Usage + shadow-cost summary for the system page.

What is counted: measured tokens, never estimates of activity. `research_jobs`
carries the graph's total (planner + extraction + critic), `reports` carries the
writer's - including failed reports, because a failure still burned tokens.
Both are summed from the rows the app already writes.

What is estimated: dollars, at list prices, with a stated 70/30 input/output
blend (see `app/core/pricing.py`). The free tier bills $0 - the number answers
"what would this have cost elsewhere?", which is the only way a token regression
shows up as money in a portfolio project.

Why `scope=all` is admin-only: one user's research questions are not another
user's business, and cost is a weak proxy for activity but still activity.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.core.config import settings
from app.core.deps import CurrentUser, SessionDep
from app.core.pricing import estimate_usd, pricing_metadata
from app.models.report import Report
from app.models.research import ResearchJob

router = APIRouter(tags=["usage"])


@router.get("/usage")
async def usage(
    session: SessionDep,
    user: CurrentUser,
    days: int = Query(default=30, ge=1, le=365),
    scope: str = Query(default="self", pattern="^(self|all)$"),
) -> dict:
    if scope == "all" and user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "scope=all requires the admin role")

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    user_filter = [] if scope == "all" else [ResearchJob.user_id == user.id]

    jobs = (
        await session.execute(
            select(
                func.count(ResearchJob.id),
                func.coalesce(func.sum(ResearchJob.tokens_used), 0),
                func.coalesce(func.sum(ResearchJob.notes_count), 0),
            ).where(ResearchJob.created_at >= cutoff, *user_filter)
        )
    ).one()
    job_count, research_tokens, notes = int(jobs[0]), int(jobs[1]), int(jobs[2])

    report_rows = (
        await session.execute(
            select(
                Report.model,
                func.count(Report.id),
                func.coalesce(func.sum(Report.tokens_used), 0),
            )
            .join(ResearchJob, Report.job_id == ResearchJob.id)
            .where(Report.created_at >= cutoff, *user_filter)
            .group_by(Report.model)
        )
    ).all()

    by_model = []
    report_tokens = 0
    report_usd = 0.0
    for model, count, tokens in report_rows:
        report_tokens += int(tokens)
        usd = estimate_usd(int(tokens), model)
        report_usd += usd
        by_model.append(
            {
                "model": model or "unknown",
                "reports": int(count),
                "tokens": int(tokens),
                "estimated_usd": usd,
            }
        )
    by_model.sort(key=lambda item: item["tokens"], reverse=True)

    research_usd = estimate_usd(research_tokens, settings.LLM_PRIMARY_MODEL)

    job_days = {
        str(day): int(tokens)
        for day, tokens in (
            await session.execute(
                select(func.date(ResearchJob.created_at), func.coalesce(func.sum(ResearchJob.tokens_used), 0))
                .where(ResearchJob.created_at >= cutoff, *user_filter)
                .group_by(func.date(ResearchJob.created_at))
            )
        ).all()
    }
    report_days = {
        str(day): int(tokens)
        for day, tokens in (
            await session.execute(
                select(func.date(Report.created_at), func.coalesce(func.sum(Report.tokens_used), 0))
                .join(ResearchJob, Report.job_id == ResearchJob.id)
                .where(Report.created_at >= cutoff, *user_filter)
                .group_by(func.date(Report.created_at))
            )
        ).all()
    }
    by_day = [
        {
            "day": day,
            "research_tokens": job_days.get(day, 0),
            "report_tokens": report_days.get(day, 0),
            "tokens": job_days.get(day, 0) + report_days.get(day, 0),
        }
        for day in sorted(set(job_days) | set(report_days))
    ]

    return {
        "scope": scope,
        "days": days,
        "window": {"from": cutoff.isoformat(), "to": now.isoformat()},
        "totals": {
            "jobs": job_count,
            "notes": notes,
            "research_tokens": research_tokens,
            "report_tokens": report_tokens,
            "tokens": research_tokens + report_tokens,
            "estimated_usd": round(research_usd + report_usd, 6),
        },
        "research_tokens_priced_as": settings.LLM_PRIMARY_MODEL,
        "by_model": by_model,
        "by_day": by_day,
        "pricing": pricing_metadata(),
    }
