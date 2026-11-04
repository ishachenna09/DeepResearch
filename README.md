# DeepResearch — Multi-Agent Web Research & Report Generator

An open-source alternative to paid "deep research" products: you give it a question,
approve the AI-generated **research plan**, watch **parallel search agents** gather sources
live, and get a structured report where **every claim has a clickable citation**.

Stack: Python · FastAPI · LangGraph · LangChain · MCP · A2A · Redis · PostgreSQL+pgvector ·
React + Tailwind · LiteLLM (Gemini→Groq fallback) · Langfuse · AWS · GitHub Actions

---

## Repo layout

```
DeepResearch/
├── api/                        # FastAPI backend (Python)
│   ├── app/
│   │   ├── main.py             # app factory, CORS, SSE, routers
│   │   ├── core/               # config, JWT/OAuth, RBAC, logging, LLM client, observability
│   │   ├── api/v1/             # auth, research, reports, admin, health
│   │   ├── agents/
│   │   │   ├── state.py        # ResearchState TypedDict (graph contract)
│   │   │   ├── graph.py        # LangGraph topology + checkpointer
│   │   │   └── nodes/          # plan, fanout, search, merge, critic, outline, write, citations
│   │   ├── a2a/                # AgentCards + agent services (Phase 6)
│   │   ├── mcp_tools/          # FastMCP servers: search, fetch, notes (Phase 2)
│   │   ├── rag/                # page cleaner, chunker, embedder, retriever
│   │   ├── guardrails/         # untrusted-web-content scanning, citation rules
│   │   ├── evals/              # DeepEval/Ragas suites + golden datasets (Phase 7)
│   │   ├── models/  schemas/  services/  workers/
│   ├── alembic/                # migrations
│   └── tests/                  # unit | integration | graph | e2e
├── frontend/                   # React + Vite + Tailwind
│   └── src/components/{plan/, report/, sources/, layout/}
├── evals/                      # promptfoo CI regression gate
├── scripts/                    # seed_questions.py, demo.ps1
├── infra/                      # caddy, ec2 deploy, CI workflows
└── docs/                       # ARCHITECTURE · TECH_DECISIONS (interview) · INTERVIEW_PREP
```

---

## Build phases (we go one at a time — you code, I instruct)

| Phase | Deliverable | You learn here |
|---|---|---|
| **0** | Auth shell (reuse SupportIQ Phase 0) + Tavily key | JWT, SSE plumbing |
| **1** | Question → **research plan** → human approves (HITL) | LangGraph `interrupt()`, checkpoints, **Redis** |
| **2** | `web_search` + `fetch_page` tools, content scanning | **MCP** servers, guardrails on untrusted web content |
| **3** | Parallel searcher fan-out, page cache, notes → pgvector | **Redis** cache/queues, LangGraph `Send` (map-reduce) |
| **4** | Critic agent + bounded gap-filling loop + token budget | Eval-driven loop control, failure modes |
| **5** | Writer streams report + **citation validator** | SSE streaming, claim→source verification |
| **6** | Agents extracted as separate A2A services | **A2A** protocol, AgentCards, distributed agents |
| **7** | Evals in CI + Langfuse traces + cost dashboard | DeepEval/Ragas/promptfoo, observability |
| **8** | Report library, export (MD/PDF), admin dashboards | S3/R2, product polish |
| **9** | Hardening: rate limits, SSRF defense, load + security tests | Production concerns |
| **10** | Deploy: GH Actions → Docker → EC2 + Vercel → **public URL** | CI/CD, TLS, containers (cloud only) |

## Local dev (no Docker on laptop)
- Infra: **Neon** (Postgres+pgvector) · **Upstash** (Redis) · **Langfuse Cloud** — all free
- Backend: `.venv` + `uvicorn` · LLM gateway: `litellm` pip process (Gemini → Groq fallback)
- Frontend: `npm run dev`
- Docker only on **EC2 + GitHub Actions** (Phase 10)
