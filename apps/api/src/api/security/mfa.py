"""TOTP multi-factor authentication (§17).

MFA is **mandatory**, not optional: this system holds the phone numbers,
locations and buying history of roughly 150,000 farmers, and a password alone is
one credential-stuffing run away from all of it.

The TOTP seed is field-level encrypted at rest under the same envelope scheme as
phone numbers, so a database copy does not yield working second factors.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import pyotp

from uaagro_db.crypto import PhoneCipher
from uaagro_domain.errors import AuthenticationError

#: One step either side of now, so a phone clock a few seconds out still works.
#: Wider windows meaningfully extend the replay surface.
VALID_WINDOW = 1

ISSUER_NAME = "UA Agro"


def generate_secret() -> str:
    """A fresh base32 TOTP seed."""
    return pyotp.random_base32()


def provisioning_uri(secret: str, *, email: str) -> str:
    """The ``otpauth://`` URI an authenticator app scans."""
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER_NAME)


def verify_code(secret: str, code: str, *, at: datetime | None = None) -> bool:
    """Check a six-digit code.

    Returns False rather than raising for a wrong code -- a failed second factor
    is an expected event, not an exceptional one.
    """
    cleaned = code.strip().replace(" ", "")
    if not cleaned.isdigit() or len(cleaned) != 6:
        return False
    return bool(
        pyotp.TOTP(secret).verify(
            cleaned, for_time=at or datetime.now(UTC), valid_window=VALID_WINDOW
        )
    )


def encrypt_secret(cipher: PhoneCipher, secret: str) -> bytes:
    """Encrypt a TOTP seed for storage.

    Uses the same envelope scheme and the same key provider as phone numbers, so
    there is one key-management path in the system rather than two that drift.
    """
    return cipher.encrypt_field(secret.encode("ascii"))


def decrypt_secret(cipher: PhoneCipher, blob: bytes) -> str:
    """Recover a TOTP seed."""
    try:
        return cipher.decrypt_field(blob).decode("ascii")
    except Exception as exc:
        # Never surface the underlying crypto error to a caller: it would say
        # whether the row is corrupt or the key is wrong.
        raise AuthenticationError("The stored second factor could not be read.") from exc


def base32_is_valid(secret: str) -> bool:
    try:
        base64.b32decode(secret, casefold=True)
    except (ValueError, TypeError):
        return False
    return True
