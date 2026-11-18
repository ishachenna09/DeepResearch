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
│   └── src/                    # pages/{Research,JobDetail,Dashboard} · components/research/ · hooks/useJob.ts · lib/{api,research,types}.ts
├── evals/                      # promptfoo CI regression gate
├── scripts/                    # seed_questions.py, demo.ps1
├── infra/                      # caddy, ec2 deploy, CI workflows
└── docs/                       # ARCHITECTURE · TECH_DECISIONS (interview) · INTERVIEW_PREP
```

---

## Build phases 

| Phase | Deliverable | You learn here |
|---|---|---|
| **0** (done) | Auth shell (reuse DeepSearch Phase 0) + Tavily key | JWT, SSE plumbing |
| **1** (done) | Question → **research plan** → human approves (HITL) | LangGraph `interrupt()`, checkpoints, **Redis** |
| **2** (done) | `web_search` + `fetch_page` tools, content scanning | **MCP** servers, guardrails on untrusted web content |
| **3** (done) | Parallel searcher fan-out, page cache, notes → pgvector | **Redis** cache/queues, LangGraph `Send` (map-reduce) |
| **4** (done) | Critic agent + bounded gap-filling loop + token budget | Eval-driven loop control, failure modes |
| **4.5** (done) | Research UI: ask → edit/approve plan → live run → evidence + semantic search | Polling vs streaming, HITL UX, input vs output schemas |
| **5** | Writer streams report + **citation validator** | SSE streaming, claim→source verification |
| **6** | Agents extracted as separate A2A services | **A2A** protocol, AgentCards, distributed agents |
| **7** | Evals in CI + Langfuse traces + cost dashboard | DeepEval/Ragas/promptfoo, observability |
| **8** | Report library, export (MD/PDF), admin dashboards | S3/R2, product polish |
| **9** | Hardening: rate limits, SSRF defense, load + security tests | Production concerns |
| **10** | Deploy: GH Actions → Docker → EC2 + Vercel → **public URL** | CI/CD, TLS, containers (cloud only) |

## Local dev (no Docker on laptop)
- Infra: **Neon** (Postgres+pgvector) · **Redis Cloud** (Redis 8 + RediSearch: checkpoints *and* page cache) · **Langfuse Cloud** — all free
- Backend: `.venv` + `uvicorn` · LLM gateway: `litellm[proxy]` in its OWN venv `.venv-litellm` (mcp 2.x vs mcp 1.x, see scripts/requirements-litellm.txt) (Gemini → Groq fallback)
- Frontend: `npm run dev`
- Docker only on **EC2 + GitHub Actions** (Phase 10)

### First-time setup (once per clone / new machine)

Two virtualenvs on purpose: the API needs `mcp<2` (langchain-mcp-adapters) and the
LiteLLM proxy needs `mcp>=2.3`. In production they are two containers.

Run from the repo root (`C:\projects\DeepResearch`). Windows PowerShell 5.1 does
NOT support `&&`, so run the lines one at a time.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r api\requirements.txt

python -m venv .venv-litellm
.venv-litellm\Scripts\python.exe -m pip install -r scripts\requirements-litellm.txt
```

Verify:
```powershell
.venv\Scripts\python.exe -c "from importlib.metadata import version; print('mcp', version('mcp'))"
.venv-litellm\Scripts\python.exe -c "from importlib.metadata import version; print('litellm', version('litellm'), 'mcp', version('mcp'))"
```

Then: `scripts\start_litellm.ps1` (terminal 1), `scripts\start_api.ps1` (terminal 2),
`npm run dev` (terminal 3, from `frontend/`).

---

## API (v1)

Auth: `POST /auth/register`, `POST /auth/login` -> Bearer JWT on everything else.

| Method | Path | What it does |
|---|---|---|
| `POST` | `/research` | plan the question (LLM) -> pauses at the HITL gate -> `awaiting_approval` |
| `GET` | `/research` | the caller's jobs, newest first (`limit`/`offset`) - powers the research list |
| `GET` | `/research/{id}` | status, plan, `notes_count`, `error`, per-page `sources` (each tagged with its `round`), plus `rounds`, `tokens_used`, `critique` and `created_at`/`updated_at` |
| `POST` | `/research/{id}/plan/approve` | `approve` -> returns `researching` immediately (background fan-out); `reject` -> terminal |
| `GET` | `/research/{id}/notes` | extracted notes (claim + verbatim quote + url) |
| `POST` | `/research/{id}/notes/search` | semantic search: embeds the query, kNN over pgvector (`score` = 1 - cosine distance) |
| `GET` | `/tools` | MCP tool schema advertised over stdio |
| `POST` | `/tools/search`, `/tools/fetch` | raw search / page fetch + injection scan |
| `GET` | `/healthz`, `/health/llm` | dependency health; LLM reports which tier served it |

Status machine: `planning -> awaiting_approval -> researching -> notes_ready`
(or `rejected` / `failed`). Poll `GET /research/{id}`; a run takes ~2-4 minutes.

A run may contain **two bounded research rounds**: the initial fan-out, then a
coverage critic that names up to 3 missing angles and re-searches them. The job
reports `rounds` (1 = no gap-filling happened), `tokens_used` (measured provider
usage, not an estimate) and `critique` (`sufficient`, `reason`, and the
`stop_reason` that ended the loop: `sufficient` / `no_gaps` / `max_rounds` /
`budget` / `no_progress` / `critic_error` / `no_notes`). If you edit the plan at
the approval gate and remove angles, expect the critic to notice.

## Frontend (research UI)

```powershell
cd frontend
npm run dev          # http://localhost:5173 (Vite reads .env* files, not process env)
```

Routes: `/` ask a question + your jobs · `/jobs/:jobId` plan approval, live run,
evidence · `/system` health badges.

- **The job page is a state machine mirroring the API's**: `planning` skeleton →
  `PlanEditor` (`awaiting_approval`) → live `RunProgress` (`researching`) → critique card +
  `SourcesPanel` + `NotesPanel` (`notes_ready`). Anything else (`rejected`, `failed`, legacy
  rows) renders an explicit explanation rather than an empty page.
- **Polling, not SSE (for now).** `hooks/useJob.ts` polls every 2 s while work is happening and
  15 s while paused at the gate, and stops on a terminal status. A job only has ~5 state changes
  over 1-4 minutes, so polling is smaller and resumable; Phase 5 swaps that one hook for an
  `EventSource` when the payload becomes a token stream.
- **The plan editor mirrors server validation** (3-7 sub-questions, budget 50k-400k) so Approve
  explains itself instead of waiting for a 422. `angles`/`constraints` are chip editors because
  planner items contain commas - a comma-separated field would mangle them.
- If another app already owns `:8000`, put `VITE_API_BASE_URL=http://localhost:8001/api/v1` in
  `frontend/.env.local` (gitignored) and run uvicorn with `--port 8001`.

## Tests (no network, no LLM)

```powershell
cd api
..\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt   # once
..\.venv\Scripts\python.exe -m pytest tests -q
```

The loop's four brakes are a pure function (`decide_stop`), so they are tested
deterministically instead of relying on a live model to misbehave on cue.

## Verify a phase (one command)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify_phase2.ps1   # MCP tools, extraction, SSRF
powershell -ExecutionPolicy Bypass -File scripts\verify_phase3.ps1   # fan-out, cache, pgvector
powershell -ExecutionPolicy Bypass -File scripts\verify_phase3.ps1 -SkipCache   # skip the 2nd run
powershell -ExecutionPolicy Bypass -File scripts\verify_phase4.ps1   # critic, gap round, tokens, dedupe
powershell -ExecutionPolicy Bypass -File scripts\verify_phase4.ps1 -SkipSecond  # skip the control run
```

Pass `-Base http://localhost:8001/api/v1` when another app already owns `:8000`.

There is no UI verify script yet - the UI is verified by `npm run build` plus a manual
run of the flow above (`docs/TECH_DECISIONS.md` § Phase 4.5 evidence records the numbers
from the last one).

Each script fails loudly with the exact HTTP status / detail, so a red line always
means something real.
