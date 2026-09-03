"""The quick dial: a campaign created, gated, approved and started in one
request (§13.1, §15.1).

What these assert is that the shortcut skips the waiting and nothing else:
the gate still decides, the four-eyes rule is waived only where the
deployment says so and the waiver is written on the row, and a contact that
is ringing in simulator mode says where the browser can pick it up.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from tests.panel_support import act_as
from uaagro_domain.enums import Role

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def recorded_queue(quiet_queue: dict[str, list[Any]]) -> dict[str, list[Any]]:
    return quiet_queue


@pytest.fixture(autouse=True)
def open_window(monkeypatch: pytest.MonkeyPatch) -> None:
    from uaagro_domain import compliance

    monkeypatch.setattr(compliance, "within_calling_window", lambda when=None: True)


#: A promotional caller id in the 140 series and a DLT registration, so the
#: campaign-level checks pass and the test is about the quick dial itself.
_GATE_PASSES = {
    "OUTBOUND_CLI_PROMOTIONAL": "1409876543",
    "DLT_ENTITY_ID": "1101",
    "DLT_TEMPLATE_ID": "1107",
    "TELEPHONY_PROVIDER": "simulator",
    "OUTBOUND_QUICK_DIAL_SELF_APPROVE": "true",
}


async def _publish_outbound(panel: AsyncClient) -> str:
    act_as(panel, Role.OPS_MANAGER)
    rows = (await panel.get("/admin/flows?flow_type=outbound")).json()
    flow = max(rows, key=lambda r: r["version"])
    assert (await panel.post(f"/admin/flows/{flow['id']}/publish")).status_code == 200
    return str(flow["id"])


async def _audit_after(migrator_engine: Any, campaign_id: str) -> dict[str, Any]:
    async with migrator_engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT after FROM audit_log WHERE resource_type = 'quick_dial' "
                    "AND resource_id = :id ORDER BY chain_index DESC LIMIT 1"
                ),
                {"id": campaign_id},
            )
        ).first()
    assert row is not None, "the quick dial is audited"
    return row.after if isinstance(row.after, dict) else dict(json.loads(row.after))


async def test_a_quick_dial_creates_approves_and_starts_in_one_step(
    panel: AsyncClient,
    recorded_queue: dict[str, list[Any]],
    settings_env: Any,
    migrator_engine: Any,
) -> None:
    settings_env(**_GATE_PASSES)
    await _publish_outbound(panel)
    creator = act_as(panel, Role.OPS_MANAGER)

    response = await panel.post(
        "/admin/dial",
        json={"numbers": "9876540201\nसीता देवी, 98765 40202\n", "consentAttested": True},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["started"] is True
    assert body["blockedBy"] == []
    assert body["imported"] == 2
    assert body["invalid"] == []
    campaign = body["campaign"]
    assert campaign["status"] == "running"
    assert campaign["name"].startswith("Quick dial ")
    assert campaign["flowName"]
    assert campaign["counts"]["total"] == 2
    assert "9876540201" not in response.text

    campaign_id = campaign["id"]
    assert ("dial_campaign", (campaign_id,)) in recorded_queue["enqueued"]
    assert (uuid.UUID(campaign_id), None) in recorded_queue["control"]

    async with migrator_engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT self_approved, approved_by_user_id, created_by, source_type "
                    "FROM campaigns WHERE id = :id"
                ),
                {"id": campaign_id},
            )
        ).one()
    assert row.self_approved is True
    assert row.approved_by_user_id == row.created_by == creator
    assert row.source_type == "quick_dial"

    audited = await _audit_after(migrator_engine, campaign_id)
    assert audited["started"] is True
    assert audited["self_approved"] is True
    assert audited["contacts"] == 2


async def test_a_blocked_quick_dial_waits_with_its_checks_named(
    panel: AsyncClient, recorded_queue: dict[str, list[Any]], settings_env: Any
) -> None:
    # A plain mobile number as the caller id: a violation on every call.
    settings_env(**{**_GATE_PASSES, "OUTBOUND_CLI_PROMOTIONAL": "9876543210"})
    await _publish_outbound(panel)
    act_as(panel, Role.OPS_MANAGER)

    response = await panel.post(
        "/admin/dial", json={"numbers": "9876540211\n9876540212", "consentAttested": True}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["started"] is False
    assert "caller_id_series" in body["blockedBy"]
    assert body["campaign"]["status"] == "pending_approval"
    assert body["campaign"]["blockedBy"] == body["blockedBy"]
    assert not recorded_queue["enqueued"]


async def test_without_self_approval_the_quick_dial_waits_for_a_second_person(
    panel: AsyncClient, recorded_queue: dict[str, list[Any]], settings_env: Any
) -> None:
    settings_env(**{**_GATE_PASSES, "OUTBOUND_QUICK_DIAL_SELF_APPROVE": "false"})
    await _publish_outbound(panel)
    act_as(panel, Role.OPS_MANAGER)

    response = await panel.post(
        "/admin/dial",
        json={"numbers": "9876540221", "name": "Rabi meeting", "consentAttested": True},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["started"] is False
    assert body["blockedBy"] == []
    assert body["campaign"]["status"] == "pending_approval"
    assert body["campaign"]["name"] == "Rabi meeting"
    # The creator cannot approve it; the four-eyes rule holds.
    assert body["campaign"]["canApprove"] is False
    assert not recorded_queue["enqueued"]


async def test_consent_numbers_and_the_role_are_required(
    panel: AsyncClient, settings_env: Any
) -> None:
    settings_env(**_GATE_PASSES)
    await _publish_outbound(panel)
    act_as(panel, Role.OPS_MANAGER)

    unattested = await panel.post("/admin/dial", json={"numbers": "9876540231"})
    assert unattested.status_code == 422
    assert unattested.json()["error"]["code"] == "consent_required"

    nothing = await panel.post(
        "/admin/dial", json={"numbers": "not a number", "consentAttested": True}
    )
    assert nothing.status_code == 422

    act_as(panel, Role.CENTRE_MANAGER)
    refused = await panel.post(
        "/admin/dial", json={"numbers": "9876540231", "consentAttested": True}
    )
    assert refused.status_code == 403


async def test_a_ringing_contact_offers_the_browser_answer_link(
    panel: AsyncClient, settings_env: Any, migrator_engine: Any
) -> None:
    settings_env(**_GATE_PASSES)
    await _publish_outbound(panel)
    act_as(panel, Role.OPS_MANAGER)
    dialled = (
        await panel.post(
            "/admin/dial", json={"numbers": "9876540241\n9876540242", "consentAttested": True}
        )
    ).json()
    assert dialled["started"] is True
    campaign_id = dialled["campaign"]["id"]

    # The dialer marked one contact as being dialled; nothing has answered.
    async with migrator_engine.begin() as connection:
        contact_id = await connection.scalar(
            text(
                "UPDATE campaign_contacts SET status = 'dialing', attempts = 1, "
                "last_attempt_at = now() WHERE id = (SELECT id FROM campaign_contacts "
                "WHERE campaign_id = :c ORDER BY created_at LIMIT 1) RETURNING id"
            ),
            {"c": campaign_id},
        )

    detail = (await panel.get(f"/admin/campaigns/{campaign_id}")).json()
    contact = next(c for c in detail["contacts"] if c["id"] == str(contact_id))
    assert contact["status"] == "ringing"
    assert contact["answerUrl"] is not None
    assert contact["answerUrl"].endswith(f"/dev/call?answer={contact_id}")
    assert detail["counts"]["inCall"] == 1
    assert detail["counts"]["waiting"] == 1

    # The browser picked up: the media path linked a call.
    async with migrator_engine.begin() as connection:
        await connection.execute(
            text("UPDATE campaign_contacts SET call_id = :call WHERE id = :id"),
            {"call": uuid.uuid4(), "id": contact_id},
        )
    detail = (await panel.get(f"/admin/campaigns/{campaign_id}")).json()
    contact = next(c for c in detail["contacts"] if c["id"] == str(contact_id))
    assert contact["status"] == "in_call"
    assert contact["answerUrl"] is None
    assert detail["counts"]["inCall"] == 1

    # A real carrier dials the farmer's phone; no page can answer it.
    settings_env(TELEPHONY_PROVIDER="exotel")
    async with migrator_engine.begin() as connection:
        await connection.execute(
            text("UPDATE campaign_contacts SET call_id = NULL WHERE id = :id"),
            {"id": contact_id},
        )
    detail = (await panel.get(f"/admin/campaigns/{campaign_id}")).json()
    contact = next(c for c in detail["contacts"] if c["id"] == str(contact_id))
    assert contact["status"] == "ringing"
    assert contact["answerUrl"] is None


async def test_the_owners_account_may_approve_its_own_campaign(
    panel: AsyncClient, settings_env: Any, migrator_engine: Any
) -> None:
    """The one-operator business: the override is written on the row."""
    settings_env(**_GATE_PASSES)
    flow_id = await _publish_outbound(panel)
    act_as(panel, Role.SUPER_ADMIN)
    created = await panel.post(
        "/admin/campaigns",
        json={
            "name": "Owner's own campaign",
            "flowId": flow_id,
            "numbers": "9876540251",
            "maxConcurrent": 2,
            "consentAttested": True,
        },
    )
    assert created.status_code == 201, created.text
    campaign_id = created.json()["campaign"]["id"]
    assert created.json()["campaign"]["canApprove"] is True

    approved = await panel.post(f"/admin/campaigns/{campaign_id}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"

    async with migrator_engine.connect() as connection:
        self_approved = await connection.scalar(
            text("SELECT self_approved FROM campaigns WHERE id = :id"), {"id": campaign_id}
        )
    assert self_approved is True
