"""The report: one cited markdown answer per research job.

Why a separate table instead of columns on `research_jobs`?
- A report has its own lifecycle (`generating -> ready | failed`), is
  regenerable, and costs its own tokens. Folding it into the job would make
  "regenerate the report" mutate the research record.
- Phase 6 extracts the writer into an A2A service. A row keyed by `job_id` is
  the seam: the writer service reads notes by id and writes one report row, and
  the API never needs to know how it was produced.

Why a unique constraint on `job_id`?
Deliberate: one current report per job. Regenerating overwrites it (and clears
the old markdown) rather than accumulating versions the UI would have to
disambiguate. Version history is a Phase 8 feature, not a Phase 5 one.

Why `citations` + `validation` as JSONB?
They are read-only snapshots of one generation: the numbered sources the model
was allowed to cite, and the deterministic check of what it actually cited.
Recomputing them later would be a lie if the note table changed (notes can be
added by a later gap round on a re-run).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # unique: one current report per job (see module docstring)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("research_jobs.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # generating | ready | failed
    status: Mapped[str] = mapped_column(String(20), default="generating")

    markdown: Mapped[str] = mapped_column(Text, default="")
    citations: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    validation: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
