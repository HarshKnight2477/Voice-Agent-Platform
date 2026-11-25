"""Liveness and readiness probes.

/health/live  -> the process is up (no dependencies checked).
/health/ready -> PostgreSQL and Redis answer within a short timeout. Returns 503 otherwise.
Error details are logged, never returned, so the endpoint leaks nothing about infrastructure.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel

from voice_platform.infrastructure.health import DependencyChecker, get_dependency_checker
from voice_platform.shared.logging import get_logger

router = APIRouter(prefix="/health", tags=["health"])
log = get_logger(__name__)

CHECK_TIMEOUT_SECONDS = 2.0

CheckStatus = Literal["ok", "unavailable"]


class LiveResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadyResponse(BaseModel):
    status: CheckStatus
    checks: dict[str, CheckStatus]


@router.get("/live", response_model=LiveResponse)
async def live() -> LiveResponse:
    return LiveResponse()


@router.get("/ready", response_model=ReadyResponse)
async def ready(
    response: Response,
    checker: Annotated[DependencyChecker, Depends(get_dependency_checker)],
) -> ReadyResponse:
    names = ("database", "redis")
    results = await asyncio.gather(
        *(asyncio.wait_for(checker.check(name), CHECK_TIMEOUT_SECONDS) for name in names),
        return_exceptions=True,
    )
    checks: dict[str, CheckStatus] = {}
    for name, result in zip(names, results, strict=True):
        if isinstance(result, BaseException):
            log.warning(
                "health.dependency_unavailable", dependency=name, error=type(result).__name__
            )
            checks[name] = "unavailable"
        else:
            checks[name] = "ok"

    overall: CheckStatus = "ok" if all(v == "ok" for v in checks.values()) else "unavailable"
    if overall != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(status=overall, checks=checks)
