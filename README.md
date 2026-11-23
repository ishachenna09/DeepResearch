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
│   │   ├── evals/              # deterministic graders for the CI gate (Phase 7)
│   │   ├── models/  schemas/  services/  workers/
│   ├── alembic/                # migrations
│   └── tests/                  # unit | integration | graph | e2e
├── evals/                      # golden cases + recorded fixtures + run_evals.py
├── frontend/                   # React + Vite + Tailwind
│   └── src/                    # pages/{Research,JobDetail,Dashboard} · components/{research,report}/ · hooks/{useJob,useReport}.ts · lib/{api,research,report,inflight,types}.ts
├── scripts/                    # start_* , verify_phase*.ps1, seed_questions.py, demo.ps1
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
| **6** (done) | Writer + critic extracted as **A2A services** | **A2A** protocol, AgentCards, distributed agents |
| **7** (done) | Eval gate in CI + Langfuse traces + cost dashboard | Deterministic evals, trace sessions, shadow cost |
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
`npm run dev` (terminal 3, from `frontend/`). For Phase 6 add
`scripts\start_agents.ps1` (terminal 4) and set `AGENTS_MODE=a2a` for the API;
the default `local` mode needs no agents at all.

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
| `GET` | `/health/agents` | A2A agent cards (writer/critic) or `mode=local` when they run in-process |
| `GET` | `/health/langfuse` | `ok` / `disabled` / `error` - "configured" is not "working" |
| `GET` | `/usage?days=30` | measured tokens + shadow cost for the caller (`scope=all`: admin) |

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

### The agents (Phase 6)

The writer and the critic are now **A2A agents**: two separate services that the API talks to over
JSON-RPC instead of importing. Same code, same prompts - the point is the boundary, so the work can
be scaled, restarted and (in Phase 10) deployed independently.

| Service | Port | Card / skill | Returns |
|---|---|---|---|
| `app.a2a.writer_app:app` | `8010` | `deepresearch-writer` / `write-cited-report` | streamed markdown artifact + metadata (`tokens`, `model`, `finish_reason`, `truncated`, `chars`, `citations`) |
| `app.a2a.critic_app:app` | `8011` | `deepresearch-critic` / `critique-coverage` | one JSON artifact `{sufficient, gaps, reason}` + `tokens`/`model`, `gaps` capped at `max_gaps` |

```powershell
scripts\start_agents.ps1            # start both, wait for /healthz, write pid files
scripts\start_agents.ps1 -Stop      # stop both
```

Then run the API with `AGENTS_MODE=a2a` (in `.env` or the shell); the default `local` needs no
agents at all, so the graph is still testable and runnable without a second process.

- **Discovery, not hard-coded addresses.** Each service publishes an AgentCard at
  `/.well-known/agent-card.json` (`protocolVersion: "1.0"`, `protocolBinding: "JSONRPC"`,
  `capabilities.streaming: true`, and the skill it implements). The API fetches the card and
  reads the RPC endpoint from `supportedInterfaces`; a moved agent is a URL change, not a code change.
- **Auth on the RPC, never on the card.** A shared bearer token (`A2A_TOKEN`) is checked with
  `secrets.compare_digest` before JSON-RPC parsing - so no token / wrong token is a plain `401`
  with `WWW-Authenticate: Bearer`, not a protocol error. The card and `/healthz` stay public
  (discovery has to work), and a service with no token configured answers `503` rather than
  running open.
- **Streaming is real.** `SendStreamingMessage` sends a `Task` first, then status updates, then
  artifact chunks (`append=false` creates the artifact, `append=true` extends it) and a final
  empty chunk carrying metadata. The API reassembles them; the report SSE the browser sees is
  unchanged, because the writer's chunks feed the same `emit()` as before.
- **`SendMessage` for the critic, `SendStreamingMessage` for the writer.** A verdict is one small
  JSON object - streaming it would be three frames of ceremony. A report is thousands of tokens
  over 6-11 s, so it streams.
- **Failure is contained and named.** If the writer is down the report task fails with
  `AgentUnavailable: http://127.0.0.1:8010: transport error`, not a generic 500: an operator can
  tell "a dependency is down" from "the model wrote nothing". Restarting the service and
  regenerating recovers. The remote critic is softer - any agent failure becomes
  `stop_reason="critic_error"`, which ends the loop keeping the notes already gathered.
- **The orchestrator keeps the decisions.** AgentCard data, the note registry, the citation audit
  and `decide_stop` stay in the API. A remote agent may *propose* a verdict; it never decides
  whether the loop stops - that is the difference between a tool call and handing over control.
- **Tasks survive a restart.** A `GetTask` after the agent has been restarted still returns the
  task and its artifacts: `RedisTaskStore` writes each task as JSON with `A2A_TASK_TTL` (3600 s)
  and falls back to memory if Redis is unreachable. `list()` is still in-process - documented,
  not pretended.
- **Health is end-to-end.** `/healthz` probes both agents in a2a mode (card discovery) and
  `/health/agents` reports `mode=a2a` plus each agent's name, or `mode=local` when they run
  in-process.

### Evals, traces and cost (Phase 7)

The quality question is answered with arithmetic, not vibes: a golden set, recorded artifacts,
and a gate that runs in ~1 s in CI.

```powershell
.\.venv\Scripts\python.exe evals\run_evals.py                       # CI mode (fixtures)
.\.venv\Scripts\python.exe evals\run_evals.py --mode live --case llm-failover-design
.\.venv\Scripts\python.exe evals\run_evals.py --mode live --record   # new fixture
.\.venv\Scripts\python.exe scripts\check_traces.py <job-id>          # did the span land?
```

- **The gate is deterministic.** `api/app/evals/graders.py` applies thresholds per case
  (markers, precision, coverage, words, distinct source domains, notes, tokens, model, stop
  reason, truncation, uncited) and re-runs the production `validate()` when the fixture carries
  the numbered registry. A stored `validation` that no longer describes the text fails
  `audit_matches_recompute` - the gate does not take the run's word for it.
- **Why not DeepEval / Ragas / promptfoo?** Their distinguishing feature is an LLM judge (or a
  Node toolchain). A judge costs a call per case, is nondeterministic, and cannot be the reason
  a pull request turns red. The metrics that matter here are arithmetic over text, so they run
  as plain Python in under a second. Qualitative review still happens - in Langfuse, by a
  human, on the traces the gate points at.
- **The gate has negative controls.** Every run strips the markers from a passing artifact and
  appends an invented `[99]`; both must be rejected. A gate that has never said no is
  decoration, and a refactor that stops checking a metric is caught by the gate itself.
- **Two modes, two costs.** `fixtures` is what CI runs (no DB, no provider). `live` drives the
  API end to end and grades the fresh result; `--record` turns a pass into the next fixture.
  CI never calls a provider, so a fork's PR cannot spend the maintainer's quota.
- **CI is one workflow.** `.github/workflows/ci.yml`: install, `pytest api/tests`, then
  `python evals/run_evals.py`; the JSON report is uploaded as an artifact. Both commands run
  locally before the push - a red badge is not the first test.
- **Traces are findable.** Each report's generation runs inside `propagate_attributes`:
  `session_id=<job id>`, `tags=[report, mode:local|a2a]`, metadata with the report id. The
  LangChain callback creates the spans, so this is the seam that labels them. `/health/langfuse`
  distinguishes `disabled` (no keys) from `error` (keys rejected/unreachable), and
  `check_traces.py` queries the observations API by session to prove a span landed.
- **Cost is measured tokens × a stated price table.** `/usage` sums `research_jobs.tokens_used`
  and `reports.tokens_used`; dollars are a *shadow* estimate at list prices (70/30 input/output
  blend, table dated in the response). The free tier bills $0 - the point is that a token
  regression becomes visible as money. `scope=all` is admin-only: cost is still user data.

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
deterministically instead of relying on a live model to misbehave on cue. The A2A layer is tested
the same way: payloads that must survive protobuf (a `None` title, ints that arrive as floats), the
event order the SDK requires (Task before status before chunks), the SSE parser, the accumulator
that reassembles a report, and the ASGI surface (card public, RPC 401/503, `-32601`/`-32001`).
The eval gate is tested by breaking artifacts on purpose: stripped markers, an invented `[99]`,
a tampered `validation`, a one-site report, unrecorded tokens.

## Verify a phase (one command)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify_phase2.ps1   # MCP tools, extraction, SSRF
powershell -ExecutionPolicy Bypass -File scripts\verify_phase3.ps1   # fan-out, cache, pgvector
powershell -ExecutionPolicy Bypass -File scripts\verify_phase3.ps1 -SkipCache   # skip the 2nd run
powershell -ExecutionPolicy Bypass -File scripts\verify_phase4.ps1   # critic, gap round, tokens, dedupe
powershell -ExecutionPolicy Bypass -File scripts\verify_phase4.ps1 -SkipSecond  # skip the control run
powershell -ExecutionPolicy Bypass -File scripts\verify_phase5.ps1   # report, citation audit, SSE + replay
powershell -ExecutionPolicy Bypass -File scripts\verify_phase6.ps1   # A2A cards, auth, agent-generated report, failure
powershell -ExecutionPolicy Bypass -File scripts\verify_phase7.ps1   # eval gate, /usage, Langfuse spans
powershell -ExecutionPolicy Bypass -File scripts\verify_phase7.ps1 -Live   # + regenerate a report
```

`verify_phase6.ps1` owns its processes: it starts the writer, the critic and a **second API on
`:8002`** with `AGENTS_MODE=a2a`, then stops all three in `finally` - your `:8001` is untouched.
It proves the report really went *through the agent* (the writer's log gains a task) and that a
stopped writer fails the report with `AgentUnavailable` rather than silently falling back to the
local path. `-SkipE2E` runs the protocol checks only; `-KeepRunning` leaves the stack up.

`verify_phase7.ps1` starts a private API on `:8003`, runs the fixture gate and its negative
controls, checks `/usage` + RBAC + Langfuse auth, then stops it. `-Live` also regenerates one
report, asserts its tokens appear in `/usage`, and queries Langfuse by session to prove the span
landed. It shares Postgres, so do not run it while a report is in flight on another API.

Pass `-Base http://localhost:8001/api/v1` when another app already owns `:8000`.

`verify_phase5.ps1` covers the API and the stream, not the pixels: the report panel is verified
by `npm run build` plus a manual pass of the flow, and both sets of numbers are recorded in
`docs/TECH_DECISIONS.md` (Phase 5 evidence).

Each script fails loudly with the exact HTTP status / detail, so a red line always
means something real.
