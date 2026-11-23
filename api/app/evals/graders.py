"""Deterministic eval graders for the research + report pipeline.

The metrics here are the same arithmetic the product already computes
(`app.agents.nodes.writer.validate`), applied to recorded or live artifacts.
No LLM judge: a judge costs a call per case, drifts run to run, and cannot be
the reason CI turns red. Qualitative review happens in Langfuse, by a human,
on the traces this gate points at.

Two artifact shapes are accepted:

- `audit="registry"`: the fixture carries the numbered registry the writer was
  given, so the grader re-runs `validate()` and compares it with the audit the
  run stored. A tampered or stale `validation` object fails the gate.
- `audit="stored"`: a live run only returns the stored audit (plus citations),
  so the grader re-parses markers and checks traceability, but trusts the
  stored precision/coverage. The artifact says so instead of pretending.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from app.agents.nodes.writer import parse_markers, validate

# A marker with no registry entry is how a model invents a source; two
# consecutive markers are fine (`[1][2]`).
_MARKER = re.compile(r"\[\d+\]")


class EvalCase(BaseModel):
    """One golden question and the minimum quality it must reach."""

    id: str
    question: str
    min_markers: int = 8
    min_precision: float = 0.90
    min_coverage: float = 0.50
    min_words: int = 200
    min_domains: int = 3
    min_notes: int = 10
    min_tokens: int = 1
    allowed_stop_reasons: list[str] = Field(
        default_factory=lambda: ["sufficient", "no_gaps", "max_rounds", "budget"]
    )
    require_ready: bool = True
    forbid_truncated: bool = True
    forbid_uncited: bool = True
    require_audit_match: bool = True


@dataclass
class Check:
    name: str
    ok: bool
    value: object
    expected: object
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "value": self.value,
            "expected": self.expected,
            "detail": self.detail,
        }


@dataclass
class GradeResult:
    case_id: str
    passed: bool
    checks: list[Check] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    @property
    def failed(self) -> list[str]:
        return [c.name for c in self.checks if not c.ok]

    def as_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "failed": self.failed,
            "checks": [c.as_dict() for c in self.checks],
            "metrics": self.metrics,
        }


def _host(url: str | None) -> str:
    if not url:
        return ""
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _stored(report: dict) -> dict:
    return report.get("validation") or {}


def _metrics(artifact: dict) -> tuple[dict, list[Check]]:
    """Recompute what we can; return the audit plus the audit-integrity checks."""
    report = artifact.get("report") or {}
    stored = _stored(report)
    markdown = report.get("markdown") or ""
    registry = artifact.get("registry") or []
    citations = report.get("citations") or []

    markers_found = parse_markers(markdown)
    marker_set = set(markers_found)

    audit = "registry" if registry else "stored"
    checks: list[Check] = []

    if registry:
        recomputed = validate(markdown, registry)
        # The stored object also carries run facts (truncated/uncited/repaired)
        # that `validate` cannot know; keep them, replace the arithmetic.
        merged = dict(stored)
        merged.update({k: recomputed[k] for k in (
            "markers", "valid_markers", "unknown_markers", "precision",
            "sentences", "cited_sentences", "coverage", "unused_sources", "words",
        )})
        if stored:
            checks.append(Check(
                "audit_matches_recompute",
                abs(float(stored.get("precision", -1)) - recomputed["precision"]) < 1e-3
                and int(stored.get("markers", -1)) == recomputed["markers"]
                and abs(float(stored.get("coverage", -1)) - recomputed["coverage"]) < 1e-3,
                {
                    "stored": {k: stored.get(k) for k in ("markers", "precision", "coverage")},
                    "recomputed": {k: recomputed[k] for k in ("markers", "precision", "coverage")},
                },
                "stored audit equals a fresh validate() over the shipped text",
                "the validation object does not describe this markdown",
            ))
        audit_obj = merged
    else:
        audit_obj = dict(stored)
        checks.append(Check(
            "audit_cross_check",
            int(stored.get("markers", len(markers_found))) == len(markers_found),
            {"stored": stored.get("markers"), "reparsed": len(markers_found)},
            "stored marker count equals markers in the text",
            "the stored audit disagrees with the shipped markdown",
        ))

    citation_indices = {int(c.get("index", -1)) for c in citations if c.get("index") is not None}
    domains = {_host(c.get("url")) for c in citations if _host(c.get("url"))}
    traceable = bool(citations) and all(c.get("note_id") and c.get("url") for c in citations)
    exact = marker_set == citation_indices

    metrics = {
        "audit": audit,
        "markers": int(audit_obj.get("markers", 0)),
        "markers_reparsed": len(markers_found),
        "precision": float(audit_obj.get("precision", 0.0)),
        "coverage": float(audit_obj.get("coverage", 0.0)),
        "unknown_markers": list(audit_obj.get("unknown_markers") or []),
        "words": int(audit_obj.get("words", 0)),
        "sentences": int(audit_obj.get("sentences", 0)),
        "citations": len(citations),
        "domains": len(domains),
        "truncated": bool(audit_obj.get("truncated", False)),
        "uncited": bool(audit_obj.get("uncited", False)),
        "tokens": int(report.get("tokens_used") or 0),
        "model": report.get("model"),
        "traceable": traceable,
        "citations_exact": exact,
    }
    return metrics, checks


def grade_case(case: EvalCase | dict, artifact: dict) -> GradeResult:
    """Apply every threshold in the case to one recorded/live artifact."""
    spec = case if isinstance(case, EvalCase) else EvalCase.model_validate(case)
    report = artifact.get("report") or {}
    job = artifact.get("job") or {}
    critique = job.get("critique") or {}
    metrics, checks = _metrics(artifact)

    checks.append(Check(
        "report_ready",
        report.get("status") == "ready",
        report.get("status"),
        "ready",
        "a failed or generating report is not evidence",
    ))
    checks.append(Check(
        "notes",
        int(job.get("notes_count") or 0) >= spec.min_notes,
        job.get("notes_count"),
        f">= {spec.min_notes}",
    ))
    checks.append(Check(
        "markers",
        metrics["markers"] >= spec.min_markers,
        metrics["markers"],
        f">= {spec.min_markers}",
    ))
    checks.append(Check(
        "precision",
        metrics["precision"] >= spec.min_precision,
        metrics["precision"],
        f">= {spec.min_precision}",
    ))
    checks.append(Check(
        "no_unknown_markers",
        not metrics["unknown_markers"],
        metrics["unknown_markers"],
        "[]",
        "an unknown marker is an invented source",
    ))
    checks.append(Check(
        "coverage",
        metrics["coverage"] >= spec.min_coverage,
        metrics["coverage"],
        f">= {spec.min_coverage}",
    ))
    checks.append(Check(
        "words",
        metrics["words"] >= spec.min_words,
        metrics["words"],
        f">= {spec.min_words}",
    ))
    checks.append(Check(
        "source_domains",
        metrics["domains"] >= spec.min_domains,
        metrics["domains"],
        f">= {spec.min_domains}",
        "one site's framing is not a literature review",
    ))
    checks.append(Check(
        "traceable",
        metrics["traceable"],
        metrics["traceable"],
        "every citation has url + note_id",
    ))
    checks.append(Check(
        "citations_exact",
        metrics["citations_exact"],
        metrics["citations_exact"],
        "markers in text == citation indices",
    ))
    checks.append(Check(
        "not_truncated",
        not (spec.forbid_truncated and metrics["truncated"]),
        metrics["truncated"],
        False,
    ))
    checks.append(Check(
        "not_uncited",
        not (spec.forbid_uncited and metrics["uncited"]),
        metrics["uncited"],
        False,
    ))
    checks.append(Check(
        "tokens_recorded",
        metrics["tokens"] >= spec.min_tokens,
        metrics["tokens"],
        f">= {spec.min_tokens}",
        "an untracked run cannot be costed or compared",
    ))
    checks.append(Check(
        "model_recorded",
        bool(metrics["model"]),
        metrics["model"],
        "non-empty",
        "the upstream that answered must be visible",
    ))

    stop_reason = critique.get("stop_reason")
    checks.append(Check(
        "stop_reason",
        stop_reason in spec.allowed_stop_reasons,
        stop_reason,
        spec.allowed_stop_reasons,
        "critic_error / no_notes mean the output is not a completed run",
    ))

    if spec.require_audit_match and metrics["audit"] == "registry":
        # Already appended by _metrics; nothing more to do. If it was absent
        # (no stored validation) the grader cannot verify a claim - fail.
        has_audit_check = any(c.name == "audit_matches_recompute" for c in checks)
        if not has_audit_check:
            checks.append(Check(
                "audit_matches_recompute", False, None, "stored validation",
                "fixture has a registry but no stored validation",
            ))

    return GradeResult(
        case_id=spec.id,
        passed=all(c.ok for c in checks),
        checks=checks,
        metrics=metrics,
    )


def strip_markers(markdown: str) -> str:
    """Negative control: same prose, no citations. The gate must reject it."""
    return _MARKER.sub("", markdown)


def negative_controls(case: EvalCase | dict, artifact: dict) -> list[dict]:
    """Deliberately break an artifact two ways; both must fail the gate.

    A gate that has never rejected anything is decoration. These controls run
    on every eval invocation (and in CI), so a refactor that accidentally stops
    checking a metric is caught by the gate itself.
    """
    spec = case if isinstance(case, EvalCase) else EvalCase.model_validate(case)
    results: list[dict] = []

    stripped = copy.deepcopy(artifact)
    stripped.setdefault("report", {})["markdown"] = strip_markers(
        stripped["report"].get("markdown") or ""
    )
    grade = grade_case(spec, stripped)
    results.append({
        "control": "markers_stripped",
        "rejected": not grade.passed,
        "failed_checks": grade.failed,
    })

    invented = copy.deepcopy(artifact)
    invented.setdefault("report", {})["markdown"] = (
        (invented["report"].get("markdown") or "") + "\n[99]"
    )
    grade = grade_case(spec, invented)
    results.append({
        "control": "invented_marker",
        "rejected": not grade.passed,
        "failed_checks": grade.failed,
    })
    return results
