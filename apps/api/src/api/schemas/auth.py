"""Request and response models for authentication.

Every field is validated at the boundary (§17). Responses never echo a
credential, a TOTP seed, or anything that would tell an attacker which half of
a guess was correct.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from ..security.tokens import ACCESS_TOKEN_TTL


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    #: Long enough to be worth hashing, bounded so a multi-megabyte body cannot
    #: turn Argon2 into a denial-of-service vector.
    password: str = Field(min_length=8, max_length=256)


class MfaChallenge(BaseModel):
    """Issued after a correct password. Not a session yet."""

    status: str = "mfa_required"
    mfa_token: str
    expires_in: int = 300


class MfaEnrolmentRequired(BaseModel):
    """§17 makes MFA mandatory, so an unenrolled account cannot sign in."""

    status: str = "mfa_enrolment_required"
    mfa_token: str
    secret: str
    provisioning_uri: str
    expires_in: int = 300


class MfaVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mfa_token: str
    code: str = Field(min_length=6, max_length=8, pattern=r"^\d{6}$")


class MfaEnrolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mfa_token: str
    secret: str = Field(min_length=16, max_length=64)
    code: str = Field(min_length=6, max_length=8, pattern=r"^\d{6}$")


class TokenResponse(BaseModel):
    """A completed sign-in.

    The refresh token is **not** in the body -- it is set as an httpOnly,
    Secure, SameSite=Lax cookie so script on a compromised page cannot read it.
    """

    access_token: str
    token_type: str = "Bearer"  # noqa: S105 -- the OAuth scheme name
    expires_in: int = int(ACCESS_TOKEN_TTL.total_seconds())


class CurrentUser(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    full_name: str
    role: str
    centre_ids: list[str]
    mfa_enrolled: bool
