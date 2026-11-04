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
