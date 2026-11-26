"""/api/v1/agents/{id}/test-calls and /api/v1/calls (history and inspector data)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from voice_platform.api.dependencies import AppSettings, AuthContext, Meta, SessionMaker, require
from voice_platform.api.errors import AppError
from voice_platform.api.pagination import (
    DEFAULT_LIMIT,
    Cursor,
    Limit,
    Page,
    apply_keyset,
    encode_cursor,
)
from voice_platform.audit.models import AuditLog
from voice_platform.calls.models import Call, CallEvent, CallMessage, ToolExecution
from voice_platform.calls.service import CallSetupError, create_test_call, dispatch_metadata
from voice_platform.infrastructure.database import tenant_transaction
from voice_platform.telephony.ports import TelephonyProvider
from voice_platform.tenancy.permissions import Permission

router = APIRouter(prefix="/api/v1", tags=["calls"])


def get_telephony(request: Request) -> TelephonyProvider:
    provider: TelephonyProvider = request.app.state.telephony
    return provider


Telephony = Annotated[TelephonyProvider, Depends(get_telephony)]


class TestCallCreate(BaseModel):
    version_id: uuid.UUID | None = Field(
        default=None, description="Version to test. Defaults to the newest version."
    )


class TestCallOut(BaseModel):
    call_id: uuid.UUID
    agent_version_id: uuid.UUID
    server_url: str
    token: str
    room: str
    expires_at: datetime


class CallSummary(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    agent_version_id: uuid.UUID
    direction: str
    channel: str
    status: str
    intent: str | None
    outcome: str | None
    end_reason: str | None
    turns: int
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    duration_seconds: float | None


class TranscriptLine(BaseModel):
    seq: int
    role: str
    text: str
    latency_ms: float | None
    at: datetime


class TimelineEvent(BaseModel):
    seq: int
    type: str
    data: dict[str, Any]
    at: datetime


class ToolAuditOut(BaseModel):
    tool: str
    outcome: str
    error_code: str | None
    latency_ms: float
    attempts: int
    at: datetime


class CallDetail(CallSummary):
    input_tokens: int
    output_tokens: int
    transcript: list[TranscriptLine] | None  # None = you lack calls.transcript.read
    timeline: list[TimelineEvent]
    tools: list[ToolAuditOut]


def _summary(c: Call) -> CallSummary:
    duration = (c.ended_at - c.started_at).total_seconds() if c.ended_at and c.started_at else None
    return CallSummary(
        id=c.id,
        agent_id=c.agent_id,
        agent_version_id=c.agent_version_id,
        direction=c.direction,
        channel=c.channel,
        status=c.status,
        intent=c.intent,
        outcome=c.outcome,
        end_reason=c.end_reason,
        turns=c.turns,
        created_at=c.created_at,
        started_at=c.started_at,
        ended_at=c.ended_at,
        duration_seconds=duration,
    )


@router.post("/agents/{agent_id}/test-calls", status_code=status.HTTP_201_CREATED)
async def start_test_call(
    agent_id: uuid.UUID,
    body: TestCallCreate,
    sm: SessionMaker,
    meta: Meta,
    settings: AppSettings,
    telephony: Telephony,
    ctx: AuthContext = require(Permission.AGENTS_WRITE),
) -> TestCallOut:
    async with tenant_transaction(sm, org_id=ctx.organization_id, user_id=ctx.user_id) as s:
        try:
            call, version = await create_test_call(
                s,
                organization_id=ctx.organization_id,
                agent_id=agent_id,
                version_id=body.version_id,
                user_id=ctx.user_id,
            )
        except CallSetupError as exc:
            raise AppError(exc.status, exc.code, exc.message) from None
        s.add(
            AuditLog(
                organization_id=ctx.organization_id,
                actor_user_id=ctx.user_id,
                action="call.test_started",
                target_type="call",
                target_id=str(call.id),
                ip_address=meta.ip,
                request_id=meta.request_id,
                details={"agent_version": version.number},
            )
        )
    join = telephony.issue_web_join(
        room=call.room_name,
        participant_identity=f"tester-{ctx.user_id.hex[:12]}",
        dispatch_metadata=dispatch_metadata(call),
        ttl=timedelta(minutes=settings.test_call_token_ttl_minutes),
    )
    return TestCallOut(
        call_id=call.id,
        agent_version_id=version.id,
        server_url=join.server_url,
        token=join.token,
        room=join.room,
        expires_at=join.expires_at,
    )


@router.get("/calls")
async def list_calls(
    sm: SessionMaker,
    ctx: AuthContext = require(Permission.CALLS_READ),
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
    agent_id: Annotated[uuid.UUID | None, Query()] = None,
) -> Page[CallSummary]:
    async with tenant_transaction(sm, org_id=ctx.organization_id, user_id=ctx.user_id) as s:
        stmt = select(Call)
        if agent_id:
            stmt = stmt.where(Call.agent_id == agent_id)
        rows = list(await s.scalars(apply_keyset(stmt, Call.created_at, Call.id, cursor, limit)))
    page = rows[:limit]
    return Page(
        data=[_summary(c) for c in page],
        next_cursor=encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None,
    )


@router.get("/calls/{call_id}")
async def get_call(
    call_id: uuid.UUID, sm: SessionMaker, ctx: AuthContext = require(Permission.CALLS_READ)
) -> CallDetail:
    can_read_transcript = Permission.CALLS_TRANSCRIPT_READ in ctx.permissions
    async with tenant_transaction(sm, org_id=ctx.organization_id, user_id=ctx.user_id) as s:
        call = await s.get(Call, call_id)
        if call is None:
            raise AppError(404, "not_found", "Call not found.")
        messages = (
            list(
                await s.scalars(
                    select(CallMessage)
                    .where(CallMessage.call_id == call_id)
                    .order_by(CallMessage.seq)
                    .limit(2000)
                )
            )
            if can_read_transcript
            else None
        )
        events = list(
            await s.scalars(
                select(CallEvent)
                .where(CallEvent.call_id == call_id)
                .order_by(CallEvent.seq)
                .limit(5000)
            )
        )
        tools = list(
            await s.scalars(
                select(ToolExecution)
                .where(ToolExecution.call_id == call_id)
                .order_by(ToolExecution.at)
                .limit(1000)
            )
        )
    base = _summary(call).model_dump()
    return CallDetail(
        **base,
        input_tokens=call.input_tokens,
        output_tokens=call.output_tokens,
        transcript=[
            TranscriptLine(
                seq=m.seq, role=m.role, text=m.text, latency_ms=m.latency_ms, at=m.created_at
            )
            for m in messages
        ]
        if messages is not None
        else None,
        timeline=[TimelineEvent(seq=e.seq, type=e.type, data=e.data, at=e.at) for e in events],
        tools=[
            ToolAuditOut(
                tool=t.tool,
                outcome=t.outcome,
                error_code=t.error_code,
                latency_ms=t.latency_ms,
                attempts=t.attempts,
                at=t.at,
            )
            for t in tools
        ],
    )
