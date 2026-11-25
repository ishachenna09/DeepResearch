import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ResearchJob(Base):
    __tablename__ = "research_jobs"

    # One *active* job per user, enforced by Postgres rather than by a Python
    # check that two workers can both pass. `awaiting_approval` is deliberately
    # outside the predicate: a user may plan another question while one sits at
    # the human gate.
    __table_args__ = (
        Index(
            "uq_research_jobs_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("status IN ('planning', 'researching')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    question: Mapped[str] = mapped_column(Text)
    plan: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # planning | awaiting_approval | researching | notes_ready | approved | rejected | failed
    status: Mapped[str] = mapped_column(String(30), default="planning")
    thread_id: Mapped[str] = mapped_column(String(64), unique=True)
    # Phase 3 outcomes, written by the background runner
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sources: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    notes_count: Mapped[int] = mapped_column(Integer, default=0)
    # Phase 4: bounded loop accounting
    rounds: Mapped[int] = mapped_column(Integer, default=1)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    critique: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)