"""Pre-synthesise the phrases that are always the same (§16.1, §11.1, §9.3).

Some of what the agent says is fixed text: the greeting that opens every call,
the hold phrase while a tool runs, the fallback when generation fails, and --
most importantly -- §16.1's poisoning script, which the spec requires be spoken
"from a cached recording".

Synthesising those on demand is wrong twice over.

**It is slow at the worst possible moment.** §11.1 wants first audio inside
~50 ms of the start frame; a synthesiser round trip is several times that, so
the greeting alone spends the whole opening budget. The safety script is worse:
a farmer who has just got pesticide in their eyes waits for a TTS API.

**It is repeated.** These are the same handful of strings on every call of
every day. Paying a vendor to render them again is §8 cost for nothing.

The cache is keyed on the text *and* the voice config, so a change of speaker
or language invalidates it correctly rather than serving the old voice.
"""

from __future__ import annotations

import asyncio

import structlog

from ..adapters.tts.base import TtsConfig, TTSService
from ..flow.agent import HOLD_SCRIPT_HI
from ..flow.safety import SAFETY_SCRIPT_HI
from ..text.speech import split_sentences, text_for_speech
from .audio_cache import AudioCache

log = structlog.get_logger(__name__)


def fixed_phrases() -> tuple[str, ...]:
    """Every phrase the agent can say without generating it.

    Ordered by how badly a delay hurts: the safety script first, because §16.1
    is the one path in the system where a synthesiser round trip is a person
    waiting with a chemical in their eyes.
    """
    # From the validator, which is where it is defined -- `flow.agent`
    # re-imports it but does not re-export it.
    from ..flow.validator import FALLBACK_SCRIPT_HI

    return (SAFETY_SCRIPT_HI, HOLD_SCRIPT_HI, FALLBACK_SCRIPT_HI)


async def prewarm(
    cache: AudioCache,
    tts: TTSService,
    config: TtsConfig,
    *,
    extra: tuple[str, ...] = (),
    timeout_s: float = 8.0,
) -> int:
    """Render the fixed phrases into `cache`. Returns how many were stored.

    Sentence by sentence, because that is the unit the pipeline synthesises
    and therefore the unit it looks up. Caching a whole three-sentence script
    under one key would produce a hit rate of zero at the moment it matters.

    Failures are counted and logged, never raised. A synthesiser that is down
    at boot must not stop the worker from starting -- the phrases will be
    rendered on demand, slowly, which is worse than this and much better than
    no service at all.
    """
    sentences: list[str] = []
    for phrase in (*fixed_phrases(), *extra):
        for sentence in split_sentences(phrase):
            spoken = text_for_speech(sentence, language=config.language)
            if spoken and spoken not in sentences:
                sentences.append(spoken)

    async def render(spoken: str) -> bool:
        try:
            if await cache.get(spoken, config, provider=tts.provider) is not None:
                return False
            chunks = [chunk.audio async for chunk in tts.synthesise(spoken, config)]
            audio = b"".join(chunks)
            if not audio:
                return False
            await cache.put(spoken, config, audio, provider=tts.provider)
        except Exception as exc:
            log.warning(
                "audio_prewarm.failed",
                error=type(exc).__name__,
                # The phrase, never its audio, and truncated: a log line is not
                # the place for the whole safety script.
                phrase=spoken[:40],
            )
            return False
        return True

    # Concurrently, and bounded. Serially, a synthesiser that is down answers
    # each request with a timeout, and a dozen phrases become a dozen timeouts
    # in series -- which is a worker that will not come up during a deploy
    # because a *cache* could not be filled. Nothing is waiting on these; the
    # phrases render on demand if this gives up.
    try:
        async with asyncio.timeout(timeout_s):
            results = await asyncio.gather(*(render(s) for s in sentences))
    except TimeoutError:
        log.warning("audio_prewarm.timed_out", seconds=timeout_s, phrases=len(sentences))
        return 0

    stored = sum(1 for ok in results if ok)
    log.info("audio_prewarm.complete", stored=stored, language=config.language)
    return stored


__all__ = ("fixed_phrases", "prewarm")
