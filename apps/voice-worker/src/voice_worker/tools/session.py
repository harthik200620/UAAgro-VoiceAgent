"""Database access for tools.

Tools read on the **in-call engine**, which carries a hard 120 ms
``statement_timeout`` (§4.1). A query that would blow the §6.3 budget is killed
by Postgres rather than allowed to stall a live call, and the tool layer turns
that into a hold phrase.

The factory is swappable so tool tests can run against a throwaway database
without a live worker. It is a seam for tests, not a fallback the production
path can wander into: nothing changes it at runtime.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.engine import incall_session

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

_factory: SessionFactory = incall_session


def set_session_factory(factory: SessionFactory) -> None:
    """Point tools at a different database. Tests only."""
    global _factory
    _factory = factory


def reset_session_factory() -> None:
    global _factory
    _factory = incall_session


@asynccontextmanager
async def tool_session() -> AsyncIterator[AsyncSession]:
    """A session for one tool call."""
    async with _factory() as session:
        yield session
