"""Running an outbound campaign (§13.1, §13.3, §17, §18).

The job that turns an approved campaign into calls. It sits between three
things that were each built and tested separately and had nothing joining them:
the compliance gate (``uaagro_db.campaigns``), the dialer (``worker.dialer``)
and the telephony control adapter.

Four properties this insists on, each of which is a regulatory obligation
rather than a preference.

**The gate runs again here.** It ran at approval, and §13.1 still has it run at
dial time, per contact. A campaign approved at 09:00 with 400 contacts is still
dialling at 20:55, and by then some of those farmers have opted out on another
call and some have hit the weekly cap. Approval is a snapshot; the dial is the
act.

**The number is decrypted one contact at a time, and the decryption is
audited.** §17 makes plaintext a privileged operation. Decrypting the whole
list up front would be faster and would also mean a crash mid-campaign leaves
four hundred numbers in a heap dump.

**An attempt is recorded before the call, not after.** If the process dies
between originate and bookkeeping, the safe residue is a contact recorded as
attempted-but-unknown, not one that gets dialled twice.

**A campaign that cannot pass the gate does not start at all.** §13.1 makes the
blocking checks non-overridable: no flag, no admin, no "force" parameter
reaches this code, because the only way to guarantee that is not to have one.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.audit import append_audit
from uaagro_db.campaigns import evaluate_campaign, load_contacts
from uaagro_db.crypto import get_cipher
from uaagro_db.models import Campaign, CampaignContact, Farmer
from uaagro_domain.compliance import CampaignSettings, Check, Contact, first_failure
from uaagro_domain.enums import AuditAction, CampaignStatus, ContactStatus
from uaagro_domain.livefeed import CAMPAIGN_UPDATED, CONTACT_UPDATED, LiveEvent, LiveFeed

from .dialer import Control, Dialer, DialerState

log = structlog.get_logger(__name__)


class CampaignBlocked(Exception):
    """The gate refused. Carries the failed checks so the panel can say which.

    Not a ``UAAgroError``: this is not a condition the agent relays to anybody,
    it is a campaign that must not run. It ends the job.
    """

    def __init__(self, checks: list[Check]) -> None:
        super().__init__(", ".join(check.value for check in checks))
        self.checks = checks


@dataclass
class CampaignRunReport:
    campaign_id: uuid.UUID
    dialled: int = 0
    skipped: int = 0
    failed: int = 0
    stopped_reason: str | None = None

    def summary(self) -> str:
        return (
            f"campaign {self.campaign_id}: {self.dialled} dialled, "
            f"{self.skipped} skipped at dial time, {self.failed} failed"
            + (f", stopped: {self.stopped_reason}" if self.stopped_reason else "")
        )


#: ``(farmer number, caller id, custom field)`` -> provider call id. The
#: custom field names the contact so the media path can find it when the
#: call connects (see ``voice_worker.runtime.direction``).
Originate = Callable[[str, str, str], Awaitable[str]]
"""``(to_number, from_number) -> provider call SID``."""

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
"""Opens a session with an RLS role bound -- ``uaagro_db.engine.system_session``.

A context manager rather than a bare session: the role has to be bound per
transaction, and an unbound session sees no rows at all.
"""


async def run_campaign(
    session_factory: SessionFactory,
    campaign_id: uuid.UUID,
    *,
    originate: Originate,
    state: DialerState | None = None,
    max_concurrent: int = 10,
    calls_per_minute: int = 20,
    live_feed: LiveFeed | None = None,
    control: Control | None = None,
) -> CampaignRunReport:
    """Dial one approved campaign.

    ``session_factory`` rather than a session: a campaign runs for an hour, and
    holding one transaction open across it would pin a connection and make
    every per-contact write invisible to the panel until the end.

    ``max_concurrent`` is the deployment's ceiling. The campaign carries its
    own "calls at a time", set in the panel, and the lower of the two wins:
    an operator can slow a campaign down, never push it past what the
    telephony account and the workers were sized for.
    """
    report = CampaignRunReport(campaign_id=campaign_id)

    def announce(event_type: str, payload: dict[str, object]) -> None:
        if live_feed is not None:
            live_feed.publish(
                LiveEvent(type=event_type, payload=payload, campaign_id=str(campaign_id))
            )

    async with session_factory() as session:
        gate, _ = await evaluate_campaign(session, campaign_id)
        if gate.blocked_by:
            # §13.1: blocking and non-overridable. Marked PAUSED rather than
            # cancelled -- the block may be "outside the calling window", which
            # is true at 21:05 and false at 09:00 tomorrow, and cancelling a
            # campaign for being late would lose the list. The panel's gate
            # screen recomputes and names the failing checks.
            await _mark(session, campaign_id, CampaignStatus.PAUSED)
            await session.commit()
            log.error(
                "campaign.blocked",
                campaign_id=str(campaign_id),
                checks=[check.value for check in gate.blocked_by],
            )
            raise CampaignBlocked(list(gate.blocked_by))

        campaign = await session.get(Campaign, campaign_id)
        if campaign is None:  # pragma: no cover -- evaluate_campaign raises first
            raise LookupError(str(campaign_id))
        caller_id = campaign.caller_id_number or ""
        settings = CampaignSettings(
            is_promotional=campaign.is_promotional,
            caller_id=caller_id,
            dlt_entity_id=campaign.dlt_entity_id,
            dlt_template_id=campaign.dlt_template_id,
        )
        contacts = await load_contacts(session, campaign)
        concurrency = max(
            1, min(max_concurrent, campaign.max_concurrent_calls or max_concurrent)
        )
        await _mark(session, campaign_id, CampaignStatus.RUNNING)
        await session.commit()
    announce(CAMPAIGN_UPDATED, {"id": str(campaign_id), "status": CampaignStatus.RUNNING.value})

    async def recheck(contact: Contact) -> Check | None:
        """Re-run the per-contact checks against fresh rows.

        A fresh session per contact, deliberately: the point is to see writes
        another call made ten minutes ago, and a long-lived session would show
        the snapshot from campaign start.
        """
        async with session_factory() as db:
            current = await _reload_contact(db, campaign_id, contact.farmer_id)
        if current is None:
            # The contact vanished -- deleted, or the campaign was edited under
            # us. Not dialling is the only safe reading.
            return Check.INTERNAL_DNC
        return first_failure(current, settings)

    async def dial(contact: Contact) -> str:
        async with session_factory() as db:
            number = await _plaintext_number(db, contact.farmer_id, campaign_id=campaign_id)
            # Recorded before the call. A crash between here and the provider's
            # response leaves a contact marked attempted, which costs one
            # missed call; the other order costs a duplicate.
            attempt = await _record_attempt(db, campaign_id, contact.farmer_id)
            await db.commit()
        announce(CONTACT_UPDATED, attempt.card("in_call", None))
        try:
            return await originate(number, caller_id, f"contact:{attempt.contact_id}")
        except Exception:
            async with session_factory() as db:
                await _attempt_failed(db, attempt.contact_id)
                await db.commit()
            announce(CONTACT_UPDATED, attempt.card("no_answer", "failed"))
            raise

    dialer = Dialer(
        dial=dial,
        recheck=recheck,
        max_concurrent=concurrency,
        calls_per_minute=calls_per_minute,
        state=state or DialerState(),
        control=control,
    )
    run = await dialer.run(campaign_id, contacts)

    report.dialled = run.dialled
    report.skipped = run.skipped
    report.failed = sum(1 for outcome in run.outcomes if outcome.error)
    report.stopped_reason = run.stopped_reason

    async with session_factory() as session:
        # Paused stays paused: a campaign stopped by the window or an operator
        # is resumable, and marking it complete would lose the rest of the list.
        final = (
            CampaignStatus.COMPLETED
            if run.stopped_reason is None
            else CampaignStatus.PAUSED
        )
        await _mark(session, campaign_id, final)
        await session.commit()
    announce(CAMPAIGN_UPDATED, {"id": str(campaign_id), "status": final.value})

    log.info("campaign.run_complete", summary=report.summary())
    return report


# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #


async def _mark(session: AsyncSession, campaign_id: uuid.UUID, status: CampaignStatus) -> None:
    await session.execute(
        update(Campaign).where(Campaign.id == campaign_id).values(status=status)
    )


async def _reload_contact(
    session: AsyncSession, campaign_id: uuid.UUID, farmer_id: uuid.UUID
) -> Contact | None:
    """One contact's current gate input.

    Goes through the same loader the approval screen uses, filtered to one
    farmer. Re-deriving the joins here is how the two copies drift, and the
    direction they drift in is "no consent row" becoming "proceed".
    """
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        return None
    for contact in await load_contacts(session, campaign, farmer_ids=[farmer_id]):
        return contact
    return None


@dataclass(frozen=True, slots=True)
class _StoredNumber:
    """Just the ciphertext, so the plaintext never sits on a mutable object."""

    phone_enc: bytes


async def _plaintext_number(
    session: AsyncSession, farmer_id: uuid.UUID, *, campaign_id: uuid.UUID
) -> str:
    """Decrypt one farmer's number, and record that we did.

    §17 makes this a privileged, audited operation. The audit row is the answer
    to "who saw this number and why", and a campaign dial is a legitimate why --
    but only if it is written down.

    The join through ``campaign_contacts`` is the real destination guard for
    this path. §17's rule is that a dialled number must come from an
    operator-approved list; ``assert_approved_destination`` enforces that when
    the destination arrives as a *value*, which cannot help here because we are
    the ones producing the value. Requiring the farmer to actually be on this
    campaign's contact list is the same guarantee at the only point where it
    can still be checked.
    """
    row = (
        await session.execute(
            select(Farmer.phone_enc)
            .join(CampaignContact, CampaignContact.farmer_id == Farmer.id)
            .where(
                CampaignContact.campaign_id == campaign_id,
                Farmer.id == farmer_id,
                Farmer.deleted_at.is_(None),
            )
        )
    ).first()
    if row is None or not row[0]:
        # Not a LookupError the dialer should retry past: a farmer who is not
        # on the list must never be dialled by this campaign.
        raise PermissionError(f"farmer {farmer_id} is not a contact of campaign {campaign_id}")
    farmer = _StoredNumber(phone_enc=row[0])

    number = str(get_cipher().decrypt(farmer.phone_enc))
    await append_audit(
        session,
        action=AuditAction.PHONE_DECRYPT,
        resource_type="farmer",
        resource_id=str(farmer_id),
        # The reason, never the number. An audit log that recorded the
        # plaintext would defeat the encryption it exists to police.
        after={"purpose": "outbound_dial", "campaign_id": str(campaign_id)},
    )
    return number


@dataclass(frozen=True, slots=True)
class _Attempt:
    """What the panel needs to paint the contact's card."""

    contact_id: uuid.UUID
    farmer_name: str | None
    last4: str
    attempts: int

    def card(self, status: str, outcome: str | None) -> dict[str, object]:
        return {
            "id": str(self.contact_id),
            "farmerName": self.farmer_name,
            "last4": self.last4,
            "status": status,
            "outcome": outcome,
            "dtmf": None,
            "callId": None,
            "attempts": self.attempts,
        }


async def _record_attempt(
    session: AsyncSession, campaign_id: uuid.UUID, farmer_id: uuid.UUID
) -> _Attempt:
    """Count the attempt and mark the contact as being dialled.

    The status matters beyond bookkeeping: it is how the media path finds
    the contact when a provider drops the custom field, and it is what the
    reconciliation job uses to spot a call that never connected.
    """
    row = (
        await session.execute(
            select(CampaignContact.id, Farmer.full_name, Farmer.phone_last4)
            .join(Farmer, Farmer.id == CampaignContact.farmer_id)
            .where(
                CampaignContact.campaign_id == campaign_id,
                CampaignContact.farmer_id == farmer_id,
            )
        )
    ).first()
    if row is None:
        raise PermissionError(f"farmer {farmer_id} is not a contact of campaign {campaign_id}")
    contact_id, farmer_name, last4 = row
    attempts = await session.scalar(
        update(CampaignContact)
        .where(CampaignContact.id == contact_id)
        .values(
            attempts=CampaignContact.attempts + 1,
            last_attempt_at=datetime.now(UTC),
            status=ContactStatus.DIALING,
            outcome=None,
            dtmf_response=None,
            call_id=None,
        )
        .returning(CampaignContact.attempts)
    )
    return _Attempt(
        contact_id=contact_id,
        farmer_name=farmer_name,
        last4=str(last4),
        attempts=int(attempts or 0),
    )


async def _attempt_failed(session: AsyncSession, contact_id: uuid.UUID) -> None:
    """The provider refused the dial. Not a no-answer: nothing rang."""
    await session.execute(
        update(CampaignContact)
        .where(CampaignContact.id == contact_id)
        .values(status=ContactStatus.FAILED, outcome="failed")
    )


async def campaign_ids_ready(
    session: AsyncSession, *, now: datetime | None = None
) -> list[uuid.UUID]:
    """Approved campaigns whose scheduled start has arrived.

    Scheduled, not immediate: §13.1 has an operator approve a campaign and set
    a time, and the dialer's own window check is what keeps a late start from
    running past 21:00.
    """
    when = now or datetime.now(UTC)
    rows = await session.scalars(
        select(Campaign.id).where(
            Campaign.status == CampaignStatus.APPROVED,
            Campaign.deleted_at.is_(None),
            Campaign.scheduled_start <= when,
        )
    )
    return list(rows)


__all__ = (
    "CampaignBlocked",
    "CampaignRunReport",
    "campaign_ids_ready",
    "run_campaign",
)
