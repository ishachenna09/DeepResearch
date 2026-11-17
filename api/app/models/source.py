"""pgvector-backed research notes.

One row = one atomic claim extracted from one page, plus its embedding.

Why notes and not whole pages?
- A 12 KB page has a handful of relevant sentences. Storing page-level vectors
  makes retrieval return a wall of text that still has to be searched again;
  storing claim-level vectors makes semantic search return the sentence that
  answers the question.
- The note carries `url` + `quote`, so Phase 4/5 can cite precisely and verify
  that the quote exists on the page.

Why store the embedding in Postgres instead of the graph state?
Checkpoint state lives in Redis Cloud (30 MB). A 1536-dim vector serialised as
JSON is ~20 KB; 20 notes per job would add 400 KB to every checkpoint copy,
including the pre-approval ones. Postgres is the right home for vectors - the
graph state only carries small note dicts.

Why a dedupe unique constraint?
LangGraph may re-run a node (retry, resume, user re-approves). Writes use
ON CONFLICT DO NOTHING against (job_id, url_hash, note_hash), so a replay is
idempotent instead of doubling the corpus.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


class SourceNote(Base):
    __tablename__ = "source_notes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("research_jobs.id", ondelete="CASCADE"), index=True
    )
    sub_question: Mapped[str] = mapped_column(Text)

    # provenance
    url: Mapped[str] = mapped_column(Text)
    url_hash: Mapped[str] = mapped_column(String(64))   # hashed: URLs can exceed btree limits
    title: Mapped[str | None] = mapped_column(Text, nullable=True)

    # content
    note: Mapped[str] = mapped_column(Text)             # the claim, in our words
    quote: Mapped[str | None] = mapped_column(Text, nullable=True)  # verbatim support
    confidence: Mapped[str] = mapped_column(String(10), default="medium")
    note_hash: Mapped[str] = mapped_column(String(64))  # dedupe on replays
    signals: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # guardrail flags

    embedding: Mapped[list[float]] = mapped_column(Vector(settings.EMBED_DIM))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("job_id", "url_hash", "note_hash", name="uq_source_notes_dedupe"),
        # HNSW + cosine: best recall/latency tradeoff at this size, and cosine
        # matches how the embedding model was trained (semantic similarity).
        # Declared here so Alembic autogenerate does not try to drop it.
        Index(
            "ix_source_notes_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
