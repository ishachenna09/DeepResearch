"""Agent-to-agent (A2A) services: the writer and the critic as network agents.

Phase 5 made the report a *resource*. Phase 6 makes the two long-running agents
*services*: each runs in its own process, publishes an AgentCard, and is called
over the A2A protocol (JSON-RPC 2.0 + SSE) instead of a Python function call.

Why extract these two and not everything?
They are the expensive, slow, independently-failing steps. The planner, the
searcher and the note extractor are short calls inside one job; the writer holds
a stream open for 20-60 s and the critic is the only node whose failure changes
the shape of a run. Those are the ones worth isolating behind a protocol.

What stays in the API:
- the citation registry (it is a database projection, not an agent's job),
- the citation audit and the loop brakes (`decide_stop`) - a remote agent may
  *propose*; the orchestrator decides,
- everything the browser talks to.

Shared secret auth is deliberate for a laptop: both services bind to loopback
and refuse requests without `A2A_TOKEN`. Phase 10 replaces this with mTLS or
per-agent OAuth2 - the card already advertises a bearer scheme, so the client
side of that change is one header.
"""
