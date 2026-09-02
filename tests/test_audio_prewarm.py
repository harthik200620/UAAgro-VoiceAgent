"""Pre-rendered audio for the phrases that never change (§16.1, §11.1, §9.3).

§16.1 does not say the safety script *may* be cached -- it says the agent
speaks it "from a cached recording". Synthesising it on demand puts a vendor
round trip between a farmer with pesticide in their eyes and the words telling
them to get to a doctor.

The other half of this is less dramatic and adds up faster: the audio cache was
constructed per call, so its in-process layer was discarded with the call and
the greeting was re-synthesised for every caller, every time, forever.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from voice_worker.adapters.tts.base import TtsChunk, TtsConfig, TTSService
from voice_worker.flow.safety import SAFETY_SCRIPT_HI
from voice_worker.runtime import audio as audio_utils
from voice_worker.runtime.audio_cache import AudioCache
from voice_worker.runtime.audio_prewarm import fixed_phrases, prewarm
from voice_worker.text.speech import split_sentences, text_for_speech


class CountingTTS(TTSService):
    """Records every synthesis request."""

    provider = "counting"

    def __init__(self, fail: bool = False) -> None:
        self.requests: list[str] = []
        self.fail = fail

    async def synthesise(self, text: str, config: TtsConfig) -> AsyncIterator[TtsChunk]:
        self.requests.append(text)
        if self.fail:
            raise RuntimeError("the voice API is down")
        yield TtsChunk(audio=audio_utils.tone(440, 80), is_first=True)
        yield TtsChunk(audio=b"", is_final=True)

    async def close(self) -> None:
        return None


def _config() -> TtsConfig:
    return TtsConfig(language="hi-IN", speaker="anushka")


async def test_the_safety_script_is_rendered_before_any_call() -> None:
    """§16.1's "cached recording", made real.

    Checked sentence by sentence because that is the unit the pipeline looks
    up: caching the three-sentence script under one key would give a hit rate
    of zero at exactly the moment it matters.
    """
    cache, tts, config = AudioCache(), CountingTTS(), _config()
    await prewarm(cache, tts, config)

    for sentence in split_sentences(SAFETY_SCRIPT_HI):
        spoken = text_for_speech(sentence, language="hi-IN")
        assert await cache.get(spoken, config, provider=tts.provider) is not None, (
            f"the safety script sentence {spoken[:30]!r} would be synthesised "
            "during an emergency"
        )


async def test_the_hold_and_fallback_phrases_are_cached_too() -> None:
    """Both are spoken while something has already gone slow or wrong -- the
    worst moment to add a synthesiser round trip."""
    cache, tts, config = AudioCache(), CountingTTS(), _config()
    await prewarm(cache, tts, config)

    for phrase in fixed_phrases():
        first = split_sentences(phrase)[0]
        spoken = text_for_speech(first, language="hi-IN")
        assert await cache.get(spoken, config, provider=tts.provider) is not None


async def test_prewarming_twice_synthesises_once() -> None:
    """A worker restart loop, or a second language sharing a phrase, must not
    pay the vendor twice for the same audio (§8)."""
    cache, tts, config = AudioCache(), CountingTTS(), _config()

    first = await prewarm(cache, tts, config)
    calls_after_first = len(tts.requests)
    second = await prewarm(cache, tts, config)

    assert first > 0
    assert second == 0, "the second pass re-synthesised cached phrases"
    assert len(tts.requests) == calls_after_first


async def test_a_synthesiser_outage_at_boot_does_not_stop_the_worker() -> None:
    """The phrases render on demand instead: slower, and far better than a
    worker that refuses to start and takes the helpline down with it."""
    cache, tts, config = AudioCache(), CountingTTS(fail=True), _config()

    stored = await prewarm(cache, tts, config)

    assert stored == 0
    assert tts.requests, "it did not even try"


async def test_extra_phrases_are_accepted() -> None:
    """The published greeting differs per organisation, so it is passed in
    rather than hard-coded here."""
    cache, tts, config = AudioCache(), CountingTTS(), _config()
    greeting = "नमस्ते जी, नवीन खुशहाली किसान सेवा केंद्र में आपका स्वागत है।"

    await prewarm(cache, tts, config, extra=(greeting,))

    spoken = text_for_speech(split_sentences(greeting)[0], language="hi-IN")
    assert await cache.get(spoken, config, provider=tts.provider) is not None


async def test_a_different_voice_does_not_serve_the_old_recording() -> None:
    """The key covers the voice config. A speaker change that kept serving the
    previous voice would be a caller hearing two different people mid-call."""
    cache, tts = AudioCache(), CountingTTS()
    await prewarm(cache, tts, _config())

    other_voice = TtsConfig(language="hi-IN", speaker="karun")
    spoken = text_for_speech(split_sentences(SAFETY_SCRIPT_HI)[0], language="hi-IN")

    assert await cache.get(spoken, other_voice, provider=tts.provider) is None
