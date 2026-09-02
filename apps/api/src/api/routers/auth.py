"""Authentication endpoints (§17).

The sign-in flow is deliberately two-step, because MFA is mandatory:

1. ``POST /auth/login`` -- email and password. A correct password yields a
   short-lived *MFA token*, never a session. An account that has not enrolled a
   second factor is handed a seed and must enrol before it can sign in at all.
2. ``POST /auth/mfa/verify`` -- the MFA token plus a TOTP code. This is what
   issues the access token and sets the refresh cookie.

Failures are uniform. Whether the email is unknown, the password wrong or the
account locked, the caller sees the same message: telling them which half was
right is a gift to a credential-stuffing run.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
import structlog
from fastapi import APIRouter, Cookie, Request, Response, status
from sqlalchemy import func, select

from uaagro_db.audit import append_audit
from uaagro_db.crypto import get_cipher
from uaagro_db.models import User, UserCentreAccess
from uaagro_db.passwords import hash_password, needs_rehash, verify_password
from uaagro_domain.enums import AuditAction
from uaagro_domain.errors import AuthenticationError
from uaagro_domain.settings import Settings

from ..schemas.auth import (
    CurrentUser,
    LoginRequest,
    MfaChallenge,
    MfaEnrolmentRequired,
    MfaEnrolRequest,
    MfaVerifyRequest,
    TokenResponse,
)
from ..security import mfa, ratelimit
from ..security.deps import DbDep, PrincipalDep, SettingsDep, UnscopedDbDep
from ..security.tokens import (
    REFRESH_TOKEN_TTL,
    issue_access_token,
    issue_refresh_token,
    revoke_family,
    rotate_refresh_token,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "uaagro_refresh"

#: The interstitial token lives only long enough to type a six-digit code.
MFA_TOKEN_TTL = timedelta(minutes=5)
MFA_TOKEN_PURPOSE = "mfa"  # noqa: S105 -- a claim label, not a secret

#: Lock an account after this many consecutive failures, for this long. Chosen
#: to stop automated guessing without letting an attacker lock a real centre
#: manager out of the panel for a whole shift during a peak.
MAX_FAILED_LOGINS = 8
LOCKOUT_DURATION = timedelta(minutes=15)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _issue_mfa_token(settings: Settings, user_id: uuid.UUID) -> str:
    from ..security.tokens import ALGORITHM, ISSUER, _signing_key

    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": ISSUER,
            "sub": str(user_id),
            "purpose": MFA_TOKEN_PURPOSE,
            "iat": int(now.timestamp()),
            "exp": int((now + MFA_TOKEN_TTL).timestamp()),
        },
        _signing_key(settings),
        algorithm=ALGORITHM,
    )


def _read_mfa_token(settings: Settings, token: str) -> uuid.UUID:
    from ..security.tokens import ALGORITHM, ISSUER, _signing_key

    try:
        payload = jwt.decode(token, _signing_key(settings), algorithms=[ALGORITHM], issuer=ISSUER)
        if payload.get("purpose") != MFA_TOKEN_PURPOSE:
            # An access token must not be usable to complete MFA.
            raise AuthenticationError("Authentication failed.")
        return uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise AuthenticationError("Authentication failed.") from exc


async def _centre_ids(db: UnscopedDbDep, user_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await db.execute(
        select(UserCentreAccess.centre_id).where(UserCentreAccess.user_id == user_id)
    )
    return list(rows.scalars().all())


def _set_refresh_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=int(REFRESH_TOKEN_TTL.total_seconds()),
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/auth",
    )


@router.post("/login", response_model=None)
async def login(
    payload: LoginRequest,
    request: Request,
    db: UnscopedDbDep,
    settings: SettingsDep,
) -> MfaChallenge | MfaEnrolmentRequired:
    """Verify a password. Never returns a session on its own."""
    await ratelimit.check("login-ip", _client_ip(request), ratelimit.LOGIN_LIMIT)
    await ratelimit.check("login-account", payload.email.lower(), ratelimit.LOGIN_PER_ACCOUNT_LIMIT)

    user = await db.scalar(
        select(User).where(
            func.lower(User.email) == payload.email.lower(), User.deleted_at.is_(None)
        )
    )
    now = datetime.now(UTC)

    if user is None:
        # Hash anyway, so a missing account is not distinguishable by timing.
        hash_password(payload.password)
        await _record_failure(db, None, request)
        raise AuthenticationError()

    if user.locked_until is not None and user.locked_until > now:
        await _record_failure(db, user, request)
        raise AuthenticationError()

    if not user.is_active or not verify_password(payload.password, user.password_hash):
        user.failed_login_count += 1
        if user.failed_login_count >= MAX_FAILED_LOGINS:
            user.locked_until = now + LOCKOUT_DURATION
        await _record_failure(db, user, request)
        raise AuthenticationError()

    user.failed_login_count = 0
    user.locked_until = None
    if needs_rehash(user.password_hash):
        # Transparently upgrade to the current Argon2 profile on a good login.
        user.password_hash = hash_password(payload.password)

    mfa_token = _issue_mfa_token(settings, user.id)

    if not user.mfa_enrolled:
        # §17: MFA is mandatory. An unenrolled account gets a seed, not a
        # session, and cannot proceed until it has enrolled.
        secret = mfa.generate_secret()
        return MfaEnrolmentRequired(
            mfa_token=mfa_token,
            secret=secret,
            provisioning_uri=mfa.provisioning_uri(secret, email=user.email),
            expires_in=int(MFA_TOKEN_TTL.total_seconds()),
        )

    return MfaChallenge(mfa_token=mfa_token, expires_in=int(MFA_TOKEN_TTL.total_seconds()))


@router.post("/mfa/enrol", response_model=TokenResponse)
async def enrol_mfa(
    payload: MfaEnrolRequest,
    request: Request,
    response: Response,
    db: UnscopedDbDep,
    settings: SettingsDep,
) -> TokenResponse:
    """Complete enrolment by proving the authenticator works, then sign in."""
    user_id = _read_mfa_token(settings, payload.mfa_token)
    user = await db.get(User, user_id)
    if user is None or not user.is_active or user.mfa_enrolled:
        raise AuthenticationError()

    if not mfa.base32_is_valid(payload.secret) or not mfa.verify_code(payload.secret, payload.code):
        raise AuthenticationError("Authentication failed.")

    user.mfa_secret_enc = mfa.encrypt_secret(get_cipher(), payload.secret)
    user.mfa_enrolled_at = datetime.now(UTC)
    await append_audit(
        db,
        action=AuditAction.MFA_ENROLLED,
        resource_type="user",
        resource_id=str(user.id),
        actor_user_id=user.id,
        actor_ip=_client_ip(request),
    )
    return await _complete_signin(db, response, settings, user, request)


@router.post("/mfa/verify", response_model=TokenResponse)
async def verify_mfa(
    payload: MfaVerifyRequest,
    request: Request,
    response: Response,
    db: UnscopedDbDep,
    settings: SettingsDep,
) -> TokenResponse:
    """Exchange a valid TOTP code for a session."""
    await ratelimit.check("mfa-ip", _client_ip(request), ratelimit.LOGIN_LIMIT)

    user_id = _read_mfa_token(settings, payload.mfa_token)
    user = await db.get(User, user_id)
    if user is None or not user.is_active or user.mfa_secret_enc is None:
        raise AuthenticationError()

    secret = mfa.decrypt_secret(get_cipher(), user.mfa_secret_enc)
    if not mfa.verify_code(secret, payload.code):
        user.failed_login_count += 1
        await db.flush()
        raise AuthenticationError()

    return await _complete_signin(db, response, settings, user, request)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    response: Response,
    db: UnscopedDbDep,
    settings: SettingsDep,
    uaagro_refresh: Annotated[str | None, Cookie()] = None,
) -> TokenResponse:
    """Rotate the refresh token and mint a new access token.

    Replaying a used token revokes the entire family (§17).
    """
    await ratelimit.check("refresh-ip", _client_ip(request), ratelimit.REFRESH_LIMIT)
    if not uaagro_refresh:
        raise AuthenticationError("The session could not be refreshed.")

    user, family_id, new_token = await rotate_refresh_token(
        db,
        presented=uaagro_refresh,
        user_agent=request.headers.get("user-agent"),
        ip_address=_client_ip(request),
    )
    centre_ids = await _centre_ids(db, user.id)
    access_token, expires_in = issue_access_token(
        settings, user=user, centre_ids=centre_ids, session_id=family_id
    )
    _set_refresh_cookie(response, settings, new_token)
    return TokenResponse(access_token=access_token, expires_in=expires_in)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    principal: PrincipalDep,
    db: DbDep,
) -> Response:
    """End the session and revoke its whole refresh family."""
    await revoke_family(db, principal.session_id, reason="logout")
    await append_audit(
        db,
        action=AuditAction.LOGOUT,
        resource_type="user",
        resource_id=str(principal.user_id),
        actor_user_id=principal.user_id,
        actor_ip=_client_ip(request),
    )
    response.delete_cookie(REFRESH_COOKIE, path="/auth")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=CurrentUser)
async def me(principal: PrincipalDep, db: DbDep) -> CurrentUser:
    user = await db.get(User, principal.user_id)
    if user is None:
        raise AuthenticationError()
    return CurrentUser(
        id=str(user.id),
        email=user.email,
        full_name=user.full_name,
        role=user.role.value,
        centre_ids=[str(cid) for cid in principal.centre_ids],
        mfa_enrolled=user.mfa_enrolled,
    )


async def _complete_signin(
    db: UnscopedDbDep,
    response: Response,
    settings: Settings,
    user: User,
    request: Request,
) -> TokenResponse:
    """Issue the session pair and record the login."""
    family_id = uuid.uuid4()
    centre_ids = await _centre_ids(db, user.id)
    access_token, expires_in = issue_access_token(
        settings, user=user, centre_ids=centre_ids, session_id=family_id
    )
    refresh_token = await issue_refresh_token(
        db,
        user_id=user.id,
        family_id=family_id,
        user_agent=request.headers.get("user-agent"),
        ip_address=_client_ip(request),
    )
    user.last_login_at = datetime.now(UTC)
    user.failed_login_count = 0
    await append_audit(
        db,
        action=AuditAction.LOGIN_SUCCESS,
        resource_type="user",
        resource_id=str(user.id),
        actor_user_id=user.id,
        actor_ip=_client_ip(request),
    )
    _set_refresh_cookie(response, settings, refresh_token)
    return TokenResponse(access_token=access_token, expires_in=expires_in)


async def _record_failure(db: UnscopedDbDep, user: User | None, request: Request) -> None:
    """Audit a failed attempt.

    Timing is levelled by hashing a password even for an unknown account -- see
    the ``login`` handler. Argon2 dominates the response time either way, which
    is what makes account enumeration by timing impractical here.
    """
    await append_audit(
        db,
        action=AuditAction.LOGIN_FAILURE,
        resource_type="user",
        resource_id=str(user.id) if user else None,
        actor_user_id=user.id if user else None,
        actor_ip=_client_ip(request),
    )
    await db.flush()
