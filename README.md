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
│   └── src/                    # pages/{Research,JobDetail,Dashboard} · components/{research,report}/ · hooks/{useJob,useReport}.ts · lib/{api,research,report,inflight,types}.ts
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
| **5** (done) | Writer streams report + **citation validator** | SSE over Redis Streams, claim→source verification |
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
| `GET` | `/research/{id}/notes` | extracted notes (claim + verbatim quote + url) - the embedding column is deferred, it is not in the payload |
| `POST` | `/research/{id}/notes/search` | semantic search: embeds the query, kNN over pgvector (`score` = 1 - cosine distance) |
| `POST` | `/research/{id}/report` | start (or restart) the cited brief; returns `202` immediately, idempotent while one is running |
| `GET` | `/research/{id}/report` | the stored report: markdown, numbered `citations`, and the `validation` audit |
| `GET` | `/research/{id}/report/stream` | **SSE**: `status` / `token` / `replace` / `done` / `error`, replayable via `Last-Event-ID` |
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

### The report (Phase 5)

The report is its own resource: a job stays `notes_ready` while its report is regenerated,
so a failed report never costs the research. Generation runs in the background and streams
tokens over SSE; the stored row is the source of truth for citations.

- **Citation contract.** The writer is given a numbered list of at most 30 notes and may only
  cite `[n]` from that list. Anything else is audited as an unknown marker. `citations[]` contains
  exactly the sources whose number appears in the text, so every chip links to a stored note.
- **Audit, deterministic, no LLM judge.** `validation` carries `precision` (valid / all markers),
  `coverage` (cited sentences / sentences), `unknown_markers`, `unused_sources`, `words`, plus
  three honest flags: `repaired` (a second attempt replaced the draft), `truncated` (the provider
  stopped at the token cap) and `uncited` (no marker resolved at all - readable prose, not evidence).
- **One bounded second attempt** fires when the draft is truncated, uncited, or invents markers.
  Both drafts are scored and the better one is kept, so a bad retry cannot make the result worse.
- **SSE over Redis Streams, not pub/sub**, so a reload mid-generation resumes with `Last-Event-ID`
  instead of joining the middle of the text. The client uses `fetch` (not `EventSource`) because the
  stream needs an `Authorization` header. If Redis is unreachable the endpoint watches the database
  and emits a single `done`: the typing effect is lost, the report is not.
- **`reports.model` names the model that actually answered**, not the alias that was requested -
  the proxy fails over Gemini -> Gemini-Lite -> Groq, and a silent failover should be visible in
  the data rather than hidden behind `support-chat`.

## Frontend (research UI)

```powershell
cd frontend
npm run dev          # http://localhost:5173 (Vite reads .env* files, not process env)
```

Routes: `/` ask a question + your jobs · `/jobs/:jobId` plan approval, live run,
evidence + cited report · `/system` health badges.

- **The job page is a state machine mirroring the API's**: `planning` skeleton →
  `PlanEditor` (`awaiting_approval`) → live `RunProgress` (`researching`) → critique card +
  `SourcesPanel` + `NotesPanel` (`notes_ready`). Anything else (`rejected`, `failed`, legacy
  rows) renders an explicit explanation rather than an empty page.
- **Polling for the job, SSE for the report.** `hooks/useJob.ts` polls every 2 s while work is
  happening and 15 s while paused at the gate, and stops on a terminal status: a job only has ~5
  state changes over 1-4 minutes, so polling is smaller and resumable there. The report is the
  opposite - ~1000 token chunks in under a minute - so `hooks/useReport.ts` reads an SSE stream,
  resuming with `Last-Event-ID` on reconnect and falling back to polling if the stream is
  unavailable. Both end at the same place: the database row is the source of truth, the stream is
  only the view.
- **The plan editor mirrors server validation** (3-7 sub-questions, budget 50k-400k) so Approve
  explains itself instead of waiting for a 422. `angles`/`constraints` are chip editors because
  planner items contain commas - a comma-separated field would mangle them.
- **The report is rendered by ~120 lines of our own markdown, not `react-markdown`.** Model output
  is never fed to `dangerouslySetInnerHTML`; `[n]` becomes a chip linking to `#cite-n`, an unknown
  number renders red, and every other string renders as text. That is ~80 kB of dependency and a
  whole class of injection risk avoided.
- **Every GET is one request, and every GET has a timeout.** React StrictMode mounts effects twice
  in development, so the job, notes and report calls each fired 4x on a page load (`lib/inflight.ts`
  now shares in-flight promises). Axios's default timeout is `0` = wait forever, which made a slow
  free-tier database look like a hang: `getReport` now gives up at 20 s and the panel shows elapsed
  seconds plus a retry.
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
powershell -ExecutionPolicy Bypass -File scripts\verify_phase5.ps1   # report, citation audit, SSE + replay
```

Pass `-Base http://localhost:8001/api/v1` when another app already owns `:8000`.

`verify_phase5.ps1` covers the API and the stream, not the pixels: the report panel is verified
by `npm run build` plus a manual pass of the flow, and both sets of numbers are recorded in
`docs/TECH_DECISIONS.md` (Phase 5 evidence).

Each script fails loudly with the exact HTTP status / detail, so a red line always
means something real.
