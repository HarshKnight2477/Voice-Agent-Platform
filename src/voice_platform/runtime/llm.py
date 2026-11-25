"""The LLMProvider port: provider-neutral request/response types for the agent runtime.

The runtime builds an LLMRequest and gets back an LLMResponse; it never sees vendor types.
Tool calls come back as *requests* only. Executing them is the Tool Gateway's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol

ModelTier = Literal["fast", "balanced", "deep"]
Effort = Literal["low", "medium", "high"]


class StopReason(StrEnum):
    END = "end"  # finished its answer
    TOOL_USE = "tool_use"  # wants one or more tools run
    MAX_TOKENS = "max_tokens"  # cut off by the output limit
    REFUSAL = "refusal"  # declined for safety reasons (after any fallback)
    OTHER = "other"


@dataclass(frozen=True)
class SystemBlock:
    """One part of the system prompt. `cacheable` blocks must be identical across turns of a
    call (and ideally across calls of the same agent version) so the provider can cache them."""

    text: str
    cacheable: bool = False


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    content: str  # already validated and sanitized by the Tool Gateway
    is_error: bool = False


@dataclass(frozen=True)
class Message:
    """A conversation turn. Assistant turns may carry tool calls; user turns may carry the
    results of those calls. `provider_content` preserves the provider's raw blocks for the
    assistant turn so they can be replayed unchanged (some providers require that)."""

    role: Literal["user", "assistant"]
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
    provider_content: Any = None


@dataclass(frozen=True)
class LLMRequest:
    system: tuple[SystemBlock, ...]
    messages: tuple[Message, ...]
    tier: ModelTier = "balanced"
    effort: Effort = "low"
    max_output_tokens: int = 400
    tools: tuple[ToolSpec, ...] = ()


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class LLMResponse:
    text: str
    stop_reason: StopReason
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    latency_ms: float = 0.0
    refusal_category: str | None = None
    provider_content: Any = None

    def as_message(self) -> Message:
        return Message(
            role="assistant",
            text=self.text,
            tool_calls=self.tool_calls,
            provider_content=self.provider_content,
        )


class LLMError(Exception):
    """Base for provider failures. Never shown to callers; the runtime turns it into a
    graceful spoken fallback or a transfer."""

    retryable: bool = False


class LLMUnavailableError(LLMError):
    """Timeout, rate limit, overload or network failure. Safe to retry or fall back."""

    retryable = True


class LLMRequestError(LLMError):
    """The request itself was rejected (bad parameters, auth, unknown model). Retrying the
    same request won't help; this is a platform bug or misconfiguration."""


class LLMProvider(Protocol):
    name: str

    async def complete(self, request: LLMRequest) -> LLMResponse: ...
