"""Writes what happens on a call: transcript, timeline, tool audit, caller messages, outcome.

Used by the agent worker (data plane). Each write is a short transaction in the call's
organization context, so row-level security applies to the worker exactly as to the API.
Writes are small and never block the conversation for long; failures are logged, not spoken.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from voice_platform.calls.models import Call, CallerMessage, CallEvent, CallMessage, ToolExecution
from voice_platform.infrastructure.database import tenant_transaction
from voice_platform.runtime.session import TurnResult
from voice_platform.shared.logging import get_logger
from voice_platform.tools.base import ToolContext
from voice_platform.tools.gateway import ToolExecutionRecord

log = get_logger(__name__)

MAX_EVENT_DATA_KEYS = 20


class CallRecorder:
    """Also serves as the Tool Gateway's AuditSink and take_message's MessageSink."""

    def __init__(
        self, sm: async_sessionmaker[AsyncSession], organization_id: uuid.UUID, call_id: uuid.UUID
    ) -> None:
        self._sm = sm
        self._org = organization_id
        self._call = call_id
        self._msg_seq = 0
        self._event_seq = 0

    def _tx(self) -> Any:
        return tenant_transaction(self._sm, org_id=self._org, user_id=None)

    async def mark_started(self) -> None:
        async with self._tx() as s:
            await s.execute(
                update(Call)
                .where(Call.id == self._call)
                .values(status="in_progress", started_at=datetime.now(UTC))
            )

    async def record_turn(
        self, caller_text: str | None, result: TurnResult, *, turns: int, intent: str | None
    ) -> None:
        try:
            async with self._tx() as s:
                if caller_text:
                    self._msg_seq += 1
                    s.add(
                        CallMessage(
                            organization_id=self._org,
                            call_id=self._call,
                            seq=self._msg_seq,
                            role="caller",
                            text=caller_text,
                        )
                    )
                if result.say:
                    self._msg_seq += 1
                    s.add(
                        CallMessage(
                            organization_id=self._org,
                            call_id=self._call,
                            seq=self._msg_seq,
                            role="agent",
                            text=result.say,
                            latency_ms=round(result.metrics.total_ms, 1),
                        )
                    )
                for ev in result.events:
                    self._event_seq += 1
                    data = {k: v for k, v in list(ev.items())[:MAX_EVENT_DATA_KEYS] if k != "type"}
                    s.add(
                        CallEvent(
                            organization_id=self._org,
                            call_id=self._call,
                            seq=self._event_seq,
                            type=str(ev.get("type", "event"))[:40],
                            data=data,
                        )
                    )
                usage = result.metrics.usage
                await s.execute(
                    update(Call)
                    .where(Call.id == self._call)
                    .values(
                        turns=turns,
                        intent=intent,
                        input_tokens=Call.input_tokens + usage.input_tokens,
                        output_tokens=Call.output_tokens + usage.output_tokens,
                    )
                )
        except Exception as exc:  # recording must never break the live call
            log.error("recorder.turn_failed", call_id=str(self._call), error=type(exc).__name__)

    async def end(self, *, status: str, end_reason: str, outcome: str | None = None) -> None:
        async with self._tx() as s:
            await s.execute(
                update(Call)
                .where(Call.id == self._call)
                .values(
                    status=status,
                    end_reason=end_reason[:64],
                    outcome=outcome,
                    ended_at=datetime.now(UTC),
                )
            )

    # AuditSink
    async def record(self, record: ToolExecutionRecord) -> None:
        try:
            async with self._tx() as s:
                s.add(
                    ToolExecution(
                        organization_id=self._org,
                        call_id=self._call,
                        agent_version_id=uuid.UUID(record.agent_version_id),
                        tool=record.tool,
                        outcome=record.outcome.value,
                        arguments=record.arguments,
                        error_code=record.error_code,
                        latency_ms=record.latency_ms,
                        attempts=record.attempts,
                        idempotency_key=record.idempotency_key,
                        at=record.at,
                    )
                )
        except Exception as exc:
            log.error("recorder.audit_failed", call_id=str(self._call), error=type(exc).__name__)

    # MessageSink
    async def save(self, ctx: ToolContext, message: dict[str, str]) -> str:
        async with self._tx() as s:
            row = CallerMessage(
                organization_id=self._org,
                call_id=self._call,
                caller_name=message["caller_name"],
                callback_number=message["callback_number"],
                message=message["message"],
                idempotency_key=message["idempotency_key"],
            )
            s.add(row)
            await s.flush()
            return str(row.id)[:8]
