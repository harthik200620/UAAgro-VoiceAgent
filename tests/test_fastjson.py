"""The JSON codec on the per-frame paths (§7, §7.6, §4.3).

A telephony media frame is encoded and decoded every 20 ms per call, so this is
the one place where the codec is worth choosing. `orjson` is the mainstream
fast option: 3.6x on decode and 18.8x on encode for a real 578-byte frame.

The honest size of that win is 17.7 ms of CPU per second at 50 concurrent calls,
under 2% of one core. It is not why calls are fast, and these tests are not
about speed. They are about the two ways swapping a JSON codec silently breaks a
protocol.

**`orjson.dumps` returns bytes.** A WebSocket `send()` takes both, and the two
are *different frame types*. Handing Exotel a control message as a binary frame
instead of a text frame is a protocol violation -- rejected by some servers,
silently dropped by others, and invisible in a unit test that only checks the
JSON parses.

**Its decode error is a different class.** Existing handlers catch
`json.JSONDecodeError`; if orjson's did not subclass it, one malformed frame
from a vendor would stop being skipped and start ending calls.
"""

from __future__ import annotations

import base64
import json

import pytest

from uaagro_domain import fastjson

# A real Exotel inbound media frame: 20 ms of 8 kHz linear16.
PCM = bytes(range(256)) + bytes(64)
FRAME = {
    "event": "media",
    "sequence_number": 42,
    "stream_sid": "abcdef0123456789",
    "media": {"chunk": 42, "payload": base64.b64encode(PCM).decode("ascii")},
}


def test_the_fast_codec_is_the_one_actually_in_use() -> None:
    """It is a declared dependency. Falling back to the standard library is a
    supported degradation, not the expected state, and a deployment that lost
    it should be visible rather than merely slower."""
    assert fastjson.backend() == "orjson"


# --------------------------------------------------------------------------- #
# The frame-type bug
# --------------------------------------------------------------------------- #


def test_dumps_returns_text_not_bytes() -> None:
    """The bug this shim exists to prevent.

    `orjson.dumps` returns bytes. Passed to `websocket.send()` that produces a
    *binary* frame where the protocol requires text -- and §4.3's media socket
    carries both kinds, so nothing downstream would notice until a provider
    rejected the stream.
    """
    encoded = fastjson.dumps(FRAME)

    assert isinstance(encoded, str), "a control message would become a binary frame"


def test_dumps_bytes_exists_for_callers_that_really_want_bytes() -> None:
    """An HTTP body or a Redis value, never a WebSocket control message."""
    assert isinstance(fastjson.dumps_bytes(FRAME), bytes)


def test_a_frame_survives_a_round_trip_unchanged() -> None:
    """Base64 audio through the codec and back, byte for byte. A payload that
    came back subtly different would be audible and nothing else would fail."""
    restored = fastjson.loads(fastjson.dumps(FRAME))

    assert restored == FRAME
    assert base64.b64decode(restored["media"]["payload"]) == PCM


def test_output_is_compact() -> None:
    """§7.6: every byte is sent 50 times a second per call. The standard
    library call sites passed `separators=(",", ":")` for this reason and the
    replacement must not quietly reintroduce the whitespace."""
    assert " " not in fastjson.dumps({"a": 1, "b": 2})


def test_what_it_writes_is_readable_by_the_standard_library() -> None:
    """The other end of the socket is a vendor, not this codec."""
    assert json.loads(fastjson.dumps(FRAME)) == FRAME


def test_what_the_standard_library_writes_is_readable_here() -> None:
    """And a fixture or a recorded frame written before this change still
    decodes -- `tests/` and `fixtures/` are full of them."""
    assert fastjson.loads(json.dumps(FRAME)) == FRAME


# --------------------------------------------------------------------------- #
# The error-class bug
# --------------------------------------------------------------------------- #


def test_a_malformed_frame_raises_something_existing_handlers_catch() -> None:
    """Adapters skip a bad frame and keep the call alive. If this error escaped
    their `except`, one malformed message from a vendor would end a call."""
    with pytest.raises(fastjson.JsonError):
        fastjson.loads("{not json")

    # And specifically the standard library's class, which is what the call
    # sites were written against.
    with pytest.raises(json.JSONDecodeError):
        fastjson.loads("{not json")


def test_invalid_utf8_is_caught_by_the_same_handler() -> None:
    """The one place the two backends genuinely differ: the standard library
    raises `UnicodeDecodeError` here and orjson folds it into a decode error.
    `JsonError` covers both so a call site behaves the same either way."""
    with pytest.raises(fastjson.JsonError):
        fastjson.loads(b'{"a": "\xff\xfe"}')


# --------------------------------------------------------------------------- #
# Devanagari
# --------------------------------------------------------------------------- #


def test_hindi_survives_the_round_trip() -> None:
    """Every transcript, every answer and §5.5's whole keyterm list is
    Devanagari. A codec that mangled it would break the product, not a
    benchmark."""
    payload = {"transcript": "डीएपी का रेट क्या है", "answer": "जी, उपलब्ध है।"}

    assert fastjson.loads(fastjson.dumps(payload)) == payload


def test_devanagari_is_sent_as_utf8_rather_than_escaped() -> None:
    """The incidental win, and the larger one.

    The standard library escapes non-ASCII to `\\uXXXX` -- six bytes per
    character against three. §5.5 sends up to 6,000 characters of Devanagari to
    the recogniser at the start of *every* call, so this roughly halves that
    message. Both forms are valid JSON and decode identically; only the size
    differs.
    """
    terms = {"context": {"terms": ["एनपीके बारह बत्तीस सोलह", "इमिडाक्लोप्रिड"] * 50}}

    fast = len(fastjson.dumps(terms).encode())
    stdlib = len(json.dumps(terms).encode())

    assert fast < stdlib * 0.75, f"{fast} bytes vs {stdlib}"
    assert fastjson.loads(fastjson.dumps(terms)) == terms


# --------------------------------------------------------------------------- #
# Where it is used
# --------------------------------------------------------------------------- #


def test_the_per_frame_paths_use_it() -> None:
    """The telephony serializers run this code 50 times a second per call.

    Asserted against the source because the alternative -- a benchmark -- would
    pass whether or not the adapter had been switched over.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "apps/voice-worker/src/voice_worker"
    for module in (
        "adapters/telephony/exotel.py",
        "adapters/telephony/mulaw_providers.py",
        "adapters/stt/soniox.py",
    ):
        source = (root / module).read_text(encoding="utf-8")
        assert "fastjson" in source, f"{module} still uses the slow codec"
