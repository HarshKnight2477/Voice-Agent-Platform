"""Browser test calls, call storage and the inspector API, against PostgreSQL and the local
LiveKit server from compose.yaml."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from api_harness import Harness, csrf, signup, unique_email
from livekit import api as lkapi
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from voice_platform.calls.recorder import CallRecorder
from voice_platform.calls.service import CallSetupError, load_call
from voice_platform.providers.telephony.livekit_provider import LiveKitProvider
from voice_platform.runtime.llm import Usage
from voice_platform.runtime.session import TurnMetrics, TurnResult
from voice_platform.shared.config import get_settings
from voice_platform.tools.base import ToolContext
from voice_platform.tools.gateway import Outcome, ToolExecutionRecord

pytestmark = pytest.mark.integration


async def agent_with_version(h: Harness, version: bool = True) -> tuple[dict[str, str], str]:
    headers = csrf(await signup(h, org="Test Diner"))
    agent = (
        await h.client.post(
            "/api/v1/agents",
            json={"name": "Desk", "template_key": "restaurant_receptionist"},
            headers=headers,
        )
    ).json()
    if version:
        await h.client.post(f"/api/v1/agents/{agent['id']}/versions", json={}, headers=headers)
    return headers, agent["id"]


async def test_test_call_needs_a_version(harness: Harness) -> None:
    headers, agent_id = await agent_with_version(harness, version=False)
    r = await harness.client.post(f"/api/v1/agents/{agent_id}/test-calls", json={}, headers=headers)
    assert r.status_code == 409 and r.json()["code"] == "no_version"


async def test_test_call_token_dispatches_the_agent_with_ids_only(harness: Harness) -> None:
    h = harness
    headers, agent_id = await agent_with_version(h)
    r = await h.client.post(f"/api/v1/agents/{agent_id}/test-calls", json={}, headers=headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["room"] == f"call-{uuid.UUID(body['call_id']).hex}"

    claims = lkapi.TokenVerifier("devkey", "secret").verify(body["token"])
    assert claims.video.room == body["room"] and claims.video.room_join
    dispatch = claims.room_config.agents[0]
    assert dispatch.agent_name == "voice-agent"
    meta = json.loads(dispatch.metadata)
    assert set(meta) == {"call_id", "organization_id", "agent_version_id"}

    calls = (await h.client.get("/api/v1/calls")).json()["data"]
    assert calls[0]["id"] == body["call_id"] and calls[0]["status"] == "created"


async def _start_call(h: Harness) -> tuple[dict[str, str], dict[str, Any]]:
    headers, agent_id = await agent_with_version(h)
    body = (
        await h.client.post(f"/api/v1/agents/{agent_id}/test-calls", json={}, headers=headers)
    ).json()
    claims = lkapi.TokenVerifier("devkey", "secret").verify(body["token"])
    return headers, json.loads(claims.room_config.agents[0].metadata)


async def test_worker_side_recording_shows_up_in_the_inspector(
    harness: Harness, app_sessions: async_sessionmaker[AsyncSession]
) -> None:
    h = harness
    _, meta = await _start_call(h)
    loaded = await load_call(app_sessions, meta)
    assert loaded.config.business.name == "Test Diner"

    rec = CallRecorder(app_sessions, loaded.organization_id, loaded.call_id)
    await rec.mark_started()
    turn = TurnResult(
        say="We open at eleven.",
        metrics=TurnMetrics(total_ms=850, usage=Usage(120, 15)),
        events=[{"type": "llm_response", "latency_ms": 600}],
    )
    await rec.record_turn("When do you open?", turn, turns=1, intent="hours")
    await rec.record(
        ToolExecutionRecord(
            organization_id=str(loaded.organization_id),
            agent_id=str(loaded.agent_id),
            agent_version_id=str(loaded.agent_version_id),
            call_id=str(loaded.call_id),
            tool="get_hours",
            outcome=Outcome.OK,
            arguments={},
            latency_ms=3.2,
            attempts=1,
            idempotency_key="k",
            at=datetime.now(UTC),
        )
    )
    ctx: Any = ToolContext.__new__(ToolContext)
    ref = await rec.save(
        ctx,
        {
            "caller_name": "Asha",
            "callback_number": "+919800000000",
            "message": "Call back",
            "idempotency_key": "idem-1",
        },
    )
    assert ref
    await rec.end(status="completed", end_reason="caller_hung_up", outcome="answered")

    detail = (await h.client.get(f"/api/v1/calls/{loaded.call_id}")).json()
    assert detail["status"] == "completed" and detail["turns"] == 1 and detail["intent"] == "hours"
    assert [(t["role"], t["text"]) for t in detail["transcript"]] == [
        ("caller", "When do you open?"),
        ("agent", "We open at eleven."),
    ]
    assert detail["timeline"][0]["type"] == "llm_response"
    assert detail["tools"][0]["tool"] == "get_hours"
    assert (detail["input_tokens"], detail["output_tokens"]) == (120, 15)
    assert detail["duration_seconds"] is not None

    with pytest.raises(CallSetupError):  # a call can only be loaded once, while 'created'
        await load_call(app_sessions, meta)


async def test_forged_metadata_finds_nothing(
    harness: Harness, app_sessions: async_sessionmaker[AsyncSession]
) -> None:
    _, meta = await _start_call(harness)
    harness.client.cookies.clear()
    other_org = (await signup(harness, org="Other")).json()["organization_id"]
    with pytest.raises(CallSetupError):
        await load_call(app_sessions, {**meta, "organization_id": other_org})
    with pytest.raises(CallSetupError):
        await load_call(app_sessions, {**meta, "agent_version_id": str(uuid.uuid4())})


async def test_transcript_needs_its_own_permission(harness: Harness) -> None:
    h = harness
    owner_email = unique_email()
    headers = csrf(await signup(h, owner_email, org="Perm Diner"))
    agent = (await h.client.post("/api/v1/agents", json={"name": "Desk"}, headers=headers)).json()
    await h.client.post(f"/api/v1/agents/{agent['id']}/versions", json={}, headers=headers)
    call = (
        await h.client.post(f"/api/v1/agents/{agent['id']}/test-calls", json={}, headers=headers)
    ).json()
    viewer_email = unique_email()
    inv = await h.client.post(
        "/api/v1/organizations/current/invitations",
        json={"email": viewer_email, "role": "VIEWER"},
        headers=headers,
    )
    assert inv.status_code == 201
    token = h.mailer.last_token("invitation")
    h.client.cookies.clear()
    v = await signup(h, viewer_email, org="Viewer Home")
    acc = await h.client.post("/api/v1/invitations/accept", json={"token": token}, headers=csrf(v))
    org_id = acc.json()["organization_id"]
    await h.client.post(
        "/api/v1/auth/switch-organization", json={"organization_id": org_id}, headers=csrf(v)
    )
    detail = (await h.client.get(f"/api/v1/calls/{call['call_id']}")).json()
    assert detail["id"] == call["call_id"] and detail["transcript"] is None
    denied = await h.client.post(
        f"/api/v1/agents/{agent['id']}/test-calls", json={}, headers=csrf(v)
    )
    assert denied.status_code == 403  # viewers can't start (billable) test calls


async def test_local_livekit_server_is_reachable() -> None:
    await LiveKitProvider(get_settings()).check_reachable()
