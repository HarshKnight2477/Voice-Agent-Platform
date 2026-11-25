"""Structured logging.

Every log line is JSON (or pretty console output locally) and carries correlation ids
bound through contextvars: request_id now, and tenant_id / call_id / agent_version_id
as later phases add them. Sensitive keys are masked before output.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

SENSITIVE_KEYS = frozenset(
    {"password", "secret", "token", "api_key", "authorization", "cookie", "set-cookie"}
)


def _mask_sensitive(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if any(s in key.lower() for s in SENSITIVE_KEYS):
            event_dict[key] = "***"
    return event_dict


def configure_logging(
    level: str = "INFO", json_output: bool = True, *, cache_loggers: bool = True
) -> None:
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _mask_sensitive,
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[*shared, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=cache_loggers,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
