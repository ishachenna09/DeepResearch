"""Deterministic tests for the Phase 7 eval gate.

The eval gate is only useful if it can say no. These tests build a passing
artifact, then break it one way at a time, and assert the gate rejects each
break. The negative controls used by `evals/run_evals.py` are covered too.

Run from api/:
    ..\\.venv\\Scripts\\python.exe -m pytest tests -q
"""
from __future__ import annotations

import copy

from app.agents.nodes.writer import citations_for, validate
from app.evals.graders import EvalCase, grade_case, negative_controls

MARKDOWN = (
    "## Answer\n"
    "Alpha is the cheaper option for small teams [1]. "
    "Beta wins on managed operations at scale [2]. "
    "Gamma is the middle ground when latency matters [1]."
)

REGISTRY = [
    {
        "index": 1,
        "note_id": "n1",
        "url": "https://alpha.example/post",
        "title": None,
        "claim": "Alpha is cheaper for small teams.",
        "quote": None,
        "confidence": "high",
        "sub_question": "Which is cheaper?",
    },
    {
        "index": 2,
        "note_id": "n2",
        "url": "https://beta.example/post",
        "title": "Beta",
        "claim": "Beta wins on managed operations.",
        "quote": "managed operations",
        "confidence": "medium",
        "sub_question": "Which is managed?",
    },
]


def artifact(**overrides) -> dict:
    registry = copy.deepcopy(REGISTRY)
    validation = validate(MARKDOWN, registry)
    report = {
        "status": "ready",
        "markdown": MARKDOWN,
        "citations": citations_for(MARKDOWN, registry),
        "validation": validation,
        "tokens_used": 1234,
        "model": "support-chat-lite",
    }
    job = {
        "status": "notes_ready",
        "notes_count": 20,
        "critique": {"stop_reason": "sufficient"},
    }
    body = {"job": job, "report": report, "registry": registry}
    body.update(overrides)
    return body


def case(**overrides) -> EvalCase:
    base = dict(
        id="t",
        question="Which option?",
        min_markers=2,
        min_precision=0.9,
        min_coverage=0.5,
        min_words=10,
        min_domains=2,
        min_notes=5,
        min_tokens=1,
    )
    base.update(overrides)
    return EvalCase(**base)


def failed(result) -> set[str]:
    return set(result.failed)


def test_good_artifact_passes_every_check():
    result = grade_case(case(), artifact())
    assert result.passed, result.as_dict()
    assert result.metrics["audit"] == "registry"
    assert result.metrics["citations_exact"] is True
    assert result.metrics["domains"] == 2


def test_stripped_markers_fail_the_gate():
    body = artifact()
    body["report"]["markdown"] = body["report"]["markdown"].replace("[1]", "").replace("[2]", "")
    result = grade_case(case(), body)
    assert not result.passed
    assert {"markers", "precision", "citations_exact"} & failed(result)


def test_invented_marker_fails_precision_and_unknown_markers():
    body = artifact()
    body["report"]["markdown"] += " A made-up claim [99]."
    result = grade_case(case(), body)
    assert not result.passed
    assert "no_unknown_markers" in failed(result)
    assert "precision" in failed(result)
    assert result.metrics["unknown_markers"] == [99]


def test_tampered_validation_is_caught_by_recompute():
    body = artifact()
    # Claim perfection while the text still has an invented source.
    body["report"]["markdown"] += " Invented [99]."
    body["report"]["validation"] = {
        **validate(body["report"]["markdown"], REGISTRY),
        "precision": 1.0,
        "unknown_markers": [],
    }
    result = grade_case(case(), body)
    assert not result.passed
    assert "audit_matches_recompute" in failed(result)


def test_truncated_and_uncited_flags_fail():
    for flag in ("truncated", "uncited"):
        body = artifact()
        body["report"]["validation"] = {**body["report"]["validation"], flag: True}
        result = grade_case(case(), body)
        assert not result.passed, flag
        assert f"not_{flag}" in failed(result)


def test_critic_error_is_not_a_completed_run():
    body = artifact()
    body["job"]["critique"] = {"stop_reason": "critic_error"}
    result = grade_case(case(), body)
    assert not result.passed
    assert "stop_reason" in failed(result)


def test_one_site_is_not_a_literature_review():
    body = artifact()
    for citation in body["report"]["citations"]:
        citation["url"] = "https://same.example/x"
    body["report"]["validation"] = validate(
        body["report"]["markdown"],
        [{**r, "url": "https://same.example/x"} for r in REGISTRY],
    )
    result = grade_case(case(min_domains=2), body)
    assert not result.passed
    assert "source_domains" in failed(result)


def test_live_artifact_without_registry_cross_checks_markers():
    body = artifact()
    body.pop("registry")
    result = grade_case(case(), body)
    assert result.metrics["audit"] == "stored"
    assert result.passed

    body["report"]["validation"] = {**body["report"]["validation"], "markers": 99}
    result = grade_case(case(), body)
    assert not result.passed
    assert "audit_cross_check" in failed(result)


def test_negative_controls_are_both_rejected():
    controls = negative_controls(case(), artifact())
    assert [c["control"] for c in controls] == ["markers_stripped", "invented_marker"]
    assert all(c["rejected"] for c in controls), controls


def test_unrecorded_tokens_and_model_fail():
    body = artifact()
    body["report"]["tokens_used"] = 0
    body["report"]["model"] = None
    result = grade_case(case(), body)
    assert "tokens_recorded" in failed(result)
    assert "model_recorded" in failed(result)


def test_a_case_override_can_demand_more_sources():
    result = grade_case(case(min_domains=5), artifact())
    assert not result.passed
    assert "source_domains" in failed(result)


def test_mutating_one_artifact_does_not_leak_into_the_next():
    body = artifact()
    broken = copy.deepcopy(body)
    broken["report"]["markdown"] = ""
    grade_case(case(), broken)
    assert grade_case(case(), body).passed
