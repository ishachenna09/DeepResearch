import uuid

from fastapi import APIRouter, HTTPException, Query, status
from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agents.graph import get_graph
from app.core.deps import CurrentUser, SessionDep
from app.core.ratelimit import user_rate_limit
from app.models.research import ResearchJob
from app.schemas.research import (
    CreateResearchIn,
    PlanDecisionIn,
    ResearchJobOut,
    ResearchPlanOut,
)
from app.services.research_runner import is_running, spawn_research

router = APIRouter(prefix="/research", tags=["research"])


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _to_out(job: ResearchJob) -> ResearchJobOut:
    return ResearchJobOut(
        job_id=job.id,
        question=job.question,
        plan=ResearchPlanOut(**job.plan) if job.plan else None,
        status=job.status,
        notes_count=job.notes_count or 0,
        error=job.error,
        sources=job.sources,
        rounds=job.rounds or 1,
        tokens_used=job.tokens_used or 0,
        critique=job.critique,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@router.post(
    "",
    response_model=ResearchJobOut,
    status_code=status.HTTP_201_CREATED,
    # Two windows: a burst cap and a daily cap. A user cannot trade one for the
    # other, and the daily cap is what a free-tier budget actually needs.
    dependencies=[
        user_rate_limit("research_create_hour", limit=5, window=3600),
        user_rate_limit("research_create_day", limit=20, window=86400),
    ],
)
async def create_research(
    payload: CreateResearchIn, user: CurrentUser, session: SessionDep
) -> ResearchJobOut:
    """Create a job and run the graph until it hits the HITL approval gate."""
    # Capture before any commit: a rollback expires the ORM object, and reading
    # `user.id` afterwards would attempt lazy IO outside the greenlet context.
    user_id = user.id
    job = ResearchJob(
        user_id=user_id,
        question=payload.question,
        thread_id=str(uuid.uuid4()),
        status="planning",
    )
    session.add(job)
    try:
        await session.commit()
    except IntegrityError as exc:
        # The partial unique index (0006) is the real guard: two requests in two
        # workers race past any Python check, but only one INSERT survives.
        await session.rollback()
        existing = await session.scalar(
            select(ResearchJob.id).where(
                ResearchJob.user_id == user_id,
                ResearchJob.status.in_(("planning", "researching")),
            )
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"An active research job already exists ({existing}); "
            "finish or abandon it before starting another",
        ) from exc
    await session.refresh(job)

    graph = get_graph()
    try:
        result = await graph.ainvoke(
            {"job_id": str(job.id), "user_id": str(user_id), "question": job.question,
             "thread_id": job.thread_id},
            config=_cfg(job.thread_id),
        )
    except Exception as exc:
        job.status = "failed"
        job.error = f"Planner failed: {str(exc)[:300]}"
        await session.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, job.error)

    # Graph paused at interrupt() -> result carries __interrupt__
    interrupts = result.get("__interrupt__") or ()
    if not interrupts:
        job.status = "failed"
        job.error = "Graph did not pause for approval"
        await session.commit()
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, job.error)

    job.plan = result["plan"]
    job.status = "awaiting_approval"
    await session.commit()
    await session.refresh(job)
    return _to_out(job)


@router.get("", response_model=list[ResearchJobOut])
async def list_research(
    user: CurrentUser,
    session: SessionDep,
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
) -> list[ResearchJobOut]:
    """Newest first, owner-scoped: the UI's job list and (later) the report library."""
    jobs = (
        (
            await session.execute(
                select(ResearchJob)
                .where(ResearchJob.user_id == user.id)
                .order_by(ResearchJob.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return [_to_out(job) for job in jobs]


@router.get("/{job_id}", response_model=ResearchJobOut)
async def get_research(job_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> ResearchJobOut:
    job = await session.scalar(
        select(ResearchJob).where(ResearchJob.id == job_id, ResearchJob.user_id == user.id)
    )
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return _to_out(job)


@router.post(
    "/{job_id}/plan/approve",
    response_model=ResearchJobOut,
    dependencies=[user_rate_limit("research_approve", limit=20, window=3600)],
)
async def decide_plan(
    job_id: uuid.UUID, payload: PlanDecisionIn, user: CurrentUser, session: SessionDep
) -> ResearchJobOut:
    """Approve -> start research in the background and return `researching` immediately.

    Reject stays synchronous: it only updates state and ends the graph, so there
    is nothing to wait for.
    """
    # FOR UPDATE serialises two concurrent approvals: the second waits, then
    # sees `researching` and gets a 409 instead of spawning a second run.
    job = await session.scalar(
        select(ResearchJob)
        .where(ResearchJob.id == job_id, ResearchJob.user_id == user.id)
        .with_for_update()
    )
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    if job.status == "researching" or is_running(job.id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Research is already running for this job")
    if job.status != "awaiting_approval":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Job is '{job.status}', not awaiting approval")

    resume = {
        "action": payload.action,
        "plan": payload.plan.model_dump() if payload.plan else job.plan,
    }

    if payload.action == "reject":
        try:
            result = await get_graph().ainvoke(Command(resume=resume), config=_cfg(job.thread_id))
        except Exception as exc:
            job.status = "failed"
            job.error = f"Resume failed: {str(exc)[:300]}"
            await session.commit()
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, job.error)

        job.status = "rejected"
        job.plan = result.get("plan") or job.plan
        await session.commit()
        await session.refresh(job)
        return _to_out(job)

    # ── approve: hand the minutes-long work to a background task ──────────
    if payload.plan:
        job.plan = payload.plan.model_dump()
    job.status = "researching"
    job.error = None
    await session.commit()
    spawn_research(job.id, resume)
    await session.refresh(job)
    return _to_out(job)
