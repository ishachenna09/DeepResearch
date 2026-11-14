from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agents.checkpointer import get_checkpointer
from app.agents.nodes.plan import plan_node
from app.agents.state import ResearchState

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


def _build() -> StateGraph:
    g = StateGraph(ResearchState)
    g.add_node("plan", plan_node)
    g.add_node("approval", approval_gate)

    g.add_edge(START, "plan")
    g.add_edge("plan", "approval")
    # Phase 3 will route approved -> fanout; for now both paths end.
    g.add_edge("approval", END)
    return g


def init_graph():
    global _graph
    _graph = _build().compile(checkpointer=get_checkpointer())
    return _graph


def get_graph():
    if _graph is None:
        raise RuntimeError("Graph not initialised — call init_graph() in lifespan")
    return _graph