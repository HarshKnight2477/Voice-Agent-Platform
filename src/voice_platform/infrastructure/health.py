"""Dependency checks behind a small interface so the health endpoint is testable without Docker."""

from __future__ import annotations

from typing import Protocol

from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class DependencyChecker(Protocol):
    async def check(self, name: str) -> None:
        """Return normally if the dependency answers, raise otherwise."""


class LiveDependencyChecker:
    def __init__(self, engine: AsyncEngine, redis: Redis) -> None:
        self._engine = engine
        self._redis = redis

    async def check(self, name: str) -> None:
        if name == "database":
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        elif name == "redis":
            await self._redis.ping()
        else:
            raise ValueError(f"unknown dependency: {name}")


def get_dependency_checker(request: Request) -> DependencyChecker:
    checker: DependencyChecker = request.app.state.dependency_checker
    return checker
