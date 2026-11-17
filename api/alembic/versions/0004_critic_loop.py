"""phase 4: bounded gap-filling loop accounting

Revision ID: 0004_critic_loop
Revises: 0003_source_notes
Create Date: 2026-11-17

Adds what the UI and evals need to explain *why* a run stopped:
- rounds:       how many research rounds ran (1 = no gap-filling happened)
- tokens_used:  measured provider usage, not an estimate
- critique:     the last critic verdict ({sufficient, reason, stop_reason, ...})
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_critic_loop"
down_revision: str | None = "0003_source_notes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "research_jobs",
        sa.Column("rounds", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "research_jobs",
        sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "research_jobs",
        sa.Column("critique", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("research_jobs", "critique")
    op.drop_column("research_jobs", "tokens_used")
    op.drop_column("research_jobs", "rounds")
