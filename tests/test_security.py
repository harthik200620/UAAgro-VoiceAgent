"""Security primitives: phone envelope encryption, passwords, audit chain (§17)."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from uaagro_db.audit import canonical_payload, compute_row_hash
from uaagro_db.crypto import ENVELOPE_LOCAL, LocalKeyProvider, PhoneCipher, build_cipher
from uaagro_db.passwords import (
    generate_api_key,
    hash_password,
    needs_rehash,
    verify_api_key,
    verify_password,
)
from uaagro_domain.enums import AuditAction
from uaagro_domain.errors import ConfigurationError, MissingCredentialError
from uaagro_domain.settings import Settings

# --------------------------------------------------------------------------- #
# Phone protection
# --------------------------------------------------------------------------- #


def test_hash_is_stable_across_every_written_form(cipher: PhoneCipher) -> None:
    """The lookup key must not depend on how the number was typed (§10)."""
    forms = ["9876543210", "09876543210", "+91 98765 43210", "919876543210"]
    digests = {cipher.hash(form) for form in forms}
    assert len(digests) == 1


def test_hash_is_32_bytes_and_differs_per_number(cipher: PhoneCipher) -> None:
    first = cipher.hash("9876543210")
    second = cipher.hash("9876543211")
    assert len(first) == 32
    assert first != second


def test_hash_verification_is_exact(cipher: PhoneCipher) -> None:
    digest = cipher.hash("9876543210")
    assert cipher.verify("+919876543210", digest)
    assert not cipher.verify("9876543211", digest)


@given(st.integers(min_value=6_000_000_000, max_value=9_999_999_999))
def test_encryption_round_trips_any_valid_number(number: int) -> None:
    from uaagro_db.crypto import build_cipher as build

    cipher = build(Settings())
    blob = cipher.encrypt(str(number))
    assert cipher.decrypt(blob).national == str(number)


def test_ciphertext_differs_every_time_for_the_same_number(cipher: PhoneCipher) -> None:
    """A deterministic ciphertext would let anyone with the database group
    farmers by number without ever decrypting anything."""
    first = cipher.encrypt("9876543210")
    second = cipher.encrypt("9876543210")
    assert first != second
    assert cipher.decrypt(first).national == cipher.decrypt(second).national


def test_envelope_declares_its_key_provider(cipher: PhoneCipher) -> None:
    blob = cipher.encrypt("9876543210")
    assert blob[0] == ENVELOPE_LOCAL


def test_tampered_ciphertext_is_rejected(cipher: PhoneCipher) -> None:
    """AES-GCM authenticates, so a flipped bit fails rather than decrypting to
    a different number -- which on an outbound dialer would call a stranger."""
    from cryptography.exceptions import InvalidTag

    blob = bytearray(cipher.encrypt("9876543210"))
    blob[-1] ^= 0x01
    with pytest.raises(InvalidTag):
        cipher.decrypt(bytes(blob))


def test_tampering_with_the_envelope_header_is_rejected(cipher: PhoneCipher) -> None:
    """The header is authenticated as AAD, so the version cannot be swapped."""
    from cryptography.exceptions import InvalidTag

    blob = bytearray(cipher.encrypt("9876543210"))
    blob[0] = 2  # claim this was KMS-wrapped
    with pytest.raises((InvalidTag, ConfigurationError)):
        cipher.decrypt(bytes(blob))


def test_truncated_ciphertext_fails_with_an_actionable_error(cipher: PhoneCipher) -> None:
    with pytest.raises(ConfigurationError) as caught:
        cipher.decrypt(b"\x01\x00")
    assert "backup" in caught.value.remedy.lower()


def test_short_pepper_is_refused() -> None:
    """A weak pepper would make the lookup index brute-forcible."""
    with pytest.raises(ConfigurationError) as caught:
        PhoneCipher(pepper=b"tooshort", key_provider=LocalKeyProvider(b"K" * 32))
    assert "PHONE_HASH_PEPPER" in caught.value.message


def test_wrong_length_dek_is_refused() -> None:
    with pytest.raises(ConfigurationError) as caught:
        LocalKeyProvider(b"only-sixteen-byt")
    assert "AES-256" in caught.value.message


def test_missing_pepper_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """§0 rule 4: fail loudly naming the missing variable."""
    monkeypatch.delenv("PHONE_HASH_PEPPER", raising=False)
    settings = Settings(phone_hash_pepper=None)
    with pytest.raises(MissingCredentialError) as caught:
        build_cipher(settings)
    assert caught.value.variable == "PHONE_HASH_PEPPER"


def test_production_without_kms_refuses_to_start() -> None:
    """§17: production must use a KMS-wrapped data key, never a local one."""
    settings = Settings(
        app_env="production",
        phone_hash_pepper=base64.b64encode(b"P" * 32).decode(),
        kms_key_id=None,
    )
    with pytest.raises(MissingCredentialError) as caught:
        build_cipher(settings)
    assert caught.value.variable == "KMS_KEY_ID"


def test_production_readiness_check_covers_every_security_control() -> None:
    settings = Settings(app_env="production")
    with pytest.raises(MissingCredentialError):
        settings.verify_production_readiness()


def _production_settings(**overrides: object) -> Settings:
    """Every §17 control present, so a refusal below is about the override."""
    return Settings(
        app_env="production",
        kms_key_id="arn:aws:kms:ap-south-1:000000000000:key/test",
        phone_hash_pepper=base64.b64encode(b"P" * 32).decode(),
        jwt_signing_key=base64.b64encode(b"J" * 32).decode(),
        telephony_ws_token="test-ws-token-not-a-secret",
        internal_api_token="test-internal-token-not-a-secret",
        session_cookie_secure=True,
        **overrides,
    )


def test_production_refuses_recordings_on_a_local_disk() -> None:
    """A recording on a container's disk vanishes with the container (§18)."""
    settings = _production_settings(storage_backend="local", outbound_quick_dial_self_approve=False)
    with pytest.raises(ConfigurationError) as caught:
        settings.verify_production_readiness()
    assert "STORAGE_BACKEND" in caught.value.message


def test_production_refuses_a_self_approving_quick_dial() -> None:
    """§13.1's four-eyes rule holds for every campaign in production."""
    settings = _production_settings(storage_backend="s3", outbound_quick_dial_self_approve=True)
    with pytest.raises(ConfigurationError) as caught:
        settings.verify_production_readiness()
    assert "OUTBOUND_QUICK_DIAL_SELF_APPROVE" in caught.value.message
    _production_settings(
        storage_backend="s3", outbound_quick_dial_self_approve=False
    ).verify_production_readiness()


def test_placeholder_credentials_are_treated_as_absent() -> None:
    """A copied-but-unedited .env must fail clearly, not send FILL_ME to a vendor."""
    settings = Settings(sarvam_api_key="FILL_ME", deepgram_api_key="  ")
    assert settings.sarvam_api_key is None
    assert settings.deepgram_api_key is None


# --------------------------------------------------------------------------- #
# Passwords and API keys
# --------------------------------------------------------------------------- #


def test_password_hash_is_argon2id() -> None:
    assert hash_password("correct horse").startswith("$argon2id$")


def test_password_verification() -> None:
    stored = hash_password("DevOnly!Passw0rd")
    assert verify_password("DevOnly!Passw0rd", stored)
    assert not verify_password("wrong", stored)


def test_verification_of_a_corrupt_hash_returns_false_rather_than_raising() -> None:
    assert not verify_password("anything", "not-a-hash")
    assert needs_rehash("not-a-hash")


def test_same_password_hashes_differently_each_time() -> None:
    assert hash_password("same") != hash_password("same")


def test_api_key_is_prefixed_hashed_and_verifiable() -> None:
    full, prefix, stored = generate_api_key()
    assert full.startswith("uak_")
    assert prefix in full
    assert verify_api_key(full, stored)
    assert not verify_api_key("uak_other_key", stored)


# --------------------------------------------------------------------------- #
# Audit chain
# --------------------------------------------------------------------------- #


def _payload(index: int, after: dict[str, object]) -> bytes:
    return canonical_payload(
        chain_index=index,
        actor_user_id=None,
        action=AuditAction.UPDATE,
        resource_type="inventory",
        resource_id="variant-1",
        before=None,
        after=after,
        at=datetime(2026, 8, 31, 10, 0, tzinfo=UTC),
        request_id="req-1",
    )


def test_chain_links_each_row_to_its_predecessor() -> None:
    first = compute_row_hash(None, _payload(1, {"price": 100}))
    second = compute_row_hash(first, _payload(2, {"price": 110}))
    third = compute_row_hash(second, _payload(3, {"price": 120}))
    assert len({first, second, third}) == 3


def test_altering_a_historical_row_breaks_its_hash() -> None:
    """§17: this is the property that makes the log tamper-evident."""
    first = compute_row_hash(None, _payload(1, {"price": 100}))
    genuine = compute_row_hash(first, _payload(2, {"price": 110}))
    forged = compute_row_hash(first, _payload(2, {"price": 999}))
    assert genuine != forged


def test_reordering_rows_breaks_the_chain() -> None:
    """The chain index is part of the payload, so rows cannot be swapped."""
    assert _payload(1, {"price": 100}) != _payload(2, {"price": 100})


def test_serialisation_is_independent_of_dict_ordering() -> None:
    """Otherwise a Python-level iteration-order change would read as tampering."""
    assert _payload(1, {"a": 1, "b": 2}) == _payload(1, {"b": 2, "a": 1})


def test_same_instant_hashes_identically_across_timezones() -> None:
    """A row written by a worker in IST and verified in UTC must still verify.

    10:00 UTC and 15:30 IST are the same moment. If the encoder took the naive
    local time, the daily chain check would report tampering every time a
    process ran in a different timezone from the one that wrote the row.
    """
    ist = timezone(timedelta(hours=5, minutes=30))
    as_utc = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
    as_ist = datetime(2026, 8, 31, 15, 30, tzinfo=ist)
    assert as_utc == as_ist  # the premise: one instant, two representations

    def encode(moment: datetime) -> bytes:
        return canonical_payload(
            chain_index=1,
            actor_user_id=None,
            action=AuditAction.LOGIN_SUCCESS,
            resource_type="user",
            resource_id="u1",
            before=None,
            after=None,
            at=moment,
            request_id=None,
        )

    assert encode(as_utc) == encode(as_ist)


def test_different_instants_do_not_collide() -> None:
    """Guards the test above from passing because the timestamp was dropped."""

    def encode(moment: datetime) -> bytes:
        return canonical_payload(
            chain_index=1,
            actor_user_id=None,
            action=AuditAction.LOGIN_SUCCESS,
            resource_type="user",
            resource_id="u1",
            before=None,
            after=None,
            at=moment,
            request_id=None,
        )

    assert encode(datetime(2026, 8, 31, 10, 0, tzinfo=UTC)) != encode(
        datetime(2026, 8, 31, 10, 0, 1, tzinfo=UTC)
    )


def test_devanagari_survives_serialisation_unescaped() -> None:
    """Audit payloads carry Hindi field values; escaping them would still hash
    consistently, but keeping them readable matters for a human reviewer."""
    payload = canonical_payload(
        chain_index=1,
        actor_user_id=None,
        action=AuditAction.UPDATE,
        resource_type="product",
        resource_id="FRT-DAP-50",
        before={"name_hi": "डीएपी"},
        after={"name_hi": "डी.ए.पी."},
        at=datetime(2026, 8, 31, tzinfo=UTC),
        request_id=None,
    )
    assert "डीएपी".encode() in payload


def test_production_may_keep_the_data_key_locally_only_when_told_to() -> None:
    """A cloud host with no KMS says so explicitly; the key is then required
    from the environment, and KMS_KEY_ID is not."""
    dek = base64.b64encode(b"K" * 32).decode()
    base: dict[str, object] = {
        "app_env": "production",
        "kms_key_id": None,
        # Explicit, so a LOCAL_DEK_BASE64 in the developer's shell cannot
        # satisfy the case that must fail.
        "local_dek_base64": None,
        "phone_hash_pepper": base64.b64encode(b"P" * 32).decode(),
        "jwt_signing_key": base64.b64encode(b"J" * 32).decode(),
        "telephony_ws_token": "test-ws-token-not-a-secret",
        "internal_api_token": "test-internal-token-not-a-secret",
        "session_cookie_secure": True,
        "storage_backend": "s3",
        "outbound_quick_dial_self_approve": False,
    }
    explicit = Settings(**{**base, "allow_local_dek_in_production": True, "local_dek_base64": dek})
    explicit.verify_production_readiness()
    cipher = build_cipher(explicit)
    assert isinstance(cipher._keys, LocalKeyProvider)

    without_key = Settings(**{**base, "allow_local_dek_in_production": True})
    with pytest.raises(MissingCredentialError) as caught:
        without_key.verify_production_readiness()
    assert caught.value.variable == "LOCAL_DEK_BASE64"

    # The flag off, the requirement is unchanged: KMS or nothing.
    silent = Settings(**{**base, "local_dek_base64": dek})
    with pytest.raises(MissingCredentialError) as caught:
        silent.verify_production_readiness()
    assert caught.value.variable == "KMS_KEY_ID"
