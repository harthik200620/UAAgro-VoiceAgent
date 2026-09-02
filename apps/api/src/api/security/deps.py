"""Authentication and authorisation dependencies (§17).

Two layers, and both must hold:

1. **Role checks here**, so an unauthorised request is refused before it reaches
   a query.
2. **Row-Level Security in Postgres**, bound per request from the caller's
   claims. §10 is explicit that application-layer scoping alone is one forgotten
   ``WHERE`` clause from a breach, so the database enforces centre scope
   independently of anything this module does.

A request that skips :func:`current_principal` therefore also skips the GUC
binding, and RLS returns nothing rather than everything -- the failure mode is
an empty result, not a leak.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.engine import bind_rls_context, get_app_sessionmaker
from uaagro_domain.enums import ROLE_RANK, Role
from uaagro_domain.errors import (
    AuthenticationError,
    AuthorizationError,
    ServiceUnavailableError,
)
from uaagro_domain.settings import Settings, get_settings

from .tokens import AccessClaims, decode_access_token


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller."""

    user_id: uuid.UUID
    organization_id: uuid.UUID
    role: Role
    centre_ids: tuple[uuid.UUID, ...]
    session_id: uuid.UUID

    def has_role(self, minimum: Role) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[minimum]

    @property
    def sees_all_centres(self) -> bool:
        return self.role in (Role.SUPER_ADMIN, Role.OPS_MANAGER, Role.AUDITOR)


def get_app_settings() -> Settings:
    return get_settings()


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError("An access token is required.")
    token = authorization[7:].strip()
    if not token:
        raise AuthenticationError("An access token is required.")
    return token


async def current_principal(
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """Decode and validate the access token."""
    claims: AccessClaims = decode_access_token(settings, _bearer_token(authorization))
    return Principal(
        user_id=claims.user_id,
        organization_id=claims.organization_id,
        role=claims.role,
        centre_ids=claims.centre_ids,
        session_id=claims.session_id,
    )


PrincipalDep = Annotated[Principal, Depends(current_principal)]


async def scoped_db(principal: PrincipalDep) -> AsyncIterator[AsyncSession]:
    """A database session with the caller's RLS context bound.

    Every read of a protected table goes through here. The GUCs are set with
    ``SET LOCAL`` inside the transaction, so they cannot leak into the next
    request that borrows the same pooled connection.
    """
    async with get_app_sessionmaker()() as session:
        try:
            await bind_rls_context(
                session,
                user_id=principal.user_id,
                role=principal.role.value,
                centre_ids=principal.centre_ids,
                org_id=principal.organization_id,
            )
        except (SQLAlchemyError, OSError) as exc:
            # asyncpg raises OSError straight through on a refused connection,
            # so both are caught: an outage must not surface as a traceback.
            raise ServiceUnavailableError(
                service="the database", detail=type(exc).__name__
            ) from exc
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


DbDep = Annotated[AsyncSession, Depends(scoped_db)]


async def unscoped_db() -> AsyncIterator[AsyncSession]:
    """A session with no principal, for the login path only.

    Authentication has to read ``users`` before a principal exists. ``users`` is
    deliberately **not** an RLS-protected table -- staff accounts are org-level,
    not centre-level -- so this cannot be used to reach farmer or call data.
    """
    async with get_app_sessionmaker()() as session:
        try:
            await bind_rls_context(session, user_id=None, role="anonymous", centre_ids=())
        except (SQLAlchemyError, OSError) as exc:
            raise ServiceUnavailableError(
                service="the database", detail=type(exc).__name__
            ) from exc
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


UnscopedDbDep = Annotated[AsyncSession, Depends(unscoped_db)]


def require_role(minimum: Role) -> object:
    """Dependency factory enforcing a minimum role."""

    async def _check(principal: PrincipalDep, request: Request) -> Principal:
        if not principal.has_role(minimum):
            raise AuthorizationError(action=request.method.lower(), resource=request.url.path)
        return principal

    return Depends(_check)


def require_exact_roles(*allowed: Role) -> object:
    """Dependency factory for a set of roles that is not a rank prefix.

    An agronomist may approve advisory content while an ops_manager may not,
    even though ops_manager outranks them -- approval is a professional
    responsibility, not a privilege level (§9, §18).
    """
    permitted = frozenset(allowed)

    async def _check(principal: PrincipalDep, request: Request) -> Principal:
        if principal.role not in permitted and principal.role is not Role.SUPER_ADMIN:
            raise AuthorizationError(action=request.method.lower(), resource=request.url.path)
        return principal

    return Depends(_check)


def assert_centre_access(principal: Principal, centre_id: uuid.UUID) -> None:
    """Guard an explicit centre reference.

    RLS already filters reads. This exists for writes that *name* a centre,
    where a rejected row would otherwise surface as a confusing constraint
    error instead of a clear authorisation failure.
    """
    if principal.sees_all_centres:
        return
    if centre_id not in principal.centre_ids:
        raise AuthorizationError(action="access", resource=f"centre {centre_id}")
