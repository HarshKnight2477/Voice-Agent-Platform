"""The Tool Gateway: the only path from a model's tool request to running code.

Every call passes, in order:
  1. lookup              unknown tool -> refused
  2. schema validation   arguments parsed by the tool's Pydantic model (extra fields rejected)
  3. tenant check        platform tool, or a tool owned by this organization
  4. agent permission    the agent version grants the tool AND every scope it requires
                         (scopes never imply each other: read is not write)
  5. business rules      verified caller if required; explicit caller "yes" to a read-back
                         for confirmation-gated tools; the tool's own rule check
  6. rate limit          per tool, per call
  7. execution           timeout; retries only for read-only tools; idempotency key for others
  8. output validation   result parsed by the tool's output model, size-capped, wrapped as data
  9. audit               one record per request, whatever the outcome
The model receives a compact JSON result or a structured error with a hint. Internal error
text, stack traces and vendor payloads never reach it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from voice_platform.agents.config_schema import ToolGrant
from voice_platform.runtime.llm import ToolCall, ToolResult
from voice_platform.runtime.prompts import wrap_untrusted
from voice_platform.runtime.state import PendingConfirmation
from voice_platform.shared.logging import get_logger
from voice_platform.tools.base import SideEffect, ToolContext, ToolDefinition, ToolFailure
from voice_platform.tools.registry import ToolRegistry

log = get_logger(__name__)

MAX_RESULT_CHARS = 2000
SENSITIVE_ARG_KEYS = ("phone", "email", "name", "address", "dob", "card", "ssn", "policy")


class Outcome(StrEnum):
    OK = "ok"
    INVALID_ARGUMENTS = "invalid_arguments"
    UNKNOWN_TOOL = "unknown_tool"
    NOT_PERMITTED = "not_permitted"
    CONFIRMATION_REQUIRED = "confirmation_required"
    VERIFICATION_REQUIRED = "verification_required"
    RULE_DENIED = "rule_denied"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    FAILED = "failed"  # expected failure reported by the tool (e.g. no availability)
    ERROR = "error"  # unexpected failure inside the tool
    INVALID_OUTPUT = "invalid_output"


@dataclass(frozen=True)
class ToolExecutionRecord:
    """What the audit trail stores for each tool request."""

    organization_id: str
    agent_id: str
    agent_version_id: str
    call_id: str
    tool: str
    outcome: Outcome
    arguments: dict[str, Any]  # redacted
    latency_ms: float
    attempts: int
    idempotency_key: str
    at: datetime
    error_code: str | None = None


class AuditSink(Protocol):
    async def record(self, record: ToolExecutionRecord) -> None: ...


class LogAuditSink:
    """Structured-log sink. A database sink replaces it once calls are stored (Phase 3)."""

    async def record(self, record: ToolExecutionRecord) -> None:
        log.info(
            "tool.executed",
            tool=record.tool,
            outcome=record.outcome.value,
            call_id=record.call_id,
            latency_ms=round(record.latency_ms, 1),
            attempts=record.attempts,
            error_code=record.error_code,
        )


@dataclass
class GatewayResult:
    result: ToolResult
    outcome: Outcome
    output: BaseModel | None = None


@dataclass
class CallToolCounters:
    """Per-call usage, kept by the runtime for the life of one call."""

    per_tool: dict[str, int] = field(default_factory=dict)


def redact(arguments: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in arguments.items():
        if any(s in k.lower() for s in SENSITIVE_ARG_KEYS):
            out[k] = "***"
        elif isinstance(v, str):
            out[k] = v[:80]
        else:
            out[k] = v
    return out


def idempotency_key(call_id: str, tool: str, arguments: dict[str, Any]) -> str:
    canon = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{call_id}|{tool}|{canon}".encode()).hexdigest()[:32]


def _grant_for(grants: tuple[ToolGrant, ...], name: str) -> ToolGrant | None:
    return next((g for g in grants if g.name == name), None)


def _payload(ok: bool, **body: Any) -> str:
    return json.dumps({"ok": ok, **body}, ensure_ascii=False, default=str)


class ToolGateway:
    def __init__(
        self,
        registry: ToolRegistry,
        audit: AuditSink | None = None,
        always_granted: frozenset[str] = frozenset({"update_state"}),
    ) -> None:
        self._registry = registry
        self._audit = audit or LogAuditSink()
        self._always = always_granted

    async def execute(
        self, ctx: ToolContext, call: ToolCall, counters: CallToolCounters
    ) -> GatewayResult:
        started = time.perf_counter()
        attempts = 0
        tool = self._registry.get(call.name)
        key = idempotency_key(ctx.call_id, call.name, call.arguments)

        async def finish(
            outcome: Outcome,
            content: str,
            *,
            is_error: bool,
            code: str | None = None,
            output: BaseModel | None = None,
        ) -> GatewayResult:
            if tool is None or not tool.internal or outcome is not Outcome.OK:
                await self._audit.record(
                    ToolExecutionRecord(
                        organization_id=str(ctx.organization_id),
                        agent_id=str(ctx.agent_id),
                        agent_version_id=str(ctx.agent_version_id),
                        call_id=ctx.call_id,
                        tool=call.name,
                        outcome=outcome,
                        arguments=redact(call.arguments),
                        latency_ms=(time.perf_counter() - started) * 1000,
                        attempts=attempts,
                        idempotency_key=key,
                        at=datetime.now(UTC),
                        error_code=code,
                    )
                )
            if outcome is not Outcome.OK and (tool is None or not tool.internal):
                ctx.state.record_action(call.name, "denied" if outcome in _DENIALS else "error")
            return GatewayResult(ToolResult(call.id, content, is_error=is_error), outcome, output)

        async def refuse(outcome: Outcome, code: str, hint: str) -> GatewayResult:
            return await finish(
                outcome, _payload(False, error=code, hint=hint), is_error=True, code=code
            )

        # 1. lookup
        if tool is None:
            return await refuse(
                Outcome.UNKNOWN_TOOL,
                "unknown_tool",
                "That tool doesn't exist. Use only the tools you were given.",
            )

        # 2. schema validation
        try:
            args = tool.input_model.model_validate(call.arguments)
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in e["loc"]) or "(root)" for e in exc.errors()})
            return await refuse(
                Outcome.INVALID_ARGUMENTS,
                "invalid_arguments",
                f"Check these fields and try again: {', '.join(fields)}.",
            )

        # 3. tenant check  4. agent permission
        grant = _grant_for(tuple(ctx.config.tools), tool.name)
        always = tool.name in self._always
        if not self._registry.available_to(tool, ctx.organization_id) or (
            grant is None and not always
        ):
            log.warning("tool.not_permitted", tool=tool.name, reason="not_granted")
            return await refuse(
                Outcome.NOT_PERMITTED,
                "not_permitted",
                "You can't do that. Offer another way to help.",
            )
        missing = tool.required_scopes - set(grant.scopes if grant else ())
        if missing and not always:
            log.warning("tool.not_permitted", tool=tool.name, missing_scopes=sorted(missing))
            return await refuse(
                Outcome.NOT_PERMITTED,
                "not_permitted",
                "You can't do that. Offer another way to help.",
            )

        # 5. business rules
        if tool.requires_verified_caller and not ctx.state.verified:
            return await refuse(
                Outcome.VERIFICATION_REQUIRED,
                "verification_required",
                "Verify the caller's identity first.",
            )
        if tool.rule_check is not None:
            denial = tool.rule_check(ctx, args)
            if denial:
                return await refuse(Outcome.RULE_DENIED, "rule_denied", denial)
        if tool.requires_confirmation and not self._confirmed(ctx, tool, call):
            readback = tool.readback(args) if tool.readback else f"{tool.name} with these details"
            ctx.state.pending_confirmation = PendingConfirmation(
                tool=tool.name,
                arguments=call.arguments,
                readback=readback,
                requested_turn=ctx.state.turns,
            )
            return await refuse(
                Outcome.CONFIRMATION_REQUIRED,
                "confirmation_required",
                f"Read this back and ask the caller to confirm: {readback}. "
                "After they clearly say yes, call the tool again with the "
                "same details.",
            )

        # 6. rate limit
        used = counters.per_tool.get(tool.name, 0)
        if used >= tool.max_calls_per_call:
            return await refuse(
                Outcome.RATE_LIMITED,
                "rate_limited",
                "That has been tried too many times on this call. Offer a person instead.",
            )
        counters.per_tool[tool.name] = used + 1

        # 7. execution
        run_ctx = replace(
            ctx, idempotency_key=key if tool.side_effect is not SideEffect.NONE else ""
        )
        output: BaseModel | None = None
        max_attempts = tool.max_attempts if tool.side_effect is SideEffect.NONE else 1
        while attempts < max_attempts:
            attempts += 1
            try:
                output = await asyncio.wait_for(tool.handler(run_ctx, args), tool.timeout_ms / 1000)
                break
            except ToolFailure as exc:
                return await finish(
                    Outcome.FAILED,
                    _payload(False, error=exc.code, hint=exc.hint),
                    is_error=True,
                    code=exc.code,
                )
            except TimeoutError:
                if attempts >= max_attempts:
                    return await refuse(
                        Outcome.TIMEOUT,
                        "timeout",
                        "That system isn't responding. Apologise briefly and "
                        "offer a person or a callback.",
                    )
            except Exception as exc:  # a bug or an outage inside the tool
                log.exception("tool.error", tool=tool.name, error=type(exc).__name__)
                if attempts >= max_attempts:
                    return await refuse(
                        Outcome.ERROR,
                        "unavailable",
                        "That system is having trouble right now. Offer a person or a callback.",
                    )

        # 8. output validation
        try:
            validated = tool.output_model.model_validate(
                output.model_dump() if isinstance(output, BaseModel) else output
            )
        except ValidationError:
            log.error("tool.invalid_output", tool=tool.name)
            return await refuse(
                Outcome.INVALID_OUTPUT,
                "unavailable",
                "That system returned something unexpected. Offer a person.",
            )
        body = validated.model_dump_json()
        if len(body) > MAX_RESULT_CHARS:
            body = body[:MAX_RESULT_CHARS] + "...(truncated)"
        if tool.requires_confirmation:
            ctx.state.pending_confirmation = None
        if not tool.internal:
            ctx.state.record_action(tool.name, "ok")
        # Results are data for the model, never instructions.
        content = _payload(True, result=wrap_untrusted(f"tool:{tool.name}", body))
        return await finish(Outcome.OK, content, is_error=False, output=validated)

    @staticmethod
    def _confirmed(ctx: ToolContext, tool: ToolDefinition, call: ToolCall) -> bool:
        """Confirmed only if: the same tool with the same arguments was read back on an
        earlier turn, the caller has spoken since, and what they said was a clear yes."""
        pending = ctx.state.pending_confirmation
        return (
            pending is not None
            and pending.tool == tool.name
            and pending.arguments == call.arguments
            and ctx.state.turns > pending.requested_turn
            and ctx.state.last_user_affirmed
        )


_DENIALS = {
    Outcome.NOT_PERMITTED,
    Outcome.CONFIRMATION_REQUIRED,
    Outcome.VERIFICATION_REQUIRED,
    Outcome.RULE_DENIED,
    Outcome.RATE_LIMITED,
    Outcome.UNKNOWN_TOOL,
}
