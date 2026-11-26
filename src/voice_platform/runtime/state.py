"""Structured conversation state for one call.

The model reports what it learned through the `update_state` tool (schema-validated); code
decides what is still missing. The prompt shows known and missing details on every turn, so
the agent never asks for something the caller already said.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_SLOTS = 40
MAX_SLOT_VALUE_CHARS = 300
_SLOT_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class EscalationStatus(StrEnum):
    NONE = "none"
    REQUESTED = "requested"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class ActionRecord(BaseModel):
    tool: str
    status: str  # "ok" | "error" | "denied"
    at: datetime
    summary: str = ""


class PendingConfirmation(BaseModel):
    """A state-changing action read back to the caller, waiting for an explicit yes."""

    tool: str
    arguments: dict[str, Any]
    readback: str
    requested_turn: int = 0


class ConversationState(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    intent: str | None = None
    detected_intents: list[str] = Field(default_factory=list)
    customer: dict[str, str] = Field(default_factory=dict)
    slots: dict[str, str] = Field(default_factory=dict)
    required_slots: list[str] = Field(default_factory=list)
    pending_confirmation: PendingConfirmation | None = None
    verified: bool = False
    escalation: EscalationStatus = EscalationStatus.NONE
    escalation_reason: str | None = None
    actions: list[ActionRecord] = Field(default_factory=list)
    turns: int = 0
    consecutive_failures: int = 0
    # Set by the runtime each caller turn from a deterministic check of what they said
    # ("yes", "that's right", ...). The model's own judgement never confirms an action.
    last_user_affirmed: bool = False
    opening: str = ""  # what the agent said when it answered

    @property
    def missing_slots(self) -> list[str]:
        return [s for s in self.required_slots if not self.slots.get(s)]

    def record_action(self, tool: str, status: str, summary: str = "") -> None:
        self.actions = [
            *self.actions[-19:],
            ActionRecord(tool=tool, status=status, at=datetime.now(UTC), summary=summary[:200]),
        ]

    def summary_for_prompt(self) -> str:
        """Short, stable rendering for the dynamic part of the system prompt."""
        lines: list[str] = []
        if self.opening and self.turns <= 1:
            lines.append(f'You answered the call by saying: "{self.opening}"')
        if self.intent:
            lines.append(f"Caller's current goal: {self.intent}")
        known = {**{f"customer.{k}": v for k, v in self.customer.items()}, **self.slots}
        if known:
            lines.append("Already known (do not ask again): "
                         + "; ".join(f"{k}={v}" for k, v in sorted(known.items())))  # fmt: skip
        if self.missing_slots:
            lines.append("Still needed, ask one at a time: " + ", ".join(self.missing_slots))
        if self.pending_confirmation:
            lines.append("Waiting for the caller to confirm: " + self.pending_confirmation.readback)
        if self.escalation is not EscalationStatus.NONE:
            lines.append(f"Escalation: {self.escalation.value}")
        if self.actions:
            recent = self.actions[-3:]
            lines.append("Recent actions: " + "; ".join(f"{a.tool} ({a.status})" for a in recent))
        return "\n".join(lines) if lines else "Nothing collected yet."


class StateUpdate(BaseModel):
    """Arguments of the `update_state` tool. Bounded so a confused or manipulated model
    cannot bloat the state or smuggle instructions into it."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    intent: str | None = Field(default=None, max_length=60)
    slots: dict[str, str] = Field(default_factory=dict, max_length=15)
    customer_name: str | None = Field(default=None, max_length=100)
    caller_wants_human: bool = False

    @field_validator("slots")
    @classmethod
    def _keys(cls, v: dict[str, str]) -> dict[str, str]:
        for k, val in v.items():
            if not _SLOT_KEY.match(k):
                raise ValueError(f"invalid slot name: {k!r}")
            if len(val) > MAX_SLOT_VALUE_CHARS:
                raise ValueError(f"slot {k!r} is too long")
        return v


UPDATE_STATE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "description": "Short label for what the caller wants, "
                   "e.g. 'reservation', 'hours', 'cancel_booking'."},
        "slots": {"type": "object", "additionalProperties": {"type": "string"},
                  "description": "Details the caller gave, e.g. {\"party_size\": \"4\", "
                  "\"date\": \"2026-12-05\"}. Use ISO dates and 24h times."},
        "customer_name": {"type": "string"},
        "caller_wants_human": {"type": "boolean"},
    },
    "additionalProperties": False,
}  # fmt: skip


def apply_update(state: ConversationState, update: StateUpdate) -> list[str]:
    """Merge a validated update into state. Returns the slot names that changed."""
    changed: list[str] = []
    if update.intent:
        state.intent = update.intent
        if update.intent not in state.detected_intents:
            state.detected_intents = [*state.detected_intents[-9:], update.intent]
    slots = dict(state.slots)
    for k, v in update.slots.items():
        if v and slots.get(k) != v:
            changed.append(k)
            slots[k] = v
    if len(slots) > MAX_SLOTS:
        raise ValueError("too many details collected for one call")
    state.slots = slots
    if update.customer_name:
        state.customer = {**state.customer, "name": update.customer_name}
    if update.caller_wants_human and state.escalation is EscalationStatus.NONE:
        state.escalation = EscalationStatus.REQUESTED
        state.escalation_reason = "caller_requests_human"
    return changed
