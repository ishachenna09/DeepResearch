"""The research graph.

    START -> plan -> approval --(approved)--> [Send x N] research_one -> persist -> END
                                    |
                                (rejected) -> END

Why a conditional edge returning Send objects?
That is LangGraph's map-reduce primitive: the router returns one Send per
sub-question, so branches are created at *runtime* from the approved plan (the
user may have edited/added/removed sub-questions). A static edge cannot do that.

Why fan-in works without extra bookkeeping:
`persist` has one incoming edge from `research_one`. LangGraph runs it once,
after every branch that can reach it has finished, and the `operator.add`
reducers on `notes`/`sources` have already merged the branch writes.
"""
from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from app.agents.checkpointer import get_checkpointer
from app.agents.nodes.plan import plan_node
from app.agents.nodes.research import persist_notes, research_one
from app.agents.state import ResearchState
from app.core.config import settings

_graph = None


async def approval_gate(state: ResearchState) -> dict:
    """HITL: freeze the graph and surface the plan for a human decision."""
    decision = interrupt(
        {"type": "plan_approval", "plan": state["plan"]}
    )  # <-- execution pauses here; state is checkpointed
    if decision.get("action") == "reject":
        return {"plan_status": "rejected"}
    return {
        "plan": decision.get("plan") or state["plan"],
        "plan_status": "approved",
    }


def route_after_approval(state: ResearchState) -> Any:
    """Rejected -> END. Approved -> one Send per sub-question (capped)."""
    if state.get("plan_status") != "approved":
        return END

    plan = state.get("plan") or {}
    sub_questions = [q.strip() for q in (plan.get("sub_questions") or []) if q and q.strip()]
    if not sub_questions:
        return END

    # A plan is LLM output: cap the fan-out so one over-eager plan cannot spawn
    # 40 branches and burn the whole daily quota in a single click.
    capped = sub_questions[: settings.MAX_BRANCHES]
    return [
        Send(
            "research_one",
            {
                "job_id": state["job_id"],
                "user_id": state.get("user_id", ""),
                "thread_id": state.get("thread_id", ""),
                "question": state["question"],
                "plan": plan,
                "plan_status": "approved",
                "sub_question": sub_question,
            },
        )
        for sub_question in capped
    ]


def _build() -> StateGraph:
    g = StateGraph(ResearchState)
    g.add_node("plan", plan_node)
    g.add_node("approval", approval_gate)
    g.add_node("research_one", research_one)
    g.add_node("persist", persist_notes)

    g.add_edge(START, "plan")
    g.add_edge("plan", "approval")
    g.add_conditional_edges("approval", route_after_approval, ["research_one", END])
    g.add_edge("research_one", "persist")
    g.add_edge("persist", END)
    return g


def init_graph():
    global _graph
    _graph = _build().compile(checkpointer=get_checkpointer())
    return _graph


def get_graph():
    if _graph is None:
        raise RuntimeError("Graph not initialised — call init_graph() in lifespan")
    return _graph
