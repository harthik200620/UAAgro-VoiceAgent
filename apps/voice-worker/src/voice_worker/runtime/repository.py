"""SQLAlchemy implementation of the call repository.

Every write here runs on the in-call engine, which carries a hard
``statement_timeout`` (§4.1). A query that would exceed the latency budget is
killed by Postgres rather than allowed to stall the call, and the caller decides
what to do about it -- which for persistence is "log and continue", because
§1 N8 wants the call recorded but never at the cost of the call itself.

The turn writer is the newest part. ``call_turns`` existed from the first
migration and was never written to: the transcript lived only in the worker's
memory and the post-call pipeline "consolidated" a table with nothing in it.
Every finished turn now lands as two rows, the farmer's and the agent's, with
the agent's carrying the §7 latency breakdown -- which is what the panel's
call page reads.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import insert, update

from uaagro_db.engine import incall_session
from uaagro_db.models import Call, CallEvent, CallTurn, CampaignContact, DtmfEvent
from uaagro_domain.enums import (
    CallOutcome,
    CallStatus,
    ContactStatus,
    InterestLevel,
    TransferReason,
    TurnRole,
)

from .session import CallRecord

log = structlog.get_logger(__name__)


class SqlCallRepository:
    """Persists calls, turns, events, DTMF and the campaign link."""

    async def create_call(self, record: CallRecord) -> None:
        """Write the ``calls`` row at INIT.

        §11.1: the row exists before anything references it, so a crash two
        seconds later still leaves a queryable call.
        """
        async with incall_session() as session:
            await session.execute(
                insert(Call).values(
                    id=record.id,
                    started_at=record.started_at,
                    organization_id=record.organization_id,
                    call_ref=record.call_ref,
                    direction=record.direction,
                    provider=record.provider,
                    from_number_hash=record.from_number_hash,
                    to_number_hash=record.to_number_hash,
                    status=CallStatus.IN_PROGRESS,
                )
            )

    async def attach_call_context(
        self,
        call_id: uuid.UUID,
        started_at: datetime,
        *,
        farmer_id: uuid.UUID | None,
        centre_id: uuid.UUID | None,
        campaign_id: uuid.UUID | None,
        language: str | None,
        agent_config_version: int | None,
    ) -> None:
        """Fill in what the pipeline learned once it identified the caller.

        Written as soon as it is known rather than at the end, so a call that
        drops after the greeting is still attributed to its farmer, centre
        and campaign -- and the live view shows who is on the line.
        """
        async with incall_session() as session:
            await session.execute(
                update(Call)
                .where(Call.id == call_id, Call.started_at == started_at)
                .values(
                    farmer_id=farmer_id,
                    centre_id=centre_id,
                    campaign_id=campaign_id,
                    language_detected=language,
                    language_final=language,
                    agent_config_version=agent_config_version,
                    answered_at=datetime.now(UTC),
                )
            )

    async def record_turn(
        self,
        call_id: uuid.UUID,
        started_at: datetime,
        *,
        turn_index: int,
        role: TurnRole,
        text: str,
        language: str | None,
        at_ms: int,
        latency: dict[str, Any] | None,
        tool_calls: list[dict[str, Any]],
        interrupted: bool,
    ) -> None:
        """One row of ``call_turns``.

        ``turn_index`` is unique per call, so the farmer's half of a turn and
        the agent's half are numbered ``2n`` and ``2n+1``: the pair stays
        adjacent in order and the constraint holds without a second column.
        """
        async with incall_session() as session:
            await session.execute(
                insert(CallTurn).values(
                    call_id=call_id,
                    started_at=started_at,
                    turn_index=turn_index,
                    role=role,
                    text_original=text,
                    language=language,
                    audio_offset_ms=at_ms,
                    was_interrupted=interrupted,
                    tool_calls={"calls": tool_calls} if tool_calls else {},
                    latency_ms=latency or {},
                )
            )

    async def record_event(
        self,
        call_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
        started_at: datetime,
    ) -> None:
        async with incall_session() as session:
            await session.execute(
                insert(CallEvent).values(
                    call_id=call_id,
                    # The partition key is the parent call's start, so a long
                    # call's events stay in one partition with the call itself.
                    started_at=started_at,
                    event_type=event_type,
                    event_at=datetime.now(started_at.tzinfo),
                    payload=payload,
                )
            )

    async def record_dtmf(
        self,
        call_id: uuid.UUID,
        digit: str,
        received_at: datetime,
        started_at: datetime,
        context: str | None,
    ) -> None:
        async with incall_session() as session:
            await session.execute(
                insert(DtmfEvent).values(
                    call_id=call_id,
                    started_at=started_at,
                    digit=digit,
                    received_at=received_at,
                    context=context,
                )
            )

    async def mark_transferred(
        self, call_id: uuid.UUID, started_at: datetime, *, reason: str, completed: bool
    ) -> None:
        """The call went to a person, or was meant to (§12.3).

        ``completed`` is whether the provider accepted the hand-over. When it
        did not, the post-call job sees a transfer that never completed and
        opens the follow-up the caller was promised.
        """
        try:
            transfer_reason: TransferReason | None = TransferReason(reason)
        except ValueError:
            transfer_reason = None
        async with incall_session() as session:
            await session.execute(
                update(Call)
                .where(Call.id == call_id, Call.started_at == started_at)
                .values(
                    was_transferred=True,
                    transfer_reason=transfer_reason,
                    transfer_at=datetime.now(UTC),
                    transfer_completed=completed,
                )
            )

    async def link_contact(self, contact_id: uuid.UUID, call_id: uuid.UUID) -> None:
        """Point the campaign contact at the call it produced."""
        async with incall_session() as session:
            await session.execute(
                update(CampaignContact)
                .where(CampaignContact.id == contact_id)
                .values(call_id=call_id, status=ContactStatus.DIALING)
            )

    async def finish_contact(
        self,
        contact_id: uuid.UUID,
        *,
        status: ContactStatus,
        outcome: str | None,
        dtmf: str | None,
        interest: InterestLevel | None,
    ) -> None:
        """What the script decided, written before the call row is finalised."""
        async with incall_session() as session:
            await session.execute(
                update(CampaignContact)
                .where(CampaignContact.id == contact_id)
                .values(status=status, outcome=outcome, dtmf_response=dtmf, interest_level=interest)
            )

    async def finalise_call(
        self,
        call_id: uuid.UUID,
        started_at: datetime,
        *,
        status: CallStatus,
        outcome: CallOutcome | None,
        ended_at: datetime,
        duration_seconds: int,
        error_code: str | None,
        error_detail: str | None,
        latency_stats: dict[str, Any] | None = None,
    ) -> None:
        async with incall_session() as session:
            await session.execute(
                update(Call)
                .where(Call.id == call_id, Call.started_at == started_at)
                .values(
                    status=status,
                    outcome=outcome,
                    ended_at=ended_at,
                    duration_seconds=duration_seconds,
                    billable_seconds=duration_seconds,
                    error_code=error_code,
                    error_detail=error_detail,
                    latency_stats=latency_stats or {},
                )
            )


class NullCallRepository:
    """No-op repository for transport tests and the standalone simulator.

    Every method exists so the session never has to ask whether persistence
    is on; it simply calls, and nothing happens.
    """

    async def create_call(self, record: CallRecord) -> None:
        return None

    async def attach_call_context(
        self,
        call_id: uuid.UUID,
        started_at: datetime,
        *,
        farmer_id: uuid.UUID | None,
        centre_id: uuid.UUID | None,
        campaign_id: uuid.UUID | None,
        language: str | None,
        agent_config_version: int | None,
    ) -> None:
        return None

    async def record_turn(
        self,
        call_id: uuid.UUID,
        started_at: datetime,
        *,
        turn_index: int,
        role: TurnRole,
        text: str,
        language: str | None,
        at_ms: int,
        latency: dict[str, Any] | None,
        tool_calls: list[dict[str, Any]],
        interrupted: bool,
    ) -> None:
        return None

    async def record_event(
        self,
        call_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
        started_at: datetime,
    ) -> None:
        return None

    async def record_dtmf(
        self,
        call_id: uuid.UUID,
        digit: str,
        received_at: datetime,
        started_at: datetime,
        context: str | None,
    ) -> None:
        return None

    async def link_contact(self, contact_id: uuid.UUID, call_id: uuid.UUID) -> None:
        return None

    async def mark_transferred(
        self, call_id: uuid.UUID, started_at: datetime, *, reason: str, completed: bool
    ) -> None:
        return None

    async def finish_contact(
        self,
        contact_id: uuid.UUID,
        *,
        status: ContactStatus,
        outcome: str | None,
        dtmf: str | None,
        interest: InterestLevel | None,
    ) -> None:
        return None

    async def finalise_call(
        self,
        call_id: uuid.UUID,
        started_at: datetime,
        *,
        status: CallStatus,
        outcome: CallOutcome | None,
        ended_at: datetime,
        duration_seconds: int,
        error_code: str | None,
        error_detail: str | None,
        latency_stats: dict[str, Any] | None = None,
    ) -> None:
        return None


__all__ = ("NullCallRepository", "SqlCallRepository")
