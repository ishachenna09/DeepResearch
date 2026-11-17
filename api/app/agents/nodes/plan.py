from app.agents.state import ResearchState
from app.core.llm import get_llm, usage_tokens
from app.schemas.research import ResearchPlan

SYSTEM_PROMPT = """You are a meticulous research planner.

Break the user's question into 3-6 concrete sub-questions that can each be
answered by searching the public web. Add relevant angles (e.g. pricing,
performance, migration, ecosystem, risks) and any constraints.
Set budget_tokens between 100000 and 250000.
(Measured on this pipeline: planner ~1k, one fan-out round over ~7 pages
~25k, critic ~1k. The critic may add one gap-filling round, so a two-round run
lands near 55k. The budget is a brake, not a target - do not inflate it.)

Rules:
- Sub-questions must be independent (parallelizable).
- No yes/no questions.
- Cover opposing viewpoints, not just the obvious angle."""


async def plan_node(state: ResearchState) -> dict:
    structured = get_llm().with_structured_output(ResearchPlan, include_raw=True)
    result = await structured.ainvoke(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": state["question"]},
        ]
    )
    plan: ResearchPlan | None = result.get("parsed")
    if plan is None:
        raise ValueError(f"planner could not produce a plan: {result.get('parsing_error')}")
    # Keep the user's original question authoritative
    plan.question = state["question"]
    return {
        "plan": plan.model_dump(),
        "plan_status": "draft",
        "round": 0,
        "tokens_used": usage_tokens(result.get("raw")),
    }
