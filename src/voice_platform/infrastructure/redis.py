"""Redis client (cache, rate limits, queues, per-call state). One pool per process."""

from __future__ import annotations

from redis.asyncio import Redis

from voice_platform.shared.config import Settings


def create_redis(settings: Settings) -> Redis:
    return Redis.from_url(
        str(settings.redis_url),
        socket_connect_timeout=2,
        socket_timeout=2,
        health_check_interval=30,
    )
