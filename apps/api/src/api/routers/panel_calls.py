"""One call, in full: recording, transcript, latency, what followed (§15.1).

The detail view is assembled from six tables the media path and the post-call
pipeline wrote independently -- the call, its turns, its events, its
keypresses, the messages it sent and the tickets it raised -- and presented
as one story in the order it happened.

The recording never leaves the API as a URL. It is streamed through this
process, under the same row-level scope as the call it belongs to, so a link
copied out of the panel is worth nothing to anyone who is not signed in.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Annotated, Any

import structlog
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select

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
from uaagro_db.storage import ObjectStore
from uaagro_domain.enums import CallDirection, FlowType, Role, TurnRole
from uaagro_domain.errors import NotFoundError
from uaagro_domain.settings import get_defaults

from ..security.deps import DbDep, Principal, require_role
from ._panel import iso, latency_of, tools_of

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin/calls", tags=["panel"])


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


async def _load(
    db: Any, call_id: uuid.UUID
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


@router.get("/{call_id}", response_model=CallDetail)
async def call_detail(
    call_id: uuid.UUID,
    db: DbDep,
    _: Annotated[Principal, require_role(Role.CENTRE_MANAGER)],
) -> CallDetail:
    call, centre_code, centre_name, farmer_name, last4 = await _load(db, call_id)

    turn_rows = (
        await db.scalars(
            select(CallTurn).where(CallTurn.call_id == call.id).order_by(CallTurn.turn_index)
        )
    ).all()
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

    stats = call.latency_stats if isinstance(call.latency_stats, dict) else {}
    first_reply = stats.get("first_reply_ms")
    retention_days = get_defaults().compliance.retention_days_recordings
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
        recording=Recording(
            available=bool(call.recording_object_key),
            durationSeconds=call.recording_duration,
            bytes=None,
            retainedUntil=iso(call.started_at + timedelta(days=retention_days))
            if call.recording_object_key
            else None,
        ),
        firstReplyMs=int(first_reply) if first_reply is not None else None,
        turns=turns,
        events=events,
        followUps=follow_ups,
    )


async def _events(db: Any, call: Call) -> list[CallEventRow]:
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


async def _follow_ups(db: Any, call: Call) -> list[str]:
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
    store = ObjectStore()
    info = await store.head(call.recording_object_key)
    headers = {"cache-control": "private, no-store", "accept-ranges": "none"}
    if info is not None and info.size:
        headers["content-length"] = str(info.size)
    return StreamingResponse(
        store.stream(call.recording_object_key),
        media_type=(info.content_type if info and info.content_type else "audio/wav"),
        headers=headers,
    )


__all__ = ("CallDetail", "router")
