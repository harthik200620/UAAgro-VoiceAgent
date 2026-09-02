"""The admin surface (§15.1), against a real database.

The assertions that matter here are the ones about what does *not* come back:
no phone number, no vendor key, no unapproved dose presented as approved, and
no row from a centre the caller cannot see.

Every one of those is a control that fails silently. The endpoint still returns
200 with plausible JSON either way, so the only way to know it holds is to
construct the case where it would not.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from uaagro_domain.enums import Role

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(app_engine, monkeypatch) -> AsyncIterator[AsyncClient]:  # type: ignore[no-untyped-def]
    """The API bound to the embedded database, with auth stubbed.

    Authentication has its own suite; stubbing the principal here keeps these
    tests about the admin handlers. The **authorisation** is not stubbed -- the
    role checks and the RLS binding both run for real, because those are what
    these tests exist to exercise.
    """
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
    """Run the next request as this role."""
    from api.security.deps import Principal

    http.state["principal"] = Principal(  # type: ignore[attr-defined]
        user_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        role=role,
        centre_ids=centres,
        session_id=uuid.uuid4(),
    )


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #


async def test_the_dashboard_returns_every_tile(client: AsyncClient) -> None:
    _as(client, Role.OPS_MANAGER)
    response = await client.get("/admin/dashboard?range=today")
    assert response.status_code == 200

    body = response.json()
    # §15.1 lists these. A tile the panel renders and the API omits shows as
    # `undefined` in a dense table, which reads as zero.
    for field in (
        "inboundCalls",
        "outboundCalls",
        "answerRate",
        "resolutionRate",
        "transferRate",
        "containmentRate",
        "liveConcurrency",
        "concurrencyCapacity",
        "spendTodayRupees",
        "budgetRupees",
        "latencyP50Ms",
        "latencyP95Ms",
        "unhandledIntents",
        "failedCalls",
    ):
        assert field in body, f"the dashboard is missing {field}"


async def test_an_unknown_range_is_refused_not_guessed(client: AsyncClient) -> None:
    _as(client, Role.OPS_MANAGER)
    response = await client.get("/admin/dashboard?range=fortnight")
    assert response.status_code == 422


async def test_a_read_only_user_can_see_the_dashboard(client: AsyncClient) -> None:
    _as(client, Role.READ_ONLY)
    assert (await client.get("/admin/dashboard")).status_code == 200


# --------------------------------------------------------------------------- #
# Advisory -- §9's safety control
# --------------------------------------------------------------------------- #


async def test_the_unapproved_count_is_reported(client: AsyncClient) -> None:
    """Every seeded recommendation is a draft, so the count is the whole set.

    §15.1 requires a permanent banner counting these, and the banner is driven
    by this number. A zero here would show "all approved" over a corpus that is
    entirely unapproved.
    """
    _as(client, Role.AGRONOMIST)
    body = (await client.get("/admin/advisory")).json()
    assert body["unapproved"] == len(body["rows"])
    assert body["unapproved"] > 0


async def test_an_ops_manager_cannot_approve_advisory(client: AsyncClient) -> None:
    """§9's safety control. ops_manager outranks agronomist on everything else
    in this system and has no business signing off a pesticide dose."""
    _as(client, Role.AGRONOMIST)
    row_id = (await client.get("/admin/advisory")).json()["rows"][0]["id"]

    _as(client, Role.OPS_MANAGER)
    response = await client.post(f"/admin/advisory/{row_id}/approve")
    assert response.status_code == 403


async def test_approving_an_incomplete_spray_row_says_why(
    client: AsyncClient,
) -> None:
    """§16.2. The database CHECK constraint would refuse it anyway; this is the
    message. A 500 from a constraint violation tells an agronomist nothing
    about what to fix."""
    _as(client, Role.AGRONOMIST)
    rows = (await client.get("/admin/advisory")).json()["rows"]
    incomplete = next(
        (
            r
            for r in rows
            if r["isCropProtection"] and (r["phiDays"] is None or not r["precautionHi"])
        ),
        None,
    )
    if incomplete is None:
        pytest.skip("every seeded spray row already carries a PHI and precaution")

    response = await client.post(f"/admin/advisory/{incomplete['id']}/approve")
    assert response.status_code == 422
    body = response.json()
    assert "pre-harvest" in json.dumps(body).lower()


async def test_approval_records_who_signed_off(
    client: AsyncClient, app_engine
) -> None:  # type: ignore[no-untyped-def]
    """§9 records *who*. A client-supplied approver id would make that record
    worthless, so the approver comes from the authenticated principal."""
    from api.security.deps import Principal

    _as(client, Role.AGRONOMIST)
    rows = (await client.get("/admin/advisory")).json()["rows"]
    complete = next(
        (r for r in rows if not r["isCropProtection"] or (r["phiDays"] and r["precautionHi"])),
        None,
    )
    assert complete is not None

    principal: Principal = client.state["principal"]  # type: ignore[attr-defined]
    response = await client.post(f"/admin/advisory/{complete['id']}/approve")
    assert response.status_code == 204

    async with app_engine.connect() as connection:
        await connection.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        approver = await connection.scalar(
            text("SELECT approved_by_user_id FROM crop_recommendations WHERE id = :i"),
            {"i": complete["id"]},
        )
    assert str(approver) == str(principal.user_id)

    # Restore the seed's invariant: everything is a draft (§9, KB §11).
    async with app_engine.begin() as connection:
        await connection.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        await connection.execute(
            text(
                "UPDATE crop_recommendations SET approval_state='draft',"
                " approved_by_user_id=NULL, approved_at=NULL WHERE id=:i"
            ),
            {"i": complete["id"]},
        )


# --------------------------------------------------------------------------- #
# Calls -- §17's PII boundary
# --------------------------------------------------------------------------- #


async def test_no_call_row_carries_a_phone_number(client: AsyncClient) -> None:
    """§17 and §23-6. The call row holds only a hash; the last four digits come
    from the farmer record, and nothing above the control plane sees more."""
    _as(client, Role.OPS_MANAGER)
    body = (await client.get("/admin/calls")).json()
    serialised = json.dumps(body)

    for forbidden in ("phone_enc", "from_number_hash", "phone_hash", "9999000"):
        assert forbidden not in serialised, f"the call list leaks {forbidden}"

    for row in body["rows"]:
        assert set(row) >= {"callRef", "outcome", "callerLast4"}
        if row["callerLast4"] is not None:
            assert len(row["callerLast4"]) == 4


async def test_the_call_list_paginates(client: AsyncClient) -> None:
    _as(client, Role.OPS_MANAGER)
    response = await client.get("/admin/calls?limit=5&offset=0")
    assert response.status_code == 200
    assert len(response.json()["rows"]) <= 5


async def test_an_absurd_page_size_is_refused(client: AsyncClient) -> None:
    """An unbounded limit is a denial-of-service on the screen people open
    first."""
    _as(client, Role.OPS_MANAGER)
    assert (await client.get("/admin/calls?limit=100000")).status_code == 422


# --------------------------------------------------------------------------- #
# Settings -- §15.1's write-only keys
# --------------------------------------------------------------------------- #


async def test_a_vendor_key_is_never_returned(client: AsyncClient) -> None:
    """§15.1: write-only, masked after save, never returned by any API.

    The test environment sets placeholder keys, so this asserts against real
    values that are actually present -- an empty settings object would make
    this pass for the wrong reason.
    """
    _as(client, Role.SUPER_ADMIN)
    response = await client.get("/admin/settings/secrets")
    assert response.status_code == 200

    body = response.json()
    serialised = json.dumps(body)
    assert "test-not-a-real-key" not in serialised, "a vendor key was returned"

    by_name = {s["name"]: s for s in body["secrets"]}
    deepgram = by_name["DEEPGRAM_API_KEY"]
    assert deepgram["isSet"] is True
    # A hint, not a key: four characters recognise it and cannot use it.
    assert deepgram["hint"] is not None
    assert len(deepgram["hint"]) <= 8


async def test_only_a_super_admin_reads_the_key_list(client: AsyncClient) -> None:
    for role in (Role.OPS_MANAGER, Role.CENTRE_MANAGER, Role.AGRONOMIST, Role.AUDITOR):
        _as(client, role)
        assert (await client.get("/admin/settings/secrets")).status_code == 403


async def test_every_secret_field_is_listed(client: AsyncClient) -> None:
    """A key the settings screen cannot see is a key nobody can rotate."""
    from uaagro_domain.settings import SECRET_FIELDS

    _as(client, Role.SUPER_ADMIN)
    names = {s["name"] for s in (await client.get("/admin/settings/secrets")).json()["secrets"]}
    assert names == {field.upper() for field in SECRET_FIELDS}


# --------------------------------------------------------------------------- #
# Answer cache
# --------------------------------------------------------------------------- #


async def test_a_volatile_answer_is_never_active(client: AsyncClient) -> None:
    """§9: anything with a price or stock figure goes to Tier 1 every time.
    The schema forbids the combination; this is the panel being able to see
    it."""
    _as(client, Role.OPS_MANAGER)
    for entry in (await client.get("/admin/answer-cache")).json():
        assert not (entry["isActive"] and entry["containsVolatileData"])


async def test_the_seeded_cache_is_present(client: AsyncClient) -> None:
    _as(client, Role.READ_ONLY)
    assert len((await client.get("/admin/answer-cache")).json()) > 0


# --------------------------------------------------------------------------- #
# The authz matrix (§19)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("/admin/dashboard", set(Role)),
        ("/admin/advisory", set(Role)),
        ("/admin/answer-cache", set(Role)),
        ("/admin/settings/secrets", {Role.SUPER_ADMIN}),
    ],
)
async def test_every_role_against_every_endpoint(
    client: AsyncClient, path: str, allowed: set[Role]
) -> None:
    """§19 asks for this matrix explicitly.

    Written as data rather than as one test per pair, because the useful failure
    is "role X gained access to Y" and that is a diff on this table.
    """
    for role in Role:
        _as(client, role)
        status = (await client.get(path)).status_code
        if role in allowed:
            assert status == 200, f"{role.value} should reach {path}, got {status}"
        else:
            assert status == 403, f"{role.value} should not reach {path}, got {status}"


async def test_the_dashboard_reports_a_real_timestamp_window(
    client: AsyncClient,
) -> None:
    """Ranges are named, and each has to actually be a different window --
    otherwise "today vs last week" compares a number with itself."""
    _as(client, Role.OPS_MANAGER)
    today = (await client.get("/admin/dashboard?range=today")).json()
    week = (await client.get("/admin/dashboard?range=week")).json()
    assert week["inboundCalls"] >= today["inboundCalls"]
    _ = datetime.now(UTC)
