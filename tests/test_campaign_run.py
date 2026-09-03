"""Running a campaign, against a real database (§13.1, §17, §18).

The gate has its own unit tests and the dialer has its own unit tests, and both
were green while nothing joined them to a campaign row. These test the join:
the job that reads an approved campaign, re-checks each contact against live
rows, decrypts one number at a time under audit, and dials.

The assertions are about refusals. A campaign that dials the right people is
the easy case; the ones worth a test are the farmer who opted out after
approval, the farmer who is not on the list at all, and the campaign that the
gate refuses outright.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from uaagro_db.models import AuditLog, Campaign, CampaignContact, ConsentRecord, DndStatus, Farmer
from uaagro_domain.compliance import Check
from uaagro_domain.enums import (
    AuditAction,
    CampaignStatus,
    ConsentChannel,
    ConsentType,
)
from uaagro_domain.timezone import ist
from worker import dialer as dialer_module
from worker.campaign import CampaignBlocked, run_campaign

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def open_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold the §18 window open.

    Without this these tests pass before 21:00 IST and fail after it. The
    window itself is covered by `test_worker.py` and the compliance suite.
    """
    monkeypatch.setattr(dialer_module, "within_calling_window", lambda _now: True)


@pytest.fixture
async def campaign(app_engine) -> AsyncIterator[uuid.UUID]:  # type: ignore[no-untyped-def]
    """An approved, compliant campaign with three consenting contacts."""
    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    now = datetime.now(UTC)

    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        organization_id = await session.scalar(text("SELECT id FROM organizations LIMIT 1"))
        # Two different users: the schema enforces §13.1's four-eyes rule
        # with a CHECK constraint, so a campaign cannot be approved by the
        # person who created it -- not even in a fixture.
        user_ids = list(
            (await session.scalars(text("SELECT id FROM users ORDER BY email LIMIT 2"))).all()
        )
        assert len(user_ids) == 2, "the seeds should provide at least two users"
        creator_id, approver_id = user_ids
        farmers = list((await session.scalars(select(Farmer).limit(3))).all())
        assert len(farmers) == 3

        row = Campaign(
            organization_id=organization_id,
            name="Test rabi offer",
            status=CampaignStatus.APPROVED,
            source_type="manual",
            # 10:00 IST today: inside §18's window, and in the past, so the
            # gate's dial-time check passes whatever hour the suite runs at.
            # The dialer's *live* window check is patched separately above.
            scheduled_start=datetime.now(ist()).replace(hour=10, minute=0, second=0, microsecond=0),
            # A 140-series CLI: §18 requires one for promotional calls, and the
            # gate blocks the campaign without it.
            caller_id_number="1400112233",
            dlt_entity_id="ENTITY-TEST-1",
            dlt_template_id="TPL-TEST-1",
            is_promotional=True,
            approved_by_user_id=approver_id,
            approved_at=now,
            created_by=creator_id,
        )
        session.add(row)
        await session.flush()

        for farmer in farmers:
            session.add(
                CampaignContact(
                    campaign_id=row.id,
                    farmer_id=farmer.id,
                    # Snapshotted onto the contact row when the list is built,
                    # so a campaign's own record of who it targets does not
                    # move when a farmer updates their number.
                    phone_hash=farmer.phone_hash,
                )
            )
            session.add(
                ConsentRecord(
                    farmer_id=farmer.id,
                    consent_type=ConsentType.PROMOTIONAL_VOICE,
                    channel=ConsentChannel.VOICE,
                    granted_at=now - timedelta(days=10),
                    expires_at=now + timedelta(days=300),
                )
            )
        campaign_id = row.id
        farmer_ids = [f.id for f in farmers]
        await session.commit()

    yield campaign_id

    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        await session.execute(
            text("DELETE FROM campaign_contacts WHERE campaign_id = :c"), {"c": campaign_id}
        )
        await session.execute(text("DELETE FROM campaigns WHERE id = :c"), {"c": campaign_id})
        for farmer_id in farmer_ids:
            await session.execute(
                text("DELETE FROM consent_records WHERE farmer_id = :f"), {"f": farmer_id}
            )
            await session.execute(
                text(
                    "DELETE FROM dnd_status WHERE phone_hash = "
                    "(SELECT phone_hash FROM farmers WHERE id = :f)"
                ),
                {"f": farmer_id},
            )
        await session.commit()


@pytest.fixture
def sessions(app_engine):  # type: ignore[no-untyped-def]
    """A session factory of the shape the campaign runner expects.

    Binds the ``system`` role, which is what ``uaagro_db.engine.system_session``
    does in production. Without it the RLS predicate is NULL and the runner
    loads an empty contact list -- and reports a clean run of zero calls.
    """
    from contextlib import asynccontextmanager

    from uaagro_db.engine import bind_rls_context
    from uaagro_db.roles import SYSTEM_ROLE

    maker = async_sessionmaker(app_engine, expire_on_commit=False)

    @asynccontextmanager
    async def factory():  # type: ignore[no-untyped-def]
        async with maker() as session:
            await bind_rls_context(session, user_id=None, role=SYSTEM_ROLE, centre_ids=())
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    return factory


class RecordingDialer:
    """Stands in for the provider. Records what it was asked to dial."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        #: The contact reference each dial carried, so the media path can
        #: find the contact when the call connects.
        self.custom_fields: list[str] = []

    async def __call__(self, to_number: str, from_number: str, custom_field: str = "") -> str:
        self.calls.append((to_number, from_number))
        self.custom_fields.append(custom_field)
        return f"SID-{len(self.calls)}"


# --------------------------------------------------------------------------- #


async def test_an_approved_campaign_dials_its_contacts(sessions, campaign) -> None:  # type: ignore[no-untyped-def]
    provider = RecordingDialer()
    report = await run_campaign(sessions, campaign, originate=provider, calls_per_minute=6000)

    assert report.dialled == 3
    assert len(provider.calls) == 3
    # Every call went out on the campaign's 140-series CLI (§18).
    assert {caller for _, caller in provider.calls} == {"1400112233"}


async def test_a_farmer_who_opted_out_after_approval_is_not_dialled(
    app_engine, sessions, campaign
) -> None:  # type: ignore[no-untyped-def]
    """§13.1's reason for re-checking at dial time.

    The campaign passed the gate. Then one farmer said "मुझे कॉल मत करो" on
    another call, which wrote a DNC row. A gate evaluated only at approval
    dials them anyway.
    """
    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        contact = await session.scalar(
            select(CampaignContact).where(CampaignContact.campaign_id == campaign).limit(1)
        )
        assert contact is not None
        farmer = await session.get(Farmer, contact.farmer_id)
        assert farmer is not None
        session.add(
            DndStatus(
                phone_hash=farmer.phone_hash,
                internal_dnc=True,
                internal_dnc_at=datetime.now(UTC),
            )
        )
        await session.commit()

    provider = RecordingDialer()
    report = await run_campaign(sessions, campaign, originate=provider, calls_per_minute=6000)

    assert report.dialled == 2, "the opted-out farmer was dialled"
    assert report.skipped == 1


async def test_a_blocked_campaign_never_dials_anybody(app_engine, sessions, campaign) -> None:  # type: ignore[no-untyped-def]
    """§13.1: the blocking checks are non-overridable.

    A promotional campaign on an ordinary CLI is a TCCCPR violation on every
    call it places, so it places none.
    """
    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        row = await session.get(Campaign, campaign)
        assert row is not None
        row.caller_id_number = "9876500000"  # not a 140-series number
        await session.commit()

    provider = RecordingDialer()
    with pytest.raises(CampaignBlocked) as raised:
        await run_campaign(sessions, campaign, originate=provider, calls_per_minute=6000)

    assert Check.CALLER_ID_SERIES in raised.value.checks
    assert provider.calls == [], "a blocked campaign placed a call"

    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        row = await session.get(Campaign, campaign)
        assert row is not None
        # Paused, not completed: the operator has something to fix and the
        # contact list is still there when they do.
        assert row.status is CampaignStatus.PAUSED


async def test_every_decryption_is_audited(app_engine, sessions, campaign) -> None:  # type: ignore[no-untyped-def]
    """§17: plaintext is a privileged operation, and the audit row is the
    answer to "who saw this number and why"."""
    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        before = int(
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == AuditAction.PHONE_DECRYPT)
            )
            or 0
        )

    await run_campaign(sessions, campaign, originate=RecordingDialer(), calls_per_minute=6000)

    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        rows = (
            await session.scalars(
                select(AuditLog)
                .where(AuditLog.action == AuditAction.PHONE_DECRYPT)
                .order_by(AuditLog.chain_index.desc())
                .limit(3)
            )
        ).all()
        after = int(
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == AuditAction.PHONE_DECRYPT)
            )
            or 0
        )

    assert after - before == 3, "one audit row per decryption"
    for row in rows:
        assert row.after["purpose"] == "outbound_dial"
        # The reason, never the number -- an audit log holding plaintext would
        # defeat the encryption it exists to police.
        serialised = str(row.after)
        assert not any(chunk.isdigit() and len(chunk) >= 10 for chunk in serialised.split())


async def test_a_farmer_not_on_the_list_cannot_be_dialled(app_engine, sessions, campaign) -> None:  # type: ignore[no-untyped-def]
    """§17's destination rule, at the only point it can still be enforced.

    The number is produced by this code rather than supplied to it, so an
    approved-list check on the *value* proves nothing. Requiring the farmer to
    be a contact of this campaign is the same guarantee, one step earlier.
    """
    from worker.campaign import _plaintext_number

    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        listed = await session.scalars(
            select(CampaignContact.farmer_id).where(CampaignContact.campaign_id == campaign)
        )
        excluded = await session.scalar(
            select(Farmer.id).where(Farmer.id.not_in(list(listed))).limit(1)
        )
        assert excluded is not None

        with pytest.raises(PermissionError):
            await _plaintext_number(session, excluded, campaign_id=campaign)


async def test_the_dialer_gets_the_number_in_e164_not_the_redacted_form(
    app_engine, sessions, campaign
) -> None:  # type: ignore[no-untyped-def]
    """The number type redacts itself when printed -- deliberately -- and the
    dialer once handed the adapter that redacted form. Every dial then failed
    as an unparseable destination before anything rang."""
    from worker.campaign import _plaintext_number

    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        listed = await session.scalar(
            select(CampaignContact.farmer_id)
            .where(CampaignContact.campaign_id == campaign)
            .limit(1)
        )
        assert listed is not None
        number = await _plaintext_number(session, listed, campaign_id=campaign)

    assert number.startswith("+91") and len(number) == 13 and number[1:].isdigit()
    assert "*" not in number


async def test_a_failed_dial_does_not_abandon_the_rest_of_the_list(sessions, campaign) -> None:  # type: ignore[no-untyped-def]
    """One carrier rejection is not a reason to stop calling everybody else."""
    attempts = 0

    async def flaky(to_number: str, from_number: str, custom_field: str = "") -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("carrier rejected")
        return f"SID-{attempts}"

    report = await run_campaign(sessions, campaign, originate=flaky, calls_per_minute=6000)

    assert report.failed == 1
    assert report.dialled == 2
