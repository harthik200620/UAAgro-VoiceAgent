"""Calls: the list the page scrolls, and one call in full (§15.1).

The detail view is assembled from six tables the media path and the post-call
pipeline wrote independently -- the call, its turns, its events, its
keypresses, the messages it sent and the tickets it raised -- and presented
as one story in the order it happened. The list is the same rows, one line
each, with the filters an operator reaches for first.

Three things hold across both.

**A test call is marked, never hidden.** A call placed from the browser page
(provider ``simulator``) sits in the list with ``isTest`` set, so an operator
who just placed one finds it at the top -- and can filter it out of the
afternoon's real traffic with one flag.

**The recording never leaves the API as a URL.** It is streamed through this
process, under the same row-level scope as the call it belongs to, so a link
copied out of the panel is worth nothing to anyone who is not signed in.
``recording.available`` means the object is actually there: the row's key is
a promise, the store's answer is the fact.

**The summary is the post-call job's summary.** The button that re-runs it
calls the same code the job does, so a summary made on demand and one made at
call end never differ in wording or in what they leave out.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.models import (
    AgentConfig,
    Call,
    CallEvent,
    CallTurn,
    Campaign,
    Centre,
    DtmfEvent,
    Farmer,
    Ticket,
    WhatsAppMessage,
)
from uaagro_db.storage import object_store
from uaagro_domain.enums import CallDirection, FlowType, Role, TelephonyProvider, TurnRole
from uaagro_domain.errors import (
    NotFoundError,
    ServiceUnavailableError,
    ValidationError,
    VendorError,
)
from uaagro_domain.settings import get_defaults, get_settings

from ..security.deps import DbDep, Principal, require_role
from ._panel import iso, latency_of, tools_of

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin/calls", tags=["panel"])

#: The most rows one page may ask for. Above this the panel should page.
MAX_PAGE = 200


class CallRow(BaseModel):
    id: str
    callRef: str
    startedAt: str
    direction: str
    centreCode: str | None
    centreId: str | None
    language: str
    qualityTier: str
    durationSeconds: int
    outcome: str
    intent: str | None
    transferred: bool
    costRupees: float
    #: Last four digits, from the farmer record. §17: the call row holds only a
    #: hash, and nothing above the control plane ever sees more than this.
    callerLast4: str | None
    farmerName: str | None
    firstReplyMs: int | None
    #: The first key the farmer pressed, when they pressed one.
    dtmf: str | None
    campaignId: str | None
    isTest: bool
    summaryHi: str | None
    intents: list[str]
    recordingAvailable: bool


class CallList(BaseModel):
    rows: list[CallRow]
    total: int


class Turn(BaseModel):
    callId: str
    turnIndex: int
    role: str
    text: str
    at: float
    latency: dict[str, Any] | None
    tools: list[dict[str, Any]]


class CallEventRow(BaseModel):
    at: float
    type: str
    text: str


class Recording(BaseModel):
    available: bool
    durationSeconds: int | None
    bytes: int | None
    retainedUntil: str | None


class CallStats(BaseModel):
    turns: int
    farmerTurns: int
    agentTurns: int
    cachedReplies: int
    toolCalls: int
    llmModel: str | None


class CallDetail(BaseModel):
    id: str
    callRef: str
    direction: str
    startedAt: str
    endedAt: str | None
    durationSeconds: int | None
    status: str
    outcome: str | None
    farmerName: str | None
    callerLast4: str | None
    centreCode: str | None
    centreName: str | None
    language: str
    campaignId: str | None
    campaignName: str | None
    flowName: str | None
    flowVersion: int | None
    summaryHi: str | None
    summaryEn: str | None
    recording: Recording
    firstReplyMs: int | None
    turns: list[Turn]
    events: list[CallEventRow]
    followUps: list[str]
    isTest: bool
    intents: list[str]
    stats: CallStats


#: Event names the media path writes, in words an operator reads.
_EVENT_TEXT = {
    "start": "Call connected",
    "stop": "Call ended by the farmer",
    "hangup": "Call ended by the agent",
    "playback_cleared": "The farmer spoke over the agent; the agent stopped",
    "pipeline_unavailable": "The agent could not be started; the call was handed over",
    "pipeline_failed": "The agent stopped unexpectedly",
    "transfer": "Handed to a person",
    "transfer_started": "Handing to a person",
    "transfer_completed": "A person took the call",
    "transfer_failed": "Nobody picked up the hand-over",
}

#: Events that carry no information an operator acts on.
_EVENT_SILENT = {"connected", "unknown_event", "dtmf", "turn"}


def _quality_tier(language: str | None) -> str:
    """The tier §5.1 declares for this language.

    Read from the routing table rather than stored on the call, so the panel
    always shows what a caller in that language *currently* gets. §5 is explicit
    that a lower tier must be labelled honestly rather than quietly shipped as
    equivalent, and a stale stored value would defeat that.
    """
    if not language:
        return "C"
    try:
        _, route = get_defaults().resolve_language(language)
    except Exception:
        return "C"
    return str(route.quality_tier)


def _first_reply(call: Call) -> int | None:
    stats = call.latency_stats if isinstance(call.latency_stats, dict) else {}
    value = stats.get("first_reply_ms")
    return int(value) if value is not None else None


def _is_test(call: Call) -> bool:
    return call.provider is TelephonyProvider.SIMULATOR


# --------------------------------------------------------------------------- #
# The list
# --------------------------------------------------------------------------- #


@router.get("", response_model=CallList)
async def calls(
    db: DbDep,
    _: Annotated[Principal, require_role(Role.CENTRE_MANAGER)],
    outcome: str | None = None,
    language: str | None = None,
    direction: str | None = None,
    centre_id: uuid.UUID | None = None,
    campaign_id: uuid.UUID | None = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    q: Annotated[str | None, Query(max_length=120)] = None,
    is_test: bool | None = None,
    limit: int = Query(50, ge=1, le=MAX_PAGE),
    offset: int = Query(0, ge=0),
) -> CallList:
    first_digit = (
        select(DtmfEvent.digit)
        .where(DtmfEvent.call_id == Call.id)
        .order_by(DtmfEvent.received_at)
        .limit(1)
        .scalar_subquery()
    )
    statement: Select[Any] = (
        select(Call, Centre.code, Farmer.phone_last4, Farmer.full_name, first_digit)
        .outerjoin(Centre, Call.centre_id == Centre.id)
        .outerjoin(Farmer, Call.farmer_id == Farmer.id)
        .order_by(Call.started_at.desc())
    )
    if outcome:
        statement = statement.where(Call.outcome == outcome)
    if language:
        statement = statement.where(Call.language_final == language)
    if direction:
        statement = statement.where(Call.direction == direction)
    if centre_id is not None:
        statement = statement.where(Call.centre_id == centre_id)
    if campaign_id is not None:
        statement = statement.where(Call.campaign_id == campaign_id)
    if from_ is not None:
        statement = statement.where(Call.started_at >= from_)
    if to is not None:
        statement = statement.where(Call.started_at < to)
    if is_test is not None:
        simulator = Call.provider == TelephonyProvider.SIMULATOR
        statement = statement.where(simulator if is_test else ~simulator)
    if q and q.strip():
        # A substring match over what was said. Simple on purpose: the panel's
        # search box is "find the call where somebody mentioned DAP", and the
        # farmer's spelling in Devanagari is not what a stemmer expects.
        needle = f"%{q.strip()}%"
        spoken = (
            select(CallTurn.id)
            .where(CallTurn.call_id == Call.id, CallTurn.text_original.ilike(needle))
            .limit(1)
        )
        statement = statement.where(spoken.exists() | Farmer.full_name.ilike(needle))

    total = int(await db.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = (await db.execute(statement.limit(limit).offset(offset))).all()

    return CallList(
        total=total,
        rows=[
            CallRow(
                id=str(call.id),
                callRef=call.call_ref,
                startedAt=call.started_at.isoformat(),
                direction=call.direction.value,
                centreCode=centre_code,
                centreId=str(call.centre_id) if call.centre_id else None,
                language=call.language_final or call.language_detected or "",
                qualityTier=_quality_tier(call.language_final or call.language_detected),
                durationSeconds=int(call.duration_seconds or 0),
                outcome=call.outcome.value if call.outcome else "",
                # The first intent of the call, for the column; the whole list
                # rides alongside for the filter chips.
                intent=call.intents[0] if call.intents else None,
                transferred=bool(call.was_transferred),
                costRupees=float(call.cost_total_inr or 0),
                callerLast4=last4,
                farmerName=farmer_name,
                firstReplyMs=_first_reply(call),
                dtmf=digit,
                campaignId=str(call.campaign_id) if call.campaign_id else None,
                isTest=_is_test(call),
                summaryHi=call.summary_hi,
                intents=list(call.intents or []),
                # By the row's key alone: asking the store about every row of
                # a page would make the list wait on object storage. The
                # detail view asks, and its answer is the fact.
                recordingAvailable=bool(call.recording_object_key),
            )
            for call, centre_code, last4, farmer_name, digit in rows
        ],
    )


# --------------------------------------------------------------------------- #
# One call
# --------------------------------------------------------------------------- #


async def _load(
    db: AsyncSession, call_id: uuid.UUID
) -> tuple[Call, str | None, str | None, str | None, str | None]:
    row = (
        await db.execute(
            select(Call, Centre.code, Centre.name, Farmer.full_name, Farmer.phone_last4)
            .outerjoin(Centre, Call.centre_id == Centre.id)
            .outerjoin(Farmer, Call.farmer_id == Farmer.id)
            .where(Call.id == call_id)
        )
    ).first()
    if row is None:
        raise NotFoundError(resource="call", identifier=str(call_id))
    call, centre_code, centre_name, farmer_name, last4 = row
    return call, centre_code, centre_name, farmer_name, last4


async def _turn_rows(db: AsyncSession, call: Call) -> list[CallTurn]:
    return list(
        (
            await db.scalars(
                select(CallTurn).where(CallTurn.call_id == call.id).order_by(CallTurn.turn_index)
            )
        ).all()
    )


async def build_detail(db: AsyncSession, call_id: uuid.UUID) -> CallDetail:
    call, centre_code, centre_name, farmer_name, last4 = await _load(db, call_id)

    turn_rows = await _turn_rows(db, call)
    turns = [
        Turn(
            callId=str(call.id),
            turnIndex=row.turn_index,
            role="agent" if row.role is TurnRole.ASSISTANT else "farmer",
            text=row.text_original or "",
            at=round((row.audio_offset_ms or 0) / 1000, 2),
            latency=latency_of(row) if row.role is TurnRole.ASSISTANT else None,
            tools=tools_of(row) if row.role is TurnRole.ASSISTANT else [],
        )
        for row in turn_rows
        if row.role in (TurnRole.USER, TurnRole.ASSISTANT)
    ]

    events = await _events(db, call)
    follow_ups = await _follow_ups(db, call)
    campaign_name = None
    if call.campaign_id is not None:
        campaign_name = await db.scalar(
            select(Campaign.name).where(Campaign.id == call.campaign_id)
        )
    flow_name = None
    if call.agent_config_version is not None:
        flow_name = await db.scalar(
            select(AgentConfig.name).where(
                AgentConfig.organization_id == call.organization_id,
                AgentConfig.flow_type
                == (
                    FlowType.OUTBOUND
                    if call.direction is CallDirection.OUTBOUND
                    else FlowType.INBOUND
                ),
                AgentConfig.version == call.agent_config_version,
            )
        )

    return CallDetail(
        id=str(call.id),
        callRef=call.call_ref,
        direction=call.direction.value,
        startedAt=call.started_at.isoformat(),
        endedAt=iso(call.ended_at),
        durationSeconds=call.duration_seconds,
        status=call.status.value,
        outcome=call.outcome.value if call.outcome else None,
        farmerName=farmer_name,
        callerLast4=last4,
        centreCode=centre_code,
        centreName=centre_name,
        language=call.language_final or call.language_detected or "",
        campaignId=str(call.campaign_id) if call.campaign_id else None,
        campaignName=campaign_name,
        flowName=flow_name,
        flowVersion=call.agent_config_version,
        summaryHi=call.summary_hi,
        summaryEn=call.summary_en,
        recording=await _recording(call),
        firstReplyMs=_first_reply(call),
        turns=turns,
        events=events,
        followUps=follow_ups,
        isTest=_is_test(call),
        intents=list(call.intents or []),
        stats=_stats(turn_rows),
    )


@router.get("/{call_id}", response_model=CallDetail)
async def call_detail(
    call_id: uuid.UUID,
    db: DbDep,
    _: Annotated[Principal, require_role(Role.CENTRE_MANAGER)],
) -> CallDetail:
    return await build_detail(db, call_id)


async def _recording(call: Call) -> Recording:
    """Whether the audio is really there, and how much of it.

    The row's key says the worker meant to store it; the store's ``head`` says
    it did. A store that is down answers "not available" rather than failing
    the whole page -- the transcript is still worth showing.
    """
    if not call.recording_object_key:
        return Recording(
            available=False, durationSeconds=call.recording_duration, bytes=None, retainedUntil=None
        )
    try:
        info = await object_store().head(call.recording_object_key)
    except VendorError as exc:
        log.warning("calls.recording_store_unreachable", error=type(exc).__name__)
        info = None
    retention_days = get_defaults().compliance.retention_days_recordings
    return Recording(
        available=info is not None,
        durationSeconds=call.recording_duration,
        bytes=info.size if info is not None else None,
        retainedUntil=iso(call.started_at + timedelta(days=retention_days))
        if info is not None
        else None,
    )


def _stats(turn_rows: list[CallTurn]) -> CallStats:
    """The numbers under the transcript: how much was said, and by what."""
    farmer = [row for row in turn_rows if row.role is TurnRole.USER]
    agent = [row for row in turn_rows if row.role is TurnRole.ASSISTANT]
    cached = 0
    tool_calls = 0
    for row in agent:
        latency = latency_of(row)
        if latency is not None and latency["fromCache"]:
            cached += 1
        tool_calls += len(tools_of(row))
    return CallStats(
        turns=len(farmer) + len(agent),
        farmerTurns=len(farmer),
        agentTurns=len(agent),
        cachedReplies=cached,
        toolCalls=tool_calls,
        llmModel=next((row.llm_model for row in agent if row.llm_model), None),
    )


async def _events(db: AsyncSession, call: Call) -> list[CallEventRow]:
    started = call.started_at
    out: list[CallEventRow] = []

    digits = (
        await db.execute(
            select(DtmfEvent.digit, DtmfEvent.received_at)
            .where(DtmfEvent.call_id == call.id)
            .order_by(DtmfEvent.received_at)
        )
    ).all()
    for digit, at in digits:
        label = {
            "1": "Pressed 1 — wants the offer",
            "2": "Pressed 2 — wants details",
            "9": "Pressed 9 — do not call again",
        }.get(digit, f"Pressed {digit}")
        out.append(CallEventRow(at=_seconds(at, started), type="dtmf", text=label))

    rows = (
        await db.execute(
            select(CallEvent.event_type, CallEvent.event_at, CallEvent.payload)
            .where(CallEvent.call_id == call.id)
            .order_by(CallEvent.event_at)
        )
    ).all()
    for event_type, at, payload in rows:
        if event_type in _EVENT_SILENT:
            continue
        text = _EVENT_TEXT.get(event_type)
        if text is None:
            text = event_type.replace("_", " ").capitalize()
        if event_type.startswith("transfer") and isinstance(payload, dict) and payload.get("to"):
            text = f"{text}: {payload['to']}"
        out.append(CallEventRow(at=_seconds(at, started), type=event_type, text=text))

    messages = (
        await db.execute(
            select(
                WhatsAppMessage.template_name, WhatsAppMessage.created_at, WhatsAppMessage.status
            )
            .where(WhatsAppMessage.call_id == call.id)
            .order_by(WhatsAppMessage.created_at)
        )
    ).all()
    for template, at, status in messages:
        label = f"WhatsApp message sent · {template}" if template else "WhatsApp message sent"
        if status is not None and getattr(status, "value", str(status)) in {
            "failed",
            "undelivered",
        }:
            label = f"WhatsApp message failed · {template or ''}".strip(" ·")
        out.append(CallEventRow(at=_seconds(at, started), type="whatsapp", text=label))

    out.sort(key=lambda row: row.at)
    return out


async def _follow_ups(db: AsyncSession, call: Call) -> list[str]:
    out: list[str] = []
    tickets = (
        await db.execute(
            select(Ticket.type, Ticket.subject)
            .where(Ticket.call_id == call.id)
            .order_by(Ticket.created_at)
        )
    ).all()
    for ticket_type, subject in tickets:
        kind = getattr(ticket_type, "value", str(ticket_type)).replace("_", " ")
        out.append(
            f"{kind.capitalize()} raised: {subject}" if subject else f"{kind.capitalize()} raised"
        )
    sent = await db.scalar(
        select(WhatsAppMessage.id).where(WhatsAppMessage.call_id == call.id).limit(1)
    )
    if sent is not None:
        out.append("WhatsApp message sent")
    if call.was_transferred:
        out.append("Handed to a person")
    return out


def _seconds(at: Any, started: Any) -> float:
    try:
        return round(max(0.0, float((at - started).total_seconds())), 2)
    except Exception:
        return 0.0


# --------------------------------------------------------------------------- #
# The summary, again
# --------------------------------------------------------------------------- #


@router.post("/{call_id}/summarise", response_model=CallDetail)
async def summarise(
    call_id: uuid.UUID,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.CENTRE_MANAGER)],
) -> CallDetail:
    """Re-run the post-call summary for one call. A model call, on purpose."""
    try:
        from worker.summary import build_summariser, summarise_call
    except ImportError as exc:
        # The summariser is the background worker's code. The API runs it in
        # process for this one button, and says so when the package is not
        # installed beside it rather than pretending the button did nothing.
        raise ServiceUnavailableError(
            service="the summariser (the uaagro-worker package)", detail=type(exc).__name__
        ) from exc

    call = (await _load(db, call_id))[0]
    if not await _turn_rows(db, call):
        raise ValidationError(
            "There is nothing to summarise: the call has no transcript.",
            remedy="A call that ended before anyone spoke has no summary to make.",
        )
    detail = await summarise_call(db, call, build_summariser(get_settings()))
    log.info("calls.summarised", call_id=str(call.id), by=str(principal.user_id), detail=detail)
    return await build_detail(db, call_id)


# --------------------------------------------------------------------------- #
# The recording
# --------------------------------------------------------------------------- #


@router.get("/{call_id}/recording")
async def recording(
    call_id: uuid.UUID,
    db: DbDep,
    _: Annotated[Principal, require_role(Role.CENTRE_MANAGER)],
) -> StreamingResponse:
    """The recording, streamed under the caller's scope. Never a redirect."""
    call = (await _load(db, call_id))[0]
    if not call.recording_object_key:
        raise NotFoundError(resource="recording", identifier=str(call_id))
    store = object_store()
    info = await store.head(call.recording_object_key)
    if info is None:
        raise NotFoundError(resource="recording", identifier=str(call_id))
    headers = {"cache-control": "private, no-store", "accept-ranges": "none"}
    if info.size:
        headers["content-length"] = str(info.size)
    return StreamingResponse(
        store.stream(call.recording_object_key),
        media_type=info.content_type or "audio/wav",
        headers=headers,
    )


__all__ = ("CallDetail", "CallRow", "build_detail", "router")
