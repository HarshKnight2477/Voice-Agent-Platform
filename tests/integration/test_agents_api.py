"""Agent lifecycle end to end: create -> edit draft -> version -> publish -> roll back."""

from __future__ import annotations

from typing import Any

import pytest
from api_harness import Harness, csrf, signup
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


async def new_agent(
    h: Harness, template: str = "restaurant_receptionist"
) -> tuple[dict[str, str], str]:
    headers = csrf(await signup(h, org="Joe's Diner"))
    resp = await h.client.post(
        "/api/v1/agents", json={"name": "Front desk", "template_key": template}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return headers, resp.json()["id"]


async def edit_greeting(h: Harness, headers: dict[str, str], agent_id: str, greeting: str) -> Any:
    draft = (await h.client.get(f"/api/v1/agents/{agent_id}/configuration")).json()
    draft["config"]["greeting"] = greeting
    return await h.client.put(
        f"/api/v1/agents/{agent_id}/configuration",
        json={"revision": draft["revision"], "config": draft["config"]},
        headers=headers,
    )


async def test_create_from_template_uses_business_details(harness: Harness) -> None:
    h = harness
    _, agent_id = await new_agent(h)
    templates = (await h.client.get("/api/v1/agent-templates")).json()
    assert {"generic_receptionist", "restaurant_receptionist"} <= {t["key"] for t in templates}

    draft = (await h.client.get(f"/api/v1/agents/{agent_id}/configuration")).json()
    assert draft["revision"] == 1
    assert draft["config"]["business"]["name"] == "Joe's Diner"
    assert "create_reservation" in [t["name"] for t in draft["config"]["tools"]]


async def test_unknown_template_and_invalid_config_rejected(harness: Harness) -> None:
    h = harness
    headers, agent_id = await new_agent(h)
    bad_template = await h.client.post(
        "/api/v1/agents", json={"name": "x", "template_key": "nope"}, headers=headers
    )
    assert bad_template.status_code == 422

    draft = (await h.client.get(f"/api/v1/agents/{agent_id}/configuration")).json()
    draft["config"]["escalation"]["transfer_number"] = "call me maybe"
    resp = await h.client.put(
        f"/api/v1/agents/{agent_id}/configuration",
        json={"revision": 1, "config": draft["config"]},
        headers=headers,
    )
    assert resp.status_code == 422
    assert any("transfer_number" in e["field"] for e in resp.json()["errors"])


async def test_concurrent_edits_conflict(harness: Harness) -> None:
    h = harness
    headers, agent_id = await new_agent(h)
    first = await edit_greeting(h, headers, agent_id, "Hello from editor one")
    assert first.status_code == 200 and first.json()["revision"] == 2

    stale = (await h.client.get(f"/api/v1/agents/{agent_id}/configuration")).json()
    stale["revision"] = 1  # editor two still has revision 1
    second = await h.client.put(
        f"/api/v1/agents/{agent_id}/configuration", json=stale, headers=headers
    )
    assert second.status_code == 409 and second.json()["code"] == "revision_conflict"


async def test_version_publish_and_rollback(harness: Harness) -> None:
    h = harness
    headers, agent_id = await new_agent(h)

    await edit_greeting(h, headers, agent_id, "Version one greeting")
    v1 = (
        await h.client.post(
            f"/api/v1/agents/{agent_id}/versions", json={"change_note": "first"}, headers=headers
        )
    ).json()
    await edit_greeting(h, headers, agent_id, "Version two greeting")
    v2 = (
        await h.client.post(f"/api/v1/agents/{agent_id}/versions", json={}, headers=headers)
    ).json()
    assert (v1["number"], v2["number"]) == (1, 2)
    assert v1["status"] == v2["status"] == "testing"

    p1 = await h.client.post(
        f"/api/v1/agents/{agent_id}/versions/{v1['id']}/publish", headers=headers
    )
    assert p1.status_code == 200 and p1.json()["previous_version_id"] is None
    p2 = await h.client.post(
        f"/api/v1/agents/{agent_id}/versions/{v2['id']}/publish", headers=headers
    )
    assert p2.json()["previous_version_id"] == v1["id"]

    again = await h.client.post(
        f"/api/v1/agents/{agent_id}/versions/{v2['id']}/publish", headers=headers
    )
    assert again.status_code == 409

    back = await h.client.post(
        f"/api/v1/agents/{agent_id}/rollback", json={"version_id": v1["id"]}, headers=headers
    )
    assert back.status_code == 200
    agent = (await h.client.get(f"/api/v1/agents/{agent_id}")).json()
    assert agent["published_version_id"] == v1["id"]

    versions = {
        v["number"]: v["status"]
        for v in (await h.client.get(f"/api/v1/agents/{agent_id}/versions")).json()["data"]
    }
    assert versions == {1: "published", 2: "retired"}

    detail = (await h.client.get(f"/api/v1/agents/{agent_id}/versions/{v1['id']}")).json()
    assert detail["snapshot"]["config"]["greeting"] == "Version one greeting"
    assert detail["snapshot"]["version_number"] == 1


async def test_versions_are_immutable_in_the_database(
    harness: Harness, owner_engine: AsyncEngine
) -> None:
    h = harness
    headers, agent_id = await new_agent(h)
    v = (
        await h.client.post(f"/api/v1/agents/{agent_id}/versions", json={}, headers=headers)
    ).json()
    async with owner_engine.connect() as conn:
        with pytest.raises(DBAPIError, match="immutable"):
            await conn.execute(
                text("UPDATE agent_versions SET snapshot = '{}'::jsonb WHERE id = :id"),
                {"id": v["id"]},
            )
        await conn.rollback()


async def test_editing_draft_does_not_change_published_version(harness: Harness) -> None:
    h = harness
    headers, agent_id = await new_agent(h)
    v1 = (
        await h.client.post(f"/api/v1/agents/{agent_id}/versions", json={}, headers=headers)
    ).json()
    await h.client.post(f"/api/v1/agents/{agent_id}/versions/{v1['id']}/publish", headers=headers)
    await edit_greeting(h, headers, agent_id, "Changed after publishing")
    live = (await h.client.get(f"/api/v1/agents/{agent_id}/versions/{v1['id']}")).json()
    assert live["snapshot"]["config"]["greeting"] != "Changed after publishing"


async def test_agents_are_private_to_their_organization(harness: Harness) -> None:
    h = harness
    _, agent_id = await new_agent(h)
    h.client.cookies.clear()
    other = csrf(await signup(h, org="Other Business"))
    assert (await h.client.get(f"/api/v1/agents/{agent_id}")).status_code == 404
    assert (await h.client.get(f"/api/v1/agents/{agent_id}/configuration")).status_code == 404
    assert (await h.client.get("/api/v1/agents")).json()["data"] == []
    stolen = await h.client.post(f"/api/v1/agents/{agent_id}/versions", json={}, headers=other)
    assert stolen.status_code == 404


async def test_archived_agent_disappears(harness: Harness) -> None:
    h = harness
    headers, agent_id = await new_agent(h)
    assert (await h.client.delete(f"/api/v1/agents/{agent_id}", headers=headers)).status_code == 204
    assert (await h.client.get(f"/api/v1/agents/{agent_id}")).status_code == 404


async def test_organization_time_zone_is_validated(harness: Harness) -> None:
    # Regression: on Windows the IANA database comes from the tzdata package.
    h = harness
    headers = csrf(await signup(h))
    ok = await h.client.patch(
        "/api/v1/organizations/current", json={"timezone": "Asia/Kolkata"}, headers=headers
    )
    bad = await h.client.patch(
        "/api/v1/organizations/current", json={"timezone": "Mars/Olympus"}, headers=headers
    )
    assert ok.status_code == 200 and ok.json()["timezone"] == "Asia/Kolkata"
    assert bad.status_code == 422
