"""phase 5: cited reports

Revision ID: 0005_reports
Revises: 0004_critic_loop
Create Date: 2026-11-18

One row per research job:
- markdown + citations + validation are a snapshot of one generation
- status is independent of the job (a job stays `notes_ready`; the report is
  `generating -> ready | failed`), so the report can be regenerated without
  touching the research record
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_reports"
down_revision: str | None = "0004_critic_loop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("research_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="generating"),
        sa.Column("markdown", sa.Text(), nullable=False, server_default=""),
        sa.Column("citations", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("validation", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model", sa.String(80), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # unique: one current report per job (regeneration overwrites it)
    op.create_index("ix_reports_job_id", "reports", ["job_id"], unique=True)
    op.create_index("ix_reports_user_id", "reports", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_reports_user_id", table_name="reports")
    op.drop_index("ix_reports_job_id", table_name="reports")
    op.drop_table("reports")
