"""The outbound compliance gate (§13.1) -- blocking and non-overridable.

Eight checks, all of which must pass before a campaign can advance out of DRAFT.
§13.1 is unusually specific about the character of this gate: it is *automatic*,
*blocking* and *non-overridable*, and the UI must show exactly how many contacts
each check removed.

That last requirement is not cosmetic. An operator who sees "1,200 contacts →
340 eligible" with no breakdown will assume the gate is broken and look for a
way around it. One who sees "DND removed 610, expired consent removed 190,
duplicate suppression removed 60" understands what happened and fixes the
consent problem instead. So :class:`GateReport` carries a per-check count, and
the checks run to completion rather than short-circuiting on the first failure
-- a gate that stops at the first problem tells the operator to fix one thing,
then reports the next one tomorrow.

**Non-overridable means there is no parameter.** There is deliberately no
``force``, no ``skip_checks``, no ``dry_run=False`` that bypasses. A campaign
whose contacts fail the gate does not run, and the only way to change that is to
change the contacts. Every override mechanism ever added to a system like this
has eventually been used routinely.

The most consequential check is the **caller ID series**, and it is the one most
likely to be wrong in a way nobody notices: a promotional campaign dialled from
a plain 10-digit mobile is a TCCCPR violation on every single call, and the
calls connect perfectly.

Lives in ``uaagro_domain`` rather than in the voice worker because all three
services need it and none of them should have to install the others to get it:
the API evaluates the gate to approve a campaign, the background worker
re-evaluates it per contact at dial time, and the voice worker enforces the
opt-out mid-call. It is pure policy -- no database, no audio, no I/O -- which
is what makes one copy possible.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum

import structlog

from .enums import ConsentType
from .timezone import now_ist

log = structlog.get_logger(__name__)

#: §13.1 and §18: 09:00-21:00 recipient local time, enforced by the dialer at
#: dial time and not by the schedule alone. A campaign scheduled for 20:55 that
#: is still dialling at 21:05 is in violation, and only a dial-time check
#: catches that.
WINDOW_OPEN = time(9, 0)
WINDOW_CLOSE = time(21, 0)

#: §13.1 frequency caps. Defaults -- §15 puts them in the admin panel.
MAX_ATTEMPTS_PER_CONTACT = 3
MIN_HOURS_BETWEEN_ATTEMPTS = 4
MAX_CONTACTS_PER_FARMER_PER_WEEK = 2

#: §13.1: promotional voice traffic must originate from a 140-series CLI.
#:
#: Verified 31 August 2026: the 1600 series that §2 mentions is restricted to
#: entities regulated by the RBI, SEBI, IRDAI or PFRDA, and to government. An
#: agri retailer does not qualify, so 140 is the series available here and the
#: spec's reference to 1600 does not apply to this operator.
PROMOTIONAL_CLI_PREFIX = "140"


class Check(StrEnum):
    """The eight §13.1 checks, in the order the report lists them."""

    CONSENT = "consent"
    DND = "dnd"
    INTERNAL_DNC = "internal_dnc"
    CALLER_ID_SERIES = "caller_id_series"
    DLT_REGISTRATION = "dlt_registration"
    CALLING_WINDOW = "calling_window"
    FREQUENCY_CAP = "frequency_cap"
    DUPLICATE_SUPPRESSION = "duplicate_suppression"


#: Checks that fail the *campaign* rather than removing contacts. A campaign
#: with a mobile CLI is not a campaign with fewer eligible contacts; it is a
#: campaign that must not run at all.
CAMPAIGN_LEVEL: frozenset[Check] = frozenset(
    {Check.CALLER_ID_SERIES, Check.DLT_REGISTRATION, Check.CALLING_WINDOW}
)


@dataclass(frozen=True, slots=True)
class Contact:
    """One campaign contact, with everything the gate needs to judge it.

    Assembled by the caller from the database. Passed in as a value rather than
    queried here so the gate is pure: a gate that issued its own queries could
    not be tested against the awkward combinations, which is where it matters.
    """

    farmer_id: uuid.UUID
    phone_hash: bytes
    #: Latest unexpired promotional_voice consent, if any.
    consent_type: ConsentType | None = None
    consent_expires_at: datetime | None = None
    consent_revoked: bool = False
    is_dnd: bool = False
    internal_dnc: bool = False
    attempts_this_campaign: int = 0
    last_attempt_at: datetime | None = None
    contacts_this_week: int = 0
    #: True when another live campaign already holds this farmer this week.
    in_another_live_campaign: bool = False


@dataclass(frozen=True, slots=True)
class CampaignSettings:
    """The campaign-level facts the gate judges."""

    is_promotional: bool
    caller_id: str
    dlt_entity_id: str | None = None
    dlt_template_id: str | None = None
    #: When the dialer would actually dial, in IST. None means "now".
    dial_at: datetime | None = None


@dataclass
class GateReport:
    """What the gate found, per check (§13.1).

    ``removed`` is the number the admin panel shows next to each check. Zero is
    meaningful and different from absent: "DND removed 0" says the scrub ran and
    found nothing, which is what an operator needs to distinguish from a scrub
    that did not run.
    """

    removed: dict[Check, int] = field(default_factory=dict)
    blocked_by: list[Check] = field(default_factory=list)
    eligible: list[Contact] = field(default_factory=list)
    total: int = 0

    @property
    def passed(self) -> bool:
        """Whether the campaign may advance.

        Requires both no campaign-level block *and* at least one eligible
        contact. A campaign with zero eligible contacts is not a campaign that
        passed the gate with nothing to do -- it is one whose contact list is
        wrong, and approving it wastes a reviewer's time.
        """
        return not self.blocked_by and bool(self.eligible)

    @property
    def eligible_count(self) -> int:
        return len(self.eligible)

    def summary(self) -> str:
        """The line the admin panel and the CLI both show."""
        parts = [f"{self.total} contacts -> {self.eligible_count} eligible"]
        for check in Check:
            count = self.removed.get(check, 0)
            if count:
                parts.append(f"{check.value} removed {count}")
        if self.blocked_by:
            parts.append(
                "BLOCKED: " + ", ".join(c.value for c in self.blocked_by)
            )
        return "; ".join(parts)


def within_calling_window(when: datetime | None = None) -> bool:
    """§13.1 and §18: 09:00-21:00 IST.

    Evaluated in IST rather than UTC. The window is a rule about when a farmer's
    phone rings, and a UTC comparison would put the boundary at 03:30 and 15:30
    local -- which would be wrong twice a day, quietly, in the direction of
    calling people at night.
    """
    moment = (when or now_ist()).astimezone(now_ist().tzinfo)
    return WINDOW_OPEN <= moment.time() <= WINDOW_CLOSE


def is_valid_promotional_cli(caller_id: str) -> bool:
    """§13.1: promotional traffic uses a 140-series CLI.

    The check that is most likely to be wrong in a way nobody notices, because
    a campaign dialled from a plain mobile connects perfectly and violates
    TCCCPR on every call.
    """
    digits = "".join(c for c in caller_id if c.isdigit())
    # Tolerate a 91 country code before the series.
    if digits.startswith("91") and len(digits) > 10:
        digits = digits[2:]
    return digits.startswith(PROMOTIONAL_CLI_PREFIX)


def evaluate(
    contacts: Sequence[Contact],
    campaign: CampaignSettings,
    *,
    now: datetime | None = None,
) -> GateReport:
    """Run all eight checks. Never raises, never partially applies.

    Every check runs even after one has already blocked the campaign, because
    §13.1 requires the panel to show what each removed -- and an operator shown
    one problem at a time fixes them one per day.
    """
    moment = now or now_ist()
    report = GateReport(total=len(contacts))
    for check in Check:
        report.removed[check] = 0

    # -- campaign-level checks ------------------------------------------- #

    if campaign.is_promotional and not is_valid_promotional_cli(campaign.caller_id):
        # Not a contact filter. A campaign with the wrong CLI is in violation on
        # every call it places, so it does not run at all.
        report.blocked_by.append(Check.CALLER_ID_SERIES)

    if campaign.is_promotional and not (
        campaign.dlt_entity_id and campaign.dlt_template_id
    ):
        report.blocked_by.append(Check.DLT_REGISTRATION)

    if not within_calling_window(campaign.dial_at or moment):
        report.blocked_by.append(Check.CALLING_WINDOW)

    # -- per-contact checks ----------------------------------------------- #

    for contact in contacts:
        reasons = _contact_failures(contact, campaign, moment)
        if reasons:
            for reason in reasons:
                report.removed[reason] += 1
            continue
        report.eligible.append(contact)

    log.info(
        "outbound.compliance_gate",
        total=report.total,
        eligible=report.eligible_count,
        blocked=[c.value for c in report.blocked_by],
        removed={c.value: n for c, n in report.removed.items() if n},
    )
    return report


def first_failure(
    contact: Contact, campaign: CampaignSettings, *, now: datetime | None = None
) -> Check | None:
    """The first reason this one contact must not be dialled, or None.

    The dialer's dial-time check (§13.1). ``evaluate`` answers "may this
    campaign run"; this answers "may I dial this person, right now" -- which is
    a different question ten hours into a campaign, and the one that decides
    whether a call is an offence.

    First failure rather than all of them: nothing downstream acts on the
    second reason, and the caller is a loop that runs once per dial.
    """
    failures = _contact_failures(contact, campaign, now or datetime.now(UTC))
    return failures[0] if failures else None


def _contact_failures(
    contact: Contact, campaign: CampaignSettings, now: datetime
) -> list[Check]:
    """Every reason this contact is excluded.

    All of them, not the first. The counts in the report are what tell an
    operator whether the consent problem or the DND problem is the one worth
    fixing, and a first-match-wins loop attributes every exclusion to whichever
    check happens to be evaluated first.
    """
    failures: list[Check] = []

    if campaign.is_promotional:
        # §18: consent is not permanent, and an expired record excludes.
        valid = (
            contact.consent_type is ConsentType.PROMOTIONAL_VOICE
            and not contact.consent_revoked
            and contact.consent_expires_at is not None
            and contact.consent_expires_at > now
        )
        if not valid:
            failures.append(Check.CONSENT)

        # §18: a prior customer relationship does not exempt a DND-registered
        # number. Checked only for promotional traffic -- a transactional call
        # about an order the farmer placed is not covered.
        if contact.is_dnd:
            failures.append(Check.DND)

    # Internal DNC applies to everything. Someone who said "don't call me" did
    # not mean "except about orders".
    if contact.internal_dnc:
        failures.append(Check.INTERNAL_DNC)

    if contact.attempts_this_campaign >= MAX_ATTEMPTS_PER_CONTACT:
        failures.append(Check.FREQUENCY_CAP)
    elif contact.last_attempt_at is not None and now - contact.last_attempt_at < timedelta(
        hours=MIN_HOURS_BETWEEN_ATTEMPTS
    ):
        failures.append(Check.FREQUENCY_CAP)
    elif contact.contacts_this_week >= MAX_CONTACTS_PER_FARMER_PER_WEEK:
        failures.append(Check.FREQUENCY_CAP)

    if contact.in_another_live_campaign:
        failures.append(Check.DUPLICATE_SUPPRESSION)

    return failures


def next_retry(
    outcome: str, *, last_attempt: datetime, attempts: int
) -> datetime | None:
    """§13.3's retry policy. ``None`` means never again.

    The table is the specification; this is it in code so that "opted out" and
    "number invalid" cannot be retried by an operator setting a schedule.
    """
    match outcome:
        case "no_answer":
            if attempts >= MAX_ATTEMPTS_PER_CONTACT:
                return None
            # §13.3: rotate the time-of-day band. A farmer who does not answer
            # at 10am three days running is not refusing -- they are in a field
            # at 10am.
            band = timedelta(hours=24 + (attempts * 3))
            return last_attempt + band
        case "busy":
            return last_attempt + timedelta(hours=4)
        case "answering_machine":
            return None if attempts >= 1 else last_attempt + timedelta(hours=24)
        case "invalid_number" | "declined" | "opted_out" | "transferred":
            # Each for a different reason, and each permanent within this
            # campaign. Opted out is permanent across all of them, which the
            # gate enforces separately through internal_dnc.
            return None
        case _:
            return None


def week_of(when: datetime) -> tuple[int, int]:
    """ISO year and week, for the rolling per-farmer contact cap."""
    iso: date = when.astimezone(UTC).date()
    year, week, _ = iso.isocalendar()
    return year, week


__all__ = (
    "MAX_ATTEMPTS_PER_CONTACT",
    "MAX_CONTACTS_PER_FARMER_PER_WEEK",
    "MIN_HOURS_BETWEEN_ATTEMPTS",
    "PROMOTIONAL_CLI_PREFIX",
    "WINDOW_CLOSE",
    "WINDOW_OPEN",
    "CampaignSettings",
    "Check",
    "Contact",
    "GateReport",
    "evaluate",
    "first_failure",
    "is_valid_promotional_cli",
    "next_retry",
    "week_of",
    "within_calling_window",
)
