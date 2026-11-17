import uuid
from typing import Literal

from pydantic import BaseModel, Field


class ResearchPlan(BaseModel):
    """What the planner LLM must produce. Keep it strict — it's a contract."""
    question: str
    sub_questions: list[str] = Field(min_length=3, max_length=7)
    angles: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    # Whole-job token cap. Calibrated against measured runs: a two-round run
    # (7 pages per round + planner + 2 critic calls) costs ~52k tokens, so the
    # default leaves room for one gap round and still trips the brake only on
    # pathological fan-out.
    budget_tokens: int = Field(ge=50_000, le=400_000, default=150_000)


class CreateResearchIn(BaseModel):
    question: str = Field(min_length=10, max_length=500)


class PlanDecisionIn(BaseModel):
    action: Literal["approve", "reject"]
    plan: ResearchPlan | None = None   # user-edited plan (optional)


class ResearchJobOut(BaseModel):
    job_id: uuid.UUID
    question: str
    plan: ResearchPlan | None
    status: str
    notes_count: int = 0
    error: str | None = None
    # per-page fetch metadata (url, title, chars, injected, cached, stage, error)
    sources: list[dict] | None = None
    # Phase 4: bounded-loop accounting
    rounds: int = 1
    tokens_used: int = 0
    critique: dict | None = None

    model_config = {"from_attributes": True}


# ── Phase 3: what the extractor LLM must return, per page ────────────────
class ExtractedNote(BaseModel):
    """One atomic claim, with the verbatim sentence that supports it."""
    note: str = Field(min_length=10, max_length=400)
    quote: str = Field(min_length=10, max_length=600)
    confidence: Literal["low", "medium", "high"] = "medium"


class NoteBatch(BaseModel):
    notes: list[ExtractedNote] = Field(default_factory=list, max_length=5)


class CoverageReview(BaseModel):
    """The critic's verdict for one round."""
    sufficient: bool
    gaps: list[str] = Field(default_factory=list, max_length=3)   # search-ready questions
    reason: str = Field(min_length=5, max_length=400)