"""Redis-backed page cache for fetched web pages.

Why cache pages at all?
- Phase 3 fans out over N sub-questions and each branch reads up to TOP_K_PAGES
  pages. Overlapping queries (and the user's "approve" retries) hit the same
  URLs; a cache converts a 300-900 ms fetch + trafilatura extraction into a
  ~2 ms Redis GET.
- It also dampens upstream politeness problems: Tavily/fetch results are reused
  instead of re-hammering a site during a retry.

Why a *separate* Redis client from the checkpointer?
The checkpointer owns its own connection and RediSearch indices. Cache entries
are plain GET/SET with a TTL; separate clients keep a cache-flush from ever
touching checkpoint state, and keep the lifetime of each explicit.

Why the hard TTL?
Redis Cloud's free tier is 30 MB, shared with LangGraph checkpoints. A 12 KB
page x 2 500 pages would fill it, so entries expire after CACHE_TTL_SECONDS and
we store only the already-truncated extracted text - never raw HTML.

Failure model: the cache is an optimisation, never a dependency. Every operation
swallows Redis errors (logged once) so a full/evicted/restarted cache degrades
to "fetch it again" instead of failing the research job.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time

import redis.asyncio as redis

from app.core.config import settings

logger = logging.getLogger(__name__)

PREFIX = "dr:cache:page:"


def cache_key(url: str) -> str:
    return PREFIX + hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:32]


class PageCache:
    def __init__(self) -> None:
        self._client: redis.Redis | None = None
        self._warned = False

    # ---------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        # decode_responses=True -> we get str back, no .decode() sprinkles.
        self._client = redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            health_check_interval=30,
        )
        try:
            await self._client.ping()
            logger.info("page cache ready (ttl=%ss)", settings.CACHE_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001 - degrade to no-op cache
            self._warn(f"page cache unreachable ({type(exc).__name__}: {exc})")

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._client = None

    @property
    def ready(self) -> bool:
        return self._client is not None

    def _warn(self, message: str) -> None:
        """Log a cache problem once per process, not once per page."""
        if not self._warned:
            logger.warning("%s - continuing without cache", message)
            self._warned = True

    # ------------------------------------------------------------------ surface
    async def get(self, url: str) -> dict | None:
        if self._client is None:
            return None
        try:
            raw = await self._client.get(cache_key(url))
        except Exception as exc:  # noqa: BLE001
            self._warn(f"cache GET failed ({type(exc).__name__}: {exc})")
            return None
        if not raw:
            return None
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return record if isinstance(record, dict) else None

    async def set(self, url: str, record: dict) -> None:
        if self._client is None:
            return
        payload = dict(record)
        payload["cached_at"] = int(time.time())
        try:
            await self._client.set(
                cache_key(url), json.dumps(payload), ex=settings.CACHE_TTL_SECONDS
            )
        except Exception as exc:  # noqa: BLE001
            self._warn(f"cache SET failed ({type(exc).__name__}: {exc})")

    async def stats(self) -> dict:
        """Cheap introspection for /health and the verify script."""
        info: dict = {"ready": self.ready, "ttl_seconds": settings.CACHE_TTL_SECONDS}
        if self._client is None:
            return info
        try:
            info["cached_pages"] = await self._client.dbsize()  # includes checkpoint keys
            sample = None
            async for key in self._client.scan_iter(match=f"{PREFIX}*", count=100):
                sample = key
                break
            info["has_page_entries"] = sample is not None
        except Exception as exc:  # noqa: BLE001
            info["error"] = f"{type(exc).__name__}: {exc}"
        return info


_cache: PageCache | None = None


async def init_page_cache() -> PageCache:
    global _cache
    _cache = PageCache()
    await _cache.connect()
    return _cache


async def close_page_cache() -> None:
    global _cache
    if _cache is not None:
        await _cache.close()
    _cache = None


def get_page_cache() -> PageCache:
    if _cache is None:
        raise RuntimeError("Page cache not initialised - call init_page_cache() in lifespan")
    return _cache
