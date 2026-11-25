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
│   │   ├── api/v1/             # auth, research, reports, library (list + export), admin, health
│   │   ├── agents/
│   │   │   ├── state.py        # ResearchState TypedDict (graph contract)
│   │   │   ├── graph.py        # LangGraph topology + checkpointer
│   │   │   └── nodes/          # plan, fanout, search, merge, critic, outline, write, citations
│   │   ├── a2a/                # AgentCards + agent services (Phase 6)
│   │   ├── mcp_tools/          # FastMCP servers: search, fetch, notes (Phase 2)
│   │   ├── rag/                # page cleaner, chunker, embedder, retriever
│   │   ├── guardrails/         # untrusted-web-content scanning, citation rules
│   │   ├── evals/              # deterministic graders for the CI gate (Phase 7)
│   │   ├── services/           # LLM, pricing, trace labelling, report_export (MD/PDF)
│   ├── alembic/                # migrations
│   └── tests/                  # unit | integration | graph | e2e
├── evals/                      # golden cases + recorded fixtures + run_evals.py
├── frontend/                   # React + Vite + Tailwind
│   └── src/                    # pages/{Research,JobDetail,Dashboard,Library,Admin} · components/{research,report,layout}/ · hooks/{useJob,useReport}.ts · lib/{api,research,report,library,inflight,types}.ts
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
| **8** (done) | Report library, MD/PDF export, admin overview | Derived artifacts vs object storage, PDF generation, RBAC |
| **9** (done) | Hardening: rate limits, SSRF defense, load + security tests | Production concerns |
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
| `GET` | `/tools/selftest` | admin-only: runs one real search + fetch through MCP (spends quota) |
| `GET` | `/healthz`, `/health/llm` | dependency health; `/health/llm` is **authenticated** (5/min) because it spends a real LLM call |
| `GET` | `/health/agents` | A2A agent cards (writer/critic) or `mode=local` when they run in-process (30/min/IP) |
| `GET` | `/health/langfuse` | `ok` / `disabled` / `error` - "configured" is not "working" (10/min/IP) |
| `GET` | `/usage?days=30` | measured tokens + shadow cost for the caller (`scope=all`: admin) |
| `GET` | `/reports?q=&status=&sort=&limit=&offset=` | the caller's report library: question + audit scalars, no markdown, with a pre-pagination `total` |
| `GET` | `/reports/{id}/export.md` | the stored report as a Markdown attachment (404 if not yours, 409 if not `ready`) |
| `GET` | `/reports/{id}/export.pdf` | the same report as a paginated A4 PDF (ReportLab, Unicode font, numbered sources) |
| `GET` | `/admin/overview?days=30` | admin-only snapshot: users, jobs, reports, tokens, shadow cost, per-day/per-model, top users, recent failures |

Status machine: `planning -> awaiting_approval -> researching -> notes_ready`
(or `rejected` / `failed`). Poll `GET /research/{id}`; a run takes ~2-4 minutes.

Every route is rate-limited (Phase 9). A refusal is **`429`** with a `Retry-After` header in
seconds and the usual security headers; the client should wait, not retry immediately. Limits are
per user when the route has an authenticated caller and per IP otherwise - the table is in
[Hardening (Phase 9)](#hardening-phase-9).

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

### Library, exports and admin (Phase 8)

The product end of the pipeline: find the reports you have written, take one away as a file,
and see what the whole deployment is doing.

- **The library is a join, not a new table.** `GET /reports` reads `reports` joined to
  `research_jobs` for the question, owner-scoped by `Report.user_id`. Rows ship the audit
  scalars (`words`, `markers`, `precision`, `coverage`, citation count, tokens, model) so the
  list can search, filter and sort in SQL without moving a megabyte of markdown to render
  titles. `total` is computed before `limit`/`offset`, so paging is not a lie. Search escapes
  `%` and `_`: a search box is text, not a pattern.
- **Exports are generated on demand, not stored.** An MD or PDF is a pure function of the row
  (markdown + citations + validation + question). There is no bucket, no signed URL and no second
  copy that can drift; a rerun overwrites the row and the next download is current, which is why
  the response is `Cache-Control: no-store`. Object storage earns its place when an artifact must
  be immutable (an emailed attachment, a legal archive) - `docs/TECH_DECISIONS.md` §Phase 8
  records the R2 design we would use then.
- **PDF is ReportLab, not a browser.** ReportLab is pure Python and deterministic, where
  WeasyPrint needs Pango/GTK and a headless Chrome is a ~300 MB process per export. Bitstream
  Vera ships with ReportLab, so an en dash or a curly quote is text rather than a box (the
  built-in Helvetica is Latin-1 only). Rendering runs in a worker thread - it is synchronous and
  would otherwise stall the event loop.
- **A small markdown subset, because HTML is the wrong intermediate.** ReportLab consumes
  flowables, not block HTML, so the 120-line parser turns the writer's output (headings,
  paragraphs, bullets, quotes, code, rules and the pipe tables the interview report emits) into
  Paragraph/Table/Preformatted. `_inline` escapes HTML first and then adds only `<b>`, `<i>`,
  `<link>` and a monospace font, so a scraped `<` cannot become a tag.
- **Exports have rules.** Only your own reports (another id is a 404, never a 403 that confirms
  it exists) and only `ready` ones (409 otherwise - a half-written file is not an answer). The
  download is an `attachment` with an ASCII slug plus the first 8 hex characters of the report
  id, so two answers to the same question do not collide. The frontend fetches the bytes with
  axios (a plain link cannot send the Bearer header) and reads the filename from
  `Content-Disposition`, which is why CORS exposes that one header.
- **One admin snapshot.** `GET /admin/overview` aggregates users, jobs, reports, tokens and
  shadow cost in the window in one round trip, includes failed jobs and reports (they burned
  tokens too), and returns `generated_at` so a stale dashboard says so. `require_role("admin")`
  answers 403 before any query runs - the hidden nav link is UX, not the control.
- **No user-management API.** Role changes are rare; `scripts/set_role.py --list` and
  `--email ... --role admin` keep that capability where the operator already has database
  access. A `PATCH /admin/users/{id}` is a privilege-escalation surface waiting for one
  authorization bug, and is not worth building before there is an audit trail to go with it.

## Hardening (Phase 9)

No new feature: the question is what happens when the input is hostile or the load is real.
Everything below is proven by `scripts/verify_phase9.ps1`, including the failures.

### Rate limits

| Route | Bucket | Limit |
|---|---|---|
| `POST /auth/register` | IP | 5 / hour |
| `POST /auth/login` | IP **and** email | 10 / min and 20 / hour |
| `POST /auth/refresh` | IP | 30 / min |
| `POST /research` | user | 5 / hour and 20 / day |
| `POST /research/{id}/plan/approve` | user | 20 / hour |
| `POST /research/{id}/report` | user | 10 / hour |
| `POST /tools/search` | user | 30 / hour |
| `POST /tools/fetch` | user | 60 / hour |
| `GET /reports` | user | 120 / min |
| `GET /reports/{id}/export.*` | user | 30 / min |
| `GET /admin/overview` | user | 60 / min |
| `GET /health/llm` | user | 5 / min |
| `GET /health/agents` | IP | 30 / min |
| `GET /health/langfuse` | IP | 10 / min |
| **everything else** | IP | 300 / min (global backstop) |

- **Fixed window in Redis, written here (~70 lines), not SlowAPI.** One `INCR` + `EXPIRE` per
  request and a 429 shape we control (`Retry-After`, security headers). The algorithm's known
  weakness - up to 2x across a window boundary - is an acceptable trade for abuse control and is
  written down in `docs/TECH_DECISIONS.md` rather than hidden.
- **Two layers.** Per-route dependencies say *what* is limited ("5 signups per hour"); a pure-ASGI
  middleware enforces a **300/min/IP** floor on every path, including `/docs` and 404s, so the next
  route someone forgets is still bounded.
- **Two identities.** Authenticated routes key on the user id; anonymous ones on the client IP.
  `X-Forwarded-For` is **ignored** unless `TRUST_PROXY_HEADERS=true`, because trusting it by default
  lets a caller pick their own bucket. Phase 10 turns it on behind Caddy.
- **Login is limited twice.** Per IP catches a single noisy host; per email (a salted SHA-256, never
  the address) catches a botnet stuffing one mailbox.
- **Redis down falls back to an in-process counter.** Fail-open would make limits advisory, fail-closed
  would turn a cache outage into an API outage. The per-worker caveat is documented, not ignored.
- **Replaying a rotated refresh token revokes the whole family.** It is the only response that stops
  a stolen token from working after the theft is detected; both sides sign in again.
- **Verification controls the limiter; the limiter does not control verification.**
  `verify_phase6/7/8` start their own API with `RATE_LIMIT_ENABLED=false` (they prove A2A, evals
  and exports), and `verify_phase3/4/5` clear only the `dr:rl:*` counters before creating their
  jobs, so a second run inside the hour is not refused. The limits themselves are proven by
  `verify_phase9.ps1`.

### Concurrency and resource caps

- **One active research job per user**, enforced by a real partial unique index
  (`(user_id) WHERE status IN ('planning','researching')`), not an application check. Two concurrent
  creates both pass a "does an active job exist?" query - the database is the only component that
  can lock the *absence* of a row. The `IntegrityError` becomes a 409.
- **Approve takes `FOR UPDATE`** on the job, so a double-click cannot fan out two research runs.
- **Report start takes `pg_try_advisory_xact_lock(hashtextextended(job_id,0))`.** The lock releases
  with the transaction even if the process dies - no TTL to tune, no stale lock to clear.
- **PDF rendering is capped at two concurrent exports** (`asyncio.Semaphore(2)`); overflow is a
  **503 + `Retry-After`**, which tells the client the truth in one round trip. ReportLab is CPU-bound
  in a worker thread, and four exports on a 2-vCPU box starve the event loop.

### SSRF and outbound fetches

- **Resolve once, then connect to the validated IP.** `resolve_public_url()` returns a
  `ResolvedTarget`; the fetch sends the IP as the URL, the original name as `Host`, and the name
  again as TLS `sni_hostname`. httpx cannot "resolve it again and get a different answer", because
  it never resolves the name.
- **`ip.is_global` plus explicit ranges.** CGNAT (100.64/10), NAT64, 6to4, benchmarking, TEST-NETs,
  multicast and ULA are blocked even where `is_global` is ambiguous. A name whose DNS answer mixes
  public and private addresses is rejected outright.
- **Every redirect hop is re-validated and re-pinned**, so a public URL that 302s to `127.0.0.1`
  stops at the 302.
- **Bounded download.** At most 2 MB is read (`aiter_bytes` stops at the cap), text-only content
  types are allowed, and a declared `Content-Length` over the cap is rejected before the body.
  `trust_env=False` stops an ambient `HTTP_PROXY` from routing around the pin.
- **Cheap diagnostics stayed public; expensive ones did not.** `/health/llm` spends a real provider
  call, so it now needs a user (and a 5/min budget); `/tools/selftest` is admin-only.

### Registration

`REGISTRATION_MODE=open` locally; `invite` requires an `X-Invite-Code` header matching
`REGISTRATION_INVITE_CODE`. The switch exists now so Phase 10 can close signups with an env var
rather than a code change.

### Measured baseline (honest numbers)

`scripts/load_test.py` runs three scenarios on the app's own `httpx` (no Locust/k6 dependency):

| scenario | result |
|---|---|
| read (3 users x 2 s) | 41 requests, p50 **2,032 ms**, p95 **2,982 ms**, no 5xx |
| export (4 concurrent, PDF/MD) | 42 requests, p95 **2,980 ms**, no 5xx |
| login burst (wrong password) | 24 attempts, 429 at least once (10/14 or 19/5 split by the minute boundary), `Retry-After` + security headers present |

The ~3 s p95 is the **dev database, not the code**: a cold `SELECT 1` to the free-tier Neon instance
took **4,253 ms**, a warm user lookup **452 ms**, a report query **671 ms**, while an unauthenticated
request answers in **9 ms** and Redis `PING` on a reused client is **3 ms**. Phase 10 puts the API
and the database in one region; the same code then pays single-digit milliseconds. The number is
recorded as measured rather than relabelled.

Residual risks carried into Phase 10 (fixed-window boundary burst, per-worker fallback, no WAF,
public `/docs`) are listed in `docs/TECH_DECISIONS.md` §Phase 9 - each with the control that
closes it.

## Frontend (research UI)

```powershell
cd frontend
npm run dev          # http://localhost:5173 (Vite reads .env* files, not process env)
```

Routes: `/` ask a question + your jobs · `/library` searchable report list with MD/PDF downloads ·
`/jobs/:jobId` plan approval, live run, evidence + cited report · `/system` health badges ·
`/admin` the admin overview (nav link only for the `admin` role).

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
a tampered `validation`, a one-site report, unrecorded tokens. The export tests read the PDF back
with `pypdf` and look for the question, the audit line, the Sources list and the page footer:
`%PDF-` alone would also pass for a blank page. `parse_blocks` is pinned against a pipe table,
and `build_context` is tested with both an ORM-shaped row and a fixture dict.

Phase 9 adds the adversarial files: `test_netguard.py` (blocked destinations without DNS, host
normalisation, the pinned-IP `Host`/SNI contract, redirect re-pinning, the streaming cap),
`test_sanitize.py` (zero-width/soft-hyphen/bidi/NFKC normalisation, each injection shape, and the
rule that the scan runs **before** truncation), `test_security.py` (alg pinning, expiry, the full
401 matrix over `app.openapi()`, HSTS only in prod, CORS, error hygiene, 404-not-403), and
`test_ratelimit.py` / `test_export_limits.py` (window arithmetic, Redis-down fallback, the PDF
semaphore). No network, no LLM, no clock dependence.

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
powershell -ExecutionPolicy Bypass -File scripts\verify_phase8.ps1   # library, MD/PDF export, admin
powershell -ExecutionPolicy Bypass -File scripts\verify_phase9.ps1   # rate limits, SSRF, concurrency, load, security
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

`verify_phase8.ps1` starts a private API on `:8004` (no provider calls) and proves the library is
owner-scoped with real audit numbers, that search/status/sort work and bad input is a 422, that the
Markdown is an attachment with the stored audit and sources, and that the PDF has real pages and
extractable text (read back with `pypdf`, not a magic-byte check). It also registers
`other@test.com`/`admin@test.com` if missing, promotes the latter with `set_role.py`, and checks
cross-user report ids are 404, `/admin/overview` is 403 for an agent and complete for an admin,
and `days=0` is a 422. It writes to the shared dev database; `-KeepRunning` leaves the API up.

`verify_phase9.ps1` starts a private API on `:8006` and stops it in `finally`. It resets **only**
the limiter keys (`dr:rl:*`) so a previous burst cannot poison the run, then proves: security
headers on a normal response and on a 429; forged `Host` -> 400; the auth matrix; the SSRF list
-> 400 through the real `/tools/fetch` path; refresh-token replay -> 401 **and** the whole family
revoked; the partial unique index against the real database (a throwaway probe user is deleted in
`finally`); cross-user report/job ids -> 404; a short read run and four concurrent PDF exports with
no 5xx; a login burst that must produce a 429; and the full unit suite. It makes **no LLM call**.

Pass `-Base http://localhost:8001/api/v1` when another app already owns `:8000`.

`verify_phase5.ps1` covers the API and the stream, not the pixels: the report panel is verified
by `npm run build` plus a manual pass of the flow, and both sets of numbers are recorded in
`docs/TECH_DECISIONS.md` (Phase 5 evidence).

Each script fails loudly with the exact HTTP status / detail, so a red line always
means something real.
