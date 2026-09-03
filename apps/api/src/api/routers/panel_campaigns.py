"""Outbound campaigns as the panel runs them (§13.1, §13.3, §15.1).

The operator pastes numbers, picks the script, attests consent, and the rest
is bookkeeping this module does correctly so nobody has to remember it: each
number becomes (or finds) a farmer with an encrypted phone and a hashed
lookup key, a consent record names who attested and when, the compliance gate
runs over the list, and the campaign waits for a second pair of eyes.

Three rules that are enforced here rather than described:

**Consent is attested, and the attestation is audited.** A promotional call
without consent is an offence per call. The panel cannot submit without the
attestation, the API refuses without it, and the audit row records who said
so -- which is the accountability the law actually asks for.

**A creator does not approve their own campaign.** The four-eyes rule from
§13.1. The owner's account can, because a business with one operator would
otherwise have no way to run at all, and that override is written on the row
(``self_approved``) and audited under a distinct action.

**Pause, resume and stop are instructions to the dialer, not edits to a
row.** The dialer runs in another process and reads its instruction before
every dial; the status on the row follows.

The import path is shared with the panel's quick dial (``panel_dial``): one
set of joins between a pasted line and a consent record, so the fast path
cannot drift into a weaker one.
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.audit import append_audit
from uaagro_db.campaigns import evaluate_campaign, load_contacts
from uaagro_db.crypto import get_cipher
from uaagro_db.models import (
    AgentConfig,
    Call,
    Campaign,
    CampaignContact,
    ConsentRecord,
    Farmer,
    User,
)
from uaagro_domain import livefeed
from uaagro_domain.compliance import CampaignSettings, Check, first_failure
from uaagro_domain.enums import (
    AuditAction,
    CampaignStatus,
    ConsentChannel,
    ConsentType,
    ContactStatus,
    ExclusionReason,
    FlowType,
    Role,
    TelephonyProvider,
)
from uaagro_domain.errors import ComplianceError, NotFoundError, ValidationError
from uaagro_domain.livefeed import LiveEvent
from uaagro_domain.phone import normalise_msisdn
from uaagro_domain.settings import Settings, get_defaults, get_settings

from ..security.deps import DbDep, Principal, PrincipalDep, require_role
from ..services import jobs
from ..services.contact_import import extract_contacts
from ..services.events import relay, streaming_response
from ._panel import as_uuid, iso, panel_status, short_session

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin/campaigns", tags=["panel"])

#: How many contacts one pasted list may carry. Above this the operator
#: should split the list; below it the import completes in one request.
MAX_CONTACTS = 5000

_STATUS_TO_BLOCK = {
    Check.CONSENT: ExclusionReason.NO_CONSENT,
    Check.DND: ExclusionReason.DND_REGISTERED,
    Check.INTERNAL_DNC: ExclusionReason.INTERNAL_DNC,
    Check.FREQUENCY_CAP: ExclusionReason.FREQUENCY_CAP,
    Check.DUPLICATE_SUPPRESSION: ExclusionReason.DUPLICATE_ACTIVE_CAMPAIGN,
}

#: The panel's card states, by the counter each one lands in. A ringing
#: contact and one in a call are both "in progress" to the tally.
_STATE_TO_COUNT = {
    "waiting": "waiting",
    "ringing": "inCall",
    "in_call": "inCall",
    "done": "done",
    "no_answer": "noAnswer",
    "removed": "removed",
}


class FourEyesError(ComplianceError):
    code = "four_eyes"


class CampaignBlockedError(ComplianceError):
    code = "campaign_blocked"


class ConsentRequiredError(ValidationError):
    code = "consent_required"


# --------------------------------------------------------------------------- #
# Shapes
# --------------------------------------------------------------------------- #


class CampaignCounts(BaseModel):
    total: int
    done: int
    inCall: int
    noAnswer: int
    waiting: int
    pressed1: int
    pressed2: int
    talked: int
    optedOut: int
    removed: int


class CampaignSummary(BaseModel):
    id: str
    name: str
    status: str
    flowId: str | None
    flowName: str | None
    flowVersion: int | None
    createdAt: str
    createdByName: str | None
    startedAt: str | None
    maxConcurrent: int
    windowStart: str
    windowEnd: str
    counts: CampaignCounts
    firstReplyP50Ms: int | None
    canApprove: bool
    blockedBy: list[str]


class Contact(BaseModel):
    id: str
    farmerName: str | None
    last4: str
    status: str
    outcome: str | None
    dtmf: str | None
    callId: str | None
    attempts: int
    lastAttemptAt: str | None
    removedReason: str | None
    #: In simulator mode, the worker's browser page that picks this call up;
    #: null once it is answered, and always null when a real carrier dials.
    answerUrl: str | None


class CampaignDetail(CampaignSummary):
    contacts: list[Contact]


class CreateCampaign(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    flowId: str
    numbers: str = Field(min_length=1)
    maxConcurrent: int = Field(default=10, ge=1, le=100)
    consentAttested: bool = False
    consentNote: str | None = Field(default=None, max_length=500)


class RemovedBy(BaseModel):
    check: str
    count: int


class CampaignCreated(BaseModel):
    campaign: CampaignSummary
    imported: int
    invalid: list[str]
    removedBy: list[RemovedBy]


class PatchCampaign(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    maxConcurrent: int | None = Field(default=None, ge=1, le=100)


# --------------------------------------------------------------------------- #
# Summaries
# --------------------------------------------------------------------------- #


async def _counts(
    db: AsyncSession, campaign_ids: list[uuid.UUID]
) -> dict[uuid.UUID, CampaignCounts]:
    in_call = CampaignContact.call_id.is_not(None)
    rows = (
        await db.execute(
            select(
                CampaignContact.campaign_id,
                CampaignContact.status,
                CampaignContact.outcome,
                in_call,
                func.count(),
            )
            .where(CampaignContact.campaign_id.in_(campaign_ids))
            .group_by(
                CampaignContact.campaign_id,
                CampaignContact.status,
                CampaignContact.outcome,
                in_call,
            )
        )
    ).all()
    tally: dict[uuid.UUID, dict[str, int]] = {}
    for campaign_id, status, outcome, linked, count in rows:
        bucket = tally.setdefault(campaign_id, {})
        bucket["total"] = bucket.get("total", 0) + count
        key = _STATE_TO_COUNT[panel_status(status, in_call=bool(linked))]
        bucket[key] = bucket.get(key, 0) + count
        if outcome in ("pressed_1", "pressed_2", "talked", "opted_out"):
            camel = {
                "pressed_1": "pressed1",
                "pressed_2": "pressed2",
                "talked": "talked",
                "opted_out": "optedOut",
            }[outcome]
            bucket[camel] = bucket.get(camel, 0) + count
    return {
        campaign_id: CampaignCounts(
            total=bucket.get("total", 0),
            done=bucket.get("done", 0),
            inCall=bucket.get("inCall", 0),
            noAnswer=bucket.get("noAnswer", 0),
            waiting=bucket.get("waiting", 0),
            pressed1=bucket.get("pressed1", 0),
            pressed2=bucket.get("pressed2", 0),
            talked=bucket.get("talked", 0),
            optedOut=bucket.get("optedOut", 0),
            removed=bucket.get("removed", 0),
        )
        for campaign_id, bucket in tally.items()
    }


async def _first_reply_p50(db: AsyncSession, campaign_id: uuid.UUID) -> int | None:
    values = sorted(
        int(stats["first_reply_ms"])
        for stats in (
            await db.scalars(select(Call.latency_stats).where(Call.campaign_id == campaign_id))
        ).all()
        if isinstance(stats, dict) and stats.get("first_reply_ms") is not None
    )
    if not values:
        return None
    return values[max(0, min(len(values) - 1, round(0.5 * len(values) + 0.5) - 1))]


async def summaries(
    db: AsyncSession, campaigns: list[Campaign], principal: Principal
) -> list[CampaignSummary]:
    if not campaigns:
        return []
    counts = await _counts(db, [c.id for c in campaigns])
    flows = {
        row.id: row
        for row in (
            await db.scalars(
                select(AgentConfig).where(
                    AgentConfig.id.in_([c.agent_config_id for c in campaigns if c.agent_config_id])
                )
            )
        ).all()
    }
    creators = {
        row.id: row.full_name
        for row in (
            await db.scalars(
                select(User).where(User.id.in_([c.created_by for c in campaigns if c.created_by]))
            )
        ).all()
    }
    out = []
    for campaign in campaigns:
        flow = flows.get(campaign.agent_config_id) if campaign.agent_config_id else None
        blocked: list[str] = []
        if campaign.status in (
            CampaignStatus.DRAFT,
            CampaignStatus.PENDING_APPROVAL,
            CampaignStatus.APPROVED,
        ):
            report, _ = await evaluate_campaign(db, campaign.id)
            blocked = [check.value for check in report.blocked_by]
            if not report.eligible:
                blocked.append("no_eligible_contacts")
        out.append(
            CampaignSummary(
                id=str(campaign.id),
                name=campaign.name,
                status=campaign.status.value,
                flowId=str(campaign.agent_config_id) if campaign.agent_config_id else None,
                flowName=flow.name if flow else None,
                flowVersion=flow.version if flow else None,
                createdAt=campaign.created_at.isoformat(),
                createdByName=creators.get(campaign.created_by) if campaign.created_by else None,
                startedAt=iso(campaign.approved_at)
                if campaign.status is not CampaignStatus.PENDING_APPROVAL
                else None,
                maxConcurrent=campaign.max_concurrent_calls,
                windowStart=campaign.daily_window_start.strftime("%H:%M"),
                windowEnd=campaign.daily_window_end.strftime("%H:%M"),
                counts=counts.get(
                    campaign.id,
                    CampaignCounts(
                        total=0,
                        done=0,
                        inCall=0,
                        noAnswer=0,
                        waiting=0,
                        pressed1=0,
                        pressed2=0,
                        talked=0,
                        optedOut=0,
                        removed=0,
                    ),
                ),
                firstReplyP50Ms=await _first_reply_p50(db, campaign.id),
                canApprove=_may_approve(principal, campaign) and not blocked,
                blockedBy=blocked,
            )
        )
    return out


def _may_approve(principal: Principal, campaign: Campaign) -> bool:
    if campaign.status is not CampaignStatus.PENDING_APPROVAL:
        return False
    if not principal.has_role(Role.OPS_MANAGER):
        return False
    if campaign.created_by == principal.user_id:
        return principal.role is Role.SUPER_ADMIN
    return True


async def _campaign(db: AsyncSession, campaign_id: uuid.UUID) -> Campaign:
    campaign = await db.scalar(
        select(Campaign).where(Campaign.id == campaign_id, Campaign.deleted_at.is_(None))
    )
    if campaign is None:
        raise NotFoundError(resource="campaign", identifier=str(campaign_id))
    return campaign


def _answer_url(contact: CampaignContact, state: str, settings: Settings) -> str | None:
    """Where the browser picks this call up, while it is still ringing.

    Only in simulator mode: with a real carrier the farmer's phone rings and
    a page cannot answer it. Only while ringing: once a call is linked the
    conversation is under way, and a second page would start a second one.
    """
    if settings.telephony_provider is not TelephonyProvider.SIMULATOR or state != "ringing":
        return None
    return f"{settings.voice_worker_public_url.rstrip('/')}/dev/call?answer={contact.id}"


async def _contacts(db: AsyncSession, campaign_id: uuid.UUID) -> list[Contact]:
    settings = get_settings()
    rows = (
        await db.execute(
            select(CampaignContact, Farmer.full_name, Farmer.phone_last4)
            .join(Farmer, Farmer.id == CampaignContact.farmer_id)
            .where(CampaignContact.campaign_id == campaign_id)
            .order_by(CampaignContact.created_at, CampaignContact.id)
        )
    ).all()
    out: list[Contact] = []
    for contact, name, last4 in rows:
        state = panel_status(contact.status, in_call=contact.call_id is not None)
        out.append(
            Contact(
                id=str(contact.id),
                farmerName=name,
                last4=str(last4),
                status=state,
                outcome=contact.outcome,
                dtmf=contact.dtmf_response,
                callId=str(contact.call_id) if contact.call_id else None,
                attempts=int(contact.attempts or 0),
                lastAttemptAt=iso(contact.last_attempt_at),
                removedReason=contact.exclusion_reason.value if contact.exclusion_reason else None,
                answerUrl=_answer_url(contact, state, settings),
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Importing
# --------------------------------------------------------------------------- #


async def published_outbound_flow(db: AsyncSession, flow_id: str | None) -> AgentConfig:
    """The script a campaign will speak: the one named, or the one that is live."""
    if flow_id is None:
        live = await db.scalar(
            select(AgentConfig).where(
                AgentConfig.flow_type == FlowType.OUTBOUND,
                AgentConfig.is_published.is_(True),
                AgentConfig.deleted_at.is_(None),
            )
        )
        if live is None:
            raise ValidationError(
                "No outbound script is published.",
                remedy="Publish an outbound script under Flows first; a campaign only "
                "ever speaks a published version.",
            )
        return live
    flow = await db.scalar(
        select(AgentConfig).where(
            AgentConfig.id == as_uuid(flow_id, what="flowId"),
            AgentConfig.flow_type == FlowType.OUTBOUND,
            AgentConfig.deleted_at.is_(None),
        )
    )
    if flow is None:
        raise ValidationError("That script does not exist.", remedy="Pick a script from the list.")
    if not flow.is_published:
        raise ValidationError(
            f"{flow.name} v{flow.version} is a draft.",
            remedy="Publish the script first; a campaign only ever speaks a published version.",
        )
    return flow


@dataclass(frozen=True, slots=True)
class Imported:
    """What a pasted list became."""

    campaign: Campaign
    imported: int
    invalid: list[str]
    removed_by: list[tuple[str, int]]


async def import_campaign(
    db: AsyncSession,
    principal: Principal,
    *,
    name: str,
    flow: AgentConfig,
    numbers: str,
    max_concurrent: int,
    consent_note: str | None,
    source_type: str = "manual",
) -> Imported:
    """A pasted list into a campaign waiting for approval, gate already run.

    The path a campaign and a quick dial share: farmers found or created with
    an encrypted phone, a consent record naming who attested, contacts, and
    the scrub that marks the ones the gate removes.
    """
    parsed, invalid = _parse_numbers(numbers)
    if not parsed:
        raise ValidationError(
            "No usable phone numbers were found.",
            remedy="One contact per line: a ten-digit mobile number, or `name, number`.",
        )
    if len(parsed) > MAX_CONTACTS:
        raise ValidationError(
            f"{len(parsed)} numbers is more than one campaign takes.",
            remedy=f"Split the list into campaigns of up to {MAX_CONTACTS}.",
        )

    settings = get_settings()
    defaults = get_defaults()
    now = datetime.now(UTC)
    campaign = Campaign(
        organization_id=principal.organization_id,
        name=name,
        agent_config_id=flow.id,
        status=CampaignStatus.PENDING_APPROVAL,
        source_type=source_type,
        daily_window_start=_time(defaults.compliance.calling_window_start),
        daily_window_end=_time(defaults.compliance.calling_window_end),
        max_concurrent_calls=max_concurrent,
        caller_id_number=settings.outbound_cli_promotional,
        dlt_entity_id=settings.dlt_entity_id,
        dlt_template_id=settings.dlt_template_id,
        is_promotional=True,
        created_by=principal.user_id,
    )
    db.add(campaign)
    await db.flush()

    cipher = get_cipher()
    consent_days = defaults.compliance.consent_validity_days
    seen: set[bytes] = set()
    imported = 0
    for farmer_name, msisdn in parsed:
        phone_hash = cipher.hash(msisdn)
        if phone_hash in seen:
            continue
        seen.add(phone_hash)
        farmer = await db.scalar(select(Farmer).where(Farmer.phone_hash == phone_hash))
        if farmer is None:
            farmer = Farmer(
                organization_id=principal.organization_id,
                phone_hash=phone_hash,
                phone_enc=cipher.encrypt(msisdn),
                phone_last4=msisdn.last4,
                full_name=farmer_name,
                preferred_language=defaults.default_language,
                first_seen_at=now,
                created_by=principal.user_id,
            )
            db.add(farmer)
            await db.flush()
        elif farmer_name and not farmer.full_name:
            farmer.full_name = farmer_name

        has_consent = await db.scalar(
            select(ConsentRecord.id)
            .where(
                ConsentRecord.farmer_id == farmer.id,
                ConsentRecord.consent_type == ConsentType.PROMOTIONAL_VOICE,
                ConsentRecord.revoked_at.is_(None),
                (ConsentRecord.expires_at.is_(None)) | (ConsentRecord.expires_at > now),
            )
            .limit(1)
        )
        if has_consent is None:
            db.add(
                ConsentRecord(
                    farmer_id=farmer.id,
                    consent_type=ConsentType.PROMOTIONAL_VOICE,
                    channel=ConsentChannel.IN_STORE,
                    granted_at=now,
                    expires_at=now + timedelta(days=consent_days),
                    evidence={
                        "attested_by_user_id": str(principal.user_id),
                        "attested_at": now.isoformat(),
                        "via": "panel_import",
                        "campaign_id": str(campaign.id),
                        "note": (consent_note or "").strip()[:500],
                    },
                )
            )
        db.add(CampaignContact(campaign_id=campaign.id, farmer_id=farmer.id, phone_hash=phone_hash))
        imported += 1
    await db.flush()

    removed_by = await _scrub(db, campaign)

    await append_audit(
        db,
        action=AuditAction.CREATE,
        resource_type="campaign",
        resource_id=str(campaign.id),
        actor_user_id=principal.user_id,
        after={
            "name": campaign.name,
            "source": source_type,
            "flow": f"{flow.name} v{flow.version}",
            "contacts": imported,
            "invalid_lines": len(invalid),
            "consent_attested": True,
            "consent_note": (consent_note or "").strip()[:500],
            "removed_by": {check: count for check, count in removed_by},
        },
    )
    log.info(
        "campaign.created", campaign_id=str(campaign.id), contacts=imported, invalid=len(invalid)
    )
    return Imported(campaign=campaign, imported=imported, invalid=invalid, removed_by=removed_by)


async def _scrub(db: AsyncSession, campaign: Campaign) -> list[tuple[str, int]]:
    """Run the gate per contact and mark the ones it removes.

    Marked rather than deleted: the panel shows "removed by the do-not-call
    list" on the card, and the operator learns which farmers asked not to be
    called rather than wondering where two numbers went.
    """
    settings = CampaignSettings(
        is_promotional=campaign.is_promotional,
        caller_id=campaign.caller_id_number or "",
        dlt_entity_id=campaign.dlt_entity_id,
        dlt_template_id=campaign.dlt_template_id,
    )
    contacts = await load_contacts(db, campaign)
    rows = {
        row.farmer_id: row
        for row in (
            await db.scalars(
                select(CampaignContact).where(CampaignContact.campaign_id == campaign.id)
            )
        ).all()
    }
    removed: dict[str, int] = {}
    for contact in contacts:
        failure = first_failure(contact, settings)
        if failure is None or failure not in _STATUS_TO_BLOCK:
            continue
        row = rows.get(contact.farmer_id)
        if row is None:
            continue
        row.status = ContactStatus.SCRUBBED_OUT
        row.exclusion_reason = _STATUS_TO_BLOCK[failure]
        removed[failure.value] = removed.get(failure.value, 0) + 1
    await db.flush()
    return sorted(removed.items())


_NUMBER = re.compile(r"(?:\+?91[\s-]?)?0?[6-9]\d{9}")


def _parse_numbers(text: str) -> tuple[list[tuple[str | None, Any]], list[str]]:
    """One contact per line: ``number`` or ``name, number``.

    The number is whatever on the line looks like an Indian mobile; the name
    is the rest with separators trimmed. Bad lines come back masked -- the
    first three and last two digits -- because the response is shown on
    screen and a wrong number is still somebody's number.
    """
    parsed: list[tuple[str | None, Any]] = []
    invalid: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().strip(",;")
        if not line:
            continue
        match = _NUMBER.search(line.replace(" ", "").replace("-", "")) or _NUMBER.search(line)
        if match is None:
            invalid.append(_mask(line))
            continue
        try:
            msisdn = normalise_msisdn(match.group(0))
        except Exception:
            invalid.append(_mask(line))
            continue
        raw_name = re.sub(r"[\d+\-\s]{6,}", " ", line).strip(" ,;\t")
        name: str | None = " ".join(raw_name.split()) or None
        parsed.append((name, msisdn))
    return parsed, invalid


def _mask(line: str) -> str:
    digits = re.sub(r"\D", "", line)
    if len(digits) >= 6:
        return f"{digits[:3]}…{digits[-2:]}"
    return line[:12]


def _time(value: str) -> Any:
    from datetime import time as _t

    hours, minutes = value.split(":")[:2]
    return _t(int(hours), int(minutes))


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.get("", response_model=list[CampaignSummary])
async def list_campaigns(
    db: DbDep, principal: Annotated[Principal, require_role(Role.CENTRE_MANAGER)]
) -> list[CampaignSummary]:
    campaigns = list(
        (
            await db.scalars(
                select(Campaign)
                .where(Campaign.deleted_at.is_(None))
                .order_by(Campaign.created_at.desc())
            )
        ).all()
    )
    return await summaries(db, campaigns, principal)


@router.post("", response_model=CampaignCreated, status_code=201)
async def create_campaign(
    body: CreateCampaign,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> CampaignCreated:
    if not body.consentAttested:
        raise ConsentRequiredError(
            "The consent attestation is required.",
            remedy="Tick the box confirming these farmers agreed to promotional calls "
            "from UA Agro. Without recorded consent a promotional call is an offence.",
        )
    flow = await published_outbound_flow(db, body.flowId)
    imported = await import_campaign(
        db,
        principal,
        name=body.name.strip(),
        flow=flow,
        numbers=body.numbers,
        max_concurrent=body.maxConcurrent,
        consent_note=body.consentNote,
    )
    summary = (await summaries(db, [imported.campaign], principal))[0]
    return CampaignCreated(
        campaign=summary,
        imported=imported.imported,
        invalid=imported.invalid,
        removedBy=[RemovedBy(check=check, count=count) for check, count in imported.removed_by],
    )


class ExtractedContacts(BaseModel):
    """What a file offered, before anything is created from it."""

    lines: list[str]
    found: int
    skipped: int
    sheets: int


@router.post("/contacts/extract", response_model=ExtractedContacts)
async def extract(
    _: Annotated[Principal, require_role(Role.OPS_MANAGER)],
    file: Annotated[UploadFile, File()],
) -> ExtractedContacts:
    """Read a spreadsheet or CSV and hand back the contact lines it contains.

    Nothing is stored and no campaign is created: the operator sees the list
    in the same box they would have pasted into, edits it if a row came out
    wrong, and creates the campaign from that. Which means a file cannot
    smuggle in a number the person who uploaded it did not see.

    The reading is done here rather than in the browser because a spreadsheet
    is a zip archive of XML, and because "which cell is the phone number" is
    the same judgement the pasted-list parser already makes -- one
    implementation, in one language, with tests.
    """
    if not file.filename:
        raise ValidationError("No file was sent.", remedy="Choose a spreadsheet or a CSV file.")
    data = await file.read()
    found = extract_contacts(data, filename=file.filename)
    log.info(
        "campaign.contacts_extracted",
        # Never the numbers, never the file name: a list of farmers is a list
        # of farmers whatever it is called (§23-6).
        found=len(found.lines),
        skipped=found.skipped,
        sheets=found.sheets,
    )
    return ExtractedContacts(
        lines=found.lines,
        found=len(found.lines),
        skipped=found.skipped,
        sheets=found.sheets,
    )


@router.get("/{campaign_id}", response_model=CampaignDetail)
async def campaign_detail(
    campaign_id: uuid.UUID,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.CENTRE_MANAGER)],
) -> CampaignDetail:
    campaign = await _campaign(db, campaign_id)
    summary = (await summaries(db, [campaign], principal))[0]
    return CampaignDetail(**summary.model_dump(), contacts=await _contacts(db, campaign.id))


@router.patch("/{campaign_id}", response_model=CampaignSummary)
async def patch_campaign(
    campaign_id: uuid.UUID,
    body: PatchCampaign,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> CampaignSummary:
    campaign = await _campaign(db, campaign_id)
    before = {"name": campaign.name, "max_concurrent_calls": campaign.max_concurrent_calls}
    if body.name is not None:
        campaign.name = body.name.strip()
    if body.maxConcurrent is not None:
        # Takes effect on the next dial of a running campaign: the dialer
        # reads the row when it starts and the operator's change is honoured
        # by the next run, which the resume button triggers.
        campaign.max_concurrent_calls = body.maxConcurrent
    await append_audit(
        db,
        action=AuditAction.UPDATE,
        resource_type="campaign",
        resource_id=str(campaign.id),
        actor_user_id=principal.user_id,
        before=before,
        after={"name": campaign.name, "max_concurrent_calls": campaign.max_concurrent_calls},
    )
    return (await summaries(db, [campaign], principal))[0]


@router.post("/{campaign_id}/approve", response_model=CampaignSummary)
async def approve_campaign(
    campaign_id: uuid.UUID,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> CampaignSummary:
    campaign = await _campaign(db, campaign_id)
    if campaign.status is not CampaignStatus.PENDING_APPROVAL:
        raise ValidationError(
            f"This campaign is {campaign.status.value.replace('_', ' ')}, "
            "not waiting for approval.",
            remedy="Only a campaign waiting for approval can be approved.",
        )
    own = campaign.created_by == principal.user_id
    if own and principal.role is not Role.SUPER_ADMIN:
        raise FourEyesError(
            "You created this campaign, so somebody else has to approve it.",
            remedy="Ask another ops manager to approve, or the owner's account can.",
        )
    report, _ = await evaluate_campaign(db, campaign.id)
    if report.blocked_by or not report.eligible:
        blocked = [check.value for check in report.blocked_by] or ["no_eligible_contacts"]
        raise CampaignBlockedError(
            "The compliance gate stops this campaign from running.",
            remedy="Fix what it names, then approve.",
            context={"blockedBy": blocked},
        )
    campaign.status = CampaignStatus.APPROVED
    campaign.approved_by_user_id = principal.user_id
    campaign.approved_at = datetime.now(UTC)
    # The owner's override, on the row: the schema refuses an approver equal
    # to the creator unless the row says the waiver was deliberate.
    campaign.self_approved = own
    await append_audit(
        db,
        action=AuditAction.APPROVE,
        resource_type="campaign",
        resource_id=str(campaign.id),
        actor_user_id=principal.user_id,
        after={
            "eligible": report.eligible_count,
            "total": report.total,
            "self_approved_by_owner": own,
        },
    )
    return (await summaries(db, [campaign], principal))[0]


@router.post("/{campaign_id}/start", response_model=CampaignSummary)
async def start_campaign(
    campaign_id: uuid.UUID,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> CampaignSummary:
    campaign = await _campaign(db, campaign_id)
    if campaign.status not in (
        CampaignStatus.APPROVED,
        CampaignStatus.SCHEDULED,
        CampaignStatus.PAUSED,
    ):
        raise ValidationError(
            f"A campaign that is {campaign.status.value.replace('_', ' ')} cannot be started.",
            remedy="Approve it first."
            if campaign.status is CampaignStatus.PENDING_APPROVAL
            else "Only an approved or paused campaign can be started.",
        )
    await jobs.set_control(campaign.id, None)
    await jobs.enqueue("dial_campaign", str(campaign.id))
    campaign.status = CampaignStatus.RUNNING
    await append_audit(
        db,
        action=AuditAction.UPDATE,
        resource_type="campaign",
        resource_id=str(campaign.id),
        actor_user_id=principal.user_id,
        after={"status": "running"},
    )
    return (await summaries(db, [campaign], principal))[0]


@router.post("/{campaign_id}/pause", response_model=CampaignSummary)
async def pause_campaign(
    campaign_id: uuid.UUID,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> CampaignSummary:
    campaign = await _campaign(db, campaign_id)
    await jobs.set_control(campaign.id, "paused")
    campaign.status = CampaignStatus.PAUSED
    campaign.paused_at = datetime.now(UTC)
    campaign.pause_reason = "paused from the panel"
    await append_audit(
        db,
        action=AuditAction.UPDATE,
        resource_type="campaign",
        resource_id=str(campaign.id),
        actor_user_id=principal.user_id,
        after={"status": "paused"},
    )
    return (await summaries(db, [campaign], principal))[0]


@router.post("/{campaign_id}/resume", response_model=CampaignSummary)
async def resume_campaign(
    campaign_id: uuid.UUID,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> CampaignSummary:
    return await start_campaign(campaign_id, db, principal)


@router.post("/{campaign_id}/stop", response_model=CampaignSummary)
async def stop_campaign(
    campaign_id: uuid.UUID,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> CampaignSummary:
    campaign = await _campaign(db, campaign_id)
    await jobs.set_control(campaign.id, "cancelled")
    campaign.status = CampaignStatus.CANCELLED
    await append_audit(
        db,
        action=AuditAction.UPDATE,
        resource_type="campaign",
        resource_id=str(campaign.id),
        actor_user_id=principal.user_id,
        after={"status": "cancelled"},
    )
    return (await summaries(db, [campaign], principal))[0]


@router.get("/{campaign_id}/events")
async def campaign_events(
    campaign_id: uuid.UUID,
    request: Request,
    principal: PrincipalDep,
    _: Annotated[Principal, require_role(Role.CENTRE_MANAGER)],
) -> StreamingResponse:
    async def snapshot() -> Any:
        async with short_session(principal) as db:
            campaign = await _campaign(db, campaign_id)
            summary = (await summaries(db, [campaign], principal))[0]
            return CampaignDetail(
                **summary.model_dump(), contacts=await _contacts(db, campaign.id)
            ).model_dump()

    async def accept(event: LiveEvent) -> list[tuple[str, Any]]:
        if event.campaign_id != str(campaign_id):
            return []
        if event.type == livefeed.CONTACT_UPDATED:
            return [(event.type, event.payload)]
        if event.type == livefeed.CAMPAIGN_UPDATED:
            async with short_session(principal) as db:
                campaign = await _campaign(db, campaign_id)
                summary = (await summaries(db, [campaign], principal))[0]
            return [(event.type, summary.model_dump())]
        if event.type == livefeed.CALL_TURN:
            return [(event.type, event.payload)]
        return []

    return streaming_response(
        relay(request, snapshot_event="snapshot", snapshot=snapshot, accept=accept)
    )


@router.get("/{campaign_id}/export.csv")
async def export_campaign(
    campaign_id: uuid.UUID,
    db: DbDep,
    _: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> StreamingResponse:
    campaign = await _campaign(db, campaign_id)
    contacts = await _contacts(db, campaign.id)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["name", "last4", "status", "outcome", "dtmf", "attempts", "call_id"])
    for contact in contacts:
        writer.writerow(
            [
                contact.farmerName or "",
                contact.last4,
                contact.status,
                contact.outcome or "",
                contact.dtmf or "",
                contact.attempts,
                contact.callId or "",
            ]
        )
    filename = re.sub(r"[^A-Za-z0-9_-]+", "-", campaign.name).strip("-") or "campaign"
    return StreamingResponse(
        iter([buffer.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"content-disposition": f'attachment; filename="{filename}.csv"'},
    )


__all__ = (
    "CampaignCreated",
    "ConsentRequiredError",
    "Imported",
    "RemovedBy",
    "import_campaign",
    "published_outbound_flow",
    "router",
    "summaries",
)
