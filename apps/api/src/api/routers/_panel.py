"""Shared pieces of the panel's control-plane surface (§15.1).

The routers under ``panel_*`` share a few things that are easy to get subtly
different: how a farmer is identified in a response, where "today" starts,
what a stored contact status is called on a card, and how a long-lived
stream borrows a database session without holding one. They live here so
there is one answer to each.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.engine import scoped_session
from uaagro_domain.enums import ContactStatus
from uaagro_domain.timezone import now_ist

from ..security.deps import Principal


def today_bounds() -> tuple[datetime, datetime]:
    """Midnight to midnight in Lucknow, as UTC instants.

    "Today" on an Indian helpline is the Indian day. A UTC day boundary falls
    at 05:30 IST, which would put the morning's calls in yesterday's count.
    """
    local = now_ist()
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start = start_local.astimezone(UTC)
    return start, start + timedelta(days=1)


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def panel_status(status: ContactStatus) -> str:
    """The contract's four-state vocabulary for a stored contact status."""
    match status:
        case ContactStatus.PENDING:
            return "waiting"
        case ContactStatus.DIALING:
            return "in_call"
        case ContactStatus.COMPLETED | ContactStatus.OPTED_OUT:
            return "done"
        case ContactStatus.NO_ANSWER | ContactStatus.BUSY | ContactStatus.FAILED:
            return "no_answer"
        case _:
            return "removed"


def visible_centre(principal: Principal, centre_id: str | None) -> bool:
    """Whether a live event about this centre is the caller's to see.

    Mirrors the row-level policy: org-wide roles see everything, a centre
    manager sees their centres and nothing that has no centre yet.
    """
    if principal.sees_all_centres:
        return True
    if centre_id is None:
        return False
    return any(str(c) == centre_id for c in principal.centre_ids)


@asynccontextmanager
async def short_session(principal: Principal) -> AsyncIterator[AsyncSession]:
    """An RLS-bound session for one lookup inside a long-lived stream.

    The request-scoped dependency would hold a pooled connection for as long
    as the tab is open. Streams open one of these per snapshot or refresh and
    give it straight back.
    """
    async with scoped_session(
        user_id=principal.user_id,
        role=principal.role.value,
        centre_ids=principal.centre_ids,
        org_id=principal.organization_id,
    ) as session:
        yield session


def latency_of(row: Any) -> dict[str, Any] | None:
    """The agent turn's latency block, as the media path stored it."""
    stored = getattr(row, "latency_ms", None)
    if not stored or not isinstance(stored, dict) or "totalMs" not in stored:
        return None
    return {
        "totalMs": stored.get("totalMs"),
        "fromCache": bool(stored.get("fromCache")),
        "turnMs": stored.get("turnMs"),
        "sttMs": stored.get("sttMs"),
        "toolMs": stored.get("toolMs"),
        "llmMs": stored.get("llmMs"),
        "ttsMs": stored.get("ttsMs"),
        "networkMs": stored.get("networkMs"),
    }


def tools_of(row: Any) -> list[dict[str, Any]]:
    stored = getattr(row, "tool_calls", None) or {}
    calls = stored.get("calls") if isinstance(stored, dict) else None
    if not isinstance(calls, list):
        return []
    return [
        {"name": str(call.get("name", "tool")), "ms": call.get("ms")}
        for call in calls
        if isinstance(call, dict)
    ]


def as_uuid(value: str, *, what: str) -> uuid.UUID:
    from uaagro_domain.errors import ValidationError

    try:
        return uuid.UUID(value)
    except ValueError:
        raise ValidationError(
            f"{what} is not a valid id.", remedy="Pick it from the list."
        ) from None


__all__ = (
    "as_uuid",
    "iso",
    "latency_of",
    "panel_status",
    "short_session",
    "today_bounds",
    "tools_of",
    "visible_centre",
)
