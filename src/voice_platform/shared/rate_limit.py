"""Fixed-window rate limiting backed by Redis (shared by every API process)."""

from __future__ import annotations

from typing import Protocol

from redis.asyncio import Redis


class RateLimiter(Protocol):
    async def hit(self, key: str, *, limit: int, window_seconds: int) -> bool:
        """Count one attempt. Return True if allowed, False if over the limit."""


class RedisRateLimiter:
    def __init__(self, redis: Redis, prefix: str = "rl") -> None:
        self._redis = redis
        self._prefix = prefix

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> bool:
        full_key = f"{self._prefix}:{key}"
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(full_key)
            pipe.expire(full_key, window_seconds, nx=True)
            count, _ = await pipe.execute()
        return int(count) <= limit
