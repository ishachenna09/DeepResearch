# Tech Decisions — "Why this, not that?" (interview prep)

Rule for every answer: **decision → alternative → why we chose it → the trade-off we accepted.**

## Orchestration & AI

| Tech we use | Why | Alternatives | Why NOT them | The trade-off we accepted |
|---|---|---|---|---|
| **LangGraph** | Research needs *loops* (critic → search again), **durable state**, and **HITL pause/resume** (`interrupt()` freezes the graph; resume continues exactly there) | LangChain LCEL chains; CrewAI; AutoGen; Haystack | LCEL chains are linear — no safe loops/pause; CrewAI/AutoGen hide control flow and are harder to test/trace | Steeper learning curve; more boilerplate than a chain |
| **LangChain** | Best ecosystem for loaders, splitters, document utils, chat abstractions — used as *libraries*, not as the orchestrator | LlamaIndex, Haystack, raw SDKs | LlamaIndex is retrieval-centric (weak agent loops); raw SDKs = reinventing chunkers/loaders | Two frameworks to know; we keep LangChain at the edges only |
| **LiteLLM proxy** | One OpenAI-compatible endpoint; **automatic fallback Gemini→Groq** on rate limit/timeout/context overflow; spend tracking | Custom try/except in app code; OpenRouter (third-party proxy) | Hand-rolled failover misses edge cases (streaming, 429 backoff) and duplicates work; OpenRouter routes to *their* list, we want our own free-tier keys | One more service to run (pip process locally, container on EC2) |
| **Gemini + Groq free tiers** | $0, no card for Gemini/Groq; Gemini = long context (1M), Groq = speed + failover | OpenAI, Anthropic, self-hosted Ollama | No permanent free tiers (OpenAI free = 3 RPM/useless; Anthropic = none) | Rate limits ~15–30 RPM → mitigated by LiteLLM fallback + queueing |
| **Local embedding model** (sentence-transformers) | Free, private, no API dependency, fast for <100k chunks | Gemini embeddings API, OpenAI, Voyage | Extra API dependency + cost + data leaves the machine | Needs ~500MB RAM on EC2 (t3.micro is 1GB — acceptable at our scale) |

## Tools & agent-to-agent

| Tech we use | Why | Alternatives | Why NOT them | Trade-off |
|---|---|---|---|---|
| **MCP** (FastMCP) | Standard *tool* protocol: write `web_search` once → usable by LangGraph agents, Claude, any MCP client. Interview-relevant (industry adoption) | Native LangChain tools; OpenAPI function-calling; A2A for tools | LangChain tools are Python-locked (can't be reused by other apps); A2A is for *agents*, not tools — different layer | JSON-schema overhead; one more process to run |
| **A2A** (Google's protocol) | Standard *agent-to-agent* protocol with **AgentCards** (discovery): searcher/critic/writer become independent services that scale & fail separately | LangGraph subgraphs; shared in-process calls; REST endpoints ad-hoc | In-process subgraphs = no independent scaling/fault isolation and it's just "functions", not a story interviewers recognize as the industry direction | HTTP latency per call — mitigated by parallel fan-out; more moving parts |

## State, data & streaming

| Tech we use | Why | Alternatives | Why NOT them | Trade-off |
|---|---|---|---|---|
| **Redis** | 4 jobs: (1) **LangGraph checkpoints** = crash-safe resume, (2) **page cache** = dedupe fetches across jobs, (3) **pub/sub** = fan out SSE tokens to browser, (4) **rate limits/budget counters** | Postgres for everything; in-memory dict; Kafka | Postgres polling = slow + DB churn; in-memory = lost on restart (no crash recovery); Kafka = overkill ops for this scale | Data is ephemeral (TTL) — durable truth stays in Postgres |
| **PostgreSQL + pgvector** | One database for users/reports/sources/**vectors** — filtered search by `job_id`, no extra vendor | Pinecone, Weaviate, Qdrant, Chroma, Milvus | Pinecone = paid vendor lock-in; Qdrant/Chroma = another service to run for no gain at our scale; pgvector keeps `WHERE job_id=` joins trivial | Weaker ANN performance at >10M vectors (honest: we'd shard/partition then) |
| **SSE** (Server-Sent Events) | Report text flows **one way, server→browser**, auto-reconnect built into `EventSource`; simpler than WS for token streams | WebSockets; polling | WebSockets = bidirectional (we don't need client→server mid-stream) + harder through proxies; polling = laggy + wasteful | One-way only — control messages go over normal REST |
| **ARQ worker** (Redis-backed) | Async jobs: page fetch/embedding without blocking requests; same Redis = no extra infra | Celery, RQ, Dramatiq, FastAPI BackgroundTasks | Celery = heavy (needs broker+result backend, sync); RQ = Redis-only but sync/older; BackgroundTasks = dies with the request (no retry) | Smaller ecosystem than Celery |

## Frontend, auth, ops

| Tech we use | Why | Alternatives | Why NOT them | Trade-off |
|---|---|---|---|---|
| **React + Vite + Tailwind** | Vite = instant dev server; Tailwind v4 = zero config; React = market-standard skill | Next.js, Vue, Svelte, plain HTML | Next.js SSR/SEO is useless behind a login; Vue/Svelte = smaller job market | No SSR/SEO (fine for an authenticated app) |
| **JWT access + rotating refresh (HttpOnly cookie)** | Stateless API auth at scale; refresh survives access-token expiry; rotation limits stolen-token life | Session IDs in Redis; long-lived JWT | Sessions = every request hits Redis; long-lived JWT = no revocation | JWT revocation needs the refresh-token DB (we have it) |
| **Authlib OAuth (Google/GitHub) + own JWT** | OAuth gets identity, *we* mint our own 15-min token — never trust/forward provider tokens downstream | Auth0, Clerk, Supabase Auth | Free tiers hit MAU limits + vendor lock-in; we'd be demoing someone else's product | We implement refresh/rotation ourselves (that's the point — it's a resume skill) |
| **Langfuse (self-host/cloud)** | Traces + cost per request + **eval scores** in one OSS tool | LangSmith, Arize Phoenix, Helicone, OpenTelemetry+Grafana | LangSmith = paid beyond dev tier; Phoenix = traces but weaker product/cost UI; raw OTel = no LLM-specific UI | Cloud free tier has span limits (fine for dev; we sample in prod) |
| **DeepEval + Ragas + promptfoo** | DeepEval = agent metrics (tool use, hallucination); Ragas = RAG metrics (faithfulness); promptfoo = **CI gate** that fails the build | LangSmith evals, Braintrust, TruLens | Braintrust/LangSmith = paid; TruLens = unmaintained-ish | Three tools = three configs — we keep one shared golden dataset |
| **FastAPI** | Async SSE, Pydantic schemas, OpenAPI docs for free, Python = AI ecosystem home | Flask, Django, Django Ninja | Flask/Django = no native async SSE story; Django = heavier ORM opinions | Async SQLAlchemy/alembic setup is fiddly (one-time cost) |
| **Docker (EC2 + GitHub Actions only)** | Reproducible prod; builds in cloud CI — laptop has no Docker | Docker Desktop locally; serverless (Lambda/Cloud Run) | Desktop = not possible/needed here; Lambda = cold starts + duration limits for long research jobs | You never run containers locally — CI builds, EC2 pulls |
| **AWS EC2 (free tier) + Vercel** | EC2 = full control (Docker, Caddy, background workers); Vercel = free instant frontend hosting | Railway/Fly/Render, VPS | Fly/Railway free tiers shrank to trials; Render sleeps and needs a card | 12-month AWS free window; EC2 = you patch the OS |
| **GitHub Actions** | CI free for public repos: lint → test → **eval gate** → image build → deploy | GitLab CI, Jenkins | Jenkins = self-hosted ops burden; GitLab = fine but we're already on GitHub | Public repo = code visible (fine — portfolio piece) |
| **Caddy** | Automatic Let's Encrypt TLS with one config line | Nginx, Traefik | Nginx = manual cert renewal (certbot), more config; Traefik = Docker-centric | Fewer plugins than Nginx |

## One-line versions (memorize these)

- **"Why LangGraph?"** — "The critic needs loops and HITL needs pause/resume; LangGraph's checkpointer gives crash-safe, resumable state, which chains can't."
- **"Why a critic, not just 'always 2 rounds'?"** — "The planner guesses what matters *before* seeing evidence, and a human can cut the plan at the approval gate. The critic compares what the answer needs with what we actually collected - and it stops on four explicit brakes, not on hope."
- **"Why poll instead of SSE on the job page?"** — "The job only has ~5 coarse state changes over 1-4 minutes. A 2 s poll is one small `GET` and survives every proxy and every laptop sleep. SSE is for the *report* in Phase 5, where the payload is a token stream rather than an enum."
- **"Why two plan schemas?"** — "One is an input contract, one is a storage contract. Tightening a validation range for today's planner must never 500 on a row written last week - that is exactly how `budget_tokens >= 50_000` broke the job list."
- **"Why MCP *and* A2A?"** — "Different layers: MCP standardizes *tools* (my search tool is reusable by any client); A2A standardizes *agents* (my writer is a discoverable service). One protocol can't do both."
- **"Why Redis over just Postgres?"** — "Postgres is durable truth; Redis is ephemeral speed — checkpoints for crash recovery, a page cache to dedupe fetches, and report token streams that cost nothing to replay."
- **"Why Redis Streams, not pub/sub?"** — "Pub/sub is fire-and-forget: a tab opened mid-report sees the tail of an answer with no beginning. A Stream keeps the last N entries, so a refresh or a second tab resumes from `Last-Event-ID` on the same code path as a live reader."
- **"Why SDK on the server, hand-rolled client on the caller?"** — "The server must pass anyone's conformance tests, so it uses the official `a2a-sdk`. The caller needs an exact read timeout, no retry after a 401, and no replay of deltas it already rendered - three behaviours I could not configure, so I wrote the 300-line client that does exactly what the report path needs."
- **"Why is the orchestrator still local?"** — "Agents propose, the orchestrator decides. The registry, the citation audit and `decide_stop` stay in the API: a remote agent must not be able to extend its own budget or declare the loop finished."
- **"How do you know it's correct?"** — "Citation precision ≥0.90 on a golden set, gated in CI; every claim links to cached source text so a human can verify."
- **"Why a deterministic gate instead of DeepEval/Ragas?"** — "An LLM judge costs a call per case and gives a different verdict on the same input; citation precision is arithmetic, so the gate is plain Python that runs in under a second and can be the reason a pull request turns red."
- **"Why recorded fixtures *and* negative controls?"** — "CI grades artifacts from real runs, so a fork's PR cannot spend my provider quota; and every run strips the markers and appends an invented `[99]`, both of which must fail — a gate that has never said no is decoration."
- **"Why shadow cost when everything is free?"** — "The invoice is $0, so a token regression would be invisible. Pricing measured tokens at list prices with one stated 70/30 blend turns 'this got slower/wordier' into 'this got 3× more expensive'."
- **"Why wrap generation in `propagate_attributes`?"** — "The LangChain callback creates the spans, not my code; ambient attributes are the one seam that labels them. Session = job id, tag = `report`, so one trace is findable without knowing an internal trace id."
- **"Why not OpenAI?"** — "No usable free tier; we're on Gemini (1M context, free) with Groq failover behind LiteLLM — one config line to swap."
- **"Why no S3/R2 for exports?"** — "An export is a pure function of a Postgres row: markdown, citations, audit. A bucket adds credentials, lifecycle rules and a second copy that can go stale to serve a download link that is cheaper to render in 0.2 s."
- **"Why ReportLab instead of WeasyPrint or headless Chrome?"** — "WeasyPrint needs Pango/GTK and Chrome is a 300 MB process per export. ReportLab is pure Python and ships Bitstream Vera, so an en dash is text, not a box — and the test reads the PDF back instead of trusting `%PDF-`."
- **"Why download blobs instead of signed links?"** — "There is nothing to sign and a URL would put the JWT in browser history. Axios sends the Bearer header, the bytes become an object URL, and CORS exposes exactly one header: `Content-Disposition`."

---

## Incident log (ops learnings - each one is an interview story)

| Date | What broke | Root cause | Fix | Lesson |
|---|---|---|---|---|
| 2026-10-28 | LLM fallback chain returned 404 | Groq retired `llama-3.3-70b-versatile` on 2026-08-16 (free/dev tiers); the backup tier was silently dead until the primary 503'd | Backup -> `groq/openai/gpt-oss-120b`; added a 3rd tier (`gemini-2.5-flash-lite`); `/health/llm` now reports `served_by` | "A fallback you never exercised is not a fallback." Hosted model IDs are a moving dependency - pin, verify, and test the failover path. |
| 2026-10-29 | API refused to start: `RedisSearchError: Upstash Redis does not support FT.* commands` | `langgraph-checkpoint-redis` requires RedisJSON + RediSearch (it builds FT.* indices in `asetup()`); Upstash is a restricted serverless Redis with its own `SEARCH.*` API and no modules | Moved checkpoints to **Redis Cloud free 30MB** (Redis 8.4, `search` + `ReJSON` loaded). Upstash kept for plain cache/queues later | "Redis" is not one thing. Capability beats brand: check `MODULE LIST` before adopting a provider. Fail at startup (lifespan), never at first request. |
| 2026-10-29 | Langfuse showed no traces at all | `get_llm()` built `ChatOpenAI` with no `callbacks=`, so nothing was ever exported; the Langfuse client existed but was never attached | `callbacks=[CallbackHandler()]` in `get_llm()` + instantiate the client in `lifespan` so bad keys fail fast | "Observability you never looked at is observability you do not have." Verify the trace appears in the UI, not just that the SDK is installed. |
| 2026-10-30 | LiteLLM proxy would not boot: `ImportError: cannot import name 'CacheableResult' from 'mcp.types'` (immediately after adding MCP tools to the API) | `litellm[proxy]` imports its experimental MCP server module at startup and needs `mcp>=2.3.0`; `langchain-mcp-adapters 0.3.2` pins `mcp<2.0.0`. Two incompatible pins in one site-packages. Note litellm 1.106.4 did not even declare the dependency - it assumed a version. | Created `.venv-litellm` with `litellm[proxy]` + `mcp 2.3.0`; API venv keeps `mcp 1.30.0`; `litellm` removed from `api/requirements.txt`; `start_litellm.ps1` uses the second venv and fails fast with setup instructions | A venv is a deployment unit. In production these are two containers, so locally they must be two environments - the shared venv was only ever an illusion of convenience. When a library imports a transitive dependency it does not declare, you discover the conflict at import time, not at install time. |
| 2026-10-30 | LiteLLM aborted startup with `UnicodeEncodeError: 'charmap' codec can't encode characters` when its log was redirected to a file | Python on Windows defaults stdout to cp1252; LiteLLM's startup banner contains non-ASCII characters, and the exception killed the process | `PYTHONIOENCODING=utf-8` set by `start_litellm.ps1` before launch | Anything that must survive CI/Docker log capture needs UTF-8 stdout. "It works in my terminal" hides an encoding landmine that only appears when stdout is a pipe. |
| 2026-11-16 | 5-7 of 14 page extractions failed with `Request timed out.`, yet the LiteLLM log showed no 429s - every request eventually returned `200 OK` | Four concurrent extraction calls occasionally exceed the client-side ceiling: the provider queue got slow, not angry. The 90 s client timeout (then 45 s) discarded pages that were about to succeed | `EXTRACT_TIMEOUT=60` + one app-level retry per page with 2 s backoff; a failure now costs one page and is recorded in `sources[].error`. Page loss after the fix: 5 -> 0 (notes 37 -> 57) | "A retry is not the same as a bigger timeout." Raising the ceiling only moves the tail; retrying once after the competing branches drain recovers the work. And read the provider log before blaming your own code - there was no rate limit to fix. |
| 2026-11-17 | First Phase 4 run reported **0 notes** and `stop_reason=no_notes` although 7 pages had been fetched successfully | `research.py` called `usage_tokens()` without importing it. The per-page `except Exception` (deliberately broad: one bad page must not kill a run) converted the `NameError` into "extraction failed", so a **code** bug was indistinguishable from a **provider** failure | Added the import; the branch now logs the exception *type* and, when every page in a branch failed with the same error, logs one `ERROR` with the count | "A broad `except` inside a fan-out branch turns your bugs into data-quality problems." The graceful degradation was working exactly as designed - too well. Volume and error-type aggregation are how you tell a poisoned page from a broken build. |
| 2026-11-17 | `POST /research` -> `502 {"detail":"Planner failed: Request timed out."}` after 90 s, while `netstat` showed nothing listening on `:4000` | An orphaned `litellm.exe` from an earlier run still accepted TCP connections on `:4000` but never answered, so the client blocked instead of failing fast. The launcher's fixed 40 s sleep also raced a 16-60 s cold start | Start scripts now kill whatever owns `:4000`, launch the proxy directly, and **poll `/health/liveliness` with a deadline** (measured cold start: 16 s) before pointing the API at it | "Never `Start-Sleep` and hope - poll a health endpoint with a deadline." A half-dead process is worse than a dead one: `connect` succeeds, so every timeout looks like your own bug. |
| 2026-11-17 | `start_litellm.ps1` printed `Versions:` with nothing after it | PowerShell 5.1 does not process backslash escapes inside double quotes, so `f'litellm {version(\"litellm\")}'` reached Python as broken source and the error went to `2>$null` | Version probe rewritten without escaped quotes (`importlib.metadata as m`) and guarded by `if ($versions)` | Swallowing stderr (`2>$null`) hides exactly the failure you added the command to detect. On PS 5.1, quote Python one-liners with single quotes *inside* the double-quoted argument. |
| 2026-11-17 | The API log showed page-fetch warnings but **no** `branch done` / `persist round` / `critic round` lines - the whole Phase 4 decision trail was invisible | Nothing configured logging: Python's root logger defaults to **WARNING** with no handler, so every `logger.info()` was dropped while `logger.warning()` still printed through `logging.lastResort`. It looked like the graph was doing nothing | `app/core/log.py::configure_logging(LOG_LEVEL)` called at import of `app.main`: one stderr handler, one format, uvicorn loggers set `propagate=False` so access logs and app logs do not double-print | "Logging is configuration, not `logger.info()` calls." A missing handler is silent in the worst way: warnings prove logging exists, so you conclude your code is fine. The trail is now one line per branch, per round and per critic verdict. |
| 2026-11-17 | The new `GET /research` list endpoint returned **500** on every page load, while `GET /research/{id}` looked fine for fresh jobs | The read path reused the strict `ResearchPlan` (budget `ge=50_000`). That range was added in Phase 4, so every pre-Phase-4 row (30k/35k/40k budgets) failed validation *on read*. Input contract and storage contract had been the same class by accident | Split `ResearchPlan` (strict: planner output + approve input) from `ResearchPlanOut` (tolerant: how stored rows are serialized). Verified by reading all 24 historical rows and their budget distribution | "Your validation is a contract with a *direction*." A constraint that protects writes will happily delete your reads if it is shared. Any schema that serializes persisted data must tolerate every value that was ever legal - and the list endpoint is the one that finds out. |
| 2026-11-17 | The plan editor showed a false **"edited"** badge before the user touched anything; approving would have sent mangled `angles` | The planner emits list items that *contain* commas (`"Performance (latency, throughput, recall)"`), and the editor round-tripped them through `join(", ")` -> `split(",")` | Replaced the two comma-separated text fields with **chip editors** (array in state, array on the wire) and a `samePlan()` deep comparison for the badge | "Any lossy serialization will eventually be applied to a value that contains your separator." If the API's type is `list[str]`, the form's state should be `string[]` - the comma is a display detail, not a data format. |
| 2026-11-17 | After editing `StatusBadge.tsx`, the running page still rendered the old label - and the dev server itself served the old module text | The Vite watcher missed the rewrite (file saved by an external process), so HMR never fired and the module graph stayed stale. `npm run build` was green the whole time, which made it look like a browser-cache problem | Restarted the dev server, then verified by `Invoke-WebRequest`-ing the module URL and asserting the new string is present before trusting the page | "A green build does not prove the server you are looking at is running that build." Check the artifact the browser actually fetches, not the file on disk. |

### Phase 1 verification evidence (keep these numbers)

- `POST /research` -> `awaiting_approval` in **14.4s** (real planner LLM call; served by `openai/gpt-oss-120b`
  because Gemini was 503 - the failover chain worked live)
- approve **after a full API restart** -> `approved` with **no LLM call**: state rehydrated from the Redis
  checkpoint by `thread_id`
- `GET /research/{id}` -> identical plan (Postgres JSONB); another user gets **404** (ownership enforced
  in the query, not the prompt)

---

## Phase 2 - MCP tools + guardrails

| Decision | Chosen | Alternatives considered | Why rejected |
|---|---|---|---|
| **Tool distribution** | MCP server over **stdio** (FastMCP) | Import the tool functions into the API | A protocol means the same server serves LangGraph today, Claude Desktop tomorrow, and a remote HTTP service in Phase 6 without agent changes. Also process isolation: a hanging fetch cannot block the API event loop. |
| **SDK version** | `mcp==1.30.0` (latest 1.x) | `mcp 2.3.0` (latest overall) | `langchain-mcp-adapters 0.3.2` pins `mcp<2.0.0`; 2.x would force a hand-written MCP client. Revisit when the adapter supports 2.x. |
| **Session lifetime** | One persistent `ClientSession` per process (AsyncExitStack) | `client.get_tools()` per request | `get_tools()` spawns a NEW python process per tool call (1-2 s + memory) and Phase 3 fans out many calls - a process storm. Persistent session + semaphore(8). |
| **Search API** | Tavily (REST via httpx) | Brave, Serper, Google CSE, DuckDuckGo scraping | Brave/Serper need a card; CSE caps at 100/day; DDG scraping = blocks + ToS risk. Tavily is agent-tuned (ranked snippets + score), 1k free/month, and the REST call is 20 lines so no SDK dependency. |
| **Extraction** | `trafilatura` 2.3.1 | `readability-lxml`, `newspaper3k`, regex | readability = low maintenance; newspaper3k = broken deps; regex keeps nav/boilerplate and blows the token budget. |
| **Injection defence** | Own layered scanner (normalise -> 8 heuristic patterns -> flags) + spotlighting + Phase 5 citation validator | `llm-guard`, `rebuff`, a HF classifier | `llm-guard` requires Python <3.13 (cannot install on 3.14) and has had no release since May 2025; HF classifiers need torch (3.14 wheels not settled) and add latency per page. Heuristics are ~0 ms, auditable, and testable in CI. |
| **Flag vs block** | Flags lower trust and are logged; never silently dropped | Hard-block on match | Regex false positives would discard good sources. Trust scores + eval metrics beat binary blocking. |
| **SSRF** | Resolve + classify every address, re-check each redirect hop, 80/443 only | Trust the search engine / check only the first URL | The attacker controls the page, not the search index. A public URL can 302 to `127.0.0.1` or `169.254.169.254` (cloud metadata -> IAM creds). |
| **Error surface** | Tool failures arrive as MCP `isError` content -> mapped to 400 (bad input) or 502 (upstream) | Let exceptions bubble as 500 | Callers need to distinguish "you sent a bad URL" from "the provider broke". |

### Phase 2 verification evidence

- `GET /tools` -> `web_search`, `fetch_page` advertised over `mcp/stdio`
- `POST /tools/search` -> 3 hits (2.2 s cold, ~70 ms warm on the persistent session)
- `POST /tools/fetch` -> 156 chars extracted from example.com in ~40-274 ms, `injected=false`
- `POST /tools/fetch` on `169.254.169.254` -> **HTTP 400** `non-public address`
- `GET /tools/selftest` -> `{ok: true}` across search, fetch and SSRF guard

---

## Phase 3 - parallel research, page cache, notes -> pgvector

| Decision | Chosen | Alternatives considered | Why rejected |
|---|---|---|---|
| **Fan-out mechanism** | Conditional edge returning one `Send` per sub-question | A `for` loop inside one node; `asyncio.gather` in one node | `Send` is the map-reduce primitive: each branch is checkpointed separately, the graph knows when all of them finished (automatic fan-in), and branches are built at runtime from the *approved* plan, which the user may have edited. A loop hides all of that inside one node and loses per-branch visibility. |
| **State reducers** | `Annotated[list, operator.add]` on `notes` / `sources` | Default (replace) semantics | With 5 branches returning `{"notes": [...]}`, the last writer wins and 4/5 of the research silently disappears. The reducer is the difference between fan-out and data loss. |
| **Embedding model** | `gemini-embedding-2` through the LiteLLM proxy (`model_name: embeddings`) | Local `sentence-transformers`; OpenAI embeddings | torch on Python 3.14 is still wheel roulette and adds a 500 MB+ image; OpenAI needs a card. Proxy routing keeps `GEMINI_API_KEY` inside the proxy container and makes the model a YAML edit. |
| **Dimensions** | **1536** (MRL truncation of the native 3072) | 3072 + `halfvec`; 768 | pgvector's HNSW index covers `vector` only up to 2000 dims. 3072 forces `halfvec` (half precision, extra ops class); 768 gives up quality for no schema benefit. Measured on the live proxy: default -> 3072, `dimensions=1536` -> 1536, `dimensions=768` -> 768. Cosine distance is scale-invariant, so truncated vectors need no re-normalisation for ranking. |
| **What gets embedded** | Atomic notes (claim + quote + url + sub-question) | Whole pages; page chunks | A page vector answers "which page", not "which sentence". Claim-level rows make search return the answer itself, and every row already carries what Phase 5 needs to cite. |
| **Vectors in graph state?** | No - the join node writes embeddings straight to Postgres | Keep vectors in state | Redis Cloud free tier is 30 MB; a 1536-dim vector as JSON is ~20 KB, so 20 notes would add ~400 KB to *every* checkpoint copy. Small dicts in state, vectors in the database. |
| **Page cache** | Redis `SETEX` per URL, TTL 6 h, sanitised text only | No cache; raw HTML; disk cache | Repeat fetch = 300-900 ms + trafilatura CPU vs ~2 ms Redis GET. Raw HTML would blow the 30 MB budget. A disk cache is not shared across containers at deploy. Soft-fail: the cache being down costs speed, never correctness. |
| **Approve endpoint** | `asyncio.create_task` + poll `GET /research/{id}` | `await` the graph in the request; `BackgroundTasks`; Celery/ARQ today | A run is 2-4 minutes, so awaiting it kills proxies and browsers. `BackgroundTasks` are tied to the response lifecycle and swallow exceptions. A real queue is the Phase 10 answer - the runner's interface is built so the endpoint does not change. |
| **Crash recovery** | Startup marks orphaned `researching` jobs `failed` | Leave them `researching` forever | A per-process `create_task` cannot survive a restart. Marking them failed at boot makes the state machine honest and tells the user to re-approve. |
| **Idempotency** | `INSERT ... ON CONFLICT DO NOTHING` on `(job_id, url_hash, note_hash)`; already-embedded hashes are skipped | Assume replays never happen | LangGraph re-runs nodes on resume/retry, and a re-approve is one click. Dedupe turns a replay into a no-op instead of doubling the corpus. |
| **Extraction failures** | Retry once per page, then skip it and record `sources[].error` | Fail the whole job; drop silently | One 403 or one slow provider should cost one page, not the run. Recorded errors become a countable signal for Phase 7 evals instead of a mystery. |

### Phase 3 verification evidence

- approve -> **`researching` in 3.6 s** (was: the request blocked for the whole run); duplicate approve -> **HTTP 409**
- fan-out: 5 branches, **14 pages read, 57 notes stored**, 1 source error (a real 403), job done in **166 s**
- `POST /notes/search` -> 5 hits in ~4.4 s *including* the query embedding; top cosine **0.7252**, ranked sensibly
  (HNSW/IVFFlat/Pinecone latency claims cluster together - semantic, not keyword, matching)
- cache proof: second job with **the same approved plan** -> **14/14 pages served from Redis**, 197 s vs 263 s
- reject path -> terminal `rejected`, **0 notes**, no research started
- schema verified in `pg_indexes`: `vector(1536)` + `USING hnsw (embedding vector_cosine_ops)`, pgvector **0.8.7**

---

## Phase 4 - coverage critic + bounded gap-filling loop + token budget

| Decision | Chosen | Alternatives considered | Why rejected |
|---|---|---|---|
| **Why a critic at all** | An LLM node after the join that judges coverage against the *original* question | Trust the planner's sub-questions; a fixed "always do 2 rounds" | The planner guesses which sub-questions matter **before seeing evidence**. A human at the HITL gate can also under-specify the plan (tested: 3 of 6 angles removed). Only a step that reads what was collected can notice what is missing. "Always 2 rounds" doubles cost on every easy question and still never checks the result. |
| **Critic input** | A digest: notes-per-sub-question, distinct sites, 3 sample claims each (~500 tokens) | The full notes (30-60k tokens) | The critic judges *coverage*, not prose. Feeding the corpus makes the critic the most expensive node in the very budget it is policing. Counts + samples produce the same verdict; a single-site sub-question is visible from the site count alone. |
| **Where the stop policy lives** | One pure function `decide_stop()` inside the critic node; the conditional edge only asks "are there gaps?" | `if` chains in the router; a `while` loop in a node | A graph can be resumed, replayed and re-entered. One place decides, one place records *why* (`critique.stop_reason`), and the router stays mechanical enough to reason about. Pure = testable without an LLM call (`api/tests/test_critic_policy.py`). |
| **The brakes** | `MAX_ROUNDS=2`, `MAX_GAP_QUESTIONS=3`, `MIN_NEW_NOTES_PER_ROUND=3`, `BUDGET_STOP_RATIO=0.9` + `sufficient` / `no_gaps` / `critic_error` | Only a token budget; only a round count | A budget alone cannot stop a loop that keeps finding cheap gaps, and a round count alone burns money on a question that is already answered. Four brakes with a fixed priority: `critic_error` > `sufficient` > `no_gaps` > `max_rounds` > `budget` > `no_progress` (first match wins, so the recorded reason is the *binding* one). |
| **`no_progress` scope** | Only checked on gap rounds (`round > 1`) | Check on every round | On round 1 `notes_new` just means "how good was the initial fan-out" - a weak first round is exactly when one more round is worth trying. Only a *repeat* round is evidence that the search space is exhausted. |
| **Gap questions** | Up to 3 new, search-ready questions, deduped against every sub-question already asked | Free-text "what is missing" advice | Advice cannot be dispatched. Questions feed straight back into the existing `Send(research_one)` fan-out, so round 2 reuses the Phase 3 machinery unchanged. Capped because each question costs a search + up to 3 pages + extractions. |
| **Token accounting** | `AIMessage.usage_metadata` via `with_structured_output(..., include_raw=True)` + `Annotated[int, operator.add]` reducer | Counting characters; Langfuse totals after the fact; `tiktoken` locally | Providers count tokens differently from any local tokeniser; `usage_metadata` is what you are actually billed for. `include_raw=True` is required because structured output otherwise returns only the parsed model and the usage disappears with it. The reducer is what makes 6 parallel branches add up instead of overwriting. |
| **Budget calibration** | Planner default **150k**, range 50k-400k; measured **~26k per round** | The earlier 40k default | Phase 3 measurement (60 notes) made the old 10k-80k range look generous; the real per-round cost is ~26k, so a two-round run lands near 55k. The budget is a *brake*, not a target - too low and it stops legitimate runs, too high and it never fires. |
| **Progress signal** | `stats.notes_new` = rows actually inserted after dedupe | Notes extracted; pages read | Extraction can succeed and add zero new claims (the same sentences on different pages). Only the post-dedupe insert count proves the round bought information. |
| **Per-round audit trail** | `round` stamped on every source and note; `rounds` + `tokens_used` + `critique` on the job | Only a final status | "Why did it stop?" must be answerable from the API without reading logs: `critique.stop_reason` + `critique.reason` + per-round page counts. |

### Phase 4 verification evidence

Every row below is a real run of `scripts/verify_phase4.ps1` against the live stack.

| run | plan | rounds | pages (r1 + r2) | notes | tokens | stop reason |
|---|---|---|---|---|---|---|
| narrow (final code) | 3 of 6 angles | 2 | 7 + 6 | 25 | 39,497 (19.7% of 200k) | `max_rounds` |
| narrow, repeat | 3 of 6 angles | 2 | 7 + 8 | 42 | 46,529 | `sufficient` |
| narrow, repeat | 3 of 6 angles | 2 | 7 + 9 | 38 | 58,438 | `max_rounds` |
| control | 6, unedited | 1 | 15 | 47 | 49,232 (32.8% of 150k) | `sufficient` |
| control, repeat | 6, unedited | 2 | 11 + 3 | 34 | 42,823 | `max_rounds` |

- the *verdict* varies because a model judges coverage; the *bound* does not - 1 or 2 rounds, always with a recorded reason and a 2-round ceiling
- a 2-round job takes **86 s** (warm page cache); approve returns `researching` in **2.9-3.4 s**
- dedupe across rounds: **25 / 25** and **42 / 42** distinct `(url, note)` pairs - the add-reducer + `ON CONFLICT DO NOTHING` are idempotent across rounds
- pure policy tests: **14 passed** (`pytest tests -q`) - each brake, its priority and the "keep going" case, with no provider call
- audit trail, one line each: per branch (`-> 2 pages, 4 notes, 6886 tokens`), per round (`persist round 1: ... 'notes_new': 16`), per verdict (`critic round 2: gaps=2/2 stop=max_rounds tokens=37223/200000`), and the final `done: 25 notes, 2 round(s), 39497 tokens, stop=max_rounds`

## Phase 4.5 - research UI slice (approval gate, live run, evidence)

Built against the existing polling API; the only backend changes were a list endpoint
and tolerant output schemas. Phase 5 adds the report panel and SSE to the same page.

| Decision | Chosen | Alternatives considered | Why rejected |
|---|---|---|---|
| **Sequence: UI before Phase 5** | Build ask -> approve/edit -> live run -> evidence now | Writer + SSE first, then one big UI phase | The UI is where the HITL contract gets exercised. Freezing API shapes for `critique`, `rounds` and plan editing before a human has clicked them is how you get a UI rewrite; Phase 5 then only *adds* a report panel to a page that already works. |
| **Transport** | 2 s polling (`hooks/useJob.ts`), 15 s at the approval gate, stop on terminal | SSE now; `setInterval` inside the page | A job has ~5 state transitions in 1-4 minutes - polling is smaller, resumable and proxy-proof at that granularity. SSE exists for the token stream in Phase 5. The loop lives in a hook precisely so that swap is one file. |
| **Polling cadence** | Fast only for `planning`/`researching`; everything else is paused-cadence | One fixed interval | Nothing but our own click can advance `awaiting_approval` (or a legacy `approved` row), so 2 s polling there is pure waste. Cadence derives from `ACTIVE_STATUSES`, not per-branch code. |
| **Plan editing** | Edit sub-questions/budget inline; `angles`/`constraints` as chips; validation mirrors the server (3-7 sub-questions, 50k-400k) | JSON textarea; comma-separated fields; server-only validation | A JSON textarea is not a product. Comma fields mangle values that contain commas (see incident log). Server-only validation teaches the rule via a 422 after a round trip; the client mirrors it so the button explains itself. |
| **"Edited" detection** | `samePlan(draft, original)` deep compare, not a dirty flag | Mark dirty on first keystroke | A dirty flag lies the moment a field is typed and undone - and it lied on load while the comma round-trip was in place. Comparing the payload is the only definition that matters, because the payload is what gets submitted. |
| **Why show `critique`** | Stop reason as a labelled card with the critic's own sentence and per-round page counts | A generic "done" badge | Phase 4's whole claim is "it stops for a *reason*". If the UI hides the reason, the feature is invisible. The card makes `max_rounds` vs `sufficient` legible without reading logs. |
| **Evidence surface** | Notes grouped by sub-question + semantic search showing the `cos` score; sources grouped by round with `cache` / injection / error badges | Flat note list; hide failed pages | Grouping shows which angle produced which claim - and which angle produced nothing. Search is the pgvector kNN endpoint made visible. Failed pages are provenance: "403 from reddit" is a different fact from "nothing exists". |
| **Job list endpoint** | `GET /research` (owner-scoped, newest first, `limit`/`offset`) + `created_at`/`updated_at` on the payload | Client-side list from local storage | The list must survive a reload and include jobs from other sessions. Timestamps are needed for "9 min ago", elapsed run time and sorting - the UI cannot invent them. |
| **Legacy statuses** | `approved` mapped to a labelled inert badge; any unhandled status renders an explanation card | Drop unknown statuses; migrate the rows | The status column is a plain string, so old rows exist (`approved`, and one with `plan=null`). A page that renders a bare header for them looks like a bug in the app; an explanation is honest and needs no migration. |

### Phase 4.5 verification evidence

One full UI-driven run - the plan was edited in the browser, not via PowerShell:

| what | value |
|---|---|
| plan | **6 sub-questions proposed -> 5 approved** (one deleted in the editor), budget **150k -> 120k**, badge flipped to `edited` |
| rounds | 2 (13 + 6 pages read; 6 fetch errors kept as provenance) |
| notes | **52** stored, deduped across rounds; 8 evidence groups (5 approved + 3 gap questions) |
| tokens | **72,335 of 120,000** (60% of budget), from `usage_metadata`, rendered as the bar |
| stop | `max_rounds`, `notes_new=12` in round 2, critic reason rendered verbatim |
| timing | approve -> `researching` in **~3 s**; total run **~4m 40s** (cold page cache, 19 pages) |
| semantic search | "cost of self-hosting a vector database" -> top hit `cos 0.814` (a pgvector-cost note) |
| legacy rows | orphan `approved`, `plan=null` failure and two `rejected` jobs each render an explanation, not a blank page |
| build | `npm run build` (`tsc -b && vite build`) clean: 262 kB JS / 86 kB gzip |

## Phase 5 - cited report + SSE stream

The report is a **separate resource** (`reports`, unique `job_id`), not a graph node: a job
stays `notes_ready` while its report regenerates, and overwriting in place is the seam
Phase 6's A2A path needs.

| Decision | Chosen | Alternatives considered | Why rejected |
|---|---|---|---|
| **Report as a resource** | `reports` table, `id`/`job_id` (unique)/`status`/`markdown`/`citations`/`validation`/`model`; `POST` regenerates by overwrite | A 6th graph node; a field on the job | A node would re-run the whole research pipeline to rewrite prose, and a job column would make "regenerate" destroy the only copy mid-flight. A resource with its own status lets a report fail without touching the notes - the research is the expensive part. |
| **Citation registry before generation** | `build_registry()` numbers ≤30 notes (round-robin across sub-questions *and* hosts) and the prompt may only cite those numbers | Let the model cite URLs/notes freely, then resolve afterwards | Free-form citation means the app must decide what the model "meant" - unfalsifiable. With a pre-numbered list, every `[n]` either resolves or is provably invented, and `citations_for()` emits exactly the sources with a marker actually used: traceability by construction. |
| **Registry balance** | Nested round-robin: one note per sub-question per round, and within a sub-question the least-used host | First N by confidence | Measured: "first N" gave 10 of 30 slots to one site, so one article's framing became the report's framing. Host rotation brought it to 9 hosts, max 5 of 30, while keeping sub-questions at 5/5/5/4/4/4/3. |
| **Audit** | Deterministic: precision (valid/markers), coverage (cited sentences/sentences), `unknown_markers`, `unused_sources` | An LLM judge scoring the report | A judge costs a call per report, is nondeterministic, and cannot be unit-tested. These four numbers are pure functions of the text and the registry, so they are testable and identical on replay. |
| **Coverage unit** | Sentences, not paragraphs | Paragraph blocks | Block-level scoring silently returned `0/0` when the writer used single newlines: headings and paragraphs collapsed into one block starting with `#`. A sentence survives any formatting the model chooses. |
| **Stream transport** | Redis Streams (`XADD`/`XREAD` + `MAXLEN` + TTL), replayable with `Last-Event-ID` | Redis pub/sub; polling only | Pub/sub cannot replay: a tab opened mid-generation would see the tail of a report with no beginning. Streams make "refresh mid-report" and "second tab" the same code path. Polling stays as the fallback when Redis is down. |
| **Client transport** | `fetch` + hand-parsed SSE frames | `EventSource`; `sse-starlette` | `EventSource` cannot send `Authorization`, so the JWT would go in the query string (logs, history). The wire format is four field names, and hand-parsing keeps the `id:` cursor - the one thing a library would hide - explicit. |
| **Markdown rendering** | ~120-line block renderer; `[n]` -> chip linking to `#cite-n`, unknown numbers red | `react-markdown` + plugins | ~80 kB for a subset we control, and a renderer makes `dangerouslySetInnerHTML` tempting. On model output that is never acceptable; here every string renders as text. |
| **Second attempt** | One bounded retry when `needs_repair()` (truncated OR `markers == 0` OR `precision < 0.9`); keep the better draft by `(not truncated, precision, coverage, words)` | Fail the report; retry until valid | Unbounded retries on a free tier is how you burn a daily quota in one click. Scoring both drafts means a bad retry can never make the result worse - verified live: a 0-marker draft was replaced by a 618-word, 94%-coverage one. |
| **Uncited is a flag, not an error** | `validation.uncited = (valid_markers == 0)`, red "no citations" badge | Treat as `failed`; render it like a normal report | The prose is still useful and the work is done; failing discards it. But it is not evidence, so it must not look like the cited briefs - the badge is the difference between "we verified this" and "we did not". |
| **Record the served model** | `reports.model` = the upstream that answered (`meta["model_name"]`), not the proxy alias | Store the alias we asked for | A report claimed `support-chat` while `openai/gpt-oss-120b` wrote every word. Without this field a silent failover is undetectable from the data. |

### Phase 5 incidents (each measured, each an interview story)

1. **Thinking tokens are charged to `max_tokens`.** `gemini-3.8-flash` at `REPORT_MAX_TOKENS=1800`
   produced 622, then 274, then **34 words** on the same prompt, the last ending mid-sentence; the
   log showed `finish=length`. Fix: 2600 for the stream, 4000 for the retry, and `finish_reason` is
   captured per chunk so `truncated` is a fact rather than a guess.
2. **A blocking read is a quiet socket.** `socket_timeout=5` against `XREAD BLOCK 8000` raised
   `TimeoutError` on every idle gap and logged "streaming disabled" while the report generated
   fine. Fix: `socket_timeout = BLOCK_MS // 1000 + 20`.
3. **Retry path `NameError: settings`.** The second attempt was written but never imported
   `settings`, so the audit flagged `truncated=True` and shipped a 706-word cut-off brief. The
   audit behaved correctly; the fix was one import - and the lesson is that the happy path is not
   the tested path until it has actually run.
4. **Silent failover changed the output contract.** Gemini returned **429**, LiteLLM failed over to
   Groq, and Groq wrote a 644-word brief with **zero** `[n]` markers. Nothing failed; the report was
   simply uncited. Fixes: record the served model, flag `uncited`, and reorder the chain (below).
5. **Retrying a 429 is worse than not retrying.** With `request_timeout: 200`, LiteLLM honoured the
   provider's `retryDelay` and held a request >45 s with no output before failing over. Fix:
   `router_settings.retry_policy` - `RateLimitErrorRetries: 0`, transient 5xx/timeout keep one retry,
   plus `allowed_fails: 1` / `cooldown_time: 30`. Failover now takes 1.6 s, then 0.8 s on cooldown.
6. **The page was slow, not stuck.** "Checking for a report…" for 20-30 s traced to (a) `GET /notes`
   selecting the 1536-dim `embedding` column the schema never returns - **3187 ms vs 455 ms** for 52
   notes, (b) two sequential round trips per request at **~455 ms each** to a database in another
   region (`/report` now uses one join), and (c) StrictMode double-mounting every effect, so each GET
   fired **4x**. Fixes: `defer(SourceNote.embedding)`, one joined query, and a shared in-flight
   promise (`lib/inflight.ts`): 4 -> 1 request, first paint 20 s+ -> **4.5 s**.
7. **No timeout means "loading" forever.** Axios defaults to `timeout: 0`. A stalled socket left the
   panel on a skeleton with no error and no retry - which is why "click Refresh" appeared to fix it.
   Now `getReport` times out at 20 s, and the panel shows elapsed seconds, a hint, and a retry.

### Phase 5 verification evidence

`scripts/verify_phase5.ps1` - 10 sections, all passing against the live stack:

| what | value |
|---|---|
| guard | `POST /report` on an `awaiting_approval` job -> **409**, message names `notes_ready` |
| idempotence | second `POST` attaches to the same report id while generating |
| stream | `text/event-stream`, `status` event carries `sources=30, notes=52`, ~1000 token frames, replay from `Last-Event-ID` resumed mid-stream and still reached `done` |
| audit (stored) | 611 words, 25 markers, precision **1.0**, coverage **0.73** (16/22 sentences), 5 unused sources |
| traceability | every citation maps to a stored note (`url` + `note_id`); **9 distinct sites** |
| regeneration | same report id, overwritten in place |
| guards | 409 / 404 / 401 all behave |
| tests | **29** deterministic tests (`tests/test_report_citations.py`) |
| frontend | `npm run build` clean: **279 kB JS / 90 kB gzip** (+1.9 kB; no `react-markdown`) |
| browser | fresh load renders the report in **4.5 s** with no click; 1 `GET /report`; 30 chips -> 23 source rows, all anchors resolve, 0 unknown markers; regenerate streams 0 -> 429 -> 4214 chars |
| best observed report | 618 words, 29 citations, precision **1.0**, coverage **0.94**, produced by the second attempt after a 0-marker first draft |

## Phase 6 - writer and critic as A2A services

The writer and the critic left the process. They are now two small FastAPI services that
advertise themselves with AgentCards and answer `SendMessage` / `SendStreamingMessage` over JSON-RPC
2.0. The API keeps the graph, the registry, the audit and the loop; only the two expensive model
calls cross a network boundary.

| Decision | Chosen | Alternatives considered | Why rejected |
|---|---|---|---|
| **Protocol version** | A2A **1.0** (`protocolVersion: "1.0"`, `A2A-Version: 1.0`, card at `/.well-known/agent-card.json`) | A2A 0.2/0.3; a bespoke HTTP API | 0.3 is the old shape (`url` on the card, `tasks/send`, streaming on the card). `a2a-sdk 1.3.1` ships both, but a new build on a deprecated shape is a migration you schedule for no reason. A bespoke API would not be discoverable by any other A2A client - and "any client works" is the whole point of the phase. |
| **Server SDK** | `a2a-sdk[fastapi]==1.3.1` - it owns routing, task lifecycle, SSE framing and JSON-RPC error codes | Hand-rolled server (as in Phase 5's client) | The server is the *conformance surface*: an outside client (or the SDK's own client) must be able to talk to it. Re-implementing `TaskState` transitions and the SSE envelope buys nothing and gets `-32601`/`-32001` subtly wrong. The SDK's quirks were cheaper to work around than to reproduce. |
| **Caller** | Hand-rolled `httpx` client (`app/a2a/client.py`): card discovery, `SendStreamingMessage` + SSE parse, accumulator, `GetTask` | The SDK's client | Needs three things a library hides: an **exact read timeout** (`A2A_READ_TIMEOUT=200`) instead of a retry/backoff policy; **no retry on 401** (a bad token is not transient); and **no replay** of a delta already yielded to the browser. Same reasoning that kept `sse-starlette` out of Phase 5: own the wire where the failure modes live. |
| **Which method per agent** | Writer: `SendStreamingMessage`. Critic: `SendMessage` | Both streamed; both unary | A report is thousands of tokens over 6-11 s, so the user should see it type. A verdict is one ~400-token JSON object; streaming it is three frames of ceremony and a worse error surface. |
| **Split point** | Only **writer** and **critic** are remote. Plan, fan-out, search, merge, audit, `decide_stop` stay in the API | Every node as an agent | The two nodes are the ones you scale and retry independently (writer = expensive, critic = cheap and frequent), and they have clean input/output contracts. Fan-out is a `Send` map-reduce inside the graph; wrapping it in RPC adds hops and state to the one place that must stay deterministic. |
| **Who decides** | Agent **proposes** (`sufficient`, `gaps`); orchestrator decides (`decide_stop`, registry, audit) | Let the critic agent end the loop | A remote agent that can declare success can also extend its own budget or stop early. Keeping the brakes local means a buggy or hostile agent costs a round, not the run - and the four brakes stay unit-testable without a network. |
| **Task store** | `RedisTaskStore(InMemoryTaskStore)`: JSON per task, key `dr:a2a:task:<ns>:<id>`, TTL `A2A_TASK_TTL=3600`, soft-fails to memory | In-memory only; Postgres | A `GetTask` after a restart must still return the artifact - otherwise "distributed" means "loses the answer on deploy". Postgres would add a migration and a write per chunk for data that is valid for an hour. `list()` stays in-process (documented limitation, not a silent one). |
| **Auth** | Shared bearer token (`A2A_TOKEN`) via middleware, `secrets.compare_digest`, card + `/healthz` public, RPC `401` + `WWW-Authenticate`, `503` when unset | No auth ("internal network"); JWT per user | An open agent is an open LLM bill. Per-user JWT would mean the agent needs the API's signing key and user table - coupling for no gain when the only caller is the API. `503` on an unset token is deliberate: a misconfigured service refuses to run open instead of quietly becoming one. Phase 10 replaces this with mTLS / OAuth2 client credentials. |
| **Transport** | One `httpx.AsyncClient` per agent, created lazily and closed in the API lifespan | New client per call; the SDK client | Per-call clients leak sockets under the report's chunk count. Lazy creation keeps `AGENTS_MODE=local` from opening connections it will never use, and the lifespan close makes shutdown deterministic. |
| **Failure semantics** | Writer down -> report `failed` with `AgentUnavailable: <url>: <reason>`. Critic down -> `stop_reason="critic_error"` and the notes are kept | Fall back to the local node silently | Silent fallback would make the phase unverifiable - you could never prove the remote path ran. The critic is optional work (its verdict only steers the next round), so it degrades; the writer is the deliverable, so it fails loudly. |

### Phase 6 incidents (measured wire quirks)

These are the ones that cost time; each is a "the spec is not the JSON you imagined" story.

1. **`GetTask` takes `id`, not `taskId`.** `params: {"id": "<task-id>"}` is the v1.0 proto field;
   `taskId` fails with `Validation failed: field 'id' is required`. The streaming frames use
   `taskId`, so the two shapes are inconsistent *within the same protocol*.
2. **`GetTask` returns the Task object directly**, not `{"task": {...}}` like `SendMessage`. The
   client accepts both now, but the measured shape is the bare object.
3. **`Struct` has no integers.** Every number comes back as a float - `max_tokens: 2600.0`,
   `tokens: 412.0`. Pydantic coerces on the way in (`int` fields accept `2600.0`), but the
   accumulator had to `int()` the metadata explicitly or `tokens` would render as `412.0`.
4. **`None` crashes `ParseDict`.** `Struct` has no null type, so a note with `title: null` made the
   *request* fail before the agent ran. `payloads._clean()` strips `None` recursively; the pydantic
   model supplies the default on the far side.
5. **The executor must enqueue a `Task` before any status event**, and the first artifact chunk must
   be `append=False`, then `append=True`. Any other order is an SDK `RuntimeError`, not a client
   error - it fails in the producer, where it is hardest to see.
6. **The final artifact chunk can be `parts=[]` with `last_chunk=True` and metadata only.** A naive
   accumulator overwrites the text with `""`. Ours only replaces text when parts are present.
7. **A down service is a transport error, not an HTTP status.** `httpx` raises
   `ConnectError` before any JSON-RPC error exists, so "agent unavailable" and "agent said no" are
   different exception types - the report error message names which one happened.
8. **`langfuse` import starvation on Windows.** Starting three CPython processes at once (each
   importing langchain -> langsmith) intermittently raised `MemoryError` inside an import and looked
   exactly like a broken service. `verify_phase6.ps1` retries startup up to 3 times and prints the
   last stderr line, so a real error is still visible instead of being retried away.

### Phase 6 verification evidence

`scripts/verify_phase6.ps1` - 8 sections, all passing; it starts writer :8010, critic :8011 and a
second API on :8002 (`AGENTS_MODE=a2a`), then stops all three.

| what | value |
|---|---|
| cards | both `protocolVersion 1.0`, `protocolBinding JSONRPC`, `streaming=true`, skills `write-cited-report` / `critique-coverage`, public without a token |
| auth | no token -> **401** + `WWW-Authenticate: Bearer`; wrong token -> **401**; unset `A2A_TOKEN` -> **503** |
| protocol errors | unknown method -> `-32601`; unknown task -> `-32001` |
| critic over A2A | `SendMessage` completed in **3 s**, **412** tokens, `support-chat-lite`, verdict `sufficient=false` with **2** gaps (`max_gaps=2` honoured) |
| discovery from the API | `/healthz` `writer=ok (deepresearch-writer)` / `critic=ok (...)`; `/health/agents` `mode=a2a status=ok` |
| report through the writer agent | `ready` in **6 s**, **537 words**, **29 markers**, precision **1.0**, coverage **0.95**, served model recorded; writer's own log gained a task, proving the remote path ran |
| failure containment | writer stopped -> report `failed` in **11 s** with `AgentUnavailable: http://127.0.0.1:8010: transport error (All connection attempts failed)`; restart + regenerate -> `ready` with 28 markers |
| `GetTask` persistence | task read back from Redis after a service restart; TTL measured **~3574 s** |
| streaming | `SendStreamingMessage` produced **34** SSE frames; `"".join(deltas) == reply.text` after reassembly |
| tests | **54** deterministic tests (29 existing + 25 in `tests/test_a2a.py`) |
| versions | `a2a-sdk 1.3.1` (pulls `protobuf 7.36.2`, `json-rpc 1.15.0`, `google-api-core 2.44.0`, `sse-starlette`); `httpx 0.28.1`; `pytest 9.2.0` |


## Phase 7 - eval gate, traces and cost

Quality had been asserted in prose ("precision >= 0.90") since Phase 5. Phase 7 makes it a command
that exits non-zero, makes a report's spans findable, and puts a dollar figure on measured tokens.
The interesting decisions are all about *what not to automate*.

| Decision | Chosen | Alternatives considered | Why rejected |
|---|---|---|---|
| **How to score quality** | Deterministic arithmetic over the produced text: the production `validate()` plus per-case thresholds | DeepEval, Ragas, promptfoo, an LLM judge | Their distinguishing feature is a judge: one paid call per case, a different verdict run to run, and no defensible way to fail CI on it. The metrics that matter here (markers, precision, coverage, words, distinct domains, traceability) are computed from strings. The README originally named these tools; it was wrong, and the eval section now says why. Qualitative review still exists - a human reading the trace in Langfuse. |
| **What the gate runs on** | Fixtures: job + report + the numbered registry recorded from a real run | Re-run the pipeline in CI; feed the live API | A live run is 1-4 minutes of provider time per case. A fork's pull request would spend the maintainer's quota, and a provider 429 would turn a code review red. Fixtures make the gate ~1 s, offline, and reproducible. |
| **Fixture integrity** | Re-run `validate()` over the registry the writer was actually given; a stored `validation` that disagrees fails `audit_matches_recompute` | Trust the stored audit; store only the markdown | A fixture is a claim about what happened. If the grader trusts the claim, a stale or edited `validation` object passes forever. Recomputing costs microseconds and makes the shipped text the source of truth. |
| **Live artifacts** | Cross-check only: stored marker count equals markers in the text; the rest of the stored audit is trusted and the artifact says `audit="stored"` | Pretend live artifacts have a registry; skip live grading | The API does not return the 30-source registry, so a live grade cannot recompute. Naming the weaker guarantee beats inventing a stronger one. |
| **Negative controls** | Every invocation strips all markers from a passing artifact and appends `[99]`; both must be rejected | Trust the unit tests alone | A gate is code too. If a refactor stops checking `precision`, the tests may still pass; the controls are the gate watching itself on every run and in CI. |
| **Thresholds** | Per case, in `evals/golden/cases.jsonl` | One global bar | An interview-advice answer legitimately needs fewer citations than a vector-database comparison. One number would either fail the first or rubber-stamp the second. |
| **What is checked** | 15 checks: ready, notes, markers, precision, unknown markers, coverage, words, distinct domains, traceability, exact citation set, truncation, uncited sentences, tokens, model, stop reason | Precision only | Precision alone is satisfied by a two-sentence answer citing one source. Domains catch single-site framing; stop reason catches `critic_error`/`no_notes`; tokens and model catch a run nobody can cost or attribute. |
| **Tracing seam** | `propagate_attributes(session_id=<job id>, tags=["report", f"mode:{mode}"], metadata={...})` around the generation, wrapped in `_SafeContext` | Update a trace after the fact; hand-create spans | LangChain's callback creates the spans; ambient attributes are the only supported way to label spans you do not create. Observability must never fail a report, so `_SafeContext` degrades any tracing exception to a warning. |
| **Tracing health** | Three states: `ok`, `disabled` (no keys), `error` (keys rejected/unreachable), plus `check_traces.py` querying observations by session | A boolean "configured" | "Keys present" and "spans arriving" are different facts. The endpoint is a diagnostic (a network round trip), not part of liveness, so it is not in `/healthz`. |
| **Cost basis** | Measured tokens (from the rows already written) × a code table of list prices, one blended USD/Mtok per model, `output_share=0.30`, `as_of` date in the response | Provider billing APIs; separate input/output prices; omit dollars | The app records `tokens_used`, not a per-call split - inventing one would look more precise than it is. Billing APIs are not uniform and need keys. A dated table is a one-line reviewable diff. Unknown models are priced as the default **and flagged `assumed=True`** rather than silently costing zero. |
| **Failed reports** | Counted in usage | Count only `ready` | A failed report burned the tokens it burned. Excluding failures makes the cost of instability invisible - exactly the number worth watching. |
| **Who sees totals** | `scope=self` by default; `scope=all` requires the `admin` role (403 otherwise) | One shared total | Cost is a weak proxy for activity, but it is still activity, and the questions behind it are user data. |

### Phase 7 incidents (each one changed the code)

1. **`validate()["markers"]` counts distinct marker indices, not occurrences.** "28 markers" means
   28 different sources were cited, not 28 `[n]` strings. A grader that compared `len(re.findall())`
   to the stored value would fail every real report. The negative control appends `[99]` (a new
   index) rather than repeating an existing one, so it tests the metric it claims to test.
2. **`citations_for()` returns references into the registry dicts.** The test helper built its
   expected artifact from the module-level registry, so a test that rewrote a citation's URL also
   rewrote the registry the next test graded. Symptom: an unrelated test failed with
   `audit_matches_recompute` after a domain test passed. Fix: `deepcopy` the registry inside the
   helper - and the incident is the argument for fixtures being immutable snapshots.
3. **PowerShell `.NET` format strings are not Python's.** `"{0,-24s}"` throws
   `Error formatting a string: Input string was not in a correct format.` - the `s` width suffix is
   Python; .NET uses `{0,-24}`. The verification script died after printing two green lines, which
   is how a *reporting* bug looked like a product failure.
4. **A Windows path in a docstring is an escape sequence.** `.\\.venv\\Scripts\\python.exe` written
   as `\.venv\Scripts\...` emits `SyntaxWarning: invalid escape sequence '\S'` on every run; `\r`
   or `\t` would silently corrupt the text. Docstrings use `\\` now, and the warning is gone from
   the gate output.
5. **Langfuse v5 has no `last_trace_id`.** The v2 idiom of reading the id back off the client does
   not exist. Spans are attributed by ambient context, and verification queries
   `api.observations.get_many(session_id=...)` - which is also the query a human would run. The
   check is "a span exists for this job", not "the SDK returned an id".
6. **A live report overwrote the fixture's source row, and the fixture did not change.** Fixtures are
   recorded snapshots, not pointers: regenerating job `60bc15f3` during `-Live` changed the DB row
   but the gate kept grading the recorded artifact. That is the intended property - a gate that
   reads live state is a gate that changes when someone clicks "regenerate".

### Phase 7 verification evidence

`scripts/verify_phase7.ps1` - 6 sections, passing with and without `-Live`; it starts a private API
on `:8003` and stops it in `finally`. The `-Live` section regenerates one report and queries
Langfuse by session, so "traces are configured" is proven by a span, not a key.

| what | value |
|---|---|
| usage | **513,555** tokens (research **510,412** + report **3,143**), **24** jobs, **743** notes, shadow **$0.097575** at list prices, free tier **$0.00** |
| per-model | `support-chat-lite` 1 report, 3,143 tokens, **$0.000597**; research priced as `LLM_PRIMARY_MODEL` (visible in the payload) |
| RBAC | `scope=all` -> **403** for role `agent`; `scope=self` returns the caller's rows |
| Langfuse | reachable + authenticated (`https://cloud.langfuse.com`); after the live report, **2 observations** found for session `60bc15f3-...` (name `ChatOpenAI`) |
| eval gate | 2 fixtures PASS: `vector-db-choice` 28 markers / precision **1.00** / coverage **0.95** / 9 domains / 52 notes / 3,143 tokens; `ai-engineer-interview` 22 / **1.00** / **0.94** / 10 domains / 38 notes / 12,127 tokens |
| gate totals | **15,270** tokens, shadow **$0.012239**; **4/4** negative controls rejected (2 cases × 2 controls) |
| gate integrity | fixtures re-run `validate()`; a tampered `validation` fails `audit_matches_recompute` (unit-tested) |
| live report | `ready` in **14 s**, **582** words, **27** citations, `support-chat-lite`, **3,075** tokens; usage increased by exactly the report tokens |
| tests | **72** deterministic tests (54 prior + 12 evals + 6 pricing); no network, no LLM |
| CI | `.github/workflows/ci.yml` on Python 3.12: install both requirement files, `pytest api/tests`, `python evals/run_evals.py`, upload `evals/results/latest.json` |
| frontend | `npm run build` clean: **283.35 kB JS / 91.22 kB gzip**; usage cards + per-model table + pricing note + Langfuse badge on `/system` |
| versions | `langfuse 5.0.4`, `pytest 9.2.0`, FastAPI 0.162.0, Pydantic 2.14.1, `langgraph 1.2.21`; no new runtime dependency was added by this phase |

## Phase 8 - report library, exports and admin overview

The research engine was finished; the product was not. Phase 8 makes the output findable,
portable and operable, and every decision is about *not* adding moving parts: no object store,
no new table, no chart library, no user-management API.

| Decision | Chosen | Alternatives considered | Why rejected |
|---|---|---|---|
| **Library data model** | `GET /reports` joins `reports` to `research_jobs` for the question; returns audit scalars, not markdown | A `report_library` table; reuse `/research?status=notes_ready` | A join is the normal form: the question already lives in one row, and a copy would need a trigger to stay true. The job list has no report id, no audit and no report status - it answers a different question. |
| **Export storage** | Generated on demand from the row; `Cache-Control: no-store` | S3/R2 with a stored object per report; a background job that pre-renders | A bucket adds credentials, lifecycle rules, signed URLs and a second source of truth for data Postgres already owns. It earns its place only when the artifact must be immutable - then use content-addressed keys and one `export_key` column. A download is not an archive. |
| **PDF engine** | ReportLab 5.1 with bundled Bitstream Vera TTFs | WeasyPrint; headless Chrome; browser print-to-PDF | WeasyPrint needs Pango/GTK system libraries (absent on Windows dev, another container dependency); Chrome is heavy and a process per export; browser print is untestable server-side. ReportLab is pure Python, deterministic, ~0.2 s, and Vera covers Unicode that Helvetica's Latin-1 cannot. |
| **Markdown -> PDF** | A ~120-line parser for the writer's subset (headings, paragraphs, bullets, numbers, quotes, code, rules, pipe tables) into flowables | `markdown-it-py`; `xhtml2pdf`; `pandoc` | `markdown-it` returns HTML and ReportLab does not consume block HTML; walking its token stream is more code than parsing the subset the writer actually emits. `xhtml2pdf` has the same CSS/HTML mismatch plus older dependencies; `pandoc` is an external binary. |
| **Inline formatting** | Escape HTML first, then re-introduce only `<b>`, `<i>`, `<link>` and a monospace font; code spans and links are stashed so bold/italic cannot edit a URL | Trust the model's markdown; a sanitizer after the fact | Scraped text can contain `<`; ReportLab parses its input as mini-XML and a stray tag is a 500. Escaping first makes the allowed tag set explicit instead of probabilistic. |
| **Export access rules** | Owner-scoped 404 (never 403); 409 unless `status == "ready"`; `attachment` with ASCII slug + 8 hex chars of the id | 403 for other users; export partial drafts | A 403 confirms the id exists. A generating/failed report is not an answer, and shipping half a file is worse than an error. The id suffix keeps two answers to the same question apart; ASCII keeps the header latin-1-safe. |
| **Download transport** | Authenticated `GET`, axios `responseType: blob`, object URL, filename from `Content-Disposition`; CORS `expose_headers` | Signed URL; token in query string; `window.open` | There is no bucket to sign. A query-string token lands in history and proxy logs. A plain link cannot send the Bearer header, and the browser hides `Content-Disposition` unless CORS exposes it - the one header this needs. |
| **Admin aggregate shape** | One `GET /admin/overview?days=` snapshot: users, jobs, reports, tokens, cost, by_day, by_model, top_users, recent_failures, `generated_at` | Five endpoints; a BI tool; per-day client queries | The dashboard needs all of it together; five round trips can render half a page from two different moments. `generated_at` makes staleness explicit. A BI tool is another service and credential for six numbers. |
| **Failure visibility** | Failed jobs and reports are counted and listed | Count only `ready` | A failure burned tokens and notes. Excluding it makes instability invisible - and the failure list is what an operator acts on. |
| **Role administration** | `scripts/set_role.py --list` / `--email --role` | `PATCH /admin/users/{id}`; an admin UI | A role endpoint is a privilege-escalation surface: one authorization bug turns any account into an admin. The capability is rare and belongs to whoever can already run commands against the database. Build the endpoint with an audit trail when a real product need exists. |
| **Frontend charts** | CSS bars sized as a percentage of the window's max | Recharts / Chart.js / D3 | One bar chart is ~15 lines of flexbox; a chart library is tens of kB and a canvas/SVG test surface for a shape that is already a `div`. |

### Phase 8 incidents (each one changed the code)

1. **A one-character table header produced a negative column width.** The writer's comparison table
   starts with `| # |`, so the width heuristic gave that column ~2 pt; ReportLab subtracts padding and
   raised `ValueError` inside `_calc_height`. Fix: clamp every column to at least 36 pt, then rescale
   the widths to the exact frame width so the table still fits the margin. The fixture that exercised
   it was already in `evals/fixtures/` - the export test now covers it.
2. **`Invoke-WebRequest` without `-UseBasicParsing` fails in non-interactive PowerShell 5.1.** The
   verification script died with *"Windows PowerShell is in NonInteractive mode. Read and Prompt
   functionality is not available"*, then reported `Object reference not set to an instance of an
   object` because `$md` was null. Fix: `-UseBasicParsing` on every call - the same rule the earlier
   phase scripts follow, applied here to a new script.
3. **A fixture dict is not an ORM row.** `build_context` originally did
   `getattr(report, "id", None) or report["id"]`; a fixture has no `.id` and uses `report_id`, so the
   conversion raised `badly formed hexadecimal UUID string`. Fix: one explicit `field()` accessor
   that handles both shapes, plus UUID normalisation - and a unit test that asserts the two paths
   produce the same `ExportContext`.
4. **`replace("www.", "")` is not "strip the prefix".** `_host` used it to prettify a missing
   citation title, which would also edit `www.` in the middle of a hostname. Fix: lowercase and strip
   the prefix only, with a `source` fallback so an empty URL never renders as an empty bold title.
5. **CORS hid the one header the download needs.** The blob arrives, but the browser safelist does not
   expose `Content-Disposition`, so every file was named `deepresearch-report.pdf`. Fix:
   `expose_headers=["Content-Disposition"]` on the middleware - a one-line consequence of choosing
   header-based filenames over query parameters.
6. **PowerShell 5.1 `Set-Content -Encoding UTF8` writes a BOM.** The edited Python module still
   imported, but the byte-order mark is noise in diffs and a trap for any tool that reads the first
   line literally. Fix: strip `\xef\xbb\xbf` with Python and write UTF-8 without a BOM; the source
   files are ASCII-safe.

### Phase 8 verification evidence

`scripts/verify_phase8.ps1` - 6 sections, all passing; it starts a private API on `:8004` and stops it
in `finally`. It writes to the shared dev database (registers `other@test.com` / `admin@test.com` if
missing and promotes the latter), and makes no provider calls.

| what | value |
|---|---|
| library | `you@test.com`: **1** report, **582** words, **27** sources, precision **1.00**, coverage **0.8947**, **3,075** tokens, model `support-chat-lite` |
| filters | `q=pgvector` -> **1/1**; `status=ready` -> **1**; `sort=tokens` descending; `status=bogus` -> **422** |
| Markdown export | **11,670** bytes, `deepresearch-how-should-a-team-choose-...-d44b935a.md`, `text/markdown`, contains title + audit + `## Sources` + `[n]` |
| PDF export | **79,742** bytes, **4** pages, extractable text, `Sources` present, `Page 1 of 4` footer (read back with `pypdf`) |
| ownership | cross-user report id -> **404**; random UUID -> **404** |
| RBAC | agent `/admin/overview` -> **403**; `days=7` echoes the window; `days=0` -> **422** |
| admin snapshot | users **4** (4 active), jobs **25**, reports **2** (2 ready / 0 failed), tokens **589,875** (research **574,673** + report **15,202**), shadow **$0.121414**, success rate **1.0**, **3** failures in window, **4** chart days |
| tests | **81** deterministic tests (54 prior + 12 evals + 6 pricing + 9 export); no network, no LLM |
| frontend | `npm run build` clean: **298.11 kB JS / 94.41 kB gzip**; library search/filter/sort/download + admin KPI/chart/tables |
| versions | `reportlab 5.1.0`, `pypdf 7.1.1` (test-only); Pillow 13.0.0 arrived as ReportLab's dependency |

## Phase 9 - hardening: rate limits, SSRF, concurrency and the load baseline

Phase 9 does not add a feature. It answers one question: *what happens when the input is hostile
or the load is real?* Every decision below is about spending the least new machinery to get a
provable answer - and every one of them is exercised by `scripts/verify_phase9.ps1`, not just
described here.

| Decision | Chosen | Alternatives considered | Why rejected |
|---|---|---|---|
| **Rate limiter** | ~70-line fixed-window counter in `app/core/ratelimit.py`: `INCR` + `EXPIRE` in one pipelined round trip, keyed `dr:rl:<bucket>:<identity>` | SlowAPI; `fastapi-limiter`; sliding-window log; token bucket; gateway/WAF limits | SlowAPI and `fastapi-limiter` wrap the same Redis counter but hide the key, the algorithm and the 429 shape behind a decorator; the 429 must carry `Retry-After` **and** the security headers, which is easiest when we own the response. A sliding-window log stores one entry per request (a burst writes thousands of members); a token bucket needs Lua for atomic refill. Fixed window is one `INCR` and its worst case is a 2x burst across a boundary - acceptable for abuse control, and documented as a residual risk rather than hidden. |
| **Where limits live** | Two layers: per-route dependencies (`user_rate_limit` / `ip_rate_limit`) plus a global **300/min/IP** pure-ASGI middleware backstop | Only per-route; only global | Per-route alone means the next route someone forgets is unlimited. Global alone cannot say "5 signups per hour". The middleware runs before routing so it protects `/docs` and 404 paths too, and it is pure ASGI so the body is never parsed to count a request. |
| **Identity** | User id for authenticated routes; `client_ip()` for anonymous ones, honouring `X-Forwarded-For` only when `TRUST_PROXY_HEADERS=true` | Always IP; always the JWT | IP alone punishes a whole office/NAT for one user. JWT alone cannot protect `/auth/login`, which has no user yet. Trusting `X-Forwarded-For` by default would let any caller choose their own bucket - the classic limiter bypass - so it is a deploy-time opt-in that Phase 10 turns on behind Caddy. |
| **Redis down** | Per-process in-memory fallback, logged once | Fail closed (503); fail open (no limits) | Fail-closed turns a cache outage into a full outage. Fail-open makes the limiter a suggestion. The fallback keeps a single worker honest; the honest limitation is that N workers each get their own window - recorded as residual risk, fixed by a real gateway in Phase 10 if it matters. |
| **Login brute force** | Two counters: 10/min/IP **and** 20/hour/email (salted SHA-256 of the address, never the address itself) | IP only | Distributed attempts rotate IPs but target one mailbox; the email counter is the one that actually stops credential stuffing. Hashing the key keeps PII out of Redis keys and logs. |
| **Refresh-token reuse** | Replaying a rotated token revokes **every live refresh token for that user** and returns 401 | Ignore replay; revoke only the replayed row | Ignoring replay means a stolen token works until it expires. Revoking only that row leaves the attacker's freshly minted token alive. Revoking the family forces both parties to sign in again - annoying, correct, and the standard OAuth guidance. |
| **One active research job** | Partial unique index `uq_research_jobs_active_per_user ON research_jobs (user_id) WHERE status IN ('planning','researching')` | Redis lock; `SELECT ... FOR UPDATE` check in the endpoint; application-level check only | A Redis lock expires or is lost and the database then holds two active jobs. `FOR UPDATE` on a row that may not exist yet cannot lock the *absence* of rows, so two concurrent creates both pass. The partial index is enforced by the only component that always sees the truth, and `IntegrityError` maps to a clean 409. |
| **Double approval / double report** | `SELECT ... FOR UPDATE` on the job at approve; `pg_try_advisory_xact_lock(hashtextextended(job_id,0))` at report start | Unique index only; Redis `SETNX` | Approve is a state transition on an existing row, so `FOR UPDATE` serialises it naturally. Report generation is idempotent-while-running, and an advisory lock releases with the transaction even on a crash - no TTL to tune, no stale lock. |
| **PDF under load** | `asyncio.Semaphore(2)` around `render_pdf_limited`; overflow -> **503 + `Retry-After`** | Unbounded render; a queue; an external worker | ReportLab is CPU-bound in a thread; four concurrent exports on a 2-vCPU box starve the event loop. A queue needs a worker and a status resource; a 503 with `Retry-After` tells the client the truth in one round trip, and capacity is two renders. |
| **SSRF: what to connect to** | Resolve once, reject the name if **any** answer is non-public (`ip.is_global` plus explicit ranges: CGNAT, NAT64, 6to4, benchmarking, TEST-NETs, multicast, ULA) | `is_private` only; a hostname blocklist; a proxy | `is_private` misses 100.64/10 and the IPv4-in-IPv6 wrappers. A blocklist is a losing game against DNS. A forward proxy is another service. Rejecting a mixed answer is deliberate: picking "the first public one" lets a rebinding resolver decide. |
| **SSRF: rebinding** | The guard returns a `ResolvedTarget`; the caller requests the **validated IP literal**, sends the original name as `Host`, and sets `sni_hostname` for TLS. Every redirect hop is re-resolved and re-pinned | Validate then call httpx with the name | httpx resolves the name a second time; that gap is the entire attack. Pinning removes the second resolution instead of racing it. `trust_env=False` stops an ambient `HTTP_PROXY` from routing around the pin. |
| **SSRF: payload size** | Stream with `aiter_bytes`, stop at `MAX_FETCH_BYTES` (2 MB), reject a declared `Content-Length` over the cap, allow only `text/*` / XHTML | `response.content[:cap]` | `response.content` downloads the whole body before slicing: an endless stream is a memory-exhaustion bug. Reading in chunks bounds the work regardless of what the header claims. |
| **Diagnostics exposure** | `/health/llm` requires a user (5/min) because it spends a real LLM call; `/tools/selftest` is admin-only; `/health/agents` and `/health/langfuse` are IP-limited | Leave them public | They were developer conveniences that leaked quota: an anonymous caller could burn Gemini calls through `/health/llm`. Cheap diagnostics stay reachable for monitoring; the expensive one now has an owner. |
| **Registration** | `REGISTRATION_MODE=open|invite` + optional `X-Invite-Code` header | Open forever; a database invitations table | Open locally is the point. An invite table is the Phase 10 design if registration must close for a launch; the switch means the deploy flips an env var instead of shipping a code change. |
| **Error hygiene** | `_as_http_error`: client-side markers -> 400 with the actionable message; everything else -> generic 502, details logged | Echo the provider error | A traceback or local path in a 502 is free reconnaissance. The SSRF and bad-URL messages are the caller's own input, so echoing those is safe **and** useful. |
| **Load tool** | ~200-line `scripts/load_test.py` on the existing `httpx` dependency (read / export / login-burst, nearest-rank percentiles, exit codes) | Locust; k6; JMeter | Locust adds gevent/Flask; k6 is a binary that is not in the repo or CI; JMeter is a GUI tool from another era. Three loops and a percentile function are readable at 3 a.m. and run on the app's own Python. |
| **Health probe cost** | One module-level Redis client reused by `/healthz` | A client per call; no Redis probe | Building a client (DNS + TCP + handshake) cost ~56 ms of every poll, and a load balancer polls every few seconds. redis-py reconnects on its own, so the shared client is also the resilient choice. |
| **Baseline validity** | The limiter is reset (`scripts/phase9_reset_limits.py`, `dr:rl:*` only) before a burst, and `RATE_LIMIT_MULTIPLIER` exists for a throughput run | Comment out limits for the test; separate Redis | Commenting out decorators means testing different code. A multiplier changes only the budget. Deleting the wrong Redis keys would wipe checkpoints and the page cache, so the reset script matches one prefix. |

### Phase 9 incidents (each one changed the code)

1. **Windows PowerShell 5.1 splatted a string as an array of characters.** `Run-Python($arguments)` was
   declared without a type, so `& $python @arguments` passed the path one character at a time and Python
   reported `can't open file 'C:\projects\DeepResearch\C'`. Fix: `param([string[]]$arguments)` - the
   splat operator needs an array type, not a string that happens to contain spaces. This is the same
   class of bug as Phase 8's `-UseBasicParsing`: a PowerShell 5.1 trap that only appears in CI-style
   non-interactive runs.
2. **Verification shared Redis with the developer's API.** A login burst from an earlier run left
   `127.0.0.1` over budget, so the next run failed before it tested anything. Fix: reset **only**
   `dr:rl:*` as test setup, then still demand a 429 from a clean window - setup, not a bypass.
3. **A verification script that starts its own API needed to wait for the MCP child process.**
   `/tools` answers 503 until the stdio server is up; the first SSRF probe hit that 503 and looked like
   a broken guard. Fix: poll `/tools` for up to 60 s before the matrix, and fail loudly if it never
   arrives.
4. **`assert_public_url` was not enough because httpx resolved the name again.** The test that redirects
   a public URL to `127.0.0.1` passed the guard and then connected to loopback. Fix: the guard now returns
   the pinned target and the fetch path connects to the IP literal only. The regression test asserts the
   transport saw the IP and the `Host` header saw the name.
5. **`response.content[:2MB]` is a memory cap, not a work cap.** A hostile server that never closes can
   still make httpx buffer forever. Fix: `_read_capped` streams and stops; the test feeds two 6-byte
   chunks through a 10-byte cap and asserts the returned body is exactly 10 bytes.
6. **The partial index had to be a *real* database test.** A mocked `IntegrityError` proves the exception
   handler, not the constraint. Fix: `scripts/phase9_db_checks.py` creates a throwaway user, inserts two
   active jobs, asserts the second is rejected, then finishes the first and asserts the slot is freed -
   all inside the verification run, with the probe user deleted in `finally`.
7. **`/health/llm` was an anonymous LLM tap.** It was public from Phase 1 and spends a real provider
   call. Fix: require a user and 5/min; `verify_phase1.ps1` now logs in first (a Phase 1 script that
   still passes after a Phase 9 change is a compatibility proof, not a formality).
8. **The 429 assertion was flaky because a fixed window can roll over mid-burst.** Twelve login
   attempts sat exactly on the 10/min IP limit, but when the burst straddled a minute boundary the
   counter reset halfway through: six in one window, six in the next, no 429, red run. The failure
   was real feedback, not noise - it is the documented boundary weakness happening on cue. Fix: reset
   the limiter immediately before the burst **and** fire 24 attempts, which exceeds the 20/hour
   per-email budget no matter where the boundary falls. The exact split varies with the boundary
   (10 allowed / 14 blocked in one run, 19/5 in another); the invariant the script asserts is that
   the burst is refused at least once, with `Retry-After` and the security headers intact.
9. **The new limits broke the old verification scripts.** Phase 3 creates two research jobs per run
   and Phase 4 creates two more; with a 5/hour per-user limit the fifth job of a normal
   phase-3-then-phase-4 session was refused with 429. Phase 6/8 also register accounts on every run,
   against a 5/hour/IP registration limit. Fix: scripts that own their API (`6`, `7`, `8`) start it
   with `RATE_LIMIT_ENABLED=false`, because they are not testing the limiter; scripts that use the
   shared API (`3`, `4`, `5`) clear the `dr:rl:*` counters as setup. The limits are still proven by
   `verify_phase9.ps1`, which runs with normal limits on purpose. A hardening phase that quietly
   breaks the demo scripts is not hardening - this incident is why every phase script was re-run.

### Phase 9 verification evidence

`scripts/verify_phase9.ps1` - 9 sections, all passing; it starts a private API on `:8006` and stops it
in `finally`, resets only the limiter keys, and makes no LLM call.

| what | value |
|---|---|
| headers | `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, CSP; **no HSTS** from a laptop (HSTS is `prod` + HTTPS only) |
| Host / auth | forged `Host: evil.example` -> **400**; anonymous `/research` -> **401**; anonymous `/health/llm` -> **401**; agent `/tools/selftest` -> **403**; agent `/admin/overview` -> **403** |
| SSRF | **11** URLs -> **400** through the real `POST /tools/fetch`: metadata `169.254.169.254`, `127.0.0.1:8000`, `[::1]`, `10.0.0.1`, `192.168.1.1`, CGNAT `100.64.0.1`, decimal `2130706433`, hex `0x7f000001`, port 22, `file://`, credentials in URL |
| refresh reuse | rotation works; replaying the old cookie -> **401**; the **new** cookie is also dead -> **401** (family revoked) |
| concurrency | second active job rejected by `uq_research_jobs_active_per_user`; finishing the first frees the slot |
| ownership | another user's report export -> **404**; another user's job report -> **404**; random UUID -> **404** |
| read load | **41** requests, **3** users x 2 s, p50 **2,032 ms**, p95 **2,982 ms**, p99 **2,993 ms**, no 5xx (see baseline note; run-to-run p95 is 2.97-3.06 s) |
| export load | **42** requests, **4** concurrent PDF/MD, p95 **2,980 ms**, no 5xx (503 is the semaphore brake) |
| login burst | **24** attempts -> 429 at least once (observed 10/14 and 19/5 allowed/blocked, depending on the minute boundary), `Retry-After` and security headers present |
| tests | **180** deterministic tests (81 prior + 46 SSRF/pinned-fetch + 22 sanitizer + 16 security + 12 limiter + 3 export-cap); no network, no LLM |
| dependencies | **none added** - the limiter is stdlib + `redis`, the load tool is `httpx`, the PDF cap is `asyncio.Semaphore` |
| version drift (not bumped here) | `langfuse 5.0.4 -> 5.2.0`, `langchain-openai 1.7.4 -> 1.8.5`, `langchain-core 1.8.0 -> 1.8.4`, `langgraph 1.2.21 -> 1.2.24`, `fastapi 0.162.0 -> 0.165.0`, `pydantic 2.14.1 -> 2.14.4`, `SQLAlchemy 2.1.6 -> 2.1.9`, `python-multipart 0.0.32 -> 0.0.34`, `email-validator 2.3.0 -> 2.4.0`; `mcp` stays 1.30.0 (pinned by `langchain-mcp-adapters 0.3.2`). Upgrade separately: a security commit should not also change the framework. |

### The measured baseline, told honestly

The read run's p95 is **~3 s**, not the <300 ms a hardened API "should" have. That is not the limiter:
an unauthenticated request is answered in **9 ms**, Redis `PING` on a reused client is **3 ms**, and
the limiter adds one round trip. The time is the **dev database**: a cold `SELECT 1` takes **4,253 ms**,
a warm user lookup **452 ms**, and a report query **671 ms**. The dev Postgres is a free-tier Neon
instance reached across the public internet from this laptop, and every request that touches it pays
a WAN round trip (often two).

The right conclusion is not "the app is slow" - it is "measure the network before optimising the code".
Phase 10 places the API and the database in the same region, where that 450-670 ms becomes single-digit
milliseconds and the same code is fast. The `pool_pre_ping=True` in `db.py` is also visible here: it
spends a round trip checking a connection the pool already owns. It stays, because a free-tier pooler
*does* drop idle connections; the fix for latency is locality, not removing the check.

### Residual risks (deliberately carried into Phase 10)

| risk | why it is acceptable now | what closes it |
|---|---|---|
| Fixed-window boundary burst: a caller can do 2x the limit across the boundary | Abuse control, not billing; the global 300/min backstop still applies | Sliding window or a gateway rule if a real client abuses it |
| Per-worker memory fallback means N workers x the limit when Redis is down | One uvicorn locally; a Redis outage is already a degraded service | A gateway (Caddy/ALB) rate limit, or a Redis that is not a single free-tier instance |
| No WAF / no bot detection | The API is private during Phase 9 | Cloudflare or an ALB WAF in Phase 10 |
| SSRF guard pins one address; a CDN whose IP rotates mid-request fails | Failure is a clean 400/502, not a leak; the next request re-resolves | Not worth solving until a real source fails |
| `/docs` and `/openapi.json` are public locally | Useful in dev; no secrets in the schema | Disable in `APP_ENV=prod` (Phase 10 checklist) |
| Login lockout is per-email/IP, not per-account with a captcha | 20/h/email already makes stuffing expensive | Captcha or proof-of-work if registration opens publicly |

