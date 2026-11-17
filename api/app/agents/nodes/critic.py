"""The coverage critic: decide whether the evidence is good enough to write from.

Why a critic at all?
A one-shot fan-out cannot know what it missed. The planner guessed which
sub-questions matter *before* seeing any evidence; after reading 15 pages we know
much more about the shape of the answer. The critic is the step that compares
"what the question needs" with "what we actually have" and, when the gap is real,
spends one more bounded round on it.

Why does the STOP POLICY live in the node instead of the router?
A graph can be re-entered, resumed and replayed. Putting "should we stop?" in one
place means one thing to test. The conditional edge only asks "are there gaps?" -
the node already decided, and recorded *why* it stopped in `critique.stop_reason`
(sufficient | max_rounds | budget | no_progress | no_gaps | critic_error), which
is what the UI and Phase 7 evals need.

Why a digest and not the full notes?
The critic must judge coverage, not re-read the corpus. Six sub-questions with
three claims each is ~500 tokens and produces the same verdict as 60k tokens of
notes - and the critic runs inside the very budget it is policing.
"""
from __future__ import annotations

import logging

from app.agents.state import ResearchState
from app.core.config import settings
from app.core.llm import get_llm, usage_tokens
from app.schemas.research import CoverageReview

logger = logging.getLogger(__name__)

CRITIC_PROMPT = """You are the coverage critic of a deep-research brief.

You receive the user's question, the approved sub-questions, and a digest of the
evidence collected so far (number of notes per sub-question, how many distinct
sites, and a few sample claims).

Decide whether the notes are enough to write a well-cited, balanced answer.

Return:
- sufficient: true only if EVERY sub-question has at least 2 concrete notes AND
  no major angle of the user's question is still unaddressed.
- gaps: at most 3 NEW, search-ready questions covering genuinely missing angles.
  Each must be specific, answerable by a web search, and different from every
  sub-question already listed (including ones added in earlier rounds).
- reason: one sentence a human can audit.

Rules:
- Do not ask for "more", "deeper" or "additional" detail on a covered
  sub-question.
- If a sub-question's notes all come from a single site, that is a real gap:
  ask for an independent source.
- Never ask for opinions, predictions, or anything that is not published on the web.
- Judge the evidence, not the writing."""


def _digest(state: ResearchState) -> str:
    """Compact, auditable view of what we have - the critic's whole input."""
    notes = state.get("notes") or []
    plan = state.get("plan") or {}

    planned = [q for q in (plan.get("sub_questions") or []) if q and q.strip()]
    extra: list[str] = []
    seen = set(planned)
    for note in notes:                      # gap questions from earlier rounds
        sq = note.get("sub_question")
        if sq and sq not in seen:
            seen.add(sq)
            extra.append(sq)

    lines: list[str] = []
    for sub_question in planned + extra:
        items = [n for n in notes if n.get("sub_question") == sub_question]
        sites = {n.get("url") for n in items if n.get("url")}
        lines.append(f'- [{len(items)} notes, {len(sites)} site(s)] {sub_question}')
        for note in items[:3]:
            lines.append(f"    . {(note.get('note') or '')[:120]}")

    stats = state.get("stats") or {}
    lines.append(
        f"\nTotals: {len(notes)} notes from {stats.get('pages_read', 0)} pages, "
        f"{stats.get('source_errors', 0)} source errors."
    )
    return "\n".join(lines)


def decide_stop(
    *,
    sufficient: bool | None,
    gaps: list[str],
    round_no: int,
    spent: int,
    budget: int,
    new_notes: int,
) -> str | None:
    """The whole stop policy, as one pure function. `None` = keep researching.

    Pure on purpose: the four brakes are the only thing preventing an unbounded
    spend loop, so they must be testable without a provider call (see
    api/tests/test_critic_policy.py). Order matters — first match wins:

      1. critic_error  the verdict is unusable; never loop on a broken critic
      2. sufficient    the question is answered; the cheapest possible stop
      3. no_gaps       it wants more but cannot name a new question
      4. max_rounds    the hard structural cap (the only guarantee of termination)
      5. budget        spent >= BUDGET_STOP_RATIO of the plan's cap
      6. no_progress   a gap round returned almost nothing new

    `no_progress` is deliberately NOT applied to round 1: there `notes_new` is
    just "how good was the initial fan-out", and a weak first round is exactly
    when one more round is worth trying. Only a *repeat* round is evidence that
    the search space is exhausted.
    """
    if sufficient is None:
        return "critic_error"
    if sufficient:
        return "sufficient"
    if not gaps:
        return "no_gaps"
    if round_no >= settings.MAX_ROUNDS:
        return "max_rounds"
    if budget and spent >= budget * settings.BUDGET_STOP_RATIO:
        return "budget"
    if round_no > 1 and new_notes < settings.MIN_NEW_NOTES_PER_ROUND:
        return "no_progress"
    return None


async def critic_node(state: ResearchState) -> dict:
    notes = state.get("notes") or []
    round_no = int(state.get("round") or 1)
    plan = state.get("plan") or {}
    budget = int(plan.get("budget_tokens") or 0)
    spent = int(state.get("tokens_used") or 0)
    new_notes = int((state.get("stats") or {}).get("notes_new") or 0)

    if not notes:
        return {
            "gaps": [],
            "critique": {
                "sufficient": False,
                "reason": "No notes were extracted, so there is nothing to assess.",
                "stop_reason": "no_notes",
                "round": round_no,
                "tokens_used": spent,
                "budget_tokens": budget,
            },
        }

    llm = get_llm(temperature=0.0, timeout=settings.EXTRACT_TIMEOUT).with_structured_output(
        CoverageReview, include_raw=True
    )
    user_message = (
        f"User question:\n{state.get('question', '')}\n\n"
        f"Approved sub-questions:\n"
        + "\n".join(f"- {q}" for q in (plan.get("sub_questions") or []))
        + f"\n\nEvidence digest:\n{_digest(state)}"
    )

    tokens = 0
    review: CoverageReview | None = None
    error = ""
    try:
        result = await llm.ainvoke(
            [
                {"role": "system", "content": CRITIC_PROMPT},
                {"role": "user", "content": user_message},
            ]
        )
        review = result.get("parsed")
        tokens = usage_tokens(result.get("raw"))
        if review is None:
            error = f"unparseable review: {result.get('parsing_error')}"
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"

    proposed = []
    if review is not None:
        proposed = [g.strip() for g in review.gaps if g and g.strip()]

    # The prompt asks for NEW questions, but a model is not a guarantee. Drop
    # anything already asked (planned or from an earlier round) so round 2 can
    # never spend its one budget on a repeat. Case/whitespace insensitive.
    asked = {(q or "").strip().lower() for q in (plan.get("sub_questions") or [])}
    asked |= {
        (n.get("sub_question") or "").strip().lower()
        for n in notes
        if n.get("sub_question")
    }
    gaps = [g for g in proposed if g.lower() not in asked][: settings.MAX_GAP_QUESTIONS]

    # ── the brakes: one pure decision, then record why (see decide_stop) ─────
    stop_reason = decide_stop(
        sufficient=(review.sufficient if review is not None else None),
        gaps=gaps,
        round_no=round_no,
        spent=spent,
        budget=budget,
        new_notes=new_notes,
    )

    critique = {
        "sufficient": bool(review.sufficient) if review is not None else False,
        "reason": (review.reason if review is not None else error)[:400],
        "stop_reason": stop_reason,
        "round": round_no,
        "tokens_used": spent,
        "budget_tokens": budget,
        "notes_new": new_notes,
        "gaps_proposed": len(proposed),
        "gaps_kept": len(gaps),
    }
    if stop_reason is not None:
        gaps = []

    logger.info(
        "critic round %d: sufficient=%s gaps=%d/%d stop=%s tokens=%d/%d",
        round_no, critique["sufficient"], critique["gaps_kept"],
        critique["gaps_proposed"], stop_reason, spent, budget,
    )
    return {"gaps": gaps, "critique": critique, "tokens_used": tokens}
