"""Covering a slow turn with "one moment" (§11.4, §7).

§7 budgets a whole turn at 700 ms p50, but a tool call and a generation can
each run to their p95 and leave the caller listening to well over a second of
nothing. On a rural GSM line that silence is indistinguishable from a dropped
call: the farmer says "हैलो? हैलो?", the recogniser hears speech, barge-in
fires, and the answer that was half a second away is abandoned.

So this is not a politeness feature. It stops a slow turn from becoming a
broken one, and the phrase it speaks was already defined in the codebase and
never once used.

The rule that matters most here is that it must never talk over the answer.
Two utterances interleaved frame by frame are not slow, they are
unintelligible.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from tests.test_latency_streaming import OneTurnSTT
from uaagro_domain.settings import Settings, get_defaults
from voice_worker.adapters.factory import build_speech_stack
from voice_worker.adapters.tts.base import TtsChunk, TtsConfig, TTSService
from voice_worker.flow.agent import HOLD_SCRIPT_HI
from voice_worker.pipelines import conversation as conv
from voice_worker.pipelines.conversation import ConversationPipeline
from voice_worker.runtime import audio as audio_utils
from voice_worker.runtime.audio_cache import AudioCache
from voice_worker.runtime.playback import PacedSender


class QuickTTS(TTSService):
    provider = "quick"

    def __init__(self) -> None:
        self.requests: list[str] = []

    async def synthesise(self, text: str, config: TtsConfig) -> AsyncIterator[TtsChunk]:
        self.requests.append(text)
        yield TtsChunk(audio=audio_utils.tone(440, 60), is_first=True)
        yield TtsChunk(audio=b"", is_final=True)

    async def close(self) -> None:
        return None


class Responder:
    """Answers after `delay_s`, as a slow tool call plus generation would."""

    def __init__(self, delay_s: float, answer: str = "जी, डीएपी उपलब्ध है।") -> None:
        self.delay_s = delay_s
        self.answer = answer

    async def respond(self, transcript: str, *, language: str) -> AsyncIterator[str]:
        await asyncio.sleep(self.delay_s)
        yield self.answer


async def _run(delay_s: float, *, cache_hold: bool = True) -> tuple[list[str], object]:
    """Run one turn; return the texts played, in order, and the metrics."""
    defaults = get_defaults()
    base = build_speech_stack("hi-IN", Settings(), defaults)
    tts = QuickTTS()
    cache = AudioCache()
    if cache_hold:
        # Pre-rendered, as the worker does at startup.
        await cache.put(
            HOLD_SCRIPT_HI, base.tts_config, audio_utils.tone(300, 200), provider=tts.provider
        )

    played: list[str] = []

    async def send(frame: bytes) -> None:
        return None

    stack = type(base)(
        language=base.language,
        served_by=base.served_by,
        stt=OneTurnSTT(),
        stt_config=base.stt_config,
        tts=tts,
        tts_config=base.tts_config,
        turn_detector=base.turn_detector,
        quality_tier=base.quality_tier,
        tier_note=base.tier_note,
    )
    sender = PacedSender(send, realtime=False)
    pipeline = ConversationPipeline(
        stack=stack,
        sender=sender,
        responder=Responder(delay_s),  # type: ignore[arg-type]
        defaults=defaults,
        cache=cache,
    )

    task = asyncio.create_task(pipeline.run())
    deadline = asyncio.get_running_loop().time() + 15
    while not pipeline.latency.turns:
        if task.done() or asyncio.get_running_loop().time() > deadline:
            break
        await asyncio.sleep(0.005)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        # Stopped mid-call on purpose; what the cancelled task raises is
        # not a test result.
        await task
    await pipeline.close()

    played = [segment.text for segment in sender.tracker.segments if segment.text]
    metrics = pipeline.latency.turns[0] if pipeline.latency.turns else None
    return played, metrics


async def test_a_slow_turn_is_covered_rather_than_silent(monkeypatch) -> None:
    """The caller hears "one moment" instead of wondering if the line dropped."""
    monkeypatch.setattr(conv, "HOLD_AFTER_MS", 60.0)

    played, metrics = await _run(delay_s=0.45)

    assert played, "nothing was played at all"
    assert played[0] == HOLD_SCRIPT_HI, f"first thing played was {played[0]!r}"
    assert metrics is not None and metrics.spoke_hold_phrase is True


async def test_a_fast_turn_says_nothing_extra(monkeypatch) -> None:
    """The phrase is for turns already over budget. Saying it on every turn
    would add a second of chatter to the ones that were working."""
    monkeypatch.setattr(conv, "HOLD_AFTER_MS", 400.0)

    played, metrics = await _run(delay_s=0.0)

    assert HOLD_SCRIPT_HI not in played, "a fast turn was padded with a hold phrase"
    assert metrics is not None and metrics.spoke_hold_phrase is False


async def test_the_answer_is_never_talked_over(monkeypatch) -> None:
    """The race this is built to lose safely.

    The hold phrase is produced by a separate task, so without the lock it can
    start streaming between the answer's chunks. Two utterances interleaved
    frame by frame are not slow -- they are unintelligible.
    """
    # Fires at almost exactly the moment the answer arrives.
    monkeypatch.setattr(conv, "HOLD_AFTER_MS", 100.0)

    played, _metrics = await _run(delay_s=0.1)

    # Whatever was said, the hold phrase never follows the answer and never
    # appears twice.
    assert played.count(HOLD_SCRIPT_HI) <= 1
    if HOLD_SCRIPT_HI in played:
        assert played.index(HOLD_SCRIPT_HI) == 0, (
            f"the hold phrase came after the answer: {played}"
        )


async def test_an_uncached_hold_phrase_is_not_synthesised(monkeypatch) -> None:
    """It exists to cover a slow turn. Adding a synthesiser round trip to it
    would deepen the hole it is filling."""
    monkeypatch.setattr(conv, "HOLD_AFTER_MS", 50.0)

    played, metrics = await _run(delay_s=0.4, cache_hold=False)

    assert HOLD_SCRIPT_HI not in played
    assert metrics is not None and metrics.spoke_hold_phrase is False
