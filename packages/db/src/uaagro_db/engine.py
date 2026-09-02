"""Async engines and session factories.

Three distinct access paths, deliberately not one pool:

``app``
    The control plane. Connects as ``uaagro_app`` (a non-owner role), so RLS
    policies apply. Every request binds the caller's identity to session GUCs
    before touching a scoped table.

``incall``
    The media path. Same role and policies, but a hard
    ``statement_timeout`` of ~120 ms: §4.1 requires that a slow admin query can
    never add latency to a live call, and §7.6 forbids blocking the audio loop.
    A query that would exceed the budget is killed rather than allowed to run
    long, and the tool layer turns that into a cached hold phrase.

``migrator``
    Schema owner. Used by Alembic and the seed loader only. Never serves
    requests -- an owner connection bypasses RLS unless every table is FORCEd,
    and relying on FORCE alone is one ``ALTER TABLE`` away from a breach.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from uaagro_domain.settings import Settings, get_settings

from .roles import SYSTEM_ROLE

#: Session GUCs the RLS policies read. Set with ``SET LOCAL`` so they are scoped
#: to the transaction and cannot leak to the next checkout of a pooled
#: connection -- a leaked centre scope is a cross-tenant read.
GUC_USER_ID = "app.user_id"
GUC_ROLE = "app.role"
GUC_CENTRE_IDS = "app.centre_ids"
GUC_ORG_ID = "app.org_id"


def _engine_kwargs(settings: Settings) -> dict[str, object]:
    return {
        "echo": False,
        "pool_pre_ping": True,
        "pool_size": settings.database_pool_size,
        "max_overflow": settings.database_pool_size,
        # Recycle below the typical cloud idle-timeout so a reaped connection
        # is never handed to a live call.
        "pool_recycle": 1800,
        "connect_args": {"server_settings": {"application_name": "uaagro"}},
    }


@lru_cache(maxsize=1)
def get_app_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(settings.database_url, **_engine_kwargs(settings))


@lru_cache(maxsize=1)
def get_incall_engine() -> AsyncEngine:
    """Engine for queries issued from the media path.

    The statement timeout is set per connection rather than per query so that a
    forgotten timeout cannot slip a slow query into the audio loop.
    """
    settings = get_settings()
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=0,  # queue rather than open unbounded connections in a peak
        pool_recycle=1800,
        connect_args={
            "server_settings": {
                "application_name": "uaagro-incall",
                "statement_timeout": str(settings.database_incall_statement_timeout_ms),
            }
        },
    )
    return engine


@lru_cache(maxsize=1)
def get_migrator_engine() -> AsyncEngine:
    """Owner-role engine. NullPool: migrations and seeds are short-lived."""
    settings = get_settings()
    return create_async_engine(settings.database_url_migrator, echo=False, poolclass=NullPool)


@lru_cache(maxsize=1)
def get_app_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        get_app_engine(), expire_on_commit=False, autoflush=False, class_=AsyncSession
    )


@lru_cache(maxsize=1)
def get_incall_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        get_incall_engine(), expire_on_commit=False, autoflush=False, class_=AsyncSession
    )


@lru_cache(maxsize=1)
def get_migrator_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        get_migrator_engine(), expire_on_commit=False, autoflush=False, class_=AsyncSession
    )


async def bind_rls_context(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    role: str,
    centre_ids: Sequence[uuid.UUID],
    org_id: uuid.UUID | None = None,
) -> None:
    """Bind the caller's identity for the current transaction.

    Values are passed as bound parameters into ``set_config(..., true)`` rather
    than interpolated into a ``SET LOCAL`` string: a GUC value is still SQL, and
    building it by concatenation is an injection point.

    The ``true`` third argument makes the setting transaction-local, so it is
    discarded on commit or rollback and never survives into another request's
    use of the same pooled connection.
    """
    centre_csv = ",".join(str(cid) for cid in centre_ids)
    await session.execute(
        text(
            "SELECT set_config(:k_user, :v_user, true),"
            "       set_config(:k_role, :v_role, true),"
            "       set_config(:k_centres, :v_centres, true),"
            "       set_config(:k_org, :v_org, true)"
        ),
        {
            "k_user": GUC_USER_ID,
            "v_user": str(user_id) if user_id else "",
            "k_role": GUC_ROLE,
            "v_role": role,
            "k_centres": GUC_CENTRE_IDS,
            "v_centres": centre_csv,
            "k_org": GUC_ORG_ID,
            "v_org": str(org_id) if org_id else "",
        },
    )


@asynccontextmanager
async def scoped_session(
    *,
    user_id: uuid.UUID | None,
    role: str,
    centre_ids: Sequence[uuid.UUID],
    org_id: uuid.UUID | None = None,
) -> AsyncIterator[AsyncSession]:
    """An app session with RLS context bound for its whole transaction."""
    async with get_app_sessionmaker()() as session:
        await bind_rls_context(
            session, user_id=user_id, role=role, centre_ids=centre_ids, org_id=org_id
        )
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def incall_session() -> AsyncIterator[AsyncSession]:
    """A session for the media path.

    The agent reads catalogue, inventory and advisory data on behalf of the
    caller, not on behalf of a staff user, so it binds the service role. RLS
    policies grant that role org-wide read access and no cross-tenant access.
    """
    async with get_incall_sessionmaker()() as session:
        await bind_rls_context(session, user_id=None, role="voice_agent", centre_ids=())
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def system_session() -> AsyncIterator[AsyncSession]:
    """A session for background jobs (§11.5, §13.1).

    Binds the ``system`` role, which the RLS policies grant org-wide read.
    Without it ``current_setting('app.role', true)`` is NULL, the policy
    predicate is NULL, and the job sees no rows at all -- so the post-call
    pipeline reports "call not found" for every call it is handed.

    Not the migrator session: RLS is FORCEd, so the owner is subject to the
    policies too, and running jobs as the owner would only mean they hold DDL
    rights while still seeing nothing.
    """
    async with get_app_sessionmaker()() as session:
        await bind_rls_context(session, user_id=None, role=SYSTEM_ROLE, centre_ids=())
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def migrator_session() -> AsyncIterator[AsyncSession]:
    """Owner session for migrations and seeding only."""
    async with get_migrator_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engines() -> None:
    """Close pools. Called on SIGTERM so a drain finishes cleanly (§20)."""
    for factory in (get_app_engine, get_incall_engine, get_migrator_engine):
        if factory.cache_info().currsize:
            await factory().dispose()
    reset_engine_cache()


def reset_engine_cache() -> None:
    for factory in (
        get_app_engine,
        get_incall_engine,
        get_migrator_engine,
        get_app_sessionmaker,
        get_incall_sessionmaker,
        get_migrator_sessionmaker,
    ):
        factory.cache_clear()
