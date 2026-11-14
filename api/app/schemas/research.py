import uuid
from typing import Literal

from pydantic import BaseModel, Field


class ResearchPlan(BaseModel):
    """What the planner LLM must produce. Keep it strict — it's a contract."""
    question: str
    sub_questions: list[str] = Field(min_length=3, max_length=7)
    angles: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    budget_tokens: int = Field(ge=10_000, le=80_000, default=40_000)


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

    model_config = {"from_attributes": True}