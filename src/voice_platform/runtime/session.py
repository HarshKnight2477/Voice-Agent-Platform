"""CallSession: the agent runtime for one call.

Transport-agnostic: the LiveKit worker (Phase 3) feeds it caller text and speaks what it
returns; the test console and automated tests do the same with plain text. One session is
pinned to one agent version for its whole life.

Per caller turn:
  update counters -> classify yes/no -> ask the LLM -> run requested tools through the
  gateway -> ask again with results (bounded) -> guard the reply for speech -> decide on
  escalation / end of call.
Every failure path ends in something sensible to say. Internal errors are never spoken.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from voice_platform.agents.config_schema import AgentConfig, EscalationTrigger
from voice_platform.runtime.confirmation import is_affirmative
from voice_platform.runtime.llm import (
    LLMError,
    LLMProvider,
    LLMRequest,
    LLMRequestError,
    LLMResponse,
    Message,
    ModelTier,
    StopReason,
    ToolResult,
    Usage,
)
from voice_platform.runtime.prompts import build_system_prompt
from voice_platform.runtime.speech import to_speech
from voice_platform.runtime.state import ConversationState, EscalationStatus
from voice_platform.shared.logging import get_logger
from voice_platform.tools.base import ToolContext
from voice_platform.tools.gateway import CallToolCounters, Outcome, ToolGateway
from voice_platform.tools.registry import ToolRegistry

log = get_logger(__name__)

MAX_HISTORY_MESSAGES = 30
FALLBACK_TIER: dict[ModelTier, ModelTier | None] = {
    "deep": "balanced",
    "balanced": "fast",
    "fast": None,
}

SAY_TROUBLE = "Sorry, I'm having a little trouble on my end."
SAY_OFFER_PERSON = "I can connect you with someone who can help, or take a message."
SAY_CANT_HELP = "I'm sorry, I can't help with that. Is there anything else I can do for you?"
SAY_REPROMPT = "Are you still there?"
SAY_GOODBYE_SILENCE = "I haven't heard anything, so I'll let you go. Thanks for calling. Goodbye."
SAY_GOODBYE_TIME = "I'm sorry, we've reached the time limit for this call. Thanks for calling."
SAY_DIDNT_CATCH = "Sorry, I didn't catch that. Could you say it again?"


class TurnAction(StrEnum):
    CONTINUE = "continue"  # keep listening
    TRANSFER = "transfer"  # hand the call to a person (runtime asks telephony to transfer)
    END_CALL = "end_call"  # say the reply, then hang up


@dataclass(frozen=True)
class Handoff:
    """What the human receives so the caller doesn't repeat themselves."""

    reason: str
    summary: str
    intent: str | None
    details: dict[str, str]
    customer: dict[str, str]
    actions: list[str]


@dataclass
class TurnMetrics:
    llm_calls: int = 0
    llm_latency_ms: float = 0.0
    tool_calls: int = 0
    tool_latency_ms: float = 0.0
    total_ms: float = 0.0
    usage: Usage = field(default_factory=Usage)


@dataclass
class TurnResult:
    say: str
    action: TurnAction = TurnAction.CONTINUE
    handoff: Handoff | None = None
    metrics: TurnMetrics = field(default_factory=TurnMetrics)
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class SessionIdentity:
    organization_id: uuid.UUID
    agent_id: uuid.UUID
    agent_version_id: uuid.UUID
    agent_name: str
    call_id: str


def _add_usage(a: Usage, b: Usage) -> Usage:
    return Usage(
        a.input_tokens + b.input_tokens,
        a.output_tokens + b.output_tokens,
        a.cache_read_tokens + b.cache_read_tokens,
        a.cache_write_tokens + b.cache_write_tokens,
    )


def trim_history(messages: list[Message], limit: int = MAX_HISTORY_MESSAGES) -> list[Message]:
    """Keep the most recent messages without splitting a tool call from its results and
    without starting on an assistant turn. Older context lives on in the call state summary."""
    if len(messages) <= limit:
        return messages
    start = len(messages) - limit
    while start < len(messages):
        m = messages[start]
        if m.role == "user" and not m.tool_results:
            break
        start += 1
    return messages[start:]


class CallSession:
    def __init__(
        self,
        identity: SessionIdentity,
        config: AgentConfig,
        llm: LLMProvider,
        registry: ToolRegistry,
        gateway: ToolGateway,
        *,
        services: dict[str, Any] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        required_slots: list[str] | None = None,
    ) -> None:
        self.identity = identity
        self.config = config
        self._llm = llm
        self._gateway = gateway
        self._services = services or {}
        self._clock = clock
        self.state = ConversationState(required_slots=required_slots or [])
        self.history: list[Message] = []
        self._counters = CallToolCounters()
        self._silences = 0
        self._started_at: datetime | None = None
        self.ended = False
        self.tools, unavailable = registry.toolset(
            identity.organization_id, config.tools, always=["update_state"]
        )
        if unavailable:
            log.warning(
                "session.tools_unavailable",
                tools=unavailable,
                agent_version_id=str(identity.agent_version_id),
            )

    # ------------------------------------------------------------------ lifecycle

    def greeting(self) -> TurnResult:
        """Spoken first. Disclosure and recording notice come before anything else."""
        self._started_at = self._clock()
        parts = [
            self.config.safety.ai_disclosure,
            self.config.safety.recording_notice,
            self.config.greeting,
        ]
        text = " ".join(p.strip() for p in parts if p.strip())
        # The conversation sent to the model must start with the caller, so the greeting is
        # carried in the call state instead of the message history.
        self.state.opening = self.config.greeting
        return TurnResult(say=to_speech(text, max_chars=600), events=[{"type": "greeting"}])

    def _elapsed_s(self) -> float:
        if self._started_at is None:
            return 0.0
        return (self._clock() - self._started_at).total_seconds()

    async def handle_silence(self) -> TurnResult:
        """Called by the transport when the caller has said nothing for a while."""
        self._silences += 1
        if self._silences > self.config.limits.max_silence_reprompts:
            self.ended = True
            return TurnResult(
                say=SAY_GOODBYE_SILENCE,
                action=TurnAction.END_CALL,
                events=[{"type": "silence_timeout"}],
            )
        return TurnResult(say=SAY_REPROMPT, events=[{"type": "silence_reprompt"}])

    # ------------------------------------------------------------------ turns

    async def handle_user_turn(self, text: str) -> TurnResult:
        started = time.perf_counter()
        metrics = TurnMetrics()
        events: list[dict[str, Any]] = [{"type": "user_turn", "chars": len(text)}]
        text = text.strip()[:2000]
        self._silences = 0

        if self.ended:
            return TurnResult(say="", action=TurnAction.END_CALL)
        if self._elapsed_s() > self.config.limits.max_call_seconds:
            self.ended = True
            return TurnResult(
                say=SAY_GOODBYE_TIME,
                action=TurnAction.END_CALL,
                events=[*events, {"type": "call_time_limit"}],
            )
        if not text:
            return TurnResult(say=SAY_DIDNT_CATCH, events=events)

        self.state.turns += 1
        self.state.last_user_affirmed = is_affirmative(text)
        self.history.append(Message(role="user", text=text))

        reply = await self._reason(metrics, events)
        result = self._finish_turn(reply, metrics, events)
        metrics.total_ms = (time.perf_counter() - started) * 1000
        return result

    async def _reason(self, metrics: TurnMetrics, events: list[dict[str, Any]]) -> str | None:
        """LLM <-> tools loop. Returns the text to say, or None after a failure."""
        llm_settings = self.config.advanced.llm
        tier: ModelTier = llm_settings.model  # type: ignore[assignment]
        for _ in range(self.config.limits.max_tool_iterations_per_turn + 1):
            request = LLMRequest(
                system=build_system_prompt(
                    self.config, self.identity.agent_name, self.state, self._clock()
                ),
                messages=tuple(trim_history(self.history)),
                tier=tier,
                effort=llm_settings.effort,
                max_output_tokens=llm_settings.max_output_tokens,
                tools=self.tools,
            )
            response = await self._complete(request, metrics, events)
            if response is None:
                return None
            if response.stop_reason is StopReason.REFUSAL:
                events.append({"type": "llm_refusal", "category": response.refusal_category})
                self.history.append(Message(role="assistant", text=SAY_CANT_HELP))
                return SAY_CANT_HELP
            self.history.append(response.as_message())
            if response.stop_reason is StopReason.TOOL_USE and response.tool_calls:
                results = await self._run_tools(response, metrics, events)
                self.history.append(Message(role="user", tool_results=tuple(results)))
                continue
            return response.text
        events.append({"type": "tool_iteration_limit"})
        return None

    async def _complete(
        self, request: LLMRequest, metrics: TurnMetrics, events: list[dict[str, Any]]
    ) -> LLMResponse | None:
        """One LLM call, falling back to a lighter tier if the provider is unavailable."""
        tier: ModelTier | None = request.tier
        while tier is not None:
            attempt = replace(request, tier=tier)
            try:
                response = await self._llm.complete(attempt)
            except LLMRequestError as exc:
                log.error(
                    "session.llm_request_error", error=str(exc), call_id=self.identity.call_id
                )
                events.append({"type": "llm_error", "kind": "request"})
                return None
            except LLMError as exc:
                log.warning("session.llm_unavailable", tier=tier, error=str(exc))
                events.append({"type": "llm_error", "kind": "unavailable", "tier": tier})
                tier = FALLBACK_TIER[tier]
                continue
            metrics.llm_calls += 1
            metrics.llm_latency_ms += response.latency_ms
            metrics.usage = _add_usage(metrics.usage, response.usage)
            events.append(
                {
                    "type": "llm_response",
                    "tier": tier,
                    "stop": response.stop_reason.value,
                    "latency_ms": round(response.latency_ms, 1),
                    "tool_calls": [c.name for c in response.tool_calls],
                }
            )
            return response
        return None

    async def _run_tools(
        self, response: LLMResponse, metrics: TurnMetrics, events: list[dict[str, Any]]
    ) -> list[ToolResult]:
        # Sequential on purpose: tools may read and change call state.
        results: list[ToolResult] = []
        ctx = ToolContext(
            organization_id=self.identity.organization_id,
            agent_id=self.identity.agent_id,
            agent_version_id=self.identity.agent_version_id,
            call_id=self.identity.call_id,
            config=self.config,
            state=self.state,
            services=self._services,
        )
        for call in response.tool_calls:
            t0 = time.perf_counter()
            outcome = await self._gateway.execute(ctx, call, self._counters)
            elapsed = (time.perf_counter() - t0) * 1000
            metrics.tool_calls += 1
            metrics.tool_latency_ms += elapsed
            events.append(
                {
                    "type": "tool",
                    "name": call.name,
                    "outcome": outcome.outcome.value,
                    "latency_ms": round(elapsed, 1),
                }
            )
            if outcome.outcome in (Outcome.TIMEOUT, Outcome.ERROR, Outcome.INVALID_OUTPUT):
                self.state.consecutive_failures += 1
            results.append(outcome.result)
        return results

    def _finish_turn(
        self, reply: str | None, metrics: TurnMetrics, events: list[dict[str, Any]]
    ) -> TurnResult:
        triggers = set(self.config.escalation.triggers)
        if reply is None:
            self.state.consecutive_failures += 1
            say = f"{SAY_TROUBLE} {SAY_OFFER_PERSON}"
            self.history.append(Message(role="assistant", text=say))
            if (
                EscalationTrigger.REPEATED_FAILURE in triggers
                and self.state.consecutive_failures >= 2
            ):
                self._request_escalation("repeated_failure")
        else:
            say = to_speech(reply) or SAY_DIDNT_CATCH
            if reply.strip():
                self.state.consecutive_failures = 0

        action = TurnAction.CONTINUE
        handoff = None
        if (
            self.state.escalation is EscalationStatus.REQUESTED
            and self.config.escalation.enabled
            and self.config.escalation.transfer_number
        ):
            self.state.escalation = EscalationStatus.IN_PROGRESS
            action = TurnAction.TRANSFER
            handoff = self.handoff()
            events.append({"type": "transfer_requested", "reason": handoff.reason})
        return TurnResult(say=say, action=action, handoff=handoff, metrics=metrics, events=events)

    def _request_escalation(self, reason: str) -> None:
        if self.state.escalation is EscalationStatus.NONE:
            self.state.escalation = EscalationStatus.REQUESTED
            self.state.escalation_reason = reason

    def handoff(self) -> Handoff:
        s = self.state
        known = ", ".join(f"{k}: {v}" for k, v in sorted(s.slots.items())) or "no details yet"
        return Handoff(
            reason=s.escalation_reason or "caller_requests_human",
            summary=f"Caller wants: {s.intent or 'unknown'}. Collected: {known}.",
            intent=s.intent,
            details=dict(s.slots),
            customer=dict(s.customer),
            actions=[f"{a.tool}: {a.status}" for a in s.actions],
        )
