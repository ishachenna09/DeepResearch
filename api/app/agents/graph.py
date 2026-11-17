"""The research graph.

    START -> plan -> approval --(approved)--> [Send x N] research_one -> persist
                        |                                                  |
                    (rejected)                                             v
                          |                                             critic
                          v                                                |
                         END                          (gaps) --> [Send x M] research_one
                                                                           |
                                                              (no gaps / brake) --> END

Two loops, both explicit:
- fan-out at runtime (`Send`) because the sub-questions come from the LLM at
  request time and the user may edit them at the approval gate
- a bounded research loop (persist -> critic -> fan-out -> persist) where every
  brake lives in the critic node, so the edges stay mechanical and testable

Why `Send` instead of a static edge?
It is LangGraph's map-reduce primitive: branches are created from the approved
plan, each is checkpointed separately, and the join node runs once after all
branches that can reach it have finished (that is the automatic fan-in).

Why fan-in works without extra bookkeeping:
the `operator.add` reducers on notes/sources/tokens_used merge the branch writes.
"""
from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from app.agents.checkpointer import get_checkpointer
from app.agents.nodes.critic import critic_node
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


def _branch_payload(state: ResearchState, sub_question: str, round_no: int) -> dict:
    return {
        "job_id": state["job_id"],
        "user_id": state.get("user_id", ""),
        "thread_id": state.get("thread_id", ""),
        "question": state["question"],
        "plan": state.get("plan") or {},
        "plan_status": "approved",
        "sub_question": sub_question,
        "round": round_no,
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
    return [
        Send("research_one", _branch_payload(state, sub_question, 1))
        for sub_question in sub_questions[: settings.MAX_BRANCHES]
    ]


def route_after_critic(state: ResearchState) -> Any:
    """Mechanical: only dispatch if the critic left gaps behind.

    Every stop condition (rounds, budget, no progress) was already applied in
    the critic node and recorded in `critique.stop_reason`.
    """
    gaps = [g for g in (state.get("gaps") or []) if g and g.strip()]
    if not gaps:
        return END

    next_round = int(state.get("round") or 1) + 1
    return [
        Send("research_one", _branch_payload(state, gap, next_round))
        for gap in gaps[: settings.MAX_GAP_QUESTIONS]
    ]


def _build() -> StateGraph:
    g = StateGraph(ResearchState)
    g.add_node("plan", plan_node)
    g.add_node("approval", approval_gate)
    g.add_node("research_one", research_one)
    g.add_node("persist", persist_notes)
    g.add_node("critic", critic_node)

    g.add_edge(START, "plan")
    g.add_edge("plan", "approval")
    g.add_conditional_edges("approval", route_after_approval, ["research_one", END])
    g.add_edge("research_one", "persist")
    g.add_edge("persist", "critic")
    g.add_conditional_edges("critic", route_after_critic, ["research_one", END])
    return g


def init_graph():
    global _graph
    _graph = _build().compile(checkpointer=get_checkpointer())
    return _graph


def get_graph():
    if _graph is None:
        raise RuntimeError("Graph not initialised — call init_graph() in lifespan")
    return _graph
