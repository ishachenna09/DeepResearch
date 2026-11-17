from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# DeepResearch/  (repo root — two levels up from app/core/)
ROOT_DIR = Path(__file__).resolve().parents[3]


#this settings class reads .env via pydantic-settings and every other files imports this settings 

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"), extra="ignore", case_sensitive=True
    )

    # ── App ──────────────────────────────────────────────
    APP_ENV: str = "local"
    APP_NAME: str = "DeepResearch"
    APP_VERSION: str = "0.1.0"
    FRONTEND_URL: str = "http://localhost:5173"

    # ── Auth ─────────────────────────────────────────────
    SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MIN: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14

    # ── Data stores (cloud free tiers) ───────────────────
    # NOTE: deliberately NOT named DATABASE_URL — LiteLLM auto-detects that exact
    # name (Heroku-style convention) and tries to use it as ITS OWN metadata DB.
    APP_DATABASE_URL: str       # Neon (Postgres + pgvector)
    REDIS_URL: str             # Redis Cloud (RediSearch) - checkpoints; Upstash is cache-only

    # ── LLM gateway (LiteLLM proxy: local pip process / EC2 container) ──
    LITELLM_BASE_URL: str = "http://localhost:4000"
    LITELLM_MASTER_KEY: str = "sk-local-dev"
    LLM_PRIMARY_MODEL: str = "support-chat"          # proxy model name (Gemini)
    GEMINI_API_KEY: str = ""
    GROQ_API_KEY: str = ""

    # ── Web tools (Phase 2: MCP servers) ─────────────────
    TAVILY_API_KEY: str = ""
    TAVILY_API_URL: str = "https://api.tavily.com/search"
    MAX_FETCH_BYTES: int = 2_000_000   # hard cap on a downloaded page (2 MB)
    MAX_PAGE_CHARS: int = 12_000       # cap on extracted text handed to an LLM
    MCP_WEB_TOOL_MODULE: str = "app.mcp_tools.servers.web_tools"

    # ── Phase 3: fan-out research + page cache + pgvector ─
    # Embeddings go through the LiteLLM proxy (model_name "embeddings"), so the
    # app never holds a provider SDK and the model can be swapped in one YAML line.
    EMBEDDINGS_MODEL: str = "embeddings"
    # gemini-embedding-2 emits 3072 dims natively. 1536 is the MRL truncation we
    # request on purpose: it fits pgvector's `vector` type (HNSW max 2000 dims),
    # so we get an ANN index without halfvec casting. Measured, not assumed.
    EMBED_DIM: int = 1536
    EMBED_BATCH_SIZE: int = 32         # notes per /v1/embeddings request
    CACHE_TTL_SECONDS: int = 6 * 3600  # Redis Cloud free tier is 30 MB shared
    SEARCH_RESULTS: int = 5            # Tavily hits considered per sub-question
    TOP_K_PAGES: int = 3               # pages actually fetched + read per branch
    MAX_BRANCHES: int = 8              # hard cap on parallel Send fan-out
    MAX_NOTES_PER_PAGE: int = 5        # keeps the note prompt small and focused
    # Per-page extraction is throwaway work, but a discarded page means missing
    # notes: 60 s covers a slow-but-successful provider call, and _extract retries
    # once before giving up on the page.
    EXTRACT_TIMEOUT: int = 60

    # ── Observability (Langfuse Cloud) ───────────────────
    LANGFUSE_HOST: str = ""
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
