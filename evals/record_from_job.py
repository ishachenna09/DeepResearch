"""Record a finished job + report from the dev database as an eval fixture.

Why read the DB instead of calling the API?
The fixture needs the **registry** the writer was given, so the grader can
re-run the exact production audit. The API only returns the citations that were
used, not the numbered list of 30 sources - so the registry is rebuilt from the
stored notes with the same `build_registry()` the writer uses.

Usage (from the repo root):
    .\\.venv\\Scripts\\python.exe evals\\record_from_job.py --job-id <guid> --case-id vector-db-choice
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

from sqlalchemy import select  # noqa: E402

from app.agents.nodes.writer import build_registry  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models.report import Report  # noqa: E402
from app.models.research import ResearchJob  # noqa: E402
from app.models.source import SourceNote  # noqa: E402


async def record(job_id: uuid.UUID, case_id: str, out: Path) -> dict:
    async with SessionLocal() as session:
        job = await session.get(ResearchJob, job_id)
        if job is None:
            raise SystemExit(f"job {job_id} not found")
        report = (
            await session.execute(select(Report).where(Report.job_id == job.id))
        ).scalar_one_or_none()
        if report is None or report.status != "ready":
            raise SystemExit(f"job {job.id} has no ready report")
        notes = (
            (
                await session.execute(
                    select(SourceNote)
                    .where(SourceNote.job_id == job.id)
                    .order_by(SourceNote.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        registry = build_registry(notes)

        fixture = {
            "case_id": case_id,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "question": job.question,
            "audit": "registry",
            "job": {
                "job_id": str(job.id),
                "status": job.status,
                "notes_count": job.notes_count,
                "rounds": job.rounds,
                "tokens_used": job.tokens_used,
                "plan": job.plan,
                "critique": job.critique,
            },
            "report": {
                "report_id": str(report.id),
                "status": report.status,
                "markdown": report.markdown,
                "citations": report.citations or [],
                "validation": report.validation or {},
                "tokens_used": report.tokens_used,
                "model": report.model,
                "error": report.error,
            },
            "registry": registry,
            "notes_total": len(notes),
        }

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(fixture, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return fixture


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--out-dir", default=str(ROOT / "evals" / "fixtures"))
    args = parser.parse_args()

    out = Path(args.out_dir) / f"{args.case_id}.json"
    fixture = asyncio.run(record(uuid.UUID(args.job_id), args.case_id, out))
    print(
        f"wrote {out}  "
        f"({len(fixture['registry'])} sources, {len(fixture['report']['markdown'])} chars, "
        f"{fixture['job']['notes_count']} notes)"
    )


if __name__ == "__main__":
    main()
