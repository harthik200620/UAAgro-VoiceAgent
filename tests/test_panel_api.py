"""The panel's control-plane surface, against a real database (§15.1).

What these assert is mostly about refusals and absences: no phone number in
any response, a campaign that cannot be approved by the person who made it,
a published script that cannot be edited, a document that cannot be published
before it is indexed. The happy paths are here too, because the panel is
built to this contract and a field that silently changes shape is a blank
column on the operator's screen.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from uaagro_db.engine import bind_rls_context
from uaagro_db.models import CampaignContact, ConsentRecord, Farmer, Organization, User
from uaagro_domain.enums import Role

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(app_engine, monkeypatch) -> AsyncIterator[AsyncClient]:  # type: ignore[no-untyped-def]
    """The API on the embedded database, signed in as a seeded user.

    Seeded users rather than invented ids: campaigns, documents and centres
    record who created them, and the audit chain is about real actors.
    """
    from api.main import app
    from api.security import deps

    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    state: dict[str, Any] = {}

    async with maker() as session:
        state["org_id"] = await session.scalar(select(Organization.id).limit(1))
        users = (await session.execute(select(User.id, User.role))).all()
    state["users"] = {role: user_id for user_id, role in users}

    async def fake_principal() -> deps.Principal:
        return state["principal"]  # type: ignore[no-any-return]

    async def fake_db() -> AsyncIterator[Any]:
        async with maker() as session:
            principal = state["principal"]
            await bind_rls_context(
                session,
                user_id=principal.user_id,
                role=principal.role.value,
                centre_ids=principal.centre_ids,
                org_id=principal.organization_id,
            )
            yield session
            await session.commit()

    app.dependency_overrides[deps.current_principal] = fake_principal
    app.dependency_overrides[deps.scoped_db] = fake_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        http.state = state  # type: ignore[attr-defined]
        yield http
    app.dependency_overrides.clear()


def _as(http: AsyncClient, role: Role) -> uuid.UUID:
    """Run the next requests as the seeded user with this role."""
    from api.security.deps import Principal

    state = http.state  # type: ignore[attr-defined]
    user_id = state["users"][role]
    state["principal"] = Principal(
        user_id=user_id,
        organization_id=state["org_id"],
        role=role,
        centre_ids=(),
        session_id=uuid.uuid4(),
    )
    return user_id  # type: ignore[no-any-return]


@pytest.fixture(autouse=True)
def no_queue(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """The job queue and the campaign control key, recorded rather than sent."""
    from api.services import jobs

    seen: dict[str, list[Any]] = {"enqueued": [], "control": []}

    async def enqueue(job: str, *args: Any) -> None:
        seen["enqueued"].append((job, args))

    async def set_control(campaign_id: uuid.UUID, instruction: str | None) -> None:
        seen["control"].append((campaign_id, instruction))

    monkeypatch.setattr(jobs, "enqueue", enqueue)
    monkeypatch.setattr(jobs, "set_control", set_control)
    return seen


@pytest.fixture(autouse=True)
def open_window(monkeypatch: pytest.MonkeyPatch) -> None:
    from uaagro_domain import compliance

    monkeypatch.setattr(compliance, "within_calling_window", lambda when=None: True)


# --------------------------------------------------------------------------- #
# Live and calls
# --------------------------------------------------------------------------- #


async def test_the_live_snapshot_has_every_section(client: AsyncClient) -> None:
    _as(client, Role.OPS_MANAGER)
    response = await client.get("/admin/live/snapshot")
    assert response.status_code == 200
    body = response.json()
    assert body["capacity"] > 0
    assert isinstance(body["calls"], list)
    for field in (
        "calls",
        "inbound",
        "outbound",
        "firstReplyP50Ms",
        "handledByAgentPct",
        "offersAccepted",
    ):
        assert field in body["today"]
    assert isinstance(body["recent"], list)


async def test_a_read_only_user_cannot_watch_live_calls(client: AsyncClient) -> None:
    _as(client, Role.READ_ONLY)
    assert (await client.get("/admin/live/snapshot")).status_code == 403


async def test_the_call_list_carries_the_panels_columns(client: AsyncClient) -> None:
    _as(client, Role.OPS_MANAGER)
    body = (await client.get("/admin/calls?limit=5&direction=inbound")).json()
    assert "rows" in body and "total" in body
    for row in body["rows"]:
        for field in ("farmerName", "firstReplyMs", "dtmf", "campaignId", "callerLast4"):
            assert field in row
        assert row["direction"] == "inbound"


async def test_an_unknown_call_is_not_found(client: AsyncClient) -> None:
    _as(client, Role.OPS_MANAGER)
    assert (await client.get(f"/admin/calls/{uuid.uuid4()}")).status_code == 404
    assert (await client.get(f"/admin/calls/{uuid.uuid4()}/recording")).status_code == 404


# --------------------------------------------------------------------------- #
# Flows
# --------------------------------------------------------------------------- #


async def _outbound_flow(client: AsyncClient) -> dict[str, Any]:
    rows = (await client.get("/admin/flows?flow_type=outbound")).json()
    assert rows, "the seed ships an outbound configuration"
    return max(rows, key=lambda r: r["version"])  # type: ignore[no-any-return]


async def test_a_version_carries_its_script_and_locked_lines(client: AsyncClient) -> None:
    _as(client, Role.OPS_MANAGER)
    flow = await _outbound_flow(client)
    assert "usedByCampaigns" in flow
    detail = (await client.get(f"/admin/flows/{flow['id']}")).json()
    script = detail["script"]
    assert script["opening"].startswith("नमस्ते! मैं यूए एग्रो")
    assert script["onPress2"] == "knowledge_base"
    assert "optOut" in script


async def test_editing_makes_a_new_draft_and_a_published_version_is_immutable(
    client: AsyncClient,
) -> None:
    _as(client, Role.OPS_MANAGER)
    base = await _outbound_flow(client)
    assert (await client.post(f"/admin/flows/{base['id']}/publish")).status_code == 200

    refused = await client.patch(
        f"/admin/flows/{base['id']}", json={"script": {"message": "बैठक चौदह अक्टूबर को"}}
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "published_immutable"

    created = await client.post(
        f"/admin/flows/{base['id']}/versions",
        json={"script": {"message": "बैठक चौदह अक्टूबर को है", "opening": "ignored"}},
    )
    assert created.status_code == 200, created.text
    draft = created.json()
    assert draft["version"] == base["version"] + 1
    assert draft["isPublished"] is False
    assert draft["script"]["message"] == "बैठक चौदह अक्टूबर को है"
    assert draft["script"]["opening"].startswith("नमस्ते! मैं यूए एग्रो")

    renamed = await client.patch(
        f"/admin/flows/{draft['id']}", json={"name": "किसान मीटिंग", "script": {}}
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "किसान मीटिंग"


async def test_an_empty_message_cannot_be_saved(client: AsyncClient) -> None:
    _as(client, Role.OPS_MANAGER)
    base = await _outbound_flow(client)
    response = await client.post(
        f"/admin/flows/{base['id']}/versions", json={"script": {"message": "   "}}
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Campaigns
# --------------------------------------------------------------------------- #

NUMBERS = "राम सिंह, 9876500001\n+91 98765 00002\nnot a number\n9876500001\n"


async def _published_outbound_flow(client: AsyncClient) -> str:
    flow = await _outbound_flow(client)
    await client.post(f"/admin/flows/{flow['id']}/publish")
    return str(flow["id"])


async def test_consent_must_be_attested(client: AsyncClient) -> None:
    _as(client, Role.OPS_MANAGER)
    flow_id = await _published_outbound_flow(client)
    response = await client.post(
        "/admin/campaigns",
        json={"name": "DAP offer", "flowId": flow_id, "numbers": NUMBERS, "maxConcurrent": 5},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "consent_required"


async def test_a_pasted_list_becomes_farmers_consent_and_contacts(
    client: AsyncClient, app_engine: Any
) -> None:
    _as(client, Role.OPS_MANAGER)
    flow_id = await _published_outbound_flow(client)
    response = await client.post(
        "/admin/campaigns",
        json={
            "name": "DAP offer — rabi",
            "flowId": flow_id,
            "numbers": NUMBERS,
            "maxConcurrent": 5,
            "consentAttested": True,
            "consentNote": "collected at the Sitapur meeting",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["imported"] == 2  # the duplicate line is folded
    assert len(body["invalid"]) == 1
    assert body["campaign"]["status"] == "pending_approval"
    assert body["campaign"]["counts"]["total"] == 2
    assert body["campaign"]["maxConcurrent"] == 5
    # No full number, anywhere in the response.
    assert "9876500001" not in response.text
    assert "9876500002" not in response.text

    from uaagro_db.crypto import get_cipher
    from uaagro_domain.phone import normalise_msisdn

    cipher = get_cipher()
    hashes = [cipher.hash(normalise_msisdn(n)) for n in ("9876500001", "9876500002")]
    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role', 'super_admin', true)"))
        farmers = (
            (await session.execute(select(Farmer).where(Farmer.phone_hash.in_(hashes))))
            .scalars()
            .all()
        )
        assert len(farmers) == 2
        named = next(f for f in farmers if f.phone_last4 == "0001")
        assert named.full_name == "राम सिंह"
        consents = (
            await session.scalars(
                select(ConsentRecord).where(ConsentRecord.farmer_id.in_([f.id for f in farmers]))
            )
        ).all()
        assert len(consents) == 2
        assert all(c.evidence["via"] == "panel_import" for c in consents)
        contacts = (
            await session.scalars(
                select(CampaignContact).where(
                    CampaignContact.campaign_id == uuid.UUID(body["campaign"]["id"])
                )
            )
        ).all()
        assert len(contacts) == 2

    detail = (await client.get(f"/admin/campaigns/{body['campaign']['id']}")).json()
    assert {c["last4"] for c in detail["contacts"]} == {"0001", "0002"}
    assert all(c["status"] in {"waiting", "removed"} for c in detail["contacts"])


async def test_the_creator_cannot_approve_and_the_gate_still_rules(
    client: AsyncClient, migrator_engine: Any, no_queue: dict[str, list[Any]]
) -> None:
    _as(client, Role.OPS_MANAGER)
    flow_id = await _published_outbound_flow(client)
    created = (
        await client.post(
            "/admin/campaigns",
            json={
                "name": "Meeting invite",
                "flowId": flow_id,
                "numbers": "9876500011\n9876500012\n",
                "maxConcurrent": 3,
                "consentAttested": True,
            },
        )
    ).json()
    campaign_id = created["campaign"]["id"]

    refused = await client.post(f"/admin/campaigns/{campaign_id}/approve")
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "four_eyes"

    # Somebody else, but the deployment has no promotional caller id or DLT
    # registration: the gate refuses, and names why.
    async with migrator_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE campaigns SET caller_id_number = NULL, dlt_entity_id = NULL, "
                "dlt_template_id = NULL WHERE id = :id"
            ),
            {"id": campaign_id},
        )
    _as(client, Role.SUPER_ADMIN)
    blocked = await client.post(f"/admin/campaigns/{campaign_id}/approve")
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "campaign_blocked"
    assert "dlt_registration" in blocked.json()["error"]["context"]["blockedBy"]

    async with migrator_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE campaigns SET caller_id_number = '1409876543', "
                "dlt_entity_id = '1101', dlt_template_id = '1107' WHERE id = :id"
            ),
            {"id": campaign_id},
        )
    approved = await client.post(f"/admin/campaigns/{campaign_id}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"

    started = await client.post(f"/admin/campaigns/{campaign_id}/start")
    assert started.status_code == 200
    assert started.json()["status"] == "running"
    assert ("dial_campaign", (campaign_id,)) in no_queue["enqueued"]

    paused = await client.post(f"/admin/campaigns/{campaign_id}/pause")
    assert paused.json()["status"] == "paused"
    assert (uuid.UUID(campaign_id), "paused") in no_queue["control"]

    export = await client.get(f"/admin/campaigns/{campaign_id}/export.csv")
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    assert "9876500011" not in export.text
    assert "0011" in export.text


# --------------------------------------------------------------------------- #
# Knowledge
# --------------------------------------------------------------------------- #


async def test_a_website_is_queued_and_cannot_be_published_before_indexing(
    client: AsyncClient, no_queue: dict[str, list[Any]]
) -> None:
    _as(client, Role.OPS_MANAGER)
    response = await client.post(
        "/admin/knowledge/documents",
        json={"url": "https://www.uaagro.in/products", "title": "Website — products"},
    )
    assert response.status_code == 202, response.text
    document = response.json()
    assert document["ingestStatus"] == "pending"
    assert document["docType"] == "web"
    assert ("ingest_document", (document["id"],)) in no_queue["enqueued"]

    listed = (await client.get("/admin/knowledge/documents")).json()
    assert any(row["id"] == document["id"] for row in listed)

    early = await client.patch(
        f"/admin/knowledge/documents/{document['id']}", json={"isPublished": True}
    )
    assert early.status_code == 422

    assert (await client.delete(f"/admin/knowledge/documents/{document['id']}")).status_code == 204
    assert (await client.get(f"/admin/knowledge/documents/{document['id']}")).status_code == 404


async def test_a_photo_is_not_a_document(client: AsyncClient) -> None:
    _as(client, Role.OPS_MANAGER)
    response = await client.post(
        "/admin/knowledge/documents",
        files={"file": ("photo.jpg", b"\xff\xd8\xff", "image/jpeg")},
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Centres, stock and hand-over
# --------------------------------------------------------------------------- #


async def test_centres_can_be_added_and_their_managers_changed(client: AsyncClient) -> None:
    _as(client, Role.OPS_MANAGER)
    rows = (await client.get("/admin/centres")).json()
    assert len(rows) >= 10
    for field in ("managerName", "managerNumber", "openTime", "stock", "openNow"):
        assert field in rows[0]

    created = await client.post(
        "/admin/centres",
        json={
            "name": "Naveen Khushhali Kisan Sewa Kendra — Bahraich",
            "district": "Bahraich",
            "block": "Nanpara",
            "pincode": "271865",
            "latitude": 27.87,
            "longitude": 81.5,
            "managerName": "अनिल वर्मा",
            "managerNumber": "98765 40099",
            "openTime": "08:00",
            "closeTime": "19:00",
        },
    )
    assert created.status_code == 201, created.text
    centre = created.json()
    assert centre["code"] == "NKSK-BAH-01"
    assert centre["managerNumber"] == "+919876540099"
    assert centre["district"] == "Bahraich"

    updated = await client.patch(
        f"/admin/centres/{centre['id']}", json={"managerNumber": "9876540100", "isActive": False}
    )
    assert updated.status_code == 200
    assert updated.json()["managerNumber"] == "+919876540100"
    assert updated.json()["isActive"] is False

    bad_hours = await client.patch(f"/admin/centres/{centre['id']}", json={"closeTime": "07:00"})
    assert bad_hours.status_code == 422


async def test_stock_can_be_switched_off_and_on(client: AsyncClient) -> None:
    _as(client, Role.OPS_MANAGER)
    centre = next(c for c in (await client.get("/admin/centres")).json() if c["stock"])
    stock = (await client.get(f"/admin/centres/{centre['id']}/stock")).json()
    assert stock
    item = stock[0]
    off = await client.patch(f"/admin/inventory/{item['inventoryId']}", json={"isAvailable": False})
    assert off.status_code == 200
    assert off.json()["isAvailable"] is False
    on = await client.patch(f"/admin/inventory/{item['inventoryId']}", json={"isAvailable": True})
    assert on.json()["isAvailable"] is True


async def test_the_handover_rules_are_readable_and_the_fallback_editable(
    client: AsyncClient,
) -> None:
    _as(client, Role.OPS_MANAGER)
    rules = (await client.get("/admin/transfer-rules")).json()
    assert rules["reasons"]
    assert rules["ringTimeoutSeconds"] > 0
    updated = await client.patch("/admin/transfer-rules", json={"fallbackNumber": "9876540000"})
    assert updated.status_code == 200
    assert updated.json()["fallbackNumber"] == "+919876540000"


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #


async def test_storage_is_described_by_the_operators_names(client: AsyncClient) -> None:
    _as(client, Role.OPS_MANAGER)
    body = (await client.get("/admin/data/storage")).json()
    tables = {row["table"]: row for row in body["tables"]}
    assert tables["calls"]["label"] == "Calls"
    assert tables["calls"]["partitioned"] is True, tables["calls"]
    assert tables["farmers"]["rows"] > 0
    assert body["recordings"]["retentionDays"] > 0


async def test_the_connection_is_described_without_its_password(client: AsyncClient) -> None:
    _as(client, Role.OPS_MANAGER)
    assert (await client.get("/admin/data/connection")).status_code == 403
    _as(client, Role.SUPER_ADMIN)
    response = await client.get("/admin/data/connection")
    assert response.status_code == 200
    body = response.json()
    assert body["host"] and body["database"] and body["user"]
    assert body["rowLevelSecurity"] is True
    assert "password" not in response.text.lower()
    # The storage endpoint is a URL and fine to show; a database DSN is not.
    assert "postgresql" not in response.text.lower()


async def test_a_bad_dsn_is_reported_not_raised(client: AsyncClient) -> None:
    _as(client, Role.SUPER_ADMIN)
    response = await client.post(
        "/admin/data/connection/test",
        json={"dsn": "postgresql://nobody:nothing@127.0.0.1:1/none"},  # not-a-secret
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error"]
    assert "DATABASE_URL" in body["note"]
    assert (
        await client.post("/admin/data/connection/test", json={"dsn": "mysql://x"})
    ).status_code == 422
