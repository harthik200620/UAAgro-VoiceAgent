"""§23-6: never log a phone number, a recording URL or a vendor key (§19).

Untested until now, which is the worst state for a control of this shape: it
sits between every call site and every log handler, it fails silently, and the
failure is only visible to whoever is reading the log aggregator -- by which
point the number is already in it.

§19 puts the redaction in the processor rather than at the call sites on
purpose. There are hundreds of call sites and one processor, and only one of
those numbers stays right as the code grows.
"""

from __future__ import annotations

import pytest

from uaagro_domain.logging import redact_processor


def redact(**fields: object) -> dict[str, object]:
    return dict(redact_processor(None, "info", dict(fields)))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Phone numbers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", ["phone", "from_number", "to_number", "msisdn", "caller", "cli"])
def test_a_phone_field_keeps_only_its_last_four_digits(key: str) -> None:
    """Last four, because support has to be able to say "the call from ...4821"
    without the log holding a number anyone could dial."""
    out = redact(**{key: "+919876543210"})
    masked = str(out[key])
    # The property, not an exact star count: everything but the last four
    # digits is gone, and the last four are still there.
    assert masked.endswith("3210")
    assert "9876543" not in masked
    assert set(masked[:-4]) == {"*"}


def test_a_number_in_free_text_is_caught_even_when_nobody_declared_it() -> None:
    """The realistic failure. Nobody writes ``phone=`` when they are logging an
    error message that happens to quote the caller."""
    out = redact(event="lookup failed for 9876543210")
    assert "9876543210" not in str(out["event"])
    assert "3210" in str(out["event"])


@pytest.mark.parametrize(
    "written",
    ["9876543210", "+919876543210", "+91 9876543210", "+91-9876543210", "919876543210"],
)
def test_every_way_a_number_gets_written_is_masked(written: str) -> None:
    """A farmer's number is written five ways across a call: by the provider,
    by the CRM, by a person typing it into a ticket."""
    out = redact(note=f"caller said {written}")
    assert "876543" not in str(out["note"]), f"{written} survived redaction"


def test_a_number_nested_in_a_dict_is_masked_too() -> None:
    """``log.info("failed", context={"from": ...})`` is an ordinary thing to
    write, and a top-level-only processor walks straight past it."""
    out = redact(context={"from_number": "+919876543210", "attempt": 2})
    nested = out["context"]
    assert isinstance(nested, dict)
    assert "9876543" not in str(nested["from_number"])
    assert str(nested["from_number"]).endswith("3210")
    assert nested["attempt"] == 2


def test_a_list_of_numbers_is_masked() -> None:
    out = redact(phone=["9876543210", "9812345678"])
    masked = out["phone"]
    assert isinstance(masked, list)
    assert [str(m)[-4:] for m in masked] == ["3210", "5678"]
    assert not any(char.isdigit() for m in masked for char in str(m)[:-4])


def test_a_short_string_is_not_mangled_into_a_fake_number() -> None:
    """An order reference is not a phone number, and masking it would make the
    log useless for the thing it was written for."""
    out = redact(order_ref="ORD-2026-0041")
    assert out["order_ref"] == "ORD-2026-0041"


def test_a_landline_and_a_pincode_are_left_alone() -> None:
    """§23-6 is about mobile numbers. A six-digit pincode and a centre's own
    published landline are business data the log is meant to carry."""
    out = redact(pincode="226001", note="centre desk 0522-2345678")
    assert out["pincode"] == "226001"


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "key",
    ["api_key", "authorization", "password", "sarvam_api_key", "refresh_token", "jwt_signing_key"],
)
def test_a_credential_is_replaced_not_masked(key: str) -> None:
    """Masked to its last four would still leak four characters of a key, and
    unlike a phone number there is no support workflow that needs them."""
    out = redact(**{key: "sk-live-abcdef0123456789"})
    assert out[key] == "[redacted]"


def test_a_field_that_merely_ends_in_token_is_redacted() -> None:
    """The suffix rule exists so a new setting added next year is covered
    without anybody remembering to update a list."""
    out = redact(telephony_ws_token="abc123", vendor_secret="s3cret")
    assert out["telephony_ws_token"] == "[redacted]"
    assert out["vendor_secret"] == "[redacted]"


# --------------------------------------------------------------------------- #
# Recording URLs
# --------------------------------------------------------------------------- #


def test_a_recording_url_never_reaches_a_log_line() -> None:
    """§23-6 treats it like a phone number, and for the same reason: it is a
    bearer link to a farmer's voice, and log aggregators are widely readable."""
    out = redact(
        event="upload complete",
        url="https://s3.ap-south-1.amazonaws.com/uaagro-recordings/recordings/2026/08/abc.wav",
    )
    assert "abc.wav" not in str(out["url"])
    assert "[redacted]" in str(out["url"])


def test_an_ordinary_url_is_left_alone() -> None:
    """Over-redaction is its own failure: an engineer who cannot see which
    endpoint failed starts logging around the processor."""
    out = redact(url="https://api.sarvam.ai/v1/speech")
    assert out["url"] == "https://api.sarvam.ai/v1/speech"


# --------------------------------------------------------------------------- #
# The processor's own contract
# --------------------------------------------------------------------------- #


def test_non_string_values_pass_through_unchanged() -> None:
    """Counters, durations and booleans are most of what gets logged."""
    out = redact(latency_ms=142.5, frames=250, ok=True, outcome=None)
    assert out == {"latency_ms": 142.5, "frames": 250, "ok": True, "outcome": None}


def test_redaction_does_not_lose_fields() -> None:
    """A processor that dropped a key would silently make an incident
    unreadable, which is harder to notice than one that over-masks."""
    fields = {"event": "call.ended", "phone": "9876543210", "duration_s": 61, "api_key": "x"}
    assert set(redact(**fields)) == set(fields)
