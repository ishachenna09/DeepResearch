import operator
from typing import Annotated, TypedDict


class ResearchState(TypedDict, total=False):
    """Graph state. Keep it SMALL - every write is copied into a Redis checkpoint.

    Parallel branches append through `operator.add` reducers: with N branches
    returning {"notes": [...]}, the default (replace) semantics would let the
    last writer win. Reducers are what make fan-out lossless.
    """
    # identity
    job_id: str
    user_id: str
    thread_id: str
    # input
    question: str
    # HITL
    plan: dict            # serialized ResearchPlan (JSON-safe for checkpoints)
    plan_status: str      # draft | approved | rejected
    # fan-out (Phase 3)
    sub_question: str     # per-branch payload, set by Send() - not persisted after
    notes: Annotated[list[dict], operator.add]      # text + provenance, NO vectors
    sources: Annotated[list[dict], operator.add]    # per-page fetch metadata
    stats: dict           # written once per round, by the join node
    # Phase 4: bounded gap-filling loop
    round: int            # completed research rounds (1 = initial fan-out)
    gaps: list[str]       # search-ready questions the critic wants answered
    critique: dict        # {sufficient, reason, stop_reason} for this round
    # Critical that this is additive: every branch reports what it spent.
    tokens_used: Annotated[int, operator.add]
    # reserved for later phases
    outline: list[dict]
    draft: str
