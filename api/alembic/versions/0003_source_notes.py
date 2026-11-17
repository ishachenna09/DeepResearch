"""phase 3: source_notes (pgvector) + research outcome columns

Revision ID: 0003_source_notes
Revises: afcf1c7b1860
Create Date: 2026-11-16

Notes
- `CREATE EXTENSION IF NOT EXISTS vector` must run before any `vector` type is
  used. Neon allows it on the default database; pgvector 0.8.7 is available.
- EMBED_DIM is hard-coded here on purpose: a migration must describe what was
  created, not follow today's settings. If the model changes, add a new
  migration that re-embeds, never an edit to this file.
- The HNSW index is created explicitly because it needs the `vector_cosine_ops`
  operator class, which Alembic's autogenerate cannot infer from the ORM model.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0003_source_notes"
down_revision: str | None = "afcf1c7b1860"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBED_DIM = 1536


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.add_column("research_jobs", sa.Column("error", sa.Text(), nullable=True))
    op.add_column(
        "research_jobs",
        sa.Column("sources", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "research_jobs",
        sa.Column("notes_count", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "source_notes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("sub_question", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("url_hash", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=True),
        sa.Column("confidence", sa.String(length=10), nullable=False),
        sa.Column("note_hash", sa.String(length=64), nullable=False),
        sa.Column("signals", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("embedding", Vector(EMBED_DIM), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["research_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "url_hash", "note_hash", name="uq_source_notes_dedupe"),
    )
    op.create_index(op.f("ix_source_notes_job_id"), "source_notes", ["job_id"], unique=False)
    op.create_index(
        "ix_source_notes_embedding_hnsw",
        "source_notes",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_source_notes_embedding_hnsw", table_name="source_notes")
    op.drop_index(op.f("ix_source_notes_job_id"), table_name="source_notes")
    op.drop_table("source_notes")
    op.drop_column("research_jobs", "notes_count")
    op.drop_column("research_jobs", "sources")
    op.drop_column("research_jobs", "error")
