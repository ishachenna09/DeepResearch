"""Deterministic tests for the critic's stop policy.

These are the four brakes that make the research loop terminate. They are pure
functions of (verdict, round, spend, progress), so they are tested here without
a provider call: a test that needs a live LLM cannot protect you at 3 a.m.

Run from api/:
    ..\\.venv\\Scripts\\python.exe -m pytest tests -q

The one path not covered here is the "no notes at all" short-circuit: it lives
in critic_node before the LLM call (stop_reason="no_notes") and is exercised by
verify_phase4.ps1 whenever extraction fails for every page.
"""
from __future__ import annotations

import pytest

from app.agents.nodes.critic import decide_stop
from app.core.config import settings


def stop(**overrides):
    """Call decide_stop with a healthy round-1 baseline, apply overrides."""
    kwargs = dict(
        sufficient=False,
        gaps=["What does Pinecone cost at 1M vectors?"],
        round_no=1,
        spent=10_000,
        budget=150_000,
        new_notes=10,
    )
    kwargs.update(overrides)
    return decide_stop(**kwargs)


def test_round_one_with_gaps_and_budget_keeps_researching():
    assert stop() is None


def test_sufficient_stops_at_the_cheapest_possible_point():
    assert stop(sufficient=True) == "sufficient"


def test_broken_critic_never_loops():
    # sufficient=None means "no usable verdict" - looping on a broken critic
    # would spend money without any decision behind it.
    assert stop(sufficient=None, gaps=["anything"]) == "critic_error"


def test_gaps_without_questions_stops():
    assert stop(gaps=[]) == "no_gaps"


def test_max_rounds_is_the_hard_cap():
    assert stop(round_no=settings.MAX_ROUNDS) == "max_rounds"
    assert stop(round_no=settings.MAX_ROUNDS + 1) == "max_rounds"


def test_round_one_is_still_allowed_at_the_cap_minus_one():
    assert stop(round_no=settings.MAX_ROUNDS - 1) is None


def test_budget_brake_trips_at_the_configured_ratio():
    budget = 100_000
    just_below = int(budget * settings.BUDGET_STOP_RATIO) - 1
    assert stop(spent=just_below, budget=budget) is None
    assert stop(spent=int(budget * settings.BUDGET_STOP_RATIO), budget=budget) == "budget"


def test_zero_budget_disables_the_budget_brake():
    # A plan without a budget must not divide-by-zero or stop instantly; the
    # structural round cap still protects us.
    assert stop(spent=10**9, budget=0) is None


def test_no_progress_only_applies_to_gap_rounds(monkeypatch):
    # MAX_ROUNDS is configuration, so this brake is *dormant* at the default 2
    # (round 2 always hits the cap first). Raise the cap to prove the brake
    # exists for a deployment that allows more rounds.
    monkeypatch.setattr(settings, "MAX_ROUNDS", 3)

    # Round 1: a weak initial fan-out is a reason to try again, not to give up.
    assert stop(round_no=1, new_notes=0) is None
    # Round 2: re-searching produced almost nothing -> the space is exhausted.
    assert stop(round_no=2, new_notes=settings.MIN_NEW_NOTES_PER_ROUND - 1) == "no_progress"
    assert stop(round_no=2, new_notes=settings.MIN_NEW_NOTES_PER_ROUND) is None


def test_at_the_default_cap_no_progress_is_unreachable():
    # Documents the interaction, so nobody "fixes" a later failure by widening
    # MAX_ROUNDS: at the cap, the structural reason is the binding one.
    assert settings.MAX_ROUNDS == 2
    assert stop(round_no=2, new_notes=0) == "max_rounds"


@pytest.mark.parametrize("round_no", [1, 2])
def test_sufficient_outranks_every_structural_brake(round_no):
    # A satisfied critic must not be overridden into another round by the cap
    # or by "no progress" - the cheapest stop always wins.
    assert stop(sufficient=True, round_no=round_no, new_notes=0) == "sufficient"


def test_max_rounds_outranks_budget():
    # Both would stop; the reason recorded should be the structural one so the
    # UI does not blame the budget for a run that simply hit MAX_ROUNDS.
    assert stop(round_no=settings.MAX_ROUNDS, spent=10**9) == "max_rounds"


def test_every_stop_reason_is_a_documented_value():
    # The reason string is a public contract: it is stored in
    # research_jobs.critique and shown in the UI / used by Phase 7 evals.
    allowed = {
        "sufficient", "no_gaps", "max_rounds", "budget", "no_progress",
        "critic_error", "no_notes",
    }
    produced = {
        stop(sufficient=True),
        stop(sufficient=None),
        stop(gaps=[]),
        stop(round_no=settings.MAX_ROUNDS),
        stop(spent=10**9, budget=100_000),
        stop(round_no=2, new_notes=0),
        stop(),
    }
    assert None in produced          # the "keep going" case is represented
    assert produced - {None} <= allowed
