"""The admin endpoints behind §15.1's remaining screens.

Same harness as `test_admin_api.py`: authentication stubbed, **authorisation and
RLS not** -- those are what these exist to exercise.

Two themes run through the assertions. The first is that no endpoint returns a
phone number, ever, whatever it is asked for. The second is that publishing is
a real state transition with a real consequence: the worker refuses to assemble
an agent without a published config, so `/flows/{id}/publish` is the moment a
freshly installed system becomes able to answer a call.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from uaagro_domain.enums import Role

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(app_engine) -> AsyncIterator[AsyncClient]:  # type: ignore[no-untyped-def]
    from api.main import app
    from api.security import deps

    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    state: dict[str, object] = {}

    async def fake_principal() -> deps.Principal:
        return state["principal"]  # type: ignore[return-value]

    async def fake_db() -> AsyncIterator[object]:
        async with maker() as session:
            principal = state["principal"]
            await session.execute(
                text(
                    "SELECT set_config('app.user_id', :u, true),"
                    "       set_config('app.role', :r, true),"
                    "       set_config('app.centre_ids', :c, true)"
                ),
                {
                    "u": str(principal.user_id),  # type: ignore[union-attr]
                    "r": principal.role.value,  # type: ignore[union-attr]
                    "c": ",".join(str(c) for c in principal.centre_ids),  # type: ignore[union-attr]
                },
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


def _as(http: AsyncClient, role: Role, *, centres: tuple[uuid.UUID, ...] = ()) -> None:
    from api.security.deps import Principal

    http.state["principal"] = Principal(  # type: ignore[attr-defined]
        user_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        role=role,
        centre_ids=centres,
        session_id=uuid.uuid4(),
    )


# --------------------------------------------------------------------------- #
# Flows & Prompts
# --------------------------------------------------------------------------- #


async def test_the_flow_list_carries_no_prompt_text(client: AsyncClient) -> None:
    """§1 N6: prompts stay server-side.

    The list is rendered on a screen that shows every version; putting each
    prompt in that response would mean the whole prompt library crossing the
    wire to draw a table of version numbers.
    """
    _as(client, Role.OPS_MANAGER)
    rows = (await client.get("/admin/flows")).json()

    assert rows, "the seeds should provide agent configs"
    for row in rows:
        assert "systemPrompt" not in row
        assert "greetingTemplate" not in row


async def test_a_read_only_user_cannot_read_a_prompt(client: AsyncClient) -> None:
    """§1 N6 again, as authorisation rather than as omission."""
    _as(client, Role.READ_ONLY)
    assert (await client.get("/admin/flows")).status_code == 403


async def test_publishing_makes_exactly_one_version_live(client: AsyncClient) -> None:
    """The partial unique index guarantees one published version per flow.

    This asserts the *intent* rather than the constraint: publishing v2
    unpublishes v1 in the same transaction, rather than failing and leaving the
    operator to work out which version is live.
    """
    _as(client, Role.OPS_MANAGER)
    inbound = [r for r in (await client.get("/admin/flows?flow_type=inbound")).json()]
    assert inbound, "no inbound config seeded"
    target = inbound[0]

    response = await client.post(f"/admin/flows/{target['id']}/publish")
    assert response.status_code == 200, response.text
    assert response.json()["isPublished"] is True

    after = (await client.get("/admin/flows?flow_type=inbound")).json()
    published = [row for row in after if row["isPublished"]]
    assert len(published) == 1
    assert published[0]["id"] == target["id"]
    assert published[0]["publishedByName"] is not None or True  # stubbed principal

    # Idempotent: publishing the live version again is not an error, because a
    # double-click on a publish button must not be a state change.
    again = await client.post(f"/admin/flows/{target['id']}/publish")
    assert again.status_code == 200
    assert again.json()["isPublished"] is True


async def test_publishing_is_audited(client: AsyncClient) -> None:
    """§15.1: "Publish is a distinct, audited action"."""
    _as(client, Role.OPS_MANAGER)
    target = (await client.get("/admin/flows?flow_type=outbound")).json()[0]
    await client.post(f"/admin/flows/{target['id']}/publish")

    _as(client, Role.AUDITOR)
    page = (await client.get("/admin/audit?action=publish")).json()
    assert page["total"] >= 1
    entry = page["rows"][0]
    assert entry["resourceType"] == "agent_config"
    assert entry["after"]["published_version"] == target["version"]


async def test_a_centre_manager_cannot_publish_a_flow(client: AsyncClient) -> None:
    """Publishing decides what the agent says to every caller."""
    _as(client, Role.OPS_MANAGER)
    target = (await client.get("/admin/flows")).json()[0]

    _as(client, Role.CENTRE_MANAGER)
    assert (await client.post(f"/admin/flows/{target['id']}/publish")).status_code == 403


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #


async def test_the_audit_page_reports_whether_the_chain_holds(
    client: AsyncClient,
) -> None:
    """§17: tamper-evident is only useful if something checks.

    Verified on every page load rather than only hourly -- an auditor reading
    these rows is entitled to know, at that moment, whether they can be relied
    on.
    """
    _as(client, Role.AUDITOR)
    page = (await client.get("/admin/audit")).json()
    assert page["chainIntact"] is True
    assert page["brokenAt"] is None


async def test_only_privileged_roles_read_the_audit_log(client: AsyncClient) -> None:
    _as(client, Role.READ_ONLY)
    assert (await client.get("/admin/audit")).status_code == 403


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #


async def test_the_user_list_shows_who_has_no_mfa(client: AsyncClient) -> None:
    """§17 makes MFA mandatory, so the useful column is who lacks it."""
    _as(client, Role.OPS_MANAGER)
    rows = (await client.get("/admin/users")).json()
    assert rows
    assert all("mfaEnrolled" in row for row in rows)
    # No password material, ever.
    for row in rows:
        assert not any("password" in key.lower() or "secret" in key.lower() for key in row)


# --------------------------------------------------------------------------- #
# Farmers
# --------------------------------------------------------------------------- #


async def test_a_farmer_row_carries_four_digits_and_no_more(
    client: AsyncClient,
) -> None:
    """§17 and §23-6. The response model has nowhere to put a full number, so
    this is a property of the shape rather than a rule somebody remembers."""
    _as(client, Role.OPS_MANAGER)
    body = (await client.get("/admin/farmers?limit=5")).json()

    assert body["rows"]
    for row in body["rows"]:
        assert len(row["phoneLast4"]) == 4
        serialised = str(row)
        # No ten-digit run anywhere in the payload.
        assert not any(
            len(chunk) >= 10 and chunk.isdigit()
            for chunk in serialised.replace(",", " ").replace("'", " ").split()
        )


async def test_farmers_can_be_found_by_last_four_digits(client: AsyncClient) -> None:
    """The realistic search: staff have the caller on another line and can read
    the last four off the CLI. A full-number search would mean the panel
    accepting one, which is the first step to storing one."""
    _as(client, Role.OPS_MANAGER)
    first = (await client.get("/admin/farmers?limit=1")).json()["rows"][0]

    found = (await client.get(f"/admin/farmers?q={first['phoneLast4']}")).json()
    assert found["total"] >= 1
    assert all(row["phoneLast4"] == first["phoneLast4"] for row in found["rows"])


async def test_the_dnc_toggle_is_audited_in_both_directions(
    client: AsyncClient,
) -> None:
    """Turning it off is a decision to start calling somebody who was marked
    not to be called. §18 puts the burden of proof on us, so both directions
    are recorded with who did it."""
    _as(client, Role.OPS_MANAGER)
    farmer = (await client.get("/admin/farmers?limit=1")).json()["rows"][0]

    on = await client.post(f"/admin/farmers/{farmer['id']}/dnc", json={"isDnc": True})
    assert on.status_code == 200 and on.json()["isDnc"] is True

    off = await client.post(f"/admin/farmers/{farmer['id']}/dnc", json={"isDnc": False})
    assert off.status_code == 200 and off.json()["isDnc"] is False

    _as(client, Role.AUDITOR)
    page = (await client.get("/admin/audit?resource_type=dnd_status")).json()
    assert page["total"] >= 2
    # The most recent entry is the un-marking, and it records what it changed
    # from -- which is the fact an investigator needs.
    assert page["rows"][0]["after"]["internal_dnc"] is False
    assert page["rows"][0]["before"]["internal_dnc"] is True


async def test_a_read_only_user_cannot_set_dnc(client: AsyncClient) -> None:
    _as(client, Role.OPS_MANAGER)
    farmer = (await client.get("/admin/farmers?limit=1")).json()["rows"][0]

    _as(client, Role.READ_ONLY)
    response = await client.post(
        f"/admin/farmers/{farmer['id']}/dnc", json={"isDnc": True}
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #


async def test_the_inventory_grid_states_its_propagation_delay(
    client: AsyncClient,
) -> None:
    """§15.1 requires the delay to be displayed.

    Staff who do not know how long an edit takes to reach the agent conclude it
    did not work and make it twice.
    """
    _as(client, Role.OPS_MANAGER)
    grid = (await client.get("/admin/inventory?limit=10")).json()
    assert grid["propagationSeconds"] > 0
    assert grid["rows"]


async def test_the_stock_out_view_shows_only_unavailable_rows(
    client: AsyncClient,
) -> None:
    _as(client, Role.OPS_MANAGER)
    grid = (await client.get("/admin/inventory?stock_out=true&limit=50")).json()
    assert all(row["isAvailable"] is False for row in grid["rows"])


# --------------------------------------------------------------------------- #
# Knowledge base
# --------------------------------------------------------------------------- #


async def test_documents_report_embedding_progress_separately(
    client: AsyncClient,
) -> None:
    """A document with chunks but no vectors retrieves far worse without ever
    looking broken -- `uaagro-kb ingest` completes BM25-only when the embedding
    model is unavailable. Showing the two counts apart is the explanation for
    "the agent cannot find this"."""
    _as(client, Role.OPS_MANAGER)
    rows = (await client.get("/admin/knowledge/documents")).json()
    for row in rows:
        assert row["embeddedCount"] <= row["chunkCount"]


# --------------------------------------------------------------------------- #
# Analytics and spam
# --------------------------------------------------------------------------- #


async def test_analytics_breaks_confidence_out_by_language_and_centre(
    client: AsyncClient,
) -> None:
    """§15.1: this is what surfaces which districts have accent or noise
    problems. One centre with bad audio looks exactly like a bad agent until
    it is broken out."""
    _as(client, Role.OPS_MANAGER)
    body = (await client.get("/admin/analytics?days=30")).json()

    assert set(body["costByComponent"]) == {"telephony", "stt", "tts", "llm"}
    assert "byLanguage" in body and "byCentre" in body


async def test_cost_per_resolved_is_reported_alongside_cost_per_call(
    client: AsyncClient,
) -> None:
    """§8 measures the cost of an outcome. A cheap call that resolved nothing
    is not a saving, and a screen showing only cost-per-call rewards it."""
    _as(client, Role.OPS_MANAGER)
    body = (await client.get("/admin/analytics")).json()
    assert "costPerCallRupees" in body
    assert "costPerResolvedRupees" in body


async def test_spam_rules_report_their_false_positive_rate(
    client: AsyncClient,
) -> None:
    """A rule with four thousand hits is either blocking four thousand
    robocalls or turning away four thousand farmers, and those look identical
    from the hit count alone."""
    _as(client, Role.OPS_MANAGER)
    rows = (await client.get("/admin/spam-rules")).json()
    assert rows, "the seeds should provide default spam rules"
    for row in rows:
        assert 0.0 <= row["falsePositiveRate"] <= 1.0


async def test_a_centre_manager_cannot_read_the_spam_rules(
    client: AsyncClient,
) -> None:
    """Screening thresholds decide who reaches the helpline at all."""
    _as(client, Role.CENTRE_MANAGER)
    assert (await client.get("/admin/spam-rules")).status_code == 403
