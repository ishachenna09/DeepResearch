from app.core.llm import get_llm
from app.schemas.research import ResearchPlan
from app.agents.state import ResearchState

SYSTEM_PROMPT = """You are a meticulous research planner.

Break the user's question into 3-6 concrete sub-questions that can each be
answered by searching the public web. Add relevant angles (e.g. pricing,
performance, migration, ecosystem, risks) and any constraints.
Set budget_tokens between 20000 and 60000.

Rules:
- Sub-questions must be independent (parallelizable).
- No yes/no questions.
- Cover opposing viewpoints, not just the obvious angle."""


async def plan_node(state: ResearchState) -> dict:
    structured = get_llm().with_structured_output(ResearchPlan)
    plan: ResearchPlan = await structured.ainvoke(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": state["question"]},
        ]
    )
    # Keep the user's original question authoritative
    plan.question = state["question"]
    return {"plan": plan.model_dump(), "plan_status": "draft", "round": 0}