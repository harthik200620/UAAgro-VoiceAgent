"""The Live page: what is happening on the phones right now (§15.1).

A snapshot from the database, then the media path's own events relayed over
server-sent events. The snapshot is what the page renders first and what a
reconnecting tab re-renders; the events are how a transcript grows while the
farmer is still speaking.

Scope is enforced on the way out. The feed carries every call in the
organisation; a centre manager's stream forwards only events for their
centres, and a call that has not been attributed to a centre yet is withheld
from them -- the ``call.identified`` event arrives seconds later with the
centre attached, and the buffered start is sent ahead of it then.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.models import Call, CallTurn, Centre, DtmfEvent, Farmer
from uaagro_domain import livefeed
from uaagro_domain.enums import CallDirection, CallOutcome, CallStatus, Role, TurnRole
from uaagro_domain.livefeed import LiveEvent
from uaagro_domain.settings import get_settings

from ..security.deps import DbDep, Principal, PrincipalDep, require_role
from ..services.events import relay, streaming_response
from ._panel import short_session, today_bounds, visible_centre

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin/live", tags=["panel"])

#: Finished calls the snapshot carries. The full history is the Calls page.
RECENT_LIMIT = 25


class LiveCall(BaseModel):
    id: str
    callRef: str
    startedAt: str
    direction: str
    centreCode: str | None
    centreName: str | None
    language: str
    farmerName: str | None
    callerLast4: str | None
    elapsedSeconds: int
    turnCount: int
    lastIntent: str | None
    activity: str
    lastReplyMs: int | None
    campaignId: str | None


class Today(BaseModel):
    calls: int
    inbound: int
    outbound: int
    firstReplyP50Ms: int | None
    firstReplyP95Ms: int | None
    handledByAgentPct: float | None
    transferred: int
    offersAccepted: int
    offersPitched: int


class RecentCall(BaseModel):
    id: str
    startedAt: str
    direction: str
    farmerName: str | None
    callerLast4: str | None
    centreCode: str | None
    outcome: str | None
    durationSeconds: int | None
    firstReplyMs: int | None
    dtmf: str | None


class LiveSnapshot(BaseModel):
    capacity: int
    calls: list[LiveCall]
    today: Today
    recent: list[RecentCall]


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #


async def build_snapshot(db: AsyncSession) -> LiveSnapshot:
    return LiveSnapshot(
        capacity=get_settings().max_concurrent_calls,
        calls=await _in_progress(db),
        today=await _today(db),
        recent=await _recent(db),
    )


async def _in_progress(db: AsyncSession) -> list[LiveCall]:
    statement: Select[Any] = (
        select(Call, Centre.code, Centre.name, Farmer.full_name, Farmer.phone_last4)
        .outerjoin(Centre, Call.centre_id == Centre.id)
        .outerjoin(Farmer, Call.farmer_id == Farmer.id)
        .where(Call.status == CallStatus.IN_PROGRESS)
        .order_by(Call.started_at)
    )
    rows = (await db.execute(statement)).all()
    if not rows:
        return []

    call_ids = [call.id for call, *_ in rows]
    turns = (
        await db.execute(
            select(CallTurn.call_id, CallTurn.role, CallTurn.latency_ms, CallTurn.turn_index)
            .where(CallTurn.call_id.in_(call_ids))
            .order_by(CallTurn.turn_index)
        )
    ).all()
    counts: dict[uuid.UUID, int] = {}
    last_reply: dict[uuid.UUID, int | None] = {}
    for call_id, role, latency, _ in turns:
        counts[call_id] = counts.get(call_id, 0) + 1
        if role is TurnRole.ASSISTANT and isinstance(latency, dict):
            total = latency.get("totalMs")
            last_reply[call_id] = int(total) if total is not None else last_reply.get(call_id)

    now = datetime.now(UTC)
    return [
        LiveCall(
            id=str(call.id),
            callRef=call.call_ref,
            startedAt=call.started_at.isoformat(),
            direction=call.direction.value,
            centreCode=centre_code,
            centreName=centre_name,
            language=call.language_final or call.language_detected or "",
            farmerName=farmer_name,
            callerLast4=last4,
            elapsedSeconds=max(0, int((now - call.started_at).total_seconds())),
            turnCount=counts.get(call.id, 0),
            lastIntent=call.intents[-1] if call.intents else None,
            # The snapshot cannot know whether the agent is mid-sentence; the
            # next activity event corrects it within a turn.
            activity="listening",
            lastReplyMs=last_reply.get(call.id),
            campaignId=str(call.campaign_id) if call.campaign_id else None,
        )
        for call, centre_code, centre_name, farmer_name, last4 in rows
    ]


async def _today(db: AsyncSession) -> Today:
    start, end = today_bounds()
    base = select(Call).where(Call.started_at >= start, Call.started_at < end)

    rows = (
        await db.execute(
            select(
                Call.direction,
                Call.outcome,
                Call.was_transferred,
                Call.status,
                Call.latency_stats,
            ).where(Call.started_at >= start, Call.started_at < end)
        )
    ).all()
    del base

    inbound = sum(1 for direction, *_ in rows if direction is CallDirection.INBOUND)
    outbound = len(rows) - inbound
    transferred = sum(1 for _, _, was_transferred, *_ in rows if was_transferred)
    finished = [r for r in rows if r[3] is not CallStatus.IN_PROGRESS]
    handled = sum(1 for _, _, was_transferred, *_ in finished if not was_transferred)
    pitched = sum(
        1
        for direction, outcome, *_ in rows
        if direction is CallDirection.OUTBOUND
        and outcome
        in (CallOutcome.OFFER_ACCEPTED, CallOutcome.OFFER_DECLINED, CallOutcome.OPTED_OUT)
    )
    accepted = sum(
        1
        for direction, outcome, *_ in rows
        if direction is CallDirection.OUTBOUND and outcome is CallOutcome.OFFER_ACCEPTED
    )
    replies = sorted(
        int(stats["first_reply_ms"])
        for *_, stats in rows
        if isinstance(stats, dict) and stats.get("first_reply_ms") is not None
    )

    return Today(
        calls=len(rows),
        inbound=inbound,
        outbound=outbound,
        firstReplyP50Ms=_nearest_rank(replies, 0.5),
        firstReplyP95Ms=_nearest_rank(replies, 0.95),
        handledByAgentPct=round(100.0 * handled / len(finished), 1) if finished else None,
        transferred=transferred,
        offersAccepted=accepted,
        offersPitched=pitched,
    )


def _nearest_rank(values: list[int], p: float) -> int | None:
    if not values:
        return None
    index = max(0, min(len(values) - 1, round(p * len(values) + 0.5) - 1))
    return values[index]


async def _recent(db: AsyncSession) -> list[RecentCall]:
    start, end = today_bounds()
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
        .where(
            Call.started_at >= start,
            Call.started_at < end,
            Call.status != CallStatus.IN_PROGRESS,
        )
        .order_by(Call.started_at.desc())
        .limit(RECENT_LIMIT)
    )
    rows = (await db.execute(statement)).all()
    return [
        RecentCall(
            id=str(call.id),
            startedAt=call.started_at.isoformat(),
            direction=call.direction.value,
            farmerName=farmer_name,
            callerLast4=last4,
            centreCode=centre_code,
            outcome=call.outcome.value if call.outcome else None,
            durationSeconds=call.duration_seconds,
            firstReplyMs=_first_reply(call),
            dtmf=digit,
        )
        for call, centre_code, farmer_name, last4, digit in rows
    ]


def _first_reply(call: Call) -> int | None:
    stats = call.latency_stats if isinstance(call.latency_stats, dict) else {}
    value = stats.get("first_reply_ms")
    return int(value) if value is not None else None


@router.get("/snapshot", response_model=LiveSnapshot)
async def snapshot(
    db: DbDep, _: Annotated[Principal, require_role(Role.CENTRE_MANAGER)]
) -> LiveSnapshot:
    return await build_snapshot(db)


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


@router.get("/events")
async def events(
    request: Request,
    principal: PrincipalDep,
    _: Annotated[Principal, require_role(Role.CENTRE_MANAGER)],
) -> StreamingResponse:
    """The live feed, scoped to what this caller may see."""

    async def snapshot_for_stream() -> Any:
        async with short_session(principal) as db:
            return (await build_snapshot(db)).model_dump()

    # Starts withheld from a scoped caller until the call is attributed to a
    # centre they can see; released ahead of the identification event.
    pending_starts: dict[str, dict[str, Any]] = {}

    async def accept(event: LiveEvent) -> list[tuple[str, Any]]:
        if not event.type.startswith("call."):
            return []
        if event.type == livefeed.CALL_STARTED and not principal.sees_all_centres:
            if event.call_id:
                pending_starts[event.call_id] = event.payload
            return []
        if not visible_centre(principal, event.centre_id):
            if event.type == livefeed.CALL_ENDED and event.call_id:
                pending_starts.pop(event.call_id, None)
            return []
        out: list[tuple[str, Any]] = []
        if event.call_id and event.call_id in pending_starts:
            out.append((livefeed.CALL_STARTED, pending_starts.pop(event.call_id)))
        out.append((event.type, event.payload))
        if event.type == livefeed.CALL_ENDED and event.call_id:
            pending_starts.pop(event.call_id, None)
        return out

    return streaming_response(
        relay(request, snapshot_event="snapshot", snapshot=snapshot_for_stream, accept=accept)
    )


__all__ = ("LiveSnapshot", "build_snapshot", "router")
