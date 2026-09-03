"""Scheduled pulls from the client's database (§15.1).

The panel starts a pull by hand; this runs the ones on a schedule. "Hourly"
and "daily" mean at least that long since the last successful run, checked
once an hour, so a daily source that failed overnight is tried again at the
next check rather than tomorrow. The service that does the pull is the
API's: one implementation of the upsert, whichever process is running it,
and the run rows it writes are the ones the panel shows.

Sources are pulled one after another. A client's database is usually one
server, and two pulls from it at once would only make both slower.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


async def run_scheduled_syncs(
    ctx: dict[str, Any], *, session_factory: SessionFactory | None = None
) -> str:
    """Pull every scheduled source whose interval has passed.

    ``session_factory`` defaults to the system session, which is what the
    upsert needs: ``inventory`` is row-level secured and a session with no
    role bound would write nothing. Tests pass their own.
    """
    from api.services.sources import sync as source_sync
    from uaagro_db.engine import system_session

    factory = session_factory or system_session
    due = await source_sync.due_sources(factory)
    if not due:
        return ""
    started = 0
    for source_id in due:
        run_id = await source_sync.run_now(factory, source_id, started_by=None)
        if run_id is not None:
            started += 1
    if started:
        log.info("sources.synced", runs=started, due=len(due))
    return f"synced {started} of {len(due)} due"


__all__ = ("run_scheduled_syncs",)
