"""SQLAlchemy implementation of the call repository.

Every write here runs on the in-call engine, which carries a hard
``statement_timeout`` (§4.1). A query that would exceed the latency budget is
killed by Postgres rather than allowed to stall the call, and the caller decides
what to do about it -- which for persistence is "log and continue", because
§1 N8 wants the call recorded but never at the cost of the call itself.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import insert, update

from uaagro_db.engine import incall_session
from uaagro_db.models import Call, CallEvent, DtmfEvent
from uaagro_domain.enums import CallOutcome, CallStatus

from .session import CallRecord

log = structlog.get_logger(__name__)


class SqlCallRepository:
    """Persists calls, events and DTMF."""

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
                )
            )


class NullCallRepository:
    """No-op repository for transport tests and the standalone simulator.

    Not a mock standing in for unwritten code -- it is the correct dependency
    when exercising the protocol without a database, and the real one is used
    everywhere else.
    """

    async def create_call(self, record: CallRecord) -> None:
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
    ) -> None:
        return None
