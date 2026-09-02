"""What Bakbak is asked for, and what comes back as audio (§5.3, §23-8).

Two kinds of defect live in a synthesis adapter, and only one of them is loud.

The loud kind is a rejected request -- a wrong field name, an unsupported
sample rate -- which fails on the first call and gets fixed. The quiet kind is
audio that decodes to *something*: a sample format read with the wrong width,
or a fractional sample dropped at every chunk boundary. That still plays. It
plays as a farmer hearing a voice that is subtly wrong on a line that is
already bad, and no test that checks "did we get bytes back" ever notices.

So the conversion tests here assert on sample values, not on lengths.

The streaming endpoint returns PCM F32LE whatever ``codec`` says, and the
worker's audio bus is signed 16-bit -- so the adapter converts. That is a
sample-*format* change at an unchanged 8 kHz, not the resampling §23-8 forbids.
The rate is requested from the vendor, and asked for wrong it is refused rather
than fixed up here, which is the distinction those tests pin down.
"""

from __future__ import annotations

import base64
import json
import struct
from typing import Any

import httpx
import numpy as np
import pytest

from uaagro_domain.enums import AudioCodec
from uaagro_domain.errors import (
    ConfigurationError,
    MissingCredentialError,
    VendorUnavailableError,
)
from uaagro_domain.settings import Settings
from voice_worker.adapters.tts.bakbak import BakbakTTS
from voice_worker.adapters.tts.base import TtsConfig

VOICE = "11111111-2222-4333-8444-555555555555"


def config_for(**overrides: Any) -> TtsConfig:
    base: dict[str, Any] = {
        "language": "hi-IN",
        "model": "standard",
        "speaker": VOICE,
        "pace": 0.97,
        "sample_rate": 8000,
        "codec": AudioCodec.LINEAR16,
    }
    base.update(overrides)
    return TtsConfig(**base)


def sse(*chunks: bytes, done: bool = True) -> bytes:
    """Build a response body in the vendor's documented SSE shape."""
    parts: list[str] = []
    for raw in chunks:
        payload = {
            "type": "chunk",
            "status_code": 206,
            "done": False,
            "data": base64.b64encode(raw).decode("ascii"),
            "step_time": 0.05,
        }
        parts.append(f"event: chunk\ndata: {json.dumps(payload)}\n\n")
    if done:
        parts.append('event: done\ndata: {"type":"done","status_code":200,"done":true}\n\n')
    return "".join(parts).encode("utf-8")


def f32(samples: list[float]) -> bytes:
    return struct.pack(f"<{len(samples)}f", *samples)


def s16(audio: bytes) -> list[int]:
    return list(np.frombuffer(audio, dtype="<i2"))


def build(
    handler: Any, settings: Settings | None = None
) -> tuple[BakbakTTS, list[httpx.Request]]:
    """A synthesiser wired to a mock transport, plus the requests it made."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    settings = settings or Settings()
    client = httpx.AsyncClient(
        base_url=settings.bakbak_base_url,
        headers={"X-API-Key": settings.bakbak_api_key or ""},
        transport=httpx.MockTransport(record),
    )
    return BakbakTTS(settings, client=client), seen


async def synthesise(tts: BakbakTTS, text: str, config: TtsConfig) -> bytes:
    return b"".join([chunk.audio async for chunk in tts.synthesise(text, config)])


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #


async def test_the_telephony_rate_is_requested_not_converted_to() -> None:
    """§23-8. Asking for 24 kHz and downsampling here would sound worse and
    cost CPU on every sentence of every call."""
    tts, seen = build(lambda _r: httpx.Response(200, content=sse(f32([0.0]))))
    try:
        await synthesise(tts, "नमस्ते", config_for())
    finally:
        await tts.close()

    body = json.loads(seen[0].content)
    assert body["sample_rate"] == 8000


async def test_the_route_language_is_translated_to_the_vendors_code() -> None:
    """The routing table speaks BCP-47 and Bakbak speaks ``hi`` and ``en-in``.

    The mapping lives in the adapter because it is one vendor's dialect (§4.1);
    putting ``en-in`` in the routing table would leak it into the audio cache
    key and the admin panel too.
    """
    tts, seen = build(lambda _r: httpx.Response(200, content=sse(f32([0.0]))))
    try:
        await synthesise(tts, "नमस्ते", config_for(language="hi-IN"))
        await synthesise(tts, "hello", config_for(language="en-IN"))
    finally:
        await tts.close()

    assert [json.loads(r.content)["language"] for r in seen] == ["hi", "en-in"]


async def test_the_voice_and_model_go_together() -> None:
    """A voice id belongs to exactly one model, and the vendor rejects the
    pair rather than substituting -- so both come from the same config."""
    tts, seen = build(lambda _r: httpx.Response(200, content=sse(f32([0.0]))))
    try:
        await synthesise(tts, "नमस्ते", config_for(model="m1"))
    finally:
        await tts.close()

    body = json.loads(seen[0].content)
    assert body["voice_id"] == VOICE
    assert body["model"] == "m1"


async def test_speech_is_slightly_slower_and_never_faster() -> None:
    """§5.3. Rural callers on a bad line lose words at default pace, and the
    vendor's own range is 0.5-1.5, so an out-of-range pace is clamped rather
    than sent to be rejected mid-call."""
    tts, seen = build(lambda _r: httpx.Response(200, content=sse(f32([0.0]))))
    try:
        await synthesise(tts, "नमस्ते", config_for(pace=0.97))
        await synthesise(tts, "नमस्ते", config_for(pace=9.0))
    finally:
        await tts.close()

    speeds = [json.loads(r.content)["speed"] for r in seen]
    assert speeds[0] == 0.97
    assert speeds[1] == 1.5


# --------------------------------------------------------------------------- #
# The audio
# --------------------------------------------------------------------------- #


async def test_float_samples_arrive_as_the_right_integers() -> None:
    """The quiet defect: 32-bit floats read as anything else still decode.

    A width or endianness mistake produces audio that plays -- as noise, or as
    a voice at the wrong pitch -- so this checks sample values rather than that
    bytes came back at all.
    """
    tts, _ = build(lambda _r: httpx.Response(200, content=sse(f32([0.0, 0.5, -0.5, 1.0]))))
    try:
        audio = await synthesise(tts, "नमस्ते", config_for())
    finally:
        await tts.close()

    assert s16(audio) == [0, 16383, -16383, 32767]


async def test_an_overshooting_sample_clips_instead_of_wrapping() -> None:
    """A synthesiser occasionally overshoots by a fraction. Wrapped, a loud
    sample becomes a loud sample of the opposite sign -- a click, on the
    loudest syllable of the sentence."""
    tts, _ = build(lambda _r: httpx.Response(200, content=sse(f32([1.4, -1.4]))))
    try:
        audio = await synthesise(tts, "नमस्ते", config_for())
    finally:
        await tts.close()

    assert s16(audio) == [32767, -32767]


async def test_a_sample_split_across_two_chunks_is_not_dropped() -> None:
    """Nothing guarantees a chunk holds a whole number of 4-byte floats.

    Dropping the remainder deletes a sample at every chunk boundary. Once, that
    is inaudible; over a whole answer it is a rising click track, and the audio
    still plays, so nothing downstream complains.
    """
    payload = f32([0.25, 0.5, 0.75])
    # Split mid-sample: 6 bytes then 6 bytes.
    tts, _ = build(lambda _r: httpx.Response(200, content=sse(payload[:6], payload[6:])))
    try:
        audio = await synthesise(tts, "नमस्ते", config_for())
    finally:
        await tts.close()

    # All three samples, in order, at the scaling the conversion uses.
    assert s16(audio) == [8191, 16383, 24575]


async def test_the_utterance_is_closed_when_the_vendor_says_done() -> None:
    """Playback has to know the difference between finished and merely paused,
    or the pipeline waits out its whole timeout on every sentence."""
    tts, _ = build(lambda _r: httpx.Response(200, content=sse(f32([0.1]))))
    try:
        chunks = [chunk async for chunk in tts.synthesise("नमस्ते", config_for())]
    finally:
        await tts.close()

    assert chunks[0].is_first
    assert chunks[-1].is_final
    assert chunks[-1].audio == b""


async def test_a_stream_that_ends_without_done_still_closes_the_utterance() -> None:
    """Otherwise a truncated response holds the turn open until the 20 s
    ceiling, which the caller experiences as a dead line."""
    tts, _ = build(lambda _r: httpx.Response(200, content=sse(f32([0.1]), done=False)))
    try:
        chunks = [chunk async for chunk in tts.synthesise("नमस्ते", config_for())]
    finally:
        await tts.close()

    assert chunks[-1].is_final


async def test_empty_text_makes_no_request_at_all() -> None:
    """§8: a blank sentence from the splitter is billed like any other."""
    tts, seen = build(lambda _r: httpx.Response(200, content=sse(f32([0.0]))))
    try:
        assert await synthesise(tts, "   ", config_for()) == b""
    finally:
        await tts.close()

    assert seen == []


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


async def test_an_unset_voice_names_the_variable() -> None:
    """§0 rule 4 and §5.3 together. There is no default voice on purpose: the
    choice is made in a bake-off over a real phone line, and a documentation
    example quietly becoming production's voice is what that rule prevents."""
    tts, _ = build(lambda _r: httpx.Response(200, content=sse(f32([0.0]))))
    try:
        with pytest.raises(MissingCredentialError) as caught:
            await synthesise(tts, "नमस्ते", config_for(speaker=None))
    finally:
        await tts.close()

    assert "BAKBAK_VOICE_HI" in str(caught.value)
    assert "bake-off" in str(caught.value)


async def test_an_unsupported_sample_rate_is_refused_rather_than_resampled() -> None:
    """§23-8 forbids resampling, so the only honest response to a rate the
    vendor will not produce is to say so."""
    tts, _ = build(lambda _r: httpx.Response(200, content=sse(f32([0.0]))))
    try:
        with pytest.raises(ConfigurationError) as caught:
            await synthesise(tts, "नमस्ते", config_for(sample_rate=44100))
    finally:
        await tts.close()

    assert "8000" in caught.value.remedy


async def test_asking_for_mulaw_is_refused_with_the_actual_reason() -> None:
    """The worker's audio bus is linear16 and ``MulawSerializer`` encodes once,
    at the wire. A synthesiser asked for mulaw as well would have it encoded
    twice, which is white noise on the line -- and it would *play*, so nothing
    downstream would raise."""
    tts, _ = build(lambda _r: httpx.Response(200, content=sse(f32([0.0]))))
    try:
        with pytest.raises(ConfigurationError) as caught:
            await synthesise(tts, "नमस्ते", config_for(codec=AudioCodec.MULAW))
    finally:
        await tts.close()

    assert "white noise" in caught.value.message
    assert "MulawSerializer" in caught.value.remedy


async def test_a_language_the_vendor_does_not_speak_is_named() -> None:
    """Punjabi and Odia are routed elsewhere for exactly this reason, and a
    future routing edit that forgets should fail loudly here."""
    tts, _ = build(lambda _r: httpx.Response(200, content=sse(f32([0.0]))))
    try:
        with pytest.raises(ConfigurationError) as caught:
            await synthesise(tts, "ਸਤ ਸ੍ਰੀ ਅਕਾਲ", config_for(language="pa-IN"))
    finally:
        await tts.close()

    assert "pa-IN" in caught.value.message


async def test_a_vendor_error_reports_its_message_and_not_the_request() -> None:
    """§23-6: an error path is the easiest place for a key to reach a log."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": 'voice_id "x" not found'})

    tts, _ = build(handler)
    try:
        with pytest.raises(VendorUnavailableError) as caught:
            await synthesise(tts, "नमस्ते", config_for())
    finally:
        await tts.close()

    detail = str(caught.value)
    assert "not found" in detail
    assert "X-API-Key" not in detail
    assert (Settings().bakbak_api_key or "unset") not in detail


async def test_a_missing_key_names_the_variable() -> None:
    with pytest.raises(MissingCredentialError) as caught:
        BakbakTTS(Settings(bakbak_api_key=None))
    assert "BAKBAK_API_KEY" in str(caught.value)


# --------------------------------------------------------------------------- #
# Voices
# --------------------------------------------------------------------------- #


async def test_the_voice_list_is_readable_for_the_bake_off() -> None:
    """Voice ids are account-scoped UUIDs, so ``scripts/bakbak_voices.py`` is
    the only way to find out what this account can actually say."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/voices")
        return httpx.Response(
            200,
            json={
                "voices": [
                    {"id": VOICE, "name": "Ananya", "language": "hi", "model": "standard"}
                ],
                "count": 1,
            },
        )

    tts, _ = build(handler)
    try:
        voices = await tts.voices()
    finally:
        await tts.close()

    assert voices == [{"id": VOICE, "name": "Ananya", "language": "hi", "model": "standard"}]


async def test_two_utterances_render_at_the_same_time() -> None:
    """The boot pre-warm renders §16.1's fixed phrases with `asyncio.gather`.

    Sarvam's adapter serialises on a lock because it holds one shared
    WebSocket. Copying that here -- each utterance is its own HTTP request over
    a pooled client -- would buy nothing and turn a concurrent pre-warm into a
    serial one, which is a slow worker boot and, on a synthesiser having a bad
    day, a boot that hits the pre-warm timeout with nothing cached.

    Nothing about the audio would change, so only a test that watches the
    overlap notices.
    """
    import asyncio

    inflight = 0
    peak = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        try:
            await asyncio.sleep(0.05)
            return httpx.Response(200, content=sse(f32([0.1])))
        finally:
            inflight -= 1

    settings = Settings()
    client = httpx.AsyncClient(
        base_url=settings.bakbak_base_url,
        transport=httpx.MockTransport(handler),
    )
    tts = BakbakTTS(settings, client=client)
    try:
        await asyncio.gather(
            synthesise(tts, "नमस्ते", config_for()),
            synthesise(tts, "धन्यवाद", config_for()),
        )
    finally:
        await tts.close()

    assert peak == 2, "the two renders were serialised"
