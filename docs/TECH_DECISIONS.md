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
- **"Why MCP *and* A2A?"** — "Different layers: MCP standardizes *tools* (my search tool is reusable by any client); A2A standardizes *agents* (my writer is a discoverable service). One protocol can't do both."
- **"Why Redis over just Postgres?"** — "Postgres is durable truth; Redis is ephemeral speed — checkpoints for crash recovery, cache to dedupe fetches, pub/sub to fan out stream tokens without polling the DB."
- **"How do you know it's correct?"** — "Citation precision ≥0.90 on a golden set, gated in CI; every claim links to cached source text so a human can verify."
- **"Why not OpenAI?"** — "No usable free tier; we're on Gemini (1M context, free) with Groq failover behind LiteLLM — one config line to swap."

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
