"""Phone-number protection (§17).

Two independent transformations of the same number, for two different jobs:

``phone_hash``
    ``HMAC-SHA256(national_number, pepper)``. Deterministic, so it can carry a
    unique index and serve as the *only* lookup key for a farmer. Not
    reversible, so a database copy alone does not yield phone numbers.

``phone_enc``
    AES-256-GCM under a data key that is itself wrapped by KMS. Reversible, and
    decryption is a privileged, audited operation -- the agent never needs it,
    only an outbound dial or an explicit staff action does.

``phone_last4`` (a plain column elsewhere) exists so list views never decrypt.

The pepper is effectively permanent: rotating it changes every hash and orphans
every farmer row. Rotating the DEK is supported, because the wrapped key travels
inside each ciphertext.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

import structlog
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from uaagro_domain.errors import ConfigurationError, MissingCredentialError
from uaagro_domain.phone import Msisdn, normalise_msisdn
from uaagro_domain.settings import Settings, get_settings

log = structlog.get_logger(__name__)

#: Envelope format version. Byte 0 of every ciphertext.
#: 1 = local development key, 2 = KMS-wrapped data key.
ENVELOPE_LOCAL: Final = 1
ENVELOPE_KMS: Final = 2

_NONCE_BYTES: Final = 12
_KEY_BYTES: Final = 32
_MIN_PEPPER_BYTES: Final = 32


# --------------------------------------------------------------------------- #
# Key providers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DataKey:
    """A plaintext data key and the wrapped form to store beside the ciphertext."""

    plaintext: bytes
    wrapped: bytes


class KeyProvider(ABC):
    """Supplies data keys for envelope encryption."""

    envelope_version: int

    @abstractmethod
    def generate(self) -> DataKey:
        """Mint a fresh data key."""

    @abstractmethod
    def unwrap(self, wrapped: bytes) -> bytes:
        """Recover the plaintext data key from its wrapped form."""


class LocalKeyProvider(KeyProvider):
    """Development provider: a single static key from ``LOCAL_DEK_BASE64``.

    Never selected when ``APP_ENV`` is staging or production -- :func:`get_cipher`
    routes those to KMS and raises if it is not configured.
    """

    envelope_version = ENVELOPE_LOCAL

    def __init__(self, key: bytes) -> None:
        if len(key) != _KEY_BYTES:
            raise ConfigurationError(
                f"LOCAL_DEK_BASE64 decodes to {len(key)} bytes; AES-256 needs {_KEY_BYTES}.",
                remedy="Generate one with: "
                'python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"',
            )
        self._key = key

    def generate(self) -> DataKey:
        # The wrapped form is empty: the key is recovered from the environment.
        return DataKey(plaintext=self._key, wrapped=b"")

    def unwrap(self, wrapped: bytes) -> bytes:
        return self._key


class KmsKeyProvider(KeyProvider):
    """Production provider: AWS KMS ``GenerateDataKey`` / ``Decrypt``.

    The plaintext data key is cached in memory so a burst of writes during a
    seasonal peak does not become a burst of KMS calls; the wrapped key travels
    inside each ciphertext, so rotation is a matter of dropping the cache.
    """

    envelope_version = ENVELOPE_KMS

    def __init__(self, key_id: str, region: str) -> None:
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ConfigurationError(
                "boto3 is required for KMS-backed encryption but is not installed.",
                remedy="Install the db package with its dependencies (uv sync).",
            ) from exc
        self._client = boto3.client("kms", region_name=region)
        self._key_id = key_id
        self._cached: DataKey | None = None
        self._unwrap_cache: dict[bytes, bytes] = {}

    def generate(self) -> DataKey:
        if self._cached is None:
            response = self._client.generate_data_key(KeyId=self._key_id, KeySpec="AES_256")
            self._cached = DataKey(
                plaintext=response["Plaintext"],
                wrapped=response["CiphertextBlob"],
            )
        return self._cached

    def unwrap(self, wrapped: bytes) -> bytes:
        cached = self._unwrap_cache.get(wrapped)
        if cached is not None:
            return cached
        response = self._client.decrypt(CiphertextBlob=wrapped, KeyId=self._key_id)
        plaintext: bytes = response["Plaintext"]
        self._unwrap_cache[wrapped] = plaintext
        return plaintext


# --------------------------------------------------------------------------- #
# The cipher
# --------------------------------------------------------------------------- #


class PhoneCipher:
    """Hashes and encrypts Indian phone numbers."""

    def __init__(self, *, pepper: bytes, key_provider: KeyProvider) -> None:
        if len(pepper) < _MIN_PEPPER_BYTES:
            raise ConfigurationError(
                f"PHONE_HASH_PEPPER decodes to {len(pepper)} bytes; at least "
                f"{_MIN_PEPPER_BYTES} are required.",
                remedy="Generate one with: "
                'python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())" '
                "-- and never rotate it, since every farmer lookup key derives from it.",
            )
        self._pepper = pepper
        self._keys = key_provider

    # -- hashing ---------------------------------------------------------- #

    def hash(self, phone: str | Msisdn) -> bytes:
        """Deterministic lookup key. Normalises first, so ``+91 98765 43210``
        and ``09876543210`` collapse to one farmer row."""
        msisdn = phone if isinstance(phone, Msisdn) else normalise_msisdn(phone)
        return hmac.new(self._pepper, msisdn.national.encode("ascii"), hashlib.sha256).digest()

    def verify(self, phone: str | Msisdn, digest: bytes) -> bool:
        """Constant-time comparison against a stored hash."""
        return hmac.compare_digest(self.hash(phone), digest)

    # -- encryption ------------------------------------------------------- #

    def encrypt_field(self, plaintext: bytes) -> bytes:
        """Envelope-encrypt an arbitrary sensitive field.

        Layout::

            version(1) | wrapped_len(2, big-endian) | wrapped | nonce(12) | ct+tag

        The envelope version and wrapped key are authenticated as AAD, so an
        attacker cannot swap a KMS ciphertext for a local-key one.

        Used for phone numbers and for TOTP seeds. Keeping both on one key path
        means there is a single place to rotate keys, rather than two that drift.
        """
        data_key = self._keys.generate()
        nonce = os.urandom(_NONCE_BYTES)
        header = struct.pack(">BH", self._keys.envelope_version, len(data_key.wrapped))
        aad = header + data_key.wrapped
        ciphertext = AESGCM(data_key.plaintext).encrypt(nonce, plaintext, aad)
        return aad + nonce + ciphertext

    def decrypt_field(self, blob: bytes) -> bytes:
        """Recover a field encrypted by :meth:`encrypt_field`."""
        if len(blob) < 3 + _NONCE_BYTES:
            raise ConfigurationError(
                "Encrypted value is too short to be a valid envelope.",
                remedy="The row is corrupt. Restore it from a backup rather than "
                "overwriting it, and record the incident.",
            )
        version, wrapped_len = struct.unpack(">BH", blob[:3])
        if version not in (ENVELOPE_LOCAL, ENVELOPE_KMS):
            raise ConfigurationError(
                f"Unknown envelope version {version}.",
                remedy="This value was written by a newer build. Upgrade the service "
                "rather than attempting to read it with this one.",
            )
        offset = 3 + wrapped_len
        wrapped = blob[3:offset]
        nonce = blob[offset : offset + _NONCE_BYTES]
        ciphertext = blob[offset + _NONCE_BYTES :]
        key = self._keys.unwrap(wrapped)
        return AESGCM(key).decrypt(nonce, ciphertext, blob[:offset])

    def encrypt(self, phone: str | Msisdn) -> bytes:
        """Envelope-encrypt a phone number."""
        msisdn = phone if isinstance(phone, Msisdn) else normalise_msisdn(phone)
        return self.encrypt_field(msisdn.national.encode("ascii"))

    def decrypt(self, blob: bytes) -> Msisdn:
        """Recover a number. Callers must write an audit row (§17)."""
        return normalise_msisdn(self.decrypt_field(blob).decode("ascii"))

    def last4(self, phone: str | Msisdn) -> str:
        msisdn = phone if isinstance(phone, Msisdn) else normalise_msisdn(phone)
        return msisdn.last4


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


def build_cipher(settings: Settings) -> PhoneCipher:
    """Construct the cipher for the current environment.

    §0 rule 4: a missing pepper or KMS key is a loud failure naming the
    variable, not a silent fallback to a weaker mode.
    """
    pepper_b64 = settings.phone_hash_pepper
    if not pepper_b64:
        raise MissingCredentialError(
            "PHONE_HASH_PEPPER", needed_for="the farmer phone lookup index"
        )
    pepper = _decode_key("PHONE_HASH_PEPPER", pepper_b64)

    if settings.is_production and not settings.allow_local_dek_in_production:
        key_id = settings.kms_key_id
        if not key_id:
            raise MissingCredentialError(
                "KMS_KEY_ID", needed_for="envelope encryption of stored phone numbers"
            )
        provider: KeyProvider = KmsKeyProvider(key_id, settings.s3_region)
    else:
        if settings.is_production:
            # Chosen, not defaulted into: the operator set the flag, and the
            # log says so once at boot so a later reader knows where the key is.
            log.warning(
                "crypto.local_dek_in_production",
                remedy="Move the data key to a KMS when one is available.",
            )
        local_b64 = settings.local_dek_base64
        if not local_b64:
            raise MissingCredentialError(
                "LOCAL_DEK_BASE64",
                needed_for="encrypting phone numbers in a non-production environment",
            )
        provider = LocalKeyProvider(_decode_key("LOCAL_DEK_BASE64", local_b64))

    return PhoneCipher(pepper=pepper, key_provider=provider)


@lru_cache(maxsize=1)
def get_cipher() -> PhoneCipher:
    return build_cipher(get_settings())


def reset_cipher_cache() -> None:
    """Tests call this after changing key material in the environment."""
    get_cipher.cache_clear()


def _decode_key(variable: str, value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ConfigurationError(
            f"{variable} is not valid base64.",
            remedy="Generate one with: "
            'python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"',
            context={"variable": variable},
        ) from exc
