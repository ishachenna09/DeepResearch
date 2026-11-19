"""Deterministic tests for the report's citation audit + registry.

The writer needs a model; the *audit* does not. That split is the point: the
number Phase 7 gates CI on (citation precision) is arithmetic over text, so it
can be tested without a provider, a network call or a stub that pretends to be
one.

Run: ..\\.venv\\Scripts\\python.exe -m pytest tests -q
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.agents.nodes.writer import (
    build_registry,
    citations_for,
    parse_markers,
    validate,
)


@dataclass
class FakeNote:
    """Only the attributes the registry reads - no DB, no ORM session."""

    id: str
    url: str
    note: str
    sub_question: str
    confidence: str = "medium"
    title: str | None = None
    quote: str | None = None
    created_at: datetime = datetime(2026, 11, 18, tzinfo=timezone.utc)


def _notes() -> list[FakeNote]:
    return [
        FakeNote("1", "https://a.example/x", "A claim about latency", "q1", "high"),
        FakeNote("2", "https://b.example/y", "A claim about cost", "q2", "high"),
        FakeNote("3", "https://c.example/z", "A second q1 claim", "q1", "low"),
        FakeNote("4", "https://d.example/w", "A q3 claim", "q3", "medium"),
    ]


# --------------------------------------------------------------- registry
def test_registry_numbers_from_one_and_is_balanced():
    registry = build_registry(_notes(), max_citations=3)
    assert [item["index"] for item in registry] == [1, 2, 3]
    # round-robin: both q1 notes must not fill the list before q2/q3 appear
    sub_questions = [item["sub_question"] for item in registry]
    assert set(sub_questions) == {"q1", "q2", "q3"}


def test_registry_prefers_high_confidence_first_within_a_group():
    registry = build_registry(_notes(), max_citations=4)
    q1 = [item for item in registry if item["sub_question"] == "q1"]
    assert [item["confidence"] for item in q1] == ["high", "low"]


def test_registry_respects_the_cap():
    registry = build_registry(_notes(), max_citations=2)
    assert len(registry) == 2


def test_registry_diversifies_sites_within_a_sub_question():
    """One site must not own the evidence base: measured 10/30 slots on one host.

    Within a sub-question the registry takes one note per host before taking a
    second note from any host, so the writer sees more than one article's view.
    """
    notes = [FakeNote(f"n{i}", "https://one.example/p", f"claim {i}", "q1") for i in range(6)]
    notes.append(FakeNote("m1", "https://two.example/p", "second site", "q1"))

    registry = build_registry(notes, max_citations=2)
    hosts = [item["url"].split("/")[2] for item in registry]
    assert hosts == ["one.example", "two.example"]


# ---------------------------------------------------------------- markers
def test_parse_markers_dedupes_and_keeps_order():
    text = "One [2], then [1], then again [2] and a pair [3, 1]."
    assert parse_markers(text) == [2, 1, 3]


def test_parse_markers_ignores_non_numeric_brackets():
    assert parse_markers("see [ref] and [abc] but not [4]") == [4]


# -------------------------------------------------------------- validation
def test_perfect_citations_score_full_precision_and_coverage():
    registry = build_registry(_notes(), max_citations=4)
    markdown = (
        "## Answer\n\n"
        "Latency matters most at scale [1].\n\n"
        "Cost is the second deciding factor [2].\n"
    )
    result = validate(markdown, registry)

    assert result["markers"] == 2
    assert result["valid_markers"] == 2
    assert result["precision"] == 1.0
    assert result["coverage"] == 1.0
    assert result["unknown_markers"] == []
    assert result["unused_sources"] == 2


def test_hallucinated_index_is_flagged_not_silently_dropped():
    registry = build_registry(_notes(), max_citations=4)
    markdown = (
        "Latency is the deciding factor at this scale of workload [1].\n\n"
        "An invented statistic claims 4000 queries per second [99].\n"
    )
    result = validate(markdown, registry)

    assert result["markers"] == 2
    assert result["valid_markers"] == 1
    assert result["unknown_markers"] == [99]
    assert result["precision"] == 0.5
    # a hallucinated citation must not count as coverage
    assert result["coverage"] == 0.5


def test_report_with_no_citations_scores_zero():
    registry = build_registry(_notes(), max_citations=4)
    markdown = "## Answer\n\nThis paragraph asserts things with no support at all.\n"
    result = validate(markdown, registry)

    assert result["markers"] == 0
    assert result["precision"] == 0.0     # 0/0 is not 1.0 - it is unproven
    assert result["coverage"] == 0.0
    assert result["unused_sources"] == 4


def test_headings_and_short_lines_are_not_counted_as_claims():
    registry = build_registry(_notes(), max_citations=4)
    markdown = "## Answer\n\n- [1]\n\nA real sentence with support [2].\n"
    result = validate(markdown, registry)

    assert result["sentences"] == 1
    assert result["coverage"] == 1.0


def test_coverage_survives_single_newline_formatting():
    """A heading and its text on consecutive lines must not collapse the report."""
    registry = build_registry(_notes(), max_citations=4)
    markdown = (
        "## Answer\n"
        "Latency is the deciding factor for this workload [1].\n"
        "## Detail\n"
        "Cost comes second and is driven by managed pricing [2].\n"
    )
    result = validate(markdown, registry)

    assert result["sentences"] == 2
    assert result["coverage"] == 1.0


def test_citations_for_follows_marker_order_and_skips_unknowns():
    registry = build_registry(_notes(), max_citations=4)
    markdown = "Cost first [2], then latency [1], then a hallucination [42]."
    citations = citations_for(markdown, registry)

    assert [item["index"] for item in citations] == [2, 1]
    assert [item["url"] for item in citations] == [
        "https://b.example/y",
        "https://a.example/x",
    ]


# ------------------------------------------------------------- second attempt
def test_truncated_draft_triggers_a_second_attempt():
    """A finish_reason of `length` is a broken draft, not a short one.

    Measured on gemini-3.8-flash: the same prompt at max_tokens=1800 produced
    622, 274 and 34 words, the last ending mid-sentence - thinking tokens are
    charged against the cap.
    """
    from app.agents.nodes.writer import needs_repair

    good = {"markers": 12, "precision": 1.0, "coverage": 0.8}
    assert needs_repair(good) is False

    assert needs_repair({**good, "truncated": True}) is True
    assert needs_repair({"markers": 0, "precision": 0.0, "coverage": 0.0}) is True
    assert needs_repair({"markers": 10, "precision": 0.5, "coverage": 0.5}) is True


def test_score_prefers_a_finished_draft_over_a_truncated_one():
    from app.services.report_runner import _score

    truncated = {"truncated": True, "precision": 1.0, "coverage": 1.0, "words": 800}
    finished = {"truncated": False, "precision": 1.0, "coverage": 0.6, "words": 600}
    assert _score(finished) > _score(truncated)


def test_uncited_is_reported_when_no_marker_resolves():
    """Prose with no markers is not evidence, and the audit must say so.

    Measured cause: the proxy failed over from Gemini (429) to Groq, and the
    backup model ignored the marker contract entirely - 644 words, zero
    brackets, which without this flag renders exactly like a cited brief.
    """
    registry = build_registry(_notes(), max_citations=4)

    cited = validate("pgvector is cheap [1]. Pinecone is managed [2].", registry)
    assert cited["uncited"] is False
    assert cited["valid_markers"] == 2

    prose = validate("pgvector is cheap. Pinecone is managed.", registry)
    assert prose["uncited"] is True
    assert prose["markers"] == 0

    # Markers that all point at sources that do not exist are not citations.
    invented = validate("pgvector is cheap [9]. Pinecone is managed [8].", registry)
    assert invented["uncited"] is True
    assert invented["precision"] == 0.0
