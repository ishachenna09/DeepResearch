"""A small, dependency-free load test for the DeepResearch API (Phase 9).

Why not Locust/k6?
- Locust pulls in gevent/Flask and a web UI; k6 is a separate binary that is not
  in the repo and not in CI. The API already depends on httpx, and the scenarios
  that matter are three loops. ~200 lines are easier to read at 3 a.m. than a
  tool configuration, and they run on the same Python as the app.

Scenarios
- read:        /healthz, /research?limit=5, /reports?limit=5 (no LLM)
- export:      one ready report, alternating Markdown and PDF (CPU bound)
- login-burst: deliberately wrong passwords; proves the limiter answers 429

Run against a *private* API. For the read/export baseline, raise the limiter so
the numbers describe the app, not the limiter:

    $env:RATE_LIMIT_MULTIPLIER = "100"     # restart the API with this set
    ..\\.venv\\Scripts\\python.exe scripts\\load_test.py --scenario read --users 20 --seconds 20

Then, with normal limits, prove the burst:

    ..\\.venv\\Scripts\\python.exe scripts\\load_test.py --scenario login-burst

Exit codes: 0 = pass, 1 = 5xx/exception, 2 = 429s polluted a baseline run,
3 = setup failure (login/library).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

import httpx


@dataclass
class Sample:
    status: int
    ms: float


@dataclass
class Results:
    samples: list[Sample] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add(self, status: int, ms: float) -> None:
        self.samples.append(Sample(status, ms))

    def error(self, message: str) -> None:
        if len(self.errors) < 10:
            self.errors.append(message)


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile: no interpolation to argue about."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * len(ordered))) - 1))
    return ordered[index]


async def timed(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> Sample:
    started = time.perf_counter()
    response = await client.request(method, url, **kwargs)
    return Sample(response.status_code, (time.perf_counter() - started) * 1000)


async def login(client: httpx.AsyncClient, base: str, email: str, password: str) -> str:
    response = await client.post(
        f"{base}/auth/login", json={"email": email, "password": password}
    )
    if response.status_code != 200:
        raise RuntimeError(f"login failed: {response.status_code} {response.text[:200]}")
    return response.json()["access_token"]


async def pick_report(client: httpx.AsyncClient, base: str, headers: dict, explicit: str | None) -> str:
    if explicit:
        return explicit
    response = await client.get(f"{base}/reports", params={"limit": 20}, headers=headers)
    response.raise_for_status()
    for item in response.json()["items"]:
        if item["status"] == "ready":
            return item["report_id"]
    raise RuntimeError("no ready report for this user; pass --report-id")


async def read_worker(client, base, headers, deadline, results: Results) -> None:
    targets = ("/healthz", "/research?limit=5", "/reports?limit=5")
    index = 0
    while time.monotonic() < deadline:
        try:
            sample = await timed(client, "GET", base + targets[index % len(targets)], headers=headers)
            results.add(sample.status, sample.ms)
        except Exception as exc:  # noqa: BLE001 - report, do not crash the run
            results.error(f"{type(exc).__name__}: {exc}")
        index += 1


async def export_worker(client, base, headers, deadline, report_id, results: Results) -> None:
    index = 0
    while time.monotonic() < deadline:
        extension = "md" if index % 2 == 0 else "pdf"
        try:
            sample = await timed(
                client,
                "GET",
                f"{base}/reports/{report_id}/export.{extension}",
                headers=headers,
            )
            results.add(sample.status, sample.ms)
        except Exception as exc:  # noqa: BLE001
            results.error(f"{type(exc).__name__}: {exc}")
        index += 1


async def login_worker(client, base, deadline, results: Results) -> None:
    body = {"email": "loadtest-burst@example.com", "password": "definitely-wrong"}
    while time.monotonic() < deadline:
        try:
            sample = await timed(client, "POST", f"{base}/auth/login", json=body)
            results.add(sample.status, sample.ms)
        except Exception as exc:  # noqa: BLE001
            results.error(f"{type(exc).__name__}: {exc}")


def summarise(results: Results, scenario: str, elapsed: float) -> dict:
    statuses = Counter(sample.status for sample in results.samples)
    latencies = [sample.ms for sample in results.samples]
    summary = {
        "scenario": scenario,
        "requests": len(results.samples),
        "elapsed_s": round(elapsed, 2),
        "rps": round(len(results.samples) / elapsed, 1) if elapsed else 0,
        "status": dict(sorted(statuses.items())),
        "p50_ms": round(percentile(latencies, 0.50), 1),
        "p95_ms": round(percentile(latencies, 0.95), 1),
        "p99_ms": round(percentile(latencies, 0.99), 1),
        "max_ms": round(max(latencies), 1) if latencies else 0,
        "errors": results.errors,
    }
    if latencies:
        summary["mean_ms"] = round(statistics.fmean(latencies), 1)
    return summary


async def main(args: argparse.Namespace) -> int:
    base = args.base.rstrip("/")
    scenarios = (
        ["read", "export", "login-burst"] if args.scenario == "all" else [args.scenario]
    )
    limits = httpx.Limits(max_connections=max(8, args.users * 2))
    failures = 0

    async with httpx.AsyncClient(timeout=60, limits=limits) as client:
        headers: dict[str, str] = {}
        report_id: str | None = None
        if any(s in ("read", "export") for s in scenarios):
            if args.token:
                headers = {"Authorization": f"Bearer {args.token}"}
            else:
                try:
                    token = await login(client, base, args.email, args.password)
                    headers = {"Authorization": f"Bearer {token}"}
                except Exception as exc:  # noqa: BLE001
                    print(f"SETUP FAILED: {exc}", file=sys.stderr)
                    return 3
        if "export" in scenarios:
            try:
                report_id = await pick_report(client, base, headers, args.report_id)
            except Exception as exc:  # noqa: BLE001
                print(f"SETUP FAILED: {exc}", file=sys.stderr)
                return 3

        for scenario in scenarios:
            results = Results()
            deadline = time.monotonic() + args.seconds
            started = time.monotonic()

            if scenario == "read":
                workers = [read_worker(client, base, headers, deadline, results) for _ in range(args.users)]
            elif scenario == "export":
                workers = [
                    export_worker(client, base, headers, deadline, report_id, results)
                    for _ in range(args.users)
                ]
            else:
                workers = [login_worker(client, base, deadline, results) for _ in range(args.users)]

            await asyncio.gather(*workers)
            summary = summarise(results, scenario, time.monotonic() - started)
            print(json.dumps(summary, indent=2))

            if any(code >= 500 for code in summary["status"]):
                print(f"FAIL: {scenario} produced 5xx", file=sys.stderr)
                failures += 1
            if scenario == "login-burst":
                if summary["status"].get(429, 0) == 0:
                    print("FAIL: login-burst never hit the limiter (no 429)", file=sys.stderr)
                    failures += 1
            elif summary["status"].get(429, 0):
                print(
                    "BASELINE INVALID: 429s observed; restart the API with "
                    "RATE_LIMIT_MULTIPLIER=100 for the read/export baseline",
                    file=sys.stderr,
                )
                return 2
            if summary["errors"]:
                failures += 1

    return 0 if failures == 0 else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8001/api/v1")
    parser.add_argument("--email", default="you@test.com")
    parser.add_argument("--password", default="password123")
    parser.add_argument("--token", default=None, help="reuse an access token (skips login)")
    parser.add_argument("--users", type=int, default=8)
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--scenario", choices=["read", "export", "login-burst", "all"], default="read")
    parser.add_argument("--report-id", default=None, help="skip the library lookup")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse_args(sys.argv[1:]))))
