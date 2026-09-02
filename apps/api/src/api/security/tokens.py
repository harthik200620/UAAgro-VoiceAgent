"""Access and refresh tokens (§17).

A short-lived access JWT plus a rotating refresh token, with **family-level
reuse detection**: presenting a refresh token that has already been used means
the token leaked, so every descendant in that family is revoked rather than just
the replayed one. Revoking only the replayed token leaves the thief holding a
valid successor.

Refresh tokens are stored hashed. The raw value exists only in the client's
cookie, so a database copy does not yield usable sessions.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.models import RefreshToken, User
from uaagro_domain.enums import Role
from uaagro_domain.errors import AuthenticationError, MissingCredentialError
from uaagro_domain.settings import Settings

#: §17: 15-minute access token.
ACCESS_TOKEN_TTL = timedelta(minutes=15)
#: Long enough that a manager is not signed out mid-shift, short enough that a
#: stolen cookie expires within a working week.
REFRESH_TOKEN_TTL = timedelta(days=7)

ALGORITHM = "HS256"
ISSUER = "uaagro-api"


@dataclass(frozen=True, slots=True)
class AccessClaims:
    """Decoded access-token claims."""

    user_id: uuid.UUID
    organization_id: uuid.UUID
    role: Role
    centre_ids: tuple[uuid.UUID, ...]
    session_id: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    expires_in: int


def _signing_key(settings: Settings) -> bytes:
    key = settings.jwt_signing_key
    if not key:
        raise MissingCredentialError("JWT_SIGNING_KEY", needed_for="admin session signing")
    try:
        return base64.b64decode(key, validate=True)
    except (ValueError, TypeError):
        # A non-base64 key is still usable as raw bytes; accepting both avoids
        # locking an operator out over an encoding detail.
        return key.encode("utf-8")


def hash_refresh_token(token: str) -> bytes:
    """SHA-256 of the raw token. Only this is stored."""
    return hashlib.sha256(token.encode("ascii")).digest()


def issue_access_token(
    settings: Settings,
    *,
    user: User,
    centre_ids: list[uuid.UUID],
    session_id: uuid.UUID,
    now: datetime | None = None,
) -> tuple[str, int]:
    """Mint an access JWT carrying the caller's role and centre scope.

    The centre list travels in the token so the request path can bind RLS GUCs
    without a database round trip on every call -- but the policies still run in
    Postgres, so a forged claim gains nothing it is not already entitled to.
    """
    moment = now or datetime.now(UTC)
    expires_at = moment + ACCESS_TOKEN_TTL
    payload = {
        "iss": ISSUER,
        "sub": str(user.id),
        "org": str(user.organization_id),
        "role": user.role.value,
        "centres": [str(cid) for cid in centre_ids],
        "sid": str(session_id),
        "iat": int(moment.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, _signing_key(settings), algorithm=ALGORITHM)
    return token, int(ACCESS_TOKEN_TTL.total_seconds())


def decode_access_token(settings: Settings, token: str) -> AccessClaims:
    """Verify and decode. Raises :class:`AuthenticationError` on any problem.

    Every failure returns the same error: distinguishing "expired" from
    "forged" tells an attacker which half of their guess was right.
    """
    try:
        payload = jwt.decode(
            token,
            _signing_key(settings),
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthenticationError("The session token is not valid.") from exc

    try:
        return AccessClaims(
            user_id=uuid.UUID(payload["sub"]),
            organization_id=uuid.UUID(payload["org"]),
            role=Role(payload["role"]),
            centre_ids=tuple(uuid.UUID(cid) for cid in payload.get("centres", [])),
            session_id=uuid.UUID(payload["sid"]),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )
    except (KeyError, ValueError) as exc:
        raise AuthenticationError("The session token is not valid.") from exc


async def issue_refresh_token(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    family_id: uuid.UUID,
    user_agent: str | None = None,
    ip_address: str | None = None,
    now: datetime | None = None,
) -> str:
    """Create and persist a refresh token, returning the raw value once."""
    moment = now or datetime.now(UTC)
    raw = secrets.token_urlsafe(48)
    session.add(
        RefreshToken(
            user_id=user_id,
            family_id=family_id,
            token_hash=hash_refresh_token(raw),
            expires_at=moment + REFRESH_TOKEN_TTL,
            user_agent=(user_agent or "")[:300] or None,
            ip_address=ip_address,
        )
    )
    await session.flush()
    return raw


async def rotate_refresh_token(
    session: AsyncSession,
    *,
    presented: str,
    user_agent: str | None = None,
    ip_address: str | None = None,
    now: datetime | None = None,
) -> tuple[User, uuid.UUID, str]:
    """Exchange a refresh token for a new one.

    Returns ``(user, family_id, new_raw_token)``.

    Raises:
        AuthenticationError: if the token is unknown, expired, revoked, or
            **already used**. Reuse revokes the whole family: a replay means the
            token leaked, and every token descended from it must be assumed
            compromised (§17).
    """
    moment = now or datetime.now(UTC)
    digest = hash_refresh_token(presented)

    stored = await session.scalar(select(RefreshToken).where(RefreshToken.token_hash == digest))
    if stored is None:
        raise AuthenticationError("The session could not be refreshed.")

    if stored.used_at is not None or stored.revoked_at is not None:
        await _revoke_family(session, stored.family_id, reason="reuse_detected", now=moment)
        raise AuthenticationError("The session could not be refreshed.")

    if stored.expires_at <= moment:
        raise AuthenticationError("The session could not be refreshed.")

    user = await session.get(User, stored.user_id)
    if user is None or not user.is_active or user.deleted_at is not None:
        await _revoke_family(session, stored.family_id, reason="user_inactive", now=moment)
        raise AuthenticationError("The session could not be refreshed.")

    stored.used_at = moment
    raw = await issue_refresh_token(
        session,
        user_id=user.id,
        family_id=stored.family_id,
        user_agent=user_agent,
        ip_address=ip_address,
        now=moment,
    )
    return user, stored.family_id, raw


async def revoke_family(
    session: AsyncSession, family_id: uuid.UUID, *, reason: str = "logout"
) -> None:
    await _revoke_family(session, family_id, reason=reason, now=datetime.now(UTC))


async def _revoke_family(
    session: AsyncSession, family_id: uuid.UUID, *, reason: str, now: datetime
) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason=reason)
    )


async def revoke_all_for_user(
    session: AsyncSession, user_id: uuid.UUID, *, reason: str = "admin_revoked"
) -> None:
    """Sign a user out everywhere. Used on deactivation and password change."""
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC), revoked_reason=reason)
    )
