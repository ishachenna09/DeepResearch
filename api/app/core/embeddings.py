"""Embeddings through the LiteLLM proxy.

Why the proxy and not the provider SDK directly?
- One credential lives in one process. The API only knows LITELLM_MASTER_KEY;
  GEMINI_API_KEY stays in the proxy (separate container in Phase 10).
- The model id is a config line (`litellm_config.yaml` -> model_name: embeddings),
  so switching to another embedder is a YAML edit plus a re-embed, not a code change.
- The proxy's fallback/retry machinery is shared with chat, one place to reason
  about provider outages.

Why 1536 dimensions when the model emits 3072?
gemini-embedding-2 is MRL-trained, so it can be truncated with little quality
loss. pgvector's HNSW index supports `vector` up to 2000 dims; 3072 would force
`halfvec(3072)` (extra ops class, half precision). 1536 keeps the plain `vector`
type and a normal HNSW index. Measured against the live proxy before choosing:
  default -> 3072 | dimensions=1536 -> 1536 | dimensions=768 -> 768

Note on normalisation: truncated Gemini vectors are not unit length, but we rank
with cosine distance (`<=>`), which divides by both norms - so rankings and the
1 - distance score are already correct without a normalisation pass.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# 408/409/425/429/5xx are transient; a 400 (bad model name, bad input) is not.
_RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504}
_ATTEMPTS = 3

_client: httpx.AsyncClient | None = None


class EmbeddingError(RuntimeError):
    """The embedding call failed after retries - the caller decides to skip."""


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=f"{settings.LITELLM_BASE_URL}/v1",
            headers={"Authorization": f"Bearer {settings.LITELLM_MASTER_KEY}"},
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
    return _client


async def close_embeddings() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def _embed_batch(texts: list[str]) -> list[list[float]]:
    body = {
        "model": settings.EMBEDDINGS_MODEL,
        "input": texts,
        "dimensions": settings.EMBED_DIM,
    }
    last_error = ""
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            resp = await _http().post("/embeddings", json=body)
        except httpx.HTTPError as exc:                    # timeout / connection reset
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code < 400:
                data = resp.json().get("data") or []
                # The API is allowed to return batches out of order: sort by index.
                data = sorted(data, key=lambda item: item.get("index", 0))
                vectors = [item["embedding"] for item in data]
                if len(vectors) != len(texts):
                    raise EmbeddingError(
                        f"embeddings: asked {len(texts)} vectors, got {len(vectors)}"
                    )
                for vec in vectors:
                    # Guards against a silent config drift: if `dimensions` were
                    # dropped by the proxy we would get 3072 and the pgvector
                    # insert would fail much later with a confusing error.
                    if len(vec) != settings.EMBED_DIM:
                        raise EmbeddingError(
                            f"embeddings: expected {settings.EMBED_DIM} dims, got {len(vec)}. "
                            "Check EMBED_DIM against litellm_config.yaml (dimensions support)."
                        )
                return vectors
            last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
            if resp.status_code not in _RETRYABLE:
                raise EmbeddingError(f"embeddings rejected: {last_error}")

        if attempt < _ATTEMPTS:
            await asyncio.sleep(1.5 * attempt)            # 1.5s, 3s

    raise EmbeddingError(f"embeddings unavailable after {_ATTEMPTS} attempts: {last_error}")


async def embed_texts(texts: list[str], batch_size: int | None = None) -> list[list[float]]:
    """Embed a list of strings, preserving order.

    Batches run sequentially on purpose: free tiers count *requests* per minute,
    so a bigger batch is strictly better than parallel small ones, and parallel
    calls would just convert rate limits into retries.
    """
    clean = [t.strip() or " " for t in texts]
    if not clean:
        return []

    size = batch_size or settings.EMBED_BATCH_SIZE
    vectors: list[list[float]] = []
    for start in range(0, len(clean), size):
        chunk = clean[start : start + size]
        vectors.extend(await _embed_batch(chunk))
        logger.debug("embedded %d/%d notes", len(vectors), len(clean))
    return vectors
