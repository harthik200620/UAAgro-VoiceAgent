"""Assembling §13.1's gate input from the database.

The gate itself is a pure function in ``uaagro_domain.compliance`` --
contacts in, report out, no I/O. That is deliberate: a gate that issued its own
queries could not be tested against the awkward combinations, and the awkward
combinations are the entire point of a compliance check.

So the querying lives here. This module's only job is to turn campaign rows into
:class:`Contact` values, and it is written to be boring: every field the gate
judges is fetched explicitly, and anything it cannot determine defaults to the
value that *excludes* the contact rather than the one that includes them.

That default direction is the important part. A join that silently returns no
consent row must mean "no consent", never "consent unknown, proceed" -- and the
difference between those two readings is a TCCCPR violation per call.

Lives in ``uaagro_db`` rather than in the API service layer because the
background worker needs exactly these queries too: §13.1 has the gate
re-evaluated per contact at *dial* time, not only at approval, and a second
copy of these joins in the worker is how "no consent row" quietly becomes
"consent unknown, proceed" in one of them.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_domain.compliance import (
    CampaignSettings,
    Contact,
    GateReport,
    evaluate,
    week_of,
)
from uaagro_domain.enums import ConsentType
from uaagro_domain.errors import NotFoundError

from .models import Campaign, CampaignContact, ConsentRecord, DndStatus, Farmer

log = structlog.get_logger(__name__)


async def evaluate_campaign(
    session: AsyncSession, campaign_id: uuid.UUID
) -> tuple[GateReport, str]:
    """Run §13.1's gate over one campaign's contacts.

    Returns:
        The report, and the id of the user who created the campaign -- which
        the approval screen needs for §13.1's four-eyes rule and which only the
        server can be trusted to supply.
    """
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFoundError(resource="campaign", identifier=str(campaign_id))

    contacts = await load_contacts(session, campaign)
    settings = CampaignSettings(
        is_promotional=campaign.is_promotional,
        caller_id=campaign.caller_id_number or "",
        dlt_entity_id=campaign.dlt_entity_id,
        dlt_template_id=campaign.dlt_template_id,
        # The scheduled time, or now. §13.1 enforces the window at dial time
        # too; this is the check that stops an out-of-window campaign being
        # *approved*, which is cheaper than stopping it mid-run.
        dial_at=campaign.scheduled_start,
    )

    report = evaluate(contacts, settings)
    log.info(
        "campaign.gate_evaluated",
        campaign_id=str(campaign_id),
        total=report.total,
        eligible=report.eligible_count,
        blocked=[c.value for c in report.blocked_by],
    )
    return report, str(campaign.created_by or "")


async def load_contacts(
    session: AsyncSession,
    campaign: Campaign,
    *,
    farmer_ids: Sequence[uuid.UUID] | None = None,
) -> list[Contact]:
    """Everything the gate needs about each contact, in three queries.

    ``farmer_ids`` narrows it to specific contacts. That is what the dialer
    uses to re-check one person at dial time (§13.1) -- through this same
    loader rather than a second set of joins, because the way two copies drift
    is that one of them starts reading a missing consent row as "unknown,
    proceed".

    Three rather than one join, because the shapes differ: consent is a
    latest-row-per-farmer question, DND is keyed by phone hash rather than
    farmer id, and the weekly contact count spans every campaign. Forcing them
    into one statement would produce a query whose correctness nobody could
    check by reading it.
    """
    now = datetime.now(UTC)

    rows = (
        await session.execute(
            select(CampaignContact, Farmer.phone_hash)
            .join(Farmer, CampaignContact.farmer_id == Farmer.id)
            .where(
                CampaignContact.campaign_id == campaign.id,
                Farmer.deleted_at.is_(None),
                *(
                    [CampaignContact.farmer_id.in_(list(farmer_ids))]
                    if farmer_ids is not None
                    else []
                ),
            )
        )
    ).all()
    if not rows:
        return []

    farmer_ids = [row[0].farmer_id for row in rows]
    phone_hashes = [row[1] for row in rows]

    consents = await _latest_consents(session, farmer_ids, now)
    blocked = await _dnd_by_hash(session, phone_hashes)
    weekly = await _contacts_this_week(session, farmer_ids, now)
    elsewhere = await _in_another_live_campaign(session, farmer_ids, campaign.id)

    contacts: list[Contact] = []
    for contact_row, phone_hash in rows:
        consent = consents.get(contact_row.farmer_id)
        dnd = blocked.get(phone_hash)
        contacts.append(
            Contact(
                farmer_id=contact_row.farmer_id,
                phone_hash=phone_hash,
                # None when no row exists, which the gate reads as "no consent".
                # The alternative reading -- unknown, therefore proceed -- is a
                # violation per call.
                consent_type=consent.consent_type if consent else None,
                consent_expires_at=consent.expires_at if consent else None,
                consent_revoked=bool(consent and consent.revoked_at is not None),
                is_dnd=bool(dnd and dnd.is_dnd),
                internal_dnc=bool(dnd and dnd.internal_dnc),
                attempts_this_campaign=contact_row.attempts,
                last_attempt_at=contact_row.last_attempt_at,
                contacts_this_week=weekly.get(contact_row.farmer_id, 0),
                in_another_live_campaign=contact_row.farmer_id in elsewhere,
            )
        )
    return contacts


async def _latest_consents(
    session: AsyncSession, farmer_ids: list[uuid.UUID], now: datetime
) -> dict[uuid.UUID, ConsentRecord]:
    """The most recent promotional-voice consent per farmer.

    Most recent rather than any: §18 makes consent non-permanent and
    re-acquirable, so a farmer can have an expired grant *and* a current one.
    Taking any row would let an old expired record mask a valid renewal, or the
    reverse.
    """
    ranked = (
        select(
            ConsentRecord.id,
            func.row_number()
            .over(
                partition_by=ConsentRecord.farmer_id,
                order_by=ConsentRecord.granted_at.desc(),
            )
            .label("rank"),
        )
        .where(
            ConsentRecord.farmer_id.in_(farmer_ids),
            ConsentRecord.consent_type == ConsentType.PROMOTIONAL_VOICE,
        )
        .subquery()
    )
    latest = select(ranked.c.id).where(ranked.c.rank == 1)
    rows = (
        await session.scalars(select(ConsentRecord).where(ConsentRecord.id.in_(latest)))
    ).all()

    # Expiry is the gate's decision, not this function's. Filtering expired rows
    # out here would turn "consent expired" into "consent missing", and the
    # report would attribute the exclusion to the wrong check -- which is
    # exactly the breakdown §13.1 requires the panel to show correctly.
    _ = now
    return {row.farmer_id: row for row in rows}


async def _dnd_by_hash(
    session: AsyncSession, phone_hashes: list[bytes]
) -> dict[bytes, DndStatus]:
    rows = (
        await session.scalars(
            select(DndStatus).where(DndStatus.phone_hash.in_(phone_hashes))
        )
    ).all()
    return {row.phone_hash: row for row in rows}


async def _contacts_this_week(
    session: AsyncSession, farmer_ids: list[uuid.UUID], now: datetime
) -> dict[uuid.UUID, int]:
    """§13.1's rolling per-farmer cap, across **all** campaigns.

    A farmer contacted twice this week by two different campaigns has still
    been contacted twice, so this deliberately does not filter by campaign.
    """
    year, week = week_of(now)
    start = datetime.fromisocalendar(year, week, 1).replace(tzinfo=UTC)
    rows = (
        await session.execute(
            select(CampaignContact.farmer_id, func.count())
            .where(
                CampaignContact.farmer_id.in_(farmer_ids),
                CampaignContact.last_attempt_at >= start,
            )
            .group_by(CampaignContact.farmer_id)
        )
    ).all()
    return {farmer_id: int(count) for farmer_id, count in rows}


async def _in_another_live_campaign(
    session: AsyncSession, farmer_ids: list[uuid.UUID], campaign_id: uuid.UUID
) -> set[uuid.UUID]:
    """§13.1's duplicate suppression.

    A week rather than "ever": the point is to stop two campaigns reaching the
    same farmer in the same few days, not to permanently exclude anyone who has
    ever been in a campaign.
    """
    since = datetime.now(UTC) - timedelta(days=7)
    rows = (
        await session.scalars(
            select(CampaignContact.farmer_id)
            .join(Campaign, CampaignContact.campaign_id == Campaign.id)
            .where(
                CampaignContact.farmer_id.in_(farmer_ids),
                CampaignContact.campaign_id != campaign_id,
                Campaign.status.in_(["running", "scheduled"]),
                Campaign.created_at >= since,
            )
        )
    ).all()
    # A contact row can have a null farmer_id -- a raw uploaded list before it
    # is matched to a farmer -- so the nulls are dropped rather than carried
    # into a set that is compared against real ids.
    return {farmer_id for farmer_id in rows if farmer_id is not None}


__all__ = ("evaluate_campaign",)
