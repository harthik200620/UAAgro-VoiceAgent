"""Password and API-key hashing.

Argon2id per §17. Lives in the data package rather than the API because the
hash is a stored artefact: the seed loader, the API and any future admin CLI
must all produce and verify the same format, and duplicating parameters across
packages is how a rehash-on-login path quietly diverges.
"""

from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from argon2.low_level import Type

#: OWASP's second recommended Argon2id profile (46 MiB, t=1, p=1). Chosen over
#: the 19 MiB profile because the admin panel is low-traffic -- login cost is
#: irrelevant here, and memory hardness is the whole point.
_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=47104,
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)

#: Prefix on generated API keys, so a leaked string is greppable in logs and
#: recognisable to secret scanners.
API_KEY_PREFIX = "uak"


def hash_password(password: str) -> str:
    return _HASHER.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time verification. Never raises for a wrong password."""
    try:
        return _HASHER.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True when the stored hash used weaker parameters than the current profile."""
    try:
        return _HASHER.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def generate_api_key() -> tuple[str, str, str]:
    """Mint an API key.

    Returns ``(full_key, prefix, hash)``. The full key is shown once at
    creation and never stored; only the prefix and hash persist (§17).
    """
    secret = secrets.token_urlsafe(32)
    prefix = secrets.token_hex(4)
    full = f"{API_KEY_PREFIX}_{prefix}_{secret}"
    return full, prefix, _HASHER.hash(full)


def verify_api_key(candidate: str, stored_hash: str) -> bool:
    try:
        return _HASHER.verify(stored_hash, candidate)
    except (VerifyMismatchError, InvalidHashError):
        return False
