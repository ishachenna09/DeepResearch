"""phase 9: one active research job per user

Revision ID: 0006_active_job_unique
Revises: 0005_reports
Create Date: 2026-11-24

Why a partial unique index instead of a Redis lock or an application check?
- An application `SELECT` then `INSERT` is a TOCTOU race: two workers both see
  "no active job" and both insert.
- A Redis lock adds a second source of truth that can outlive the row (worker
  crash) or disappear under eviction, leaving a job that cannot be started.
- Postgres already serialises the write. A partial unique index makes "at most
  one row per user with an active status" a database invariant, and the loser
  of the race gets an IntegrityError the route turns into 409.

`awaiting_approval` is intentionally excluded: a user can plan a second
question while the first waits at the human gate.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_active_job_unique"
down_revision: str | None = "0005_reports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Pre-existing rows from before the rule: keep the newest active job per
    # user and fail the older ones, otherwise the index cannot be created on a
    # database that was already in use.
    op.execute(
        sa.text(
            """
            UPDATE research_jobs
               SET status = 'failed',
                   error = 'Superseded by the Phase 9 single-active-job rule'
             WHERE id IN (
                   SELECT id FROM (
                       SELECT id,
                              row_number() OVER (
                                  PARTITION BY user_id ORDER BY created_at DESC
                              ) AS rank
                         FROM research_jobs
                        WHERE status IN ('planning', 'researching')
                   ) ranked
                    WHERE rank > 1
             )
            """
        )
    )
    op.create_index(
        "uq_research_jobs_active_per_user",
        "research_jobs",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('planning', 'researching')"),
    )


def downgrade() -> None:
    op.drop_index("uq_research_jobs_active_per_user", table_name="research_jobs")
