"""Platform tools every tenant can grant. Industry tools (reservations, appointments, CRM)
are added with their integrations; they use the same definition and gateway."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from voice_platform.runtime.prompts import is_open, render_hours
from voice_platform.runtime.state import (
    UPDATE_STATE_TOOL_SCHEMA,
    EscalationStatus,
    StateUpdate,
    apply_update,
)
from voice_platform.tools.base import SideEffect, ToolContext, ToolDefinition, ToolFailure

_PHONE = re.compile(r"^\+?[0-9][0-9 ()-]{5,19}$")


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------- update_state


class StateUpdateResult(BaseModel):
    still_needed: list[str]


async def _update_state(ctx: ToolContext, args: StateUpdate) -> StateUpdateResult:
    try:
        apply_update(ctx.state, args)
    except ValueError as exc:
        raise ToolFailure("too_many_details", "Record only what matters for the request.") from exc
    return StateUpdateResult(still_needed=ctx.state.missing_slots)


UPDATE_STATE = ToolDefinition(
    name="update_state",
    description="Record what the caller has told you: their goal (intent), details such as "
    "date, time, party size, and their name. Call it as soon as you learn something. "
    "Don't use it for anything you only guessed.",
    input_model=StateUpdate,
    schema_override=UPDATE_STATE_TOOL_SCHEMA,
    output_model=StateUpdateResult,
    handler=_update_state,
    timeout_ms=500,
    max_calls_per_call=60,
    internal=True,
)


# ---------------------------------------------------------------- get_hours


class NoArgs(_Args):
    pass


class HoursResult(BaseModel):
    open_now: bool | None
    hours: str
    timezone: str


async def _get_hours(ctx: ToolContext, _: NoArgs) -> HoursResult:
    return HoursResult(
        open_now=is_open(ctx.config, datetime.now(UTC)),
        hours=render_hours(ctx.config),
        timezone=ctx.config.business.timezone,
    )


GET_HOURS = ToolDefinition(
    name="get_hours",
    description="Get the business's opening hours and whether it is open right now. Use it "
    "for any question about hours or whether the business is open.",
    input_model=NoArgs,
    output_model=HoursResult,
    handler=_get_hours,
    timeout_ms=500,
    max_attempts=2,
)


# ---------------------------------------------------------------- transfer_to_human


class TransferArgs(_Args):
    reason: str = Field(min_length=3, max_length=200)
    summary: str = Field(default="", max_length=600, description="What the caller needs so far.")


class TransferResult(BaseModel):
    status: str  # "transferring" | "message_needed" | "unavailable"
    say: str


async def _transfer(ctx: ToolContext, args: TransferArgs) -> TransferResult:
    esc = ctx.config.escalation
    if not esc.enabled:
        return TransferResult(
            status="unavailable", say="Offer to take a message for a callback instead."
        )
    ctx.state.escalation = EscalationStatus.REQUESTED
    ctx.state.escalation_reason = args.reason
    if not esc.transfer_number:
        return TransferResult(
            status="message_needed", say="No one can take the call live; offer to take a message."
        )
    # The runtime performs the actual transfer through the telephony provider (Phase 3).
    return TransferResult(status="transferring", say="Tell the caller you're connecting them now.")


TRANSFER_TO_HUMAN = ToolDefinition(
    name="transfer_to_human",
    description="Hand the call to a person. Use it when the caller asks for a person, when you "
    "can't help after trying, or when the business rules say to. Include a short reason and "
    "summary so the caller doesn't have to repeat themselves.",
    input_model=TransferArgs,
    output_model=TransferResult,
    handler=_transfer,
    side_effect=SideEffect.REVERSIBLE,
    timeout_ms=1000,
    max_calls_per_call=2,
)


# ---------------------------------------------------------------- take_message


class MessageSink(Protocol):
    async def save(self, ctx: ToolContext, message: dict[str, str]) -> str: ...


class TakeMessageArgs(_Args):
    caller_name: str = Field(min_length=1, max_length=100)
    callback_number: str = Field(max_length=25)
    message: str = Field(min_length=3, max_length=1000)

    @field_validator("callback_number")
    @classmethod
    def _phone(cls, v: str) -> str:
        if not _PHONE.match(v):
            raise ValueError("not a phone number")
        return v


class TakeMessageResult(BaseModel):
    saved: bool
    reference: str


async def _take_message(ctx: ToolContext, args: TakeMessageArgs) -> TakeMessageResult:
    sink: MessageSink | None = ctx.services.get("message_sink")
    if sink is None:
        raise ToolFailure(
            "unavailable", "Messages can't be saved right now; offer a callback time instead."
        )
    ref = await sink.save(
        ctx,
        {
            "caller_name": args.caller_name,
            "callback_number": args.callback_number,
            "message": args.message,
            "idempotency_key": ctx.idempotency_key,
        },
    )
    return TakeMessageResult(saved=True, reference=ref)


TAKE_MESSAGE = ToolDefinition(
    name="take_message",
    description="Save a message for the business to call the caller back. Collect their name, "
    "callback number and message first.",
    input_model=TakeMessageArgs,
    output_model=TakeMessageResult,
    handler=_take_message,
    required_scopes=frozenset({"messages:create"}),
    side_effect=SideEffect.REVERSIBLE,
    requires_confirmation=True,
    readback=lambda a: f"a message from {a.caller_name}, call back on {a.callback_number}",
    timeout_ms=3000,
    max_calls_per_call=3,
)

BUILTIN_TOOLS = (UPDATE_STATE, GET_HOURS, TRANSFER_TO_HUMAN, TAKE_MESSAGE)
