"""Barge-in, playback tracking and the audio cache (§5.3, §5.4, §8).

The tests that matter most here are the ones about what the caller *heard*.
§5.4's third step — truncating the assistant message to played bytes — has no
visible failure mode: the call continues, the audio sounds fine, and the model
quietly believes it said things nobody heard. These assertions are the only
place that error becomes detectable.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest

from uaagro_domain.enums import AudioCodec
from voice_worker.adapters.tts.base import TtsChunk, TtsConfig, TTSService
from voice_worker.runtime import audio as audio_utils
from voice_worker.runtime.audio_cache import (
    AudioCache,
    AudioStore,
    MemoryAudioStore,
    cache_key,
)
from voice_worker.runtime.playback import (
    BargeInPolicy,
    PacedSender,
    PlaybackTracker,
    handle_barge_in,
)

CONFIG = TtsConfig(language="hi-IN", speaker="shubh", codec=AudioCodec.LINEAR16)


def _audio(ms: int) -> bytes:
    return audio_utils.tone(440, ms)


# --------------------------------------------------------------------------- #
# What the caller actually heard
# --------------------------------------------------------------------------- #


def test_nothing_sent_means_nothing_heard() -> None:
    tracker = PlaybackTracker()
    tracker.register("डीएपी उपलब्ध है", _audio(1000))
    assert tracker.spoken_text() == ""


def test_a_fully_played_segment_is_reported_whole() -> None:
    tracker = PlaybackTracker()
    audio = _audio(1000)
    tracker.register("डीएपी उपलब्ध है", audio)
    tracker.mark_sent(len(audio))
    assert tracker.spoken_text(jitter_ms=0) == "डीएपी उपलब्ध है"


def test_a_partly_played_segment_truncates_to_whole_words() -> None:
    """A fragment of a word in the transcript is something nobody said."""
    tracker = PlaybackTracker()
    audio = _audio(1000)
    tracker.register("एक दो तीन चार", audio)
    tracker.mark_sent(len(audio) // 2)

    spoken = tracker.spoken_text(jitter_ms=0)
    assert spoken == "एक दो"
    assert "तीन" not in spoken


def test_later_segments_are_dropped_entirely() -> None:
    """§5.4: the model must not believe it delivered the second sentence."""
    tracker = PlaybackTracker()
    first, second = _audio(500), _audio(500)
    tracker.register("डीएपी उपलब्ध है", first)
    tracker.register("तेरह सौ पचास रुपये की बोरी", second)
    tracker.mark_sent(len(first))

    spoken = tracker.spoken_text(jitter_ms=0)
    assert spoken == "डीएपी उपलब्ध है"
    assert "पचास" not in spoken


def test_the_jitter_allowance_rounds_down_not_up() -> None:
    """Crediting the agent with audio the farmer may not have heard is the
    error that desynchronises the conversation, so the discount is one-way."""
    tracker = PlaybackTracker()
    audio = _audio(1000)
    tracker.register("एक दो तीन चार पाँच छह सात आठ", audio)
    tracker.mark_sent(len(audio))

    generous = len(tracker.spoken_text(jitter_ms=0).split())
    cautious = len(tracker.spoken_text(jitter_ms=200).split())
    assert cautious < generous


def test_reset_clears_the_turn() -> None:
    tracker = PlaybackTracker()
    tracker.register("कुछ", _audio(100))
    tracker.mark_sent(100)
    tracker.reset()
    assert tracker.spoken_text() == ""
    assert tracker.total_bytes == 0


# --------------------------------------------------------------------------- #
# Paced sending
# --------------------------------------------------------------------------- #


async def test_a_full_segment_sends_every_frame() -> None:
    sent: list[bytes] = []
    sender = PacedSender(lambda frame: _collect(sent, frame), realtime=False)
    audio = _audio(200)

    completed = await sender.play("नमस्ते", audio)

    assert completed
    assert len(sent) == audio_utils.frame_count(audio)
    assert sender.tracker.bytes_sent == len(audio)


async def test_cancelling_stops_mid_segment() -> None:
    sent: list[bytes] = []

    async def send(frame: bytes) -> None:
        sent.append(frame)
        if len(sent) == 3:
            sender.cancel()

    sender = PacedSender(send, realtime=False)
    completed = await sender.play("एक दो तीन चार पाँच", _audio(1000))

    assert not completed
    assert len(sent) == 3
    assert sender.tracker.bytes_sent == 3 * audio_utils.FRAME_BYTES


async def test_pacing_tracks_real_time() -> None:
    """Blasting frames would make the played-byte count fiction, and the
    buffer-clear would discard seconds of already-committed audio."""
    sender = PacedSender(lambda _: _noop(), realtime=True)
    started = time.perf_counter()
    await sender.play("कुछ", _audio(200))
    elapsed_ms = (time.perf_counter() - started) * 1000
    # 200 ms of audio should take roughly 200 ms to send, not ~0.
    assert elapsed_ms > 120


# --------------------------------------------------------------------------- #
# Barge-in
# --------------------------------------------------------------------------- #


async def test_barge_in_cancels_clears_and_reports_what_was_heard() -> None:
    """All three §5.4 steps, asserted together because doing two of them is
    the failure mode that ships."""
    cleared = asyncio.Event()
    cancelled_synthesis = asyncio.Event()

    sender = PacedSender(lambda _: _noop(), realtime=False)
    audio = _audio(1000)
    sender.tracker.register("एक दो तीन चार", audio)
    sender.tracker.mark_sent(len(audio) // 2)

    result = await handle_barge_in(
        sender,
        policy=BargeInPolicy(),
        clear_playback=lambda: _set(cleared),
        cancel_synthesis=lambda: _set(cancelled_synthesis),
    )

    assert result.interrupted
    assert sender.cancelled
    assert cleared.is_set(), "the provider buffer was not cleared"
    assert cancelled_synthesis.is_set(), "synthesis kept generating and billing"
    assert result.spoken_text
    assert "चार" not in result.spoken_text


async def test_barge_in_is_well_inside_the_deadline() -> None:
    """§5.4 caps the cut at 200 ms."""
    sender = PacedSender(lambda _: _noop(), realtime=False)
    result = await handle_barge_in(sender, policy=BargeInPolicy(), clear_playback=lambda: _noop())
    assert result.cut_latency_ms < 200


async def test_the_disclosure_resists_interruption_briefly() -> None:
    """§13.2: identifying the call as automated is a legal requirement, so it
    is the one thing a caller cannot talk over."""
    policy = BargeInPolicy(suppression_ms=600)
    policy.begin_disclosure()

    sender = PacedSender(lambda _: _noop(), realtime=False)
    result = await handle_barge_in(sender, policy=policy, clear_playback=lambda: _noop())

    assert not result.interrupted
    assert not sender.cancelled
    assert "disclosure" in result.reason


async def test_the_disclosure_becomes_interruptible_once_heard() -> None:
    """Protection is brief on purpose -- it is not a licence to talk over the
    farmer for the rest of the call."""
    policy = BargeInPolicy(suppression_ms=600)
    policy.begin_disclosure()
    assert not policy.may_interrupt()

    # Far enough past the window that the caller has heard it.
    assert policy.may_interrupt(now=time.perf_counter() + 10)

    policy.end_disclosure()
    assert policy.may_interrupt()


async def test_everything_else_is_interruptible() -> None:
    policy = BargeInPolicy()
    assert policy.may_interrupt()


# --------------------------------------------------------------------------- #
# Audio cache
# --------------------------------------------------------------------------- #


async def test_a_hit_returns_the_same_bytes() -> None:
    cache = AudioCache()
    audio = _audio(500)
    await cache.put("नमस्ते", CONFIG, audio, provider="sarvam")
    assert await cache.get("नमस्ते", CONFIG, provider="sarvam") == audio
    assert cache.stats.hits == 1


async def test_a_miss_is_reported_rather_than_guessed() -> None:
    cache = AudioCache()
    assert await cache.get("कभी नहीं", CONFIG, provider="sarvam") is None
    assert cache.stats.misses == 1


@pytest.mark.parametrize(
    "changed",
    [
        {"speaker": "someone-else"},
        {"language": "mr-IN"},
        {"codec": AudioCodec.MULAW},
        {"sample_rate": 16000},
        {"pace": 1.2},
        {"model": "bulbul:v2"},
    ],
)
async def test_every_parameter_that_changes_the_sound_changes_the_key(
    changed: dict[str, object],
) -> None:
    """Serving Hindi audio on a Marathi call, or mu-law bytes to a linear16
    provider, is worse than a cache miss. Changing the bake-off speaker must
    invalidate by construction, not by someone remembering to flush."""
    other = replace(CONFIG, **changed)  # type: ignore[arg-type]
    assert cache_key("नमस्ते", CONFIG, provider="sarvam") != cache_key(
        "नमस्ते", other, provider="sarvam"
    )


async def test_the_provider_is_part_of_the_key() -> None:
    assert cache_key("नमस्ते", CONFIG, provider="sarvam") != cache_key(
        "नमस्ते", CONFIG, provider="elevenlabs"
    )


async def test_a_cache_outage_degrades_rather_than_breaks() -> None:
    """§8 makes the cache a cost and latency optimisation. A Redis failure
    must cost money, never a call."""

    class BrokenStore(AudioStore):
        async def get(self, key: str) -> bytes | None:
            raise ConnectionError("redis is down")

        async def set(self, key: str, value: bytes, ttl_seconds: int) -> None:
            raise ConnectionError("redis is down")

    cache = AudioCache(store=BrokenStore())
    assert await cache.get("नमस्ते", CONFIG, provider="sarvam") is None
    await cache.put("नमस्ते", CONFIG, _audio(100), provider="sarvam")
    assert cache.stats.errors >= 1


async def test_warming_synthesises_each_phrase_once() -> None:
    """§5.3 pre-synthesises the greeting so §11.1 gets first audio out in
    about 50 ms instead of 250."""
    tts = _CountingTTS()
    cache = AudioCache(store=MemoryAudioStore())
    phrases = {"greeting": "नमस्ते जी", "hold": "एक क्षण", "closing": "नमस्ते"}

    first = await cache.warm(phrases, tts, CONFIG)
    assert set(first) == set(phrases)
    assert tts.calls == 3

    # A second warm on a fresh process-local layer still finds them in the store.
    cache.invalidate_local()
    await cache.warm(phrases, tts, CONFIG)
    assert tts.calls == 3, "warming re-synthesised phrases already in the store"


async def test_warming_survives_one_failing_phrase() -> None:
    """A worker that refuses to boot because one hold phrase failed is worse
    than one that boots and synthesises it on demand."""
    cache = AudioCache()
    warmed = await cache.warm({"good": "नमस्ते", "bad": "FAIL"}, _FlakyTTS(), CONFIG)
    assert "good" in warmed
    assert "bad" not in warmed


async def test_empty_phrases_are_skipped() -> None:
    cache = AudioCache()
    warmed = await cache.warm({"blank": "   "}, _CountingTTS(), CONFIG)
    assert warmed == {}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


async def _noop() -> None:
    return None


async def _set(event: asyncio.Event) -> None:
    event.set()


async def _collect(sink: list[bytes], frame: bytes) -> None:
    sink.append(frame)


class _CountingTTS(TTSService):
    """A test double, selected explicitly by the test -- never a fallback the
    production factory could reach."""

    provider = "sarvam"

    def __init__(self) -> None:
        self.calls = 0

    async def synthesise(self, text: str, config: TtsConfig):  # type: ignore[no-untyped-def]
        self.calls += 1
        yield TtsChunk(audio=_audio(200), is_first=True)
        yield TtsChunk(audio=b"", is_final=True)

    async def close(self) -> None:
        return None


class _FlakyTTS(_CountingTTS):
    async def synthesise(self, text: str, config: TtsConfig):  # type: ignore[no-untyped-def]
        if text == "FAIL":
            raise RuntimeError("synthesis failed")
        yield TtsChunk(audio=_audio(100), is_first=True)
