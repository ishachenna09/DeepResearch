"""Prove a session's spans reached Langfuse (Phase 7 verification).

`/health/langfuse` answers "are the keys valid?". This answers the next
question: "did the report I just generated actually land as spans?" It queries
the observations API by `session_id`, which is the job id our report wrapper
sets. Exits 0 only when at least one observation is found.

Usage:
    .\\.venv\\Scripts\\python.exe scripts\\check_traces.py <session-id> [--wait 30]

Run from the repo root (or set PYTHONPATH=api).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

from app.core.observability import get_langfuse  # noqa: E402


def fetch(session_id: str) -> list:
    client = get_langfuse()
    if client is None:
        raise SystemExit("Langfuse is disabled (no keys) - nothing to check")
    response = client.api.observations.get_many(session_id=session_id, limit=20)
    return list(getattr(response, "data", []) or [])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("session_id")
    parser.add_argument("--wait", type=int, default=30, help="seconds to wait for export")
    args = parser.parse_args()

    deadline = time.monotonic() + args.wait
    rows: list = []
    while time.monotonic() < deadline:
        rows = fetch(args.session_id)
        if rows:
            break
        time.sleep(3)
    if not rows:
        raise SystemExit(
            f"no Langfuse observations for session {args.session_id} after {args.wait}s"
        )

    names = sorted({getattr(row, "name", None) or "?" for row in rows})
    print(f"{len(rows)} observation(s) for session {args.session_id}")
    for name in names[:10]:
        print(f"  - {name}")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
