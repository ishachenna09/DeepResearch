"""Langfuse client (Langfuse Cloud) — tracing wired fully in Phase 1."""
from langfuse import Langfuse

from app.core.config import settings

_client: Langfuse | None = None


def get_langfuse() -> Langfuse | None:
    global _client
    if _client is None and settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY:
        _client = Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            host=settings.LANGFUSE_HOST or "https://cloud.langfuse.com",
        )
    return _client


def shutdown_langfuse() -> None:
    if _client is not None:
        _client.flush()
