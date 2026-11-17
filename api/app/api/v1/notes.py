"""Read + semantic-search the notes extracted by a research run."""
import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.core.deps import CurrentUser, SessionDep
from app.core.embeddings import EmbeddingError, embed_texts
from app.models.research import ResearchJob
from app.models.source import SourceNote
from app.schemas.notes import NoteHit, NoteSearchIn, NoteSearchOut, NotesOut

router = APIRouter(prefix="/research", tags=["notes"])


async def _owned_job(job_id: uuid.UUID, user, session) -> ResearchJob:
    job = await session.scalar(
        select(ResearchJob).where(ResearchJob.id == job_id, ResearchJob.user_id == user.id)
    )
    if job is None:
        # 404, not 403: never confirm that someone else's job id exists.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return job


@router.get("/{job_id}/notes", response_model=NotesOut)
async def list_notes(
    job_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> NotesOut:
    job = await _owned_job(job_id, user, session)
    total = await session.scalar(
        select(func.count()).select_from(SourceNote).where(SourceNote.job_id == job.id)
    )
    notes = (
        (
            await session.execute(
                select(SourceNote)
                .where(SourceNote.job_id == job.id)
                .order_by(SourceNote.sub_question, SourceNote.created_at)
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return NotesOut(job_id=job.id, status=job.status, count=int(total or 0), notes=notes)


@router.post("/{job_id}/notes/search", response_model=NoteSearchOut)
async def search_notes(
    job_id: uuid.UUID, payload: NoteSearchIn, user: CurrentUser, session: SessionDep
) -> NoteSearchOut:
    """kNN over the job's notes: embed the query, rank by cosine distance.

    `ORDER BY embedding <=> :q` is served by the HNSW index, so this stays fast
    as the corpus grows (it is exact enough at 1536 dims for a single job).
    """
    job = await _owned_job(job_id, user, session)

    try:
        vector = (await embed_texts([payload.query]))[0]
    except EmbeddingError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Embedding provider unavailable: {exc}"
        ) from exc

    distance = SourceNote.embedding.cosine_distance(vector)
    rows = (
        await session.execute(
            select(SourceNote, (1 - distance).label("score"))
            .where(SourceNote.job_id == job.id)
            .order_by(distance)
            .limit(payload.top_k)
        )
    ).all()

    hits = [
        NoteHit(
            id=note.id,
            sub_question=note.sub_question,
            url=note.url,
            title=note.title,
            note=note.note,
            quote=note.quote,
            confidence=note.confidence,
            signals=note.signals,
            created_at=note.created_at,
            score=round(float(score), 4),
        )
        for note, score in rows
    ]
    return NoteSearchOut(job_id=job.id, query=payload.query, count=len(hits), results=hits)
