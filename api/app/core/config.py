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

    # ── Observability (Langfuse Cloud) ───────────────────
    LANGFUSE_HOST: str = ""
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
