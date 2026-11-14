from typing import TypedDict


class ResearchState(TypedDict, total=False):
    # identity
    job_id: str
    user_id: str
    thread_id: str
    # input
    question: str
    # HITL
    plan: dict            # serialized ResearchPlan (JSON-safe for checkpoints)
    plan_status: str      # draft | approved | rejected
    # reserved for later phases
    round: int
    notes: list[dict]
    gaps: list[str]
    outline: list[dict]
    draft: str
    sources: list[dict]