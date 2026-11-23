"""Phase 7 eval gate: score the pipeline against a golden set and say yes/no.

Two modes, on purpose:

- `fixtures` (default, what CI runs): grade artifacts recorded from real runs.
  No database, no provider, no API key - so a pull request can be gated in ~1 s.
- `live`: drive a running API end to end (create -> approve -> wait -> report)
  and grade the fresh result. Expensive (minutes, provider tokens), so it is a
  manual command, never a CI step. `--record` turns a live pass into a fixture.

The gate fails if any case fails, if fewer than `--min-cases` were graded, or if
the negative controls pass (a gate that cannot reject is not a gate).

Usage (repo root):
    .\\.venv\\Scripts\\python.exe evals\\run_evals.py
    .\\.venv\\Scripts\\python.exe evals\\run_evals.py --mode live --case llm-failover-design
    .\\.venv\\Scripts\\python.exe evals\\run_evals.py --mode live --record
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

from app.core.pricing import estimate_usd, pricing_metadata  # noqa: E402
from app.evals.graders import (  # noqa: E402
    EvalCase,
    grade_case,
    negative_controls,
)

CASES = ROOT / "evals" / "golden" / "cases.jsonl"
FIXTURES = ROOT / "evals" / "fixtures"
RESULTS = ROOT / "evals" / "results"


def load_cases(path: Path) -> dict[str, EvalCase]:
    cases: dict[str, EvalCase] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            case = EvalCase.model_validate(json.loads(line))
        except Exception as exc:  # noqa: BLE001 - a broken golden file must be loud
            raise SystemExit(f"{path}:{number}: {exc}") from exc
        if case.id in cases:
            raise SystemExit(f"{path}:{number}: duplicate case id {case.id!r}")
        cases[case.id] = case
    return cases


def load_fixtures(path: Path) -> dict[str, dict]:
    fixtures: dict[str, dict] = {}
    for file in sorted(path.glob("*.json")):
        body = json.loads(file.read_text(encoding="utf-8"))
        case_id = body.get("case_id") or file.stem
        if case_id in fixtures:
            raise SystemExit(f"two fixtures for case {case_id!r} ({file})")
        body.setdefault("case_id", case_id)
        fixtures[case_id] = body
    return fixtures


def minutes(seconds: float) -> str:
    return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"


def _get_json(client: httpx.Client, url: str, **kwargs) -> dict:
    response = client.get(url, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"GET {url} -> {response.status_code}: {response.text[:300]}")
    return response.json()


def _post_json(client: httpx.Client, url: str, body: dict | None = None) -> dict:
    response = client.post(url, json=body or {})
    if response.status_code >= 400:
        raise RuntimeError(f"POST {url} -> {response.status_code}: {response.text[:300]}")
    return response.json() if response.content else {}


def run_live_case(
    client: httpx.Client,
    case: EvalCase,
    *,
    timeout: int = 420,
    poll: int = 4,
) -> dict:
    """One full job through the HTTP API. Returns an artifact shaped like a fixture."""
    job = _post_json(client, "/research", {"question": case.question})
    job_id = job["job_id"]
    print(f"  created {job_id} ({job.get('status')})")

    if job.get("status") == "awaiting_approval":
        job = _post_json(client, f"/research/{job_id}/plan/approve", {"action": "approve"})
        print(f"  approved -> {job.get('status')}")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(poll)
        job = _get_json(client, f"/research/{job_id}")
        if job.get("status") in {"notes_ready", "failed", "rejected"}:
            break
        print(f"  {job.get('status')}: {job.get('notes_count', 0)} notes")
    if job.get("status") != "notes_ready":
        raise RuntimeError(f"research ended as {job.get('status')}: {job.get('error')}")

    _post_json(client, f"/research/{job_id}/report")
    while time.monotonic() < deadline:
        report = _get_json(client, f"/research/{job_id}/report")
        if report.get("status") in {"ready", "failed"}:
            break
        time.sleep(2)
    if report.get("status") != "ready":
        raise RuntimeError(f"report ended as {report.get('status')}: {report.get('error')}")

    return {
        "case_id": case.id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "question": case.question,
        "audit": "stored",
        "job": {
            "job_id": job_id,
            "status": job.get("status"),
            "notes_count": job.get("notes_count", 0),
            "rounds": job.get("rounds"),
            "tokens_used": job.get("tokens_used", 0),
            "plan": job.get("plan"),
            "critique": job.get("critique"),
        },
        "report": {
            "report_id": report.get("report_id"),
            "status": report.get("status"),
            "markdown": report.get("markdown", ""),
            "citations": report.get("citations", []),
            "validation": report.get("validation") or {},
            "tokens_used": report.get("tokens_used", 0),
            "model": report.get("model"),
            "error": report.get("error"),
        },
        "notes_total": job.get("notes_count", 0),
    }


def print_table(results: list[dict], controls: list[dict]) -> None:
    header = (
        f"{'case':24s} {'audit':8s} {'markers':>7s} {'prec':>6s} {'cov':>6s} "
        f"{'domain':>6s} {'notes':>5s} {'tokens':>7s}  gate"
    )
    print(header)
    print("-" * len(header))
    for result in results:
        m = result["metrics"]
        gate = "PASS" if result["passed"] else "FAIL"
        print(
            f"{result['case_id']:24s} {m['audit']:8s} {m['markers']:>7d} "
            f"{m['precision']:>6.2f} {m['coverage']:>6.2f} {m['domains']:>6d} "
            f"{m.get('notes', 0):>5d} {m['tokens']:>7d}  {gate}"
        )
        if not result["passed"]:
            print(f"    failed: {', '.join(result['failed'])}")
    print("-" * len(header))
    for control in controls:
        state = "rejected (good)" if control["rejected"] else "ACCEPTED (gate is broken)"
        print(f"negative control {control['control']:18s} -> {state}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fixtures", "live"), default="fixtures")
    parser.add_argument("--case", action="append", default=[], help="only this case id (repeatable)")
    parser.add_argument("--cases", default=str(CASES))
    parser.add_argument("--fixtures", default=str(FIXTURES))
    parser.add_argument("--json-out", default=str(RESULTS / "latest.json"))
    parser.add_argument("--min-cases", type=int, default=2)
    parser.add_argument("--base-url", default="http://localhost:8001/api/v1")
    parser.add_argument("--email", default="you@test.com")
    parser.add_argument("--password", default="password123")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=420, help="seconds per live case")
    parser.add_argument("--record", action="store_true", help="live mode: write a fixture")
    args = parser.parse_args()

    cases = load_cases(Path(args.cases))
    if args.case:
        missing = [c for c in args.case if c not in cases]
        if missing:
            raise SystemExit(f"unknown case id(s): {', '.join(missing)}")
        cases = {k: v for k, v in cases.items() if k in set(args.case)}

    artifacts: list[dict] = []

    if args.mode == "fixtures":
        fixtures = load_fixtures(Path(args.fixtures))
        unknown = [c for c in fixtures if c not in cases]
        if unknown:
            raise SystemExit(f"fixtures for unknown cases: {', '.join(unknown)}")
        artifacts = [fixtures[c] for c in cases if c in fixtures]
        if args.limit:
            artifacts = artifacts[: args.limit]
        if not artifacts:
            raise SystemExit(f"no fixtures in {args.fixtures} match {args.cases}")
    else:
        with httpx.Client(base_url=args.base_url, timeout=30) as client:
            login = _post_json(client, "/auth/login", {"email": args.email, "password": args.password})
            client.headers["Authorization"] = f"Bearer {login['access_token']}"
            selected = list(cases.values())
            if args.limit:
                selected = selected[: args.limit]
            for case in selected:
                print(f"live: {case.id}")
                artifact = run_live_case(client, case, timeout=args.timeout)
                artifacts.append(artifact)
                if args.record:
                    out = Path(args.fixtures) / f"{case.id}.json"
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_text(json.dumps(artifact, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
                    print(f"  recorded {out}")

    results: list[dict] = []
    controls: list[dict] = []
    for artifact in artifacts:
        case = cases[artifact["case_id"]]
        grade = grade_case(case, artifact)
        body = grade.as_dict()
        body["metrics"]["notes"] = int((artifact.get("job") or {}).get("notes_count") or 0)
        results.append(body)
        controls.extend(negative_controls(case, artifact))

    print_table(results, controls)

    tokens = sum(r["metrics"]["tokens"] for r in results)
    models = sorted({r["metrics"]["model"] or "" for r in results if r["metrics"]["model"]})
    gate = (
        bool(results)
        and len(results) >= args.min_cases
        and all(r["passed"] for r in results)
        and all(c["rejected"] for c in controls)
    )

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode,
        "gate": {"passed": gate, "min_cases": args.min_cases, "graded": len(results)},
        "cases": results,
        "negative_controls": controls,
        "totals": {
            "tokens": tokens,
            "estimated_usd": round(sum(estimate_usd(r["metrics"]["tokens"], r["metrics"]["model"]) for r in results), 6),
            "models": models,
        },
        "pricing": pricing_metadata(),
    }

    out = Path(args.json_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"\n{len(results)} case(s), {tokens:,} tokens, "
        f"{'GATE PASSED' if gate else 'GATE FAILED'} -> {out}"
    )
    raise SystemExit(0 if gate else 1)


if __name__ == "__main__":
    main()
