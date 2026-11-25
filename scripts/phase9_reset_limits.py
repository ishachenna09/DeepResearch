"""Clear only the Phase 9 rate-limit keys in Redis.

Why this exists: verification runs share one Redis with the developer's API, so
a burst from an earlier run can leave 127.0.0.1 over budget and make the next
run fail before it tests anything. Resetting the limiter is test *setup*, not a
way around the test: the login burst later in the script still has to produce a
429 from a clean window.

It only deletes keys under `dr:rl:` - never checkpoints, streams or cache.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

import redis.asyncio as redis  # noqa: E402

from app.core.config import settings  # noqa: E402

PREFIX = "dr:rl:*"


async def main() -> int:
    client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    removed = 0
    cursor = 0
    try:
        while True:
            cursor, keys = await client.scan(cursor=cursor, match=PREFIX, count=500)
            if keys:
                removed += int(await client.delete(*keys))
            if cursor == 0:
                break
    finally:
        await client.aclose()
    print(f"cleared {removed} rate-limit key(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
