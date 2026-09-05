"""The first page: what a manager needs to know at a glance (§15.1).

Everything here is derived from rows other parts of the system already write
-- calls, turns, campaigns, tickets, stock, documents -- and nothing here is
stored. The page is opened first and often, so the answer is computed once
every fifteen seconds per audience and served from memory in between;
``generatedAt`` says when. The audience is the caller's role and centre set:
row-level security gives a centre manager a different overview from the ops
manager's, and one cache for both would hand the wider one to whoever asked
second.

Two rules hold across every number.

**Test calls are counted apart.** A call placed from the browser page
(provider ``simulator``) says nothing about farmers. It appears in
``testCalls`` and nowhere else, so a morning of testing does not read as a
morning of calls.

**"Today" is the Indian day.** Bounds and buckets are in Asia/Kolkata; a UTC
boundary would put the morning's calls in yesterday.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Integer, Interval, Select, and_, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.campaigns import evaluate_campaign
from uaagro_db.models import (
    Call,
    CallTurn,
    Campaign,
    CampaignContact,
    Centre,
    DtmfEvent,
    Farmer,
    Inventory,
    KbDocument,
    Ticket,
)
from uaagro_domain.enums import (
    CallDirection,
    CallOutcome,
    CallStatus,
    CampaignStatus,
    Intent,
    Role,
    TelephonyProvider,
    TicketPriority,
    TicketStatus,
    TicketType,
    TurnRole,
)
from uaagro_domain.settings import Settings, get_defaults, get_settings
from uaagro_domain.timezone import now_ist

from ..security.deps import DbDep, Principal, require_role
from ..services.jobs import HEARTBEAT_TTL_S, worker_last_seen
from ._panel import iso
from .panel_live import RecentCall

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["panel"])

Range = Literal["today", "7d", "30d"]
_DAYS: dict[str, int] = {"today": 1, "7d": 7, "30d": 30}

#: How long a computed overview stands before it is computed again.
CACHE_TTL_S = 15.0
MAX_ATTENTION = 12
MAX_RECENT = 10
MAX_TOP_QUESTIONS = 8
#: A document still pending after this long has been forgotten by something.
KNOWLEDGE_PENDING_AFTER = timedelta(minutes=2)

Severity = Literal["high", "medium", "low"]
_SEVERITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}

#: CallOutcome values in words an operator reads.
_OUTCOME_LABELS: dict[CallOutcome, str] = {
    CallOutcome.RESOLVED: "Resolved",
    CallOutcome.TRANSFERRED: "Handed to a person",
    CallOutcome.TICKET_CREATED: "Follow-up raised",
    CallOutcome.ABANDONED_SILENCE: "Went silent",
    CallOutcome.CALLER_HUNG_UP: "Caller hung up",
    CallOutcome.OPTED_OUT: "Asked not to be called",
    CallOutcome.OFFER_ACCEPTED: "Offer accepted",
    CallOutcome.OFFER_DECLINED: "Offer declined",
    CallOutcome.NOT_REACHED: "Not reached",
    CallOutcome.SYSTEM_FAILURE: "System failure",
    CallOutcome.REJECTED_SPAM: "Rejected as spam",
}

#: §11.2's intents as the question a farmer asked.
_INTENT_LABELS: dict[str, str] = {
    Intent.PRODUCT_AVAILABILITY: "Is it in stock?",
    Intent.PRICE_ENQUIRY: "What is the price?",
    Intent.CROP_RECOMMENDATION: "What should I use?",
    Intent.PROBLEM_DIAGNOSIS: "What is wrong with my crop?",
    Intent.DOSAGE_QUERY: "How much to apply?",
    Intent.PRODUCT_COMPOSITION: "What is in it?",
    Intent.CENTRE_LOCATION: "Where is the centre?",
    Intent.ORDER_STATUS: "Where is my order?",
    Intent.SERVICE_REQUEST: "Book a service",
    Intent.SCHEME_QUERY: "Government schemes",
    Intent.COMPLAINT: "Complaint",
    Intent.TALK_TO_HUMAN: "Talk to a person",
    Intent.DEALERSHIP_ENQUIRY: "Dealership enquiry",
    Intent.SAFETY_EMERGENCY: "Safety emergency",
    Intent.OUT_OF_SCOPE: "Something we do not cover",
}
#: Not questions: a call the agent did not understand, and a call that was
#: not a farmer at all.
_NOT_A_QUESTION = (Intent.UNKNOWN.value, Intent.SPAM.value)

#: What each telephony provider needs before it can carry a call.
_CREDENTIALS: dict[TelephonyProvider, tuple[str, ...]] = {
    TelephonyProvider.EXOTEL: ("exotel_sid", "exotel_api_key", "exotel_api_token"),
    TelephonyProvider.TWILIO: ("twilio_account_sid", "twilio_api_key_sid", "twilio_api_key_secret"),
    TelephonyProvider.PLIVO: ("plivo_auth_id", "plivo_auth_token"),
}


# --------------------------------------------------------------------------- #
# Shapes
# --------------------------------------------------------------------------- #


class LiveNow(BaseModel):
    calls: int
    capacity: int


class CallCounts(BaseModel):
    total: int
    inbound: int
    outbound: int
    answeredByAgent: int
    transferred: int
    missed: int
    testCalls: int


class OutcomeCount(BaseModel):
    key: str
    label: str
    count: int


class Speed(BaseModel):
    firstReplyP50Ms: int | None
    firstReplyP95Ms: int | None
    replyP50Ms: int | None
    withinBudgetPct: float | None


class OutboundCounts(BaseModel):
    campaignsRunning: int
    contactsDialled: int
    reached: int
    pressed1: int
    pressed2: int
    optedOut: int


class AttentionItem(BaseModel):
    kind: str
    severity: Severity
    title: str
    detail: str | None
    href: str | None
    at: str | None
    count: int


class OverviewRecentCall(RecentCall):
    isTest: bool
    summaryHi: str | None


class ByHourRow(BaseModel):
    hour: str
    inbound: int
    outbound: int


class TopQuestion(BaseModel):
    intent: str
    label: str
    count: int


class Overview(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    range: str
    from_: str = Field(alias="from")
    to: str
    generatedAt: str
    live: LiveNow
    calls: CallCounts
    outcomes: list[OutcomeCount]
    speed: Speed
    outbound: OutboundCounts
    attention: list[AttentionItem]
    recent: list[OverviewRecentCall]
    byHour: list[ByHourRow]
    topQuestions: list[TopQuestion]


class TelephonyStatus(BaseModel):
    provider: str
    mode: str
    configured: bool
    inboundNumber: str | None
    remedy: str | None
    browserCallUrl: str | None


# --------------------------------------------------------------------------- #
# The window
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Window:
    """The range as UTC instants, plus the local midnight it starts on."""

    range: str
    start: datetime
    end: datetime
    start_local: datetime

    @property
    def days(self) -> int:
        return _DAYS[self.range]


def window_for(range_: str) -> Window:
    local_now = now_ist()
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_local = midnight - timedelta(days=_DAYS[range_] - 1)
    return Window(
        range=range_,
        start=start_local.astimezone(UTC),
        end=datetime.now(UTC),
        start_local=start_local,
    )


# --------------------------------------------------------------------------- #
# The cache
# --------------------------------------------------------------------------- #

_cache: dict[tuple[str, ...], tuple[float, Overview]] = {}


def _audience(principal: Principal, range_: str) -> tuple[str, ...]:
    return (
        str(principal.organization_id),
        principal.role.value,
        ",".join(sorted(str(c) for c in principal.centre_ids)),
        range_,
    )


def reset_cache() -> None:
    """Forget every computed overview. Tests use it between fixtures."""
    _cache.clear()


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.get("/overview", response_model=Overview, response_model_by_alias=True)
async def overview(
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.CENTRE_MANAGER)],
    range: Annotated[Range, Query()] = "today",
) -> Overview:
    key = _audience(principal, range)
    now = time.monotonic()
    cached = _cache.get(key)
    if cached is not None and now - cached[0] < CACHE_TTL_S:
        return cached[1]
    built = await build_overview(db, principal, range, get_settings())
    _cache[key] = (now, built)
    return built


@router.get("/telephony", response_model=TelephonyStatus)
async def telephony(
    _: Annotated[Principal, require_role(Role.CENTRE_MANAGER)],
) -> TelephonyStatus:
    return telephony_status(get_settings())


# --------------------------------------------------------------------------- #
# Building the overview
# --------------------------------------------------------------------------- #


async def build_overview(
    db: AsyncSession, principal: Principal, range_: str, settings: Settings
) -> Overview:
    window = window_for(range_)
    counts, speed = await _calls(db, window)
    return Overview(
        range=window.range,
        from_=window.start.isoformat(),
        to=window.end.isoformat(),
        generatedAt=datetime.now(UTC).isoformat(),
        live=LiveNow(calls=await _live(db), capacity=settings.max_concurrent_calls),
        calls=counts,
        outcomes=await _outcomes(db, window),
        speed=speed,
        outbound=await _outbound(db, window),
        attention=await _attention(db, principal, window, settings),
        recent=await _recent(db, window),
        byHour=await _by_hour(db, window),
        topQuestions=await _top_questions(db, window),
    )


def _real() -> Any:
    """Calls that were farmers, not the browser page."""
    return Call.provider != TelephonyProvider.SIMULATOR


def _in_range(window: Window) -> tuple[Any, Any]:
    return Call.started_at >= window.start, Call.started_at < window.end


def _first_reply() -> Any:
    return Call.latency_stats["first_reply_ms"].astext.cast(Integer)


def _ended() -> Any:
    return Call.status != CallStatus.IN_PROGRESS


def _missed() -> Any:
    """Ended with an error, or before the agent ever replied."""
    return and_(
        _ended(),
        or_(
            Call.error_code.is_not(None),
            Call.outcome == CallOutcome.SYSTEM_FAILURE,
            _first_reply().is_(None),
        ),
    )


async def _live(db: AsyncSession) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(Call)
            .where(Call.status == CallStatus.IN_PROGRESS, _real())
        )
        or 0
    )


async def _calls(db: AsyncSession, window: Window) -> tuple[CallCounts, Speed]:
    real = _real()
    first_reply = _first_reply()
    missed = _missed()
    totals = (
        await db.execute(
            select(
                func.count().filter(real).label("total"),
                func.count().filter(real, Call.direction == CallDirection.INBOUND).label("inbound"),
                func.count()
                .filter(real, Call.direction == CallDirection.OUTBOUND)
                .label("outbound"),
                func.count().filter(real, Call.was_transferred.is_(True)).label("transferred"),
                func.count().filter(real, missed).label("missed"),
                # "Answered by the agent" is what is left once the transfers
                # and the calls that never got an answer are taken out; the
                # three add up to the finished calls, which is how the tile
                # reads.
                func.count()
                .filter(real, _ended(), Call.was_transferred.is_(False), ~missed)
                .label("answered"),
                func.count().filter(~real).label("tests"),
                func.percentile_cont(0.5).within_group(first_reply).filter(real).label("p50"),
                func.percentile_cont(0.95).within_group(first_reply).filter(real).label("p95"),
            ).where(*_in_range(window))
        )
    ).one()

    reply_ms = CallTurn.latency_ms["totalMs"].astext.cast(Integer)
    budget_ms = get_defaults().latency_budget_ms.total_no_tool.p95
    replies = (
        await db.execute(
            select(
                func.percentile_cont(0.5).within_group(reply_ms).label("p50"),
                func.count().label("replies"),
                func.count().filter(reply_ms < budget_ms).label("within"),
            )
            .select_from(CallTurn)
            .join(Call, Call.id == CallTurn.call_id)
            .where(
                # The turn carries its call's start, so the partition is
                # pruned by the turn's own column rather than by a join.
                CallTurn.started_at >= window.start,
                CallTurn.started_at < window.end,
                CallTurn.role == TurnRole.ASSISTANT,
                reply_ms.is_not(None),
                real,
            )
        )
    ).one()

    counts = CallCounts(
        total=int(totals.total or 0),
        inbound=int(totals.inbound or 0),
        outbound=int(totals.outbound or 0),
        answeredByAgent=int(totals.answered or 0),
        transferred=int(totals.transferred or 0),
        missed=int(totals.missed or 0),
        testCalls=int(totals.tests or 0),
    )
    total_replies = int(replies.replies or 0)
    speed = Speed(
        firstReplyP50Ms=_ms(totals.p50),
        firstReplyP95Ms=_ms(totals.p95),
        replyP50Ms=_ms(replies.p50),
        withinBudgetPct=round(100.0 * int(replies.within or 0) / total_replies, 1)
        if total_replies
        else None,
    )
    return counts, speed


def _ms(value: Any) -> int | None:
    return round(float(value)) if value is not None else None


async def _outcomes(db: AsyncSession, window: Window) -> list[OutcomeCount]:
    rows = (
        await db.execute(
            select(Call.outcome, func.count())
            .where(*_in_range(window), _real(), Call.outcome.is_not(None))
            .group_by(Call.outcome)
            .order_by(func.count().desc(), Call.outcome)
        )
    ).all()
    return [
        OutcomeCount(
            key=outcome.value,
            label=_OUTCOME_LABELS.get(outcome, outcome.value.replace("_", " ").capitalize()),
            count=int(count),
        )
        for outcome, count in rows
    ]


#: A contact whose call connected, whatever it decided.
_REACHED = ("talked", "pressed_1", "pressed_2", "opted_out")


async def _outbound(db: AsyncSession, window: Window) -> OutboundCounts:
    running = int(
        await db.scalar(
            select(func.count())
            .select_from(Campaign)
            .where(Campaign.status == CampaignStatus.RUNNING, Campaign.deleted_at.is_(None))
        )
        or 0
    )
    row = (
        await db.execute(
            select(
                func.count().label("dialled"),
                func.count().filter(CampaignContact.outcome.in_(_REACHED)).label("reached"),
                func.count().filter(CampaignContact.outcome == "pressed_1").label("pressed1"),
                func.count().filter(CampaignContact.outcome == "pressed_2").label("pressed2"),
                func.count().filter(CampaignContact.outcome == "opted_out").label("opted_out"),
            ).where(
                CampaignContact.last_attempt_at >= window.start,
                CampaignContact.last_attempt_at < window.end,
            )
        )
    ).one()
    return OutboundCounts(
        campaignsRunning=running,
        contactsDialled=int(row.dialled or 0),
        reached=int(row.reached or 0),
        pressed1=int(row.pressed1 or 0),
        pressed2=int(row.pressed2 or 0),
        optedOut=int(row.opted_out or 0),
    )


async def _recent(db: AsyncSession, window: Window) -> list[OverviewRecentCall]:
    first_digit = (
        select(DtmfEvent.digit)
        .where(DtmfEvent.call_id == Call.id)
        .order_by(DtmfEvent.received_at)
        .limit(1)
        .scalar_subquery()
    )
    statement: Select[Any] = (
        select(Call, Centre.code, Farmer.full_name, Farmer.phone_last4, first_digit)
        .outerjoin(Centre, Call.centre_id == Centre.id)
        .outerjoin(Farmer, Call.farmer_id == Farmer.id)
        .where(*_in_range(window), _ended())
        .order_by(Call.started_at.desc())
        .limit(MAX_RECENT)
    )
    rows = (await db.execute(statement)).all()
    out: list[OverviewRecentCall] = []
    for call, centre_code, farmer_name, last4, digit in rows:
        stats = call.latency_stats if isinstance(call.latency_stats, dict) else {}
        first_reply = stats.get("first_reply_ms")
        out.append(
            OverviewRecentCall(
                id=str(call.id),
                startedAt=call.started_at.isoformat(),
                direction=call.direction.value,
                farmerName=farmer_name,
                callerLast4=last4,
                centreCode=centre_code,
                outcome=call.outcome.value if call.outcome else None,
                durationSeconds=call.duration_seconds,
                firstReplyMs=int(first_reply) if first_reply is not None else None,
                dtmf=digit,
                isTest=call.provider is TelephonyProvider.SIMULATOR,
                summaryHi=call.summary_hi,
            )
        )
    return out


async def _by_hour(db: AsyncSession, window: Window) -> list[ByHourRow]:
    """One row per local hour today, or per local day over a longer range.

    Grouped in the database in Indian time, so a call at 00:30 IST lands in
    the 00:00 row rather than in the previous day's 19:00 UTC bucket. The
    zone is passed as its offset rather than by name: IST has no daylight
    saving, and a Postgres built without a zone database -- the embedded one
    the tests run on -- knows the offset and not the name.
    """
    by_day = window.range != "today"
    local = func.timezone(literal(window.start_local.utcoffset(), Interval), Call.started_at)
    bucket = func.date_trunc("day" if by_day else "hour", local)
    rows = (
        await db.execute(
            select(bucket, Call.direction, func.count())
            .where(*_in_range(window), _real())
            .group_by(bucket, Call.direction)
        )
    ).all()
    counted: dict[datetime, dict[str, int]] = {}
    for moment, direction, count in rows:
        local = moment.replace(tzinfo=None)
        counted.setdefault(local, {})[direction.value] = int(count)

    origin = window.start_local.replace(tzinfo=None)
    step = timedelta(days=1) if by_day else timedelta(hours=1)
    slots = window.days if by_day else 24
    out: list[ByHourRow] = []
    for index in range(slots):
        moment = origin + step * index
        tallies = counted.get(moment, {})
        out.append(
            ByHourRow(
                hour=f"{moment:%a} {moment.day}" if by_day else f"{moment:%H}:00",
                inbound=tallies.get(CallDirection.INBOUND.value, 0),
                outbound=tallies.get(CallDirection.OUTBOUND.value, 0),
            )
        )
    return out


async def _top_questions(db: AsyncSession, window: Window) -> list[TopQuestion]:
    # `intents` is an array on the call, so the count unnests it: counting the
    # column would count calls, and a call that asked three things is three
    # questions.
    unnested = (
        select(func.unnest(Call.intents).label("intent"))
        .where(*_in_range(window), _real())
        .subquery()
    )
    rows = (
        await db.execute(
            select(unnested.c.intent, func.count())
            .where(unnested.c.intent.not_in(_NOT_A_QUESTION))
            .group_by(unnested.c.intent)
            .order_by(func.count().desc(), unnested.c.intent)
            .limit(MAX_TOP_QUESTIONS)
        )
    ).all()
    return [
        TopQuestion(
            intent=str(intent),
            label=_INTENT_LABELS.get(str(intent), str(intent).replace("_", " ").capitalize()),
            count=int(count),
        )
        for intent, count in rows
    ]


# --------------------------------------------------------------------------- #
# Attention
# --------------------------------------------------------------------------- #


async def _attention(
    db: AsyncSession, principal: Principal, window: Window, settings: Settings
) -> list[AttentionItem]:
    items: list[AttentionItem] = []
    items.extend(await _open_tickets(db))
    items.extend(await _failed_transfers(db, window))
    items.extend(await _unanswered(db, window))
    items.extend(await _stock_outs(db))
    items.extend(await _knowledge(db))
    if principal.sees_all_centres:
        # The gate reads consent and contact rows farmer by farmer, and a
        # centre-scoped session sees only its own farmers: the same campaign
        # would look blocked to a centre manager and clear to the ops
        # manager. Campaigns are the ops manager's to unblock, so only the
        # org-wide view reports them.
        items.extend(await _blocked_campaigns(db))
    status = telephony_status(settings)
    if not status.configured:
        items.append(
            AttentionItem(
                kind="telephony",
                severity="high",
                title="Telephony is not configured",
                detail=status.remedy,
                href=None,
                at=None,
                count=1,
            )
        )
    items.extend(await _worker())
    items.sort(key=_urgency)
    return items[:MAX_ATTENTION]


def _urgency(item: AttentionItem) -> tuple[int, float]:
    """Most urgent first, then the newest; undated items last in their band."""
    when = datetime.fromisoformat(item.at).timestamp() if item.at else float("-inf")
    return _SEVERITY_RANK[item.severity], -when


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    word = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {word}"


async def _open_tickets(db: AsyncSession) -> list[AttentionItem]:
    rows = (
        await db.execute(
            select(Ticket.type, Ticket.priority, Ticket.subject, Ticket.call_id, Ticket.created_at)
            .where(
                Ticket.status.in_((TicketStatus.OPEN, TicketStatus.IN_PROGRESS)),
                Ticket.deleted_at.is_(None),
            )
            .order_by(Ticket.created_at.desc())
        )
    ).all()
    if not rows:
        return []
    safety = any(
        kind is TicketType.SAFETY_INCIDENT or priority is TicketPriority.P0
        for kind, priority, *_ in rows
    )
    _, _, subject, call_id, created_at = rows[0]
    return [
        AttentionItem(
            kind="ticket",
            severity="high" if safety else "medium",
            title=_plural(len(rows), "open ticket"),
            detail=("Includes a safety incident. " if safety else "") + f"Newest: {subject}",
            href=f"/calls/{call_id}" if call_id else "/calls",
            at=created_at.isoformat(),
            count=len(rows),
        )
    ]


async def _failed_transfers(db: AsyncSession, window: Window) -> list[AttentionItem]:
    rows = (
        await db.execute(
            select(Call.id, Call.started_at)
            .where(
                *_in_range(window),
                _real(),
                _ended(),
                Call.was_transferred.is_(True),
                Call.transfer_completed.is_(False),
            )
            .order_by(Call.started_at.desc())
        )
    ).all()
    if not rows:
        return []
    call_id, started_at = rows[0]
    return [
        AttentionItem(
            kind="transfer_failed",
            severity="high",
            title=_plural(len(rows), "hand-over nobody took", "hand-overs nobody took"),
            detail="The farmer was promised a person and the line was not picked up.",
            href=f"/calls/{call_id}",
            at=started_at.isoformat(),
            count=len(rows),
        )
    ]


async def _unanswered(db: AsyncSession, window: Window) -> list[AttentionItem]:
    """Calls where the agent logged ``unknown`` twice: it did not understand."""
    twice_unknown = func.cardinality(func.array_positions(Call.intents, Intent.UNKNOWN.value)) >= 2
    rows = (
        await db.execute(
            select(Call.id, Call.started_at)
            .where(*_in_range(window), _real(), twice_unknown)
            .order_by(Call.started_at.desc())
        )
    ).all()
    if not rows:
        return []
    call_id, started_at = rows[0]
    return [
        AttentionItem(
            kind="unanswered",
            severity="medium",
            title=_plural(
                len(rows), "call the agent could not answer", "calls the agent could not answer"
            ),
            detail="The farmer's question was not understood twice in one call.",
            href=f"/calls/{call_id}",
            at=started_at.isoformat(),
            count=len(rows),
        )
    ]


async def _stock_outs(db: AsyncSession) -> list[AttentionItem]:
    rows = (
        await db.execute(
            select(Centre.code, Centre.name, func.count())
            .join(Inventory, Inventory.centre_id == Centre.id)
            .where(
                Inventory.is_available.is_(False),
                Centre.deleted_at.is_(None),
                Centre.is_active.is_(True),
            )
            .group_by(Centre.code, Centre.name)
            .order_by(func.count().desc(), Centre.code)
        )
    ).all()
    return [
        AttentionItem(
            kind="stock_out",
            severity="low",
            title=f"{_plural(int(count), 'product')} out of stock at {code}",
            detail=name,
            href="/inbound/centres",
            at=None,
            count=int(count),
        )
        for code, name, count in rows
    ]


async def _knowledge(db: AsyncSession) -> list[AttentionItem]:
    items: list[AttentionItem] = []
    forgotten_before = datetime.now(UTC) - KNOWLEDGE_PENDING_AFTER
    pending = (
        await db.execute(
            select(KbDocument.title, KbDocument.updated_at)
            .where(
                KbDocument.deleted_at.is_(None),
                KbDocument.ingest_status == "pending",
                KbDocument.updated_at < forgotten_before,
            )
            .order_by(KbDocument.updated_at)
        )
    ).all()
    if pending:
        title, since = pending[0]
        items.append(
            AttentionItem(
                kind="knowledge_pending",
                severity="low",
                title=_plural(
                    len(pending),
                    "document waiting to be indexed",
                    "documents waiting to be indexed",
                ),
                detail=f"Oldest: {title}. Is the background worker running?",
                href="/inbound/knowledge",
                at=since.isoformat(),
                count=len(pending),
            )
        )
    failed = (
        await db.execute(
            select(KbDocument.title, KbDocument.ingest_error, KbDocument.updated_at)
            .where(KbDocument.deleted_at.is_(None), KbDocument.ingest_status == "failed")
            .order_by(KbDocument.updated_at.desc())
        )
    ).all()
    if failed:
        title, error, at = failed[0]
        items.append(
            AttentionItem(
                kind="knowledge_failed",
                severity="medium",
                title=_plural(len(failed), "document failed to index", "documents failed to index"),
                detail=f"{title}: {error}" if error else title,
                href="/inbound/knowledge",
                at=at.isoformat(),
                count=len(failed),
            )
        )
    return items


async def _blocked_campaigns(db: AsyncSession) -> list[AttentionItem]:
    campaigns = (
        await db.scalars(
            select(Campaign)
            .where(
                Campaign.deleted_at.is_(None),
                Campaign.status.in_(
                    (CampaignStatus.DRAFT, CampaignStatus.PENDING_APPROVAL, CampaignStatus.APPROVED)
                ),
            )
            .order_by(Campaign.created_at.desc())
        )
    ).all()
    items: list[AttentionItem] = []
    for campaign in campaigns:
        report, _ = await evaluate_campaign(db, campaign.id)
        blocked = [check.value for check in report.blocked_by]
        if not report.eligible:
            blocked.append("no_eligible_contacts")
        if not blocked:
            continue
        items.append(
            AttentionItem(
                kind="campaign_blocked",
                severity="medium",
                title=f"Campaign blocked: {campaign.name}",
                detail=", ".join(blocked),
                href=f"/outbound/{campaign.id}",
                at=campaign.created_at.isoformat(),
                count=len(blocked),
            )
        )
    return items


async def _worker() -> list[AttentionItem]:
    last_seen = await worker_last_seen()
    silent_for = timedelta(seconds=HEARTBEAT_TTL_S)
    if last_seen is not None and datetime.now(UTC) - last_seen < silent_for:
        return []
    return [
        AttentionItem(
            kind="worker",
            severity="high",
            title="The background worker has not been seen",
            detail=(
                f"Last heartbeat {last_seen.isoformat()}."
                if last_seen is not None
                else "No heartbeat yet. Uploads wait and post-call summaries stop until it runs."
            ),
            href="/inbound/knowledge",
            at=iso(last_seen),
            count=1,
        )
    ]


# --------------------------------------------------------------------------- #
# Telephony
# --------------------------------------------------------------------------- #


def telephony_status(settings: Settings) -> TelephonyStatus:
    """What carries the calls, and whether it can.

    Configured means the credentials the provider's adapter asks for are
    present -- not that they work; the first real call is the test of that.
    The simulator needs nothing: the browser page is the phone.
    """
    provider = settings.telephony_provider
    simulator = provider is TelephonyProvider.SIMULATOR
    missing = [
        name.upper() for name in _CREDENTIALS.get(provider, ()) if not getattr(settings, name)
    ]
    configured = simulator or not missing
    remedy = None
    if not configured:
        remedy = (
            f"Set {', '.join(missing)} in the environment for {provider.value}, or "
            "TELEPHONY_PROVIDER=simulator to answer calls from the browser page."
        )
    return TelephonyStatus(
        provider=provider.value,
        mode="simulator" if simulator else "live",
        configured=configured,
        inboundNumber=_masked(settings.inbound_did),
        remedy=remedy,
        browserCallUrl=(
            f"{settings.demo_public_url.rstrip('/')}/call"
            if settings.app_env == "development"
            else None
        ),
    )


def _masked(number: str | None) -> str | None:
    """The last four digits of the DID, and no more -- it is dialled, not read."""
    digits = re.sub(r"\D", "", number or "")
    if len(digits) < 4:
        return None
    return f"…{digits[-4:]}"


__all__ = (
    "AttentionItem",
    "Overview",
    "TelephonyStatus",
    "build_overview",
    "reset_cache",
    "router",
    "telephony_status",
    "window_for",
)
