"""Tool definitions.

A tool is code the platform owns. The model only ever *asks* for a tool by name with JSON
arguments; the Tool Gateway decides whether it runs. Each definition declares everything the
gateway needs to decide: schemas, permission scopes, side effects, confirmation and
verification needs, timeout, retry and rate limits.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from voice_platform.agents.config_schema import AgentConfig
from voice_platform.runtime.state import ConversationState

_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class SideEffect(StrEnum):
    NONE = "none"  # read-only; safe to retry
    REVERSIBLE = "reversible"  # changes something that can be undone (e.g. a booking)
    IRREVERSIBLE = "irreversible"  # cannot be undone (e.g. sending a message)


@dataclass(frozen=True)
class ToolContext:
    """Who is acting. Built by the runtime from the call's server-side identity; the model
    can never set any of these values."""

    organization_id: uuid.UUID
    agent_id: uuid.UUID
    agent_version_id: uuid.UUID
    call_id: str
    config: AgentConfig
    state: ConversationState
    idempotency_key: str = ""
    services: dict[str, Any] = field(default_factory=dict)


class ToolFailure(Exception):
    """Raised by a handler for an expected, caller-safe failure ("no tables at 7pm").
    `code` and `hint` go back to the model; the message is never spoken verbatim."""

    def __init__(self, code: str, hint: str) -> None:
        super().__init__(code)
        self.code = code
        self.hint = hint


Handler = Callable[[ToolContext, Any], Awaitable[BaseModel]]
RuleCheck = Callable[[ToolContext, Any], str | None]  # returns a denial hint, or None


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: Handler
    required_scopes: frozenset[str] = frozenset()
    side_effect: SideEffect = SideEffect.NONE
    requires_confirmation: bool = False
    requires_verified_caller: bool = False
    timeout_ms: int = 3000
    max_attempts: int = 1  # retries only ever apply to read-only tools
    max_calls_per_call: int = 10
    # None = platform tool available to every tenant; otherwise only this organization's agents.
    owner_organization_id: uuid.UUID | None = None
    rule_check: RuleCheck | None = None
    readback: Callable[[Any], str] | None = None  # how to read the action back for confirmation
    internal: bool = False  # bookkeeping tools (e.g. update_state): no audit-log noise
    schema_override: dict[str, Any] | None = None  # hand-written schema with descriptions

    def __post_init__(self) -> None:
        if not _NAME.match(self.name):
            raise ValueError(f"invalid tool name: {self.name}")
        if self.side_effect is not SideEffect.NONE and self.max_attempts > 1:
            raise ValueError(f"{self.name}: tools with side effects must not auto-retry")
        if self.side_effect is SideEffect.IRREVERSIBLE and not self.requires_confirmation:
            raise ValueError(f"{self.name}: irreversible tools must require confirmation")
        if not 100 <= self.timeout_ms <= 30_000:
            raise ValueError(f"{self.name}: timeout_ms must be between 100 and 30000")

    def json_schema(self) -> dict[str, Any]:
        if self.schema_override is not None:
            return dict(self.schema_override)
        schema = self.input_model.model_json_schema()
        schema.setdefault("additionalProperties", False)
        schema.pop("title", None)
        return schema
