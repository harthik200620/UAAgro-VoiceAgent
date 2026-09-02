"""Telephony: codecs, provider serializers, call control, recordings (§4.3, §12.3, §17).

The µ-law tests measure signal-to-noise rather than comparing bytes to a golden
file. A byte comparison tells you the codec changed; an SNR figure tells you
whether it still sounds like speech -- and the first version of this codec
produced bytes that were confidently wrong at -5 dB, which a golden file written
from that same version would have blessed.
"""

from __future__ import annotations

import json
import math
import struct
import wave
from datetime import UTC, datetime

import pytest

from uaagro_domain.enums import AudioCodec, TelephonyProvider
from uaagro_domain.errors import AuthorizationError
from voice_worker.adapters.telephony.base import InboundEventType
from voice_worker.adapters.telephony.control import (
    assert_approved_destination,
    verify_signature,
)
from voice_worker.adapters.telephony.exotel import ExotelSerializer
from voice_worker.adapters.telephony.mulaw import mulaw_to_pcm, pcm_to_mulaw
from voice_worker.adapters.telephony.mulaw_providers import (
    PlivoSerializer,
    TwilioSerializer,
)
from voice_worker.runtime.recording import (
    CHANNELS,
    MAX_BUFFERED_BYTES,
    SAMPLE_RATE,
    RecordingBuffer,
    object_key,
)


def _tone(amplitude: int, samples: int = 800, hz: int = 300) -> bytes:
    return b"".join(
        struct.pack("<h", int(amplitude * math.sin(2 * math.pi * hz * t / SAMPLE_RATE)))
        for t in range(samples)
    )


def _snr_db(original: bytes, decoded: bytes) -> float:
    a = struct.unpack(f"<{len(original) // 2}h", original)
    b = struct.unpack(f"<{len(decoded) // 2}h", decoded)
    error = [x - y for x, y in zip(a, b, strict=True)]
    signal = math.sqrt(sum(x * x for x in a) / len(a))
    noise = math.sqrt(sum(x * x for x in error) / len(error))
    return 20 * math.log10(signal / noise) if noise else math.inf


# --------------------------------------------------------------------------- #
# G.711 µ-law
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("amplitude", [12000, 3000, 500])
def test_mulaw_round_trip_meets_the_standard_snr(amplitude: int) -> None:
    """G.711 gives roughly 38 dB SNR across its dynamic range.

    The number is the test. A codec with the wrong segment table, or one that
    skips the 14-bit shift, still produces plausible bytes at the right length
    -- the first version of this file did, at -5 dB, and sounded like a broken
    model rather than a broken codec.
    """
    pcm = _tone(amplitude)
    decoded = mulaw_to_pcm(pcm_to_mulaw(pcm))
    assert _snr_db(pcm, decoded) > 30, f"SNR collapsed at amplitude {amplitude}"


def test_mulaw_halves_the_byte_count() -> None:
    """8-bit companded from 16-bit linear. A codec that returned the same
    length would be a no-op that happened to typecheck."""
    assert len(pcm_to_mulaw(_tone(8000))) == 800


def test_mulaw_is_flat_across_amplitude() -> None:
    """Companding's whole point: a quiet caller on a bad line gets the same
    relative fidelity as a loud one. Linear PCM at 8 bits would not."""
    quiet = _snr_db(_tone(500), mulaw_to_pcm(pcm_to_mulaw(_tone(500))))
    loud = _snr_db(_tone(12000), mulaw_to_pcm(pcm_to_mulaw(_tone(12000))))
    assert abs(quiet - loud) < 6, f"quiet {quiet:.1f} dB vs loud {loud:.1f} dB"


def test_silence_survives_the_round_trip() -> None:
    """Digital silence must stay near silence. µ-law has no zero codepoint, so
    the round trip lands a few counts off -- audible only if it did not."""
    decoded = mulaw_to_pcm(pcm_to_mulaw(b"\x00\x00" * 400))
    samples = struct.unpack(f"<{len(decoded) // 2}h", decoded)
    assert max(abs(s) for s in samples) < 16


def test_an_odd_trailing_byte_is_dropped_not_padded() -> None:
    """Half a sample is a framing error upstream. Inventing the missing byte
    would turn a detectable bug into a click on the line."""
    assert len(pcm_to_mulaw(b"\x00\x01\x02")) == 1


# --------------------------------------------------------------------------- #
# Provider serializers (§4.3)
# --------------------------------------------------------------------------- #


def test_the_codecs_differ_by_provider_and_are_not_a_flag() -> None:
    """§4.3: separate implementations, not one class with a codec flag.
    Conflating them puts linear16 bytes in a µ-law stream, and the caller hears
    white noise on a provider that worked yesterday."""
    assert ExotelSerializer.codec is AudioCodec.LINEAR16
    assert PlivoSerializer.codec is AudioCodec.MULAW
    assert TwilioSerializer.codec is AudioCodec.MULAW
    # A class attribute, so it cannot be threaded wrong at runtime.
    assert "codec" not in ExotelSerializer.__init__.__code__.co_varnames


@pytest.mark.parametrize("serializer_cls", [PlivoSerializer, TwilioSerializer])
def test_a_start_frame_yields_metadata_for_the_session_to_bind(
    serializer_cls: type,
) -> None:
    """Decoding does not bind. `runtime.session` binds after writing the calls
    row, and the Exotel serializer has the same contract -- a serializer that
    bound itself would work with one call site and silently not with the other.
    """
    serializer = serializer_cls()
    assert not serializer.is_bound

    event = serializer.decode(
        json.dumps(
            {
                "event": "start",
                "start": {
                    "streamSid": "st-1",
                    "callSid": "call-1",
                    "from": "+919999000000",
                    "to": "+911800000000",
                },
            }
        )
    )
    assert event.type is InboundEventType.START
    assert event.metadata is not None
    assert event.metadata.stream_sid == "st-1"
    assert not serializer.is_bound, "decode must not bind; the session does"

    serializer.bind(event.metadata)
    assert serializer.is_bound


def test_plivo_reads_the_caller_from_the_start_frame() -> None:
    serializer = PlivoSerializer()
    event = serializer.decode(
        json.dumps(
            {"event": "start", "start": {"streamSid": "s", "from": "+919999000000"}}
        )
    )
    assert event.metadata is not None
    assert event.metadata.from_number == "+919999000000"


def test_twilio_has_no_caller_unless_twiml_passed_one() -> None:
    """§4.3 records that Twilio needs a companion webhook for identity. The
    serializer returns None rather than inventing a number, which would give
    every Twilio call the same fabricated caller."""
    serializer = TwilioSerializer()
    bare = serializer.decode(json.dumps({"event": "start", "start": {"streamSid": "s"}}))
    assert bare.metadata is not None
    assert bare.metadata.from_number is None

    with_params = TwilioSerializer().decode(
        json.dumps(
            {
                "event": "start",
                "start": {"streamSid": "s", "customParameters": {"from": "+919999000001"}},
            }
        )
    )
    assert with_params.metadata is not None
    assert with_params.metadata.from_number == "+919999000001"


def test_media_arrives_as_pcm_whatever_the_provider_sent() -> None:
    """The serializer's whole job: the pipeline sees one audio format and does
    not know which provider the call came in on."""
    import base64

    pcm = _tone(6000, samples=160)
    serializer = PlivoSerializer()
    frame = json.dumps(
        {
            "event": "media",
            "media": {"payload": base64.b64encode(pcm_to_mulaw(pcm)).decode()},
        }
    )
    event = serializer.decode(frame)
    assert event.type is InboundEventType.MEDIA
    assert event.audio is not None
    assert len(event.audio) == len(pcm)
    assert _snr_db(pcm, event.audio) > 30


def test_outbound_audio_is_mulaw_for_a_mulaw_provider() -> None:
    serializer = PlivoSerializer()
    start = serializer.decode(json.dumps({"event": "start", "start": {"streamSid": "st-9"}}))
    assert start.metadata is not None
    serializer.bind(start.metadata)

    message = json.loads(serializer.encode_audio(_tone(6000, samples=160)))
    assert message["event"] == "media"
    assert message["streamSid"] == "st-9"

    import base64

    payload = base64.b64decode(message["media"]["payload"])
    # 160 samples of PCM is 320 bytes; µ-law halves it.
    assert len(payload) == 160


def test_the_clear_control_exists_for_every_provider() -> None:
    """§5.4: cancelling TTS without flushing the provider's buffer leaves the
    agent talking for a second after the farmer interrupts."""
    # Exotel uses snake_case for the stream id and the µ-law providers use
    # camelCase. Both spellings are sent so this exercises the real frames
    # rather than a shape no provider emits.
    frame = json.dumps(
        {"event": "start", "start": {"streamSid": "s", "stream_sid": "s"}}
    )
    for cls in (ExotelSerializer, PlivoSerializer, TwilioSerializer):
        serializer = cls()
        start = serializer.decode(frame)
        assert start.metadata is not None, f"{cls.__name__} did not decode start"
        serializer.bind(start.metadata)
        assert json.loads(serializer.encode_clear())["event"] == "clear"


@pytest.mark.parametrize(
    "message",
    ["not json at all", '{"event":', json.dumps(["a", "list"]), b"\xff\xfe binary"],
)
def test_a_malformed_frame_never_raises(message: str | bytes) -> None:
    """An exception here drops a live call over one bad packet."""
    assert PlivoSerializer().decode(message).type is InboundEventType.UNKNOWN


def test_an_unrecognised_event_does_not_collide_with_structlog() -> None:
    """`event` is structlog's own key. Binding a provider event name to it
    raises -- on an unrecognised frame, mid-call. That bug shipped once."""
    assert PlivoSerializer().decode(json.dumps({"event": "mark"})).type is (
        InboundEventType.UNKNOWN
    )


def test_dtmf_is_decoded() -> None:
    event = PlivoSerializer().decode(json.dumps({"event": "dtmf", "dtmf": {"digit": "1"}}))
    assert event.type is InboundEventType.DTMF
    assert event.digit == "1"


# --------------------------------------------------------------------------- #
# Call control (§17)
# --------------------------------------------------------------------------- #


def test_an_unapproved_destination_is_refused() -> None:
    """§17: the destination must come from an operator-approved list. A number
    that reached this from a transcript or a retrieved document is how toll
    fraud happens."""
    approved = frozenset({"+919999000000"})
    assert assert_approved_destination("9999000000", approved) == "+919999000000"

    with pytest.raises(AuthorizationError):
        assert_approved_destination("+911234567890", approved)


def test_an_empty_allowlist_permits_nothing() -> None:
    """The failure mode of a misconfigured allowlist must be refusing every
    call, not permitting every call."""
    with pytest.raises(AuthorizationError):
        assert_approved_destination("+919999000000", frozenset())


def test_the_allowlist_is_checked_after_normalisation() -> None:
    """A farmer's number arrives in half a dozen formats. Comparing raw strings
    would let 09999000000 past a list holding +919999000000."""
    approved = frozenset({"+919999000000"})
    for spelling in ("09999000000", "9999000000", "+91 9999 000 000", "919999000000"):
        assert assert_approved_destination(spelling, approved) == "+919999000000"


def test_a_webhook_signature_is_verified() -> None:
    """§17. A forged webhook on this system can mark a call answered, a message
    delivered, or a consent granted."""
    import hashlib
    import hmac as hmac_module

    body = b'{"CallSid":"abc","Status":"completed"}'
    secret = "shhh"
    good = hmac_module.new(secret.encode(), body, hashlib.sha256).hexdigest()

    assert verify_signature(body=body, signature=good, secret=secret)
    assert verify_signature(body=body, signature=f"sha256={good}", secret=secret)
    assert not verify_signature(body=body, signature=good, secret="wrong")
    assert not verify_signature(body=b"tampered", signature=good, secret=secret)


@pytest.mark.parametrize("signature", ["", "   ", "not-hex", "deadbeef"])
def test_a_malformed_signature_is_false_not_an_exception(signature: str) -> None:
    """A webhook endpoint that raises on bad input is a denial-of-service
    target."""
    assert not verify_signature(body=b"x", signature=signature, secret="s")


def test_a_missing_secret_refuses_rather_than_permits() -> None:
    assert not verify_signature(body=b"x", signature="abc", secret="")


# --------------------------------------------------------------------------- #
# Recordings (§11.5, §17, §23-6)
# --------------------------------------------------------------------------- #


def test_the_two_legs_stay_separate() -> None:
    """§19 needs the caller's audio without the agent over it to measure WER,
    and a mixdown cannot be un-mixed."""
    buffer = RecordingBuffer()
    buffer.add_caller(_tone(8000, samples=160))
    buffer.add_agent(_tone(4000, samples=160))

    with wave.open(__import__("io").BytesIO(buffer.to_wav())) as handle:
        assert handle.getnchannels() == CHANNELS
        assert handle.getframerate() == SAMPLE_RATE
        assert handle.getnframes() == 160


def test_a_leg_that_started_late_is_padded_not_truncated() -> None:
    """The agent's leg starts after the greeting, so the two are never the same
    length. Interleaving without padding makes the file go abruptly mono."""
    buffer = RecordingBuffer()
    buffer.add_caller(_tone(8000, samples=320))
    buffer.add_agent(_tone(4000, samples=80))

    with wave.open(__import__("io").BytesIO(buffer.to_wav())) as handle:
        assert handle.getnframes() == 320


def test_an_overlong_call_truncates_loudly() -> None:
    """A recording that silently stops halfway is worse than one marked
    incomplete, because nobody knows to distrust it."""
    buffer = RecordingBuffer()
    chunk = b"\x00" * 100_000
    while not buffer.truncated:
        before = len(buffer.caller)
        buffer.add_caller(chunk)
        if len(buffer.caller) == before and not buffer.truncated:
            pytest.fail("stopped accepting audio without setting truncated")
    assert len(buffer.caller) <= MAX_BUFFERED_BYTES


def test_the_object_key_carries_no_phone_number() -> None:
    """§23-6. A key containing a number puts that number in every access log
    line that touches the object."""
    key = object_key(call_ref="CALL-2026-0001", started_at=datetime(2026, 3, 3, tzinfo=UTC))
    assert key == "recordings/2026/03/03/CALL-2026-0001.wav"
    assert "9999" not in key


def test_the_key_is_stable_so_retries_overwrite() -> None:
    """§11.5's post-call pipeline retries with backoff. A key that varied per
    attempt would multiply storage and leave an auditor unable to say which
    copy was real."""
    started = datetime(2026, 3, 3, 14, 30, tzinfo=UTC)
    assert object_key(call_ref="A", started_at=started) == object_key(
        call_ref="A", started_at=started
    )


def test_an_empty_recording_is_detectable() -> None:
    """A call that dropped before any audio should not upload a header-only
    WAV that looks like a successful recording of silence."""
    assert RecordingBuffer().is_empty
    assert not RecordingBuffer(caller=bytearray(b"\x00\x00")).is_empty


def test_provider_identity_is_on_the_class() -> None:
    assert ExotelSerializer.provider is TelephonyProvider.EXOTEL
    assert PlivoSerializer.provider is TelephonyProvider.PLIVO
    assert TwilioSerializer.provider is TelephonyProvider.TWILIO
