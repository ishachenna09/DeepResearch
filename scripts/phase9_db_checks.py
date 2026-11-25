"""Database-level Phase 9 checks, used by scripts/verify_phase9.ps1.

Two jobs:
1. Prove the partial unique index really prevents a second active job, using a
   throwaway user so no real account is touched.
2. Print a ready report/job owned by *another* user, so the PowerShell script
   can exercise the IDOR path over HTTP.

Output is one JSON object on stdout. Exit 0 = index verified.

Run from the repo root:
    $env:PYTHONPATH = "api"
    ..\\.venv\\Scripts\\python.exe scripts\\phase9_db_checks.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models.report import Report
from app.models.research import ResearchJob
from app.models.user import User

INDEX_NAME = "uq_research_jobs_active_per_user"


async def check_index() -> None:
    async with SessionLocal() as session:
        index_name = await session.scalar(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'research_jobs' AND indexname = :name"
            ),
            {"name": INDEX_NAME},
        )
        if index_name != INDEX_NAME:
            raise RuntimeError(f"index {INDEX_NAME} is missing")

        probe = User(
            email=f"phase9-probe-{uuid.uuid4().hex[:10]}@example.test",
            full_name="Phase 9 index probe",
            hashed_password="not-a-real-hash",
            role="agent",
        )
        session.add(probe)
        await session.commit()
        probe_id = probe.id

        try:
            first = ResearchJob(
                user_id=probe_id,
                question="[phase9-index-probe]",
                thread_id=str(uuid.uuid4()),
                status="planning",
            )
            session.add(first)
            await session.commit()

            session.add(
                ResearchJob(
                    user_id=probe_id,
                    question="[phase9-index-probe]",
                    thread_id=str(uuid.uuid4()),
                    status="researching",
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
            else:
                raise RuntimeError("second active job was accepted")

            # A finished job must free the slot.
            first.status = "notes_ready"
            await session.commit()
            session.add(
                ResearchJob(
                    user_id=probe_id,
                    question="[phase9-index-probe]",
                    thread_id=str(uuid.uuid4()),
                    status="planning",
                )
            )
            await session.commit()
        finally:
            # CASCADE removes the probe's jobs with the user.
            await session.execute(delete(User).where(User.id == probe_id))
            await session.commit()


async def find_other_users_report(login_email: str) -> tuple[str | None, str | None]:
    async with SessionLocal() as session:
        login_user = await session.scalar(select(User).where(User.email == login_email))
        login_id = login_user.id if login_user else None
        query = (
            select(Report.id, Report.job_id)
            .where(Report.status == "ready")
            .order_by(Report.created_at.desc())
            .limit(1)
        )
        if login_id is not None:
            query = query.where(Report.user_id != login_id)
        row = (await session.execute(query)).first()
        if row is None:
            return None, None
        return str(row[0]), str(row[1])


async def main(email: str) -> int:
    try:
        await check_index()
    except Exception as exc:  # noqa: BLE001 - one clear failure for the script
        print(json.dumps({"index": "failed", "error": str(exc)}))
        return 1

    other_report, other_job = await find_other_users_report(email)
    print(
        json.dumps(
            {
                "index": "ok",
                "other_report_id": other_report,
                "other_job_id": other_job,
            }
        )
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", default="you@test.com", help="the user the verify script logs in as")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.email)))
