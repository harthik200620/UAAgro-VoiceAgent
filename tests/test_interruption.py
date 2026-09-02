"""Stopping when the farmer talks, and carrying on when they only nodded (§5.4).

These run with a *paced* sender, so playback takes real time and the caller's
audio can arrive in the middle of it -- which is the whole subject. The
answers are short (a few hundred milliseconds of tone per sentence) so the
suite stays quick.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import numpy as np

from tests.test_voice_gate import fan, pcm, quiet, voice
from uaagro_domain.settings import Settings, get_defaults
from voice_worker.adapters.factory import build_speech_stack
from voice_worker.adapters.stt.base import SttConfig, SttEvent, SttEventType, STTService
from voice_worker.adapters.tts.base import TtsChunk, TtsConfig, TTSService
from voice_worker.pipelines.conversation import ConversationPipeline
from voice_worker.runtime import audio as audio_utils
from voice_worker.runtime.audio_cache import AudioCache
from voice_worker.runtime.playback import PacedSender
from voice_worker.runtime.vad import VoiceGate
from voice_worker.text.speech import text_for_speech

S1 = "डीएपी की बोरी उपलब्ध है।"
S2 = "पचास किलो की बोरी आती है।"
S3 = "आप आज ही केंद्र से ले सकते हैं।"
QUESTION = "डीएपी का रेट बताइए"


class TimedSTT(STTService):
    """Replays events at given times; the test drives audio separately."""

    provider = "timed"
    emits_turn_events = True

    def __init__(self, script: list[tuple[float, SttEvent]]) -> None:
        self.script = script
        self.audio_bytes = 0
        self.closed = asyncio.Event()
        #: Set once every scripted event has been handed over *and*
        #: handled -- the pipeline asks for the next event only after it
        #: has dealt with the last one.
        self.drained = asyncio.Event()

    async def start(self, config: SttConfig) -> None:
        return None

    async def send_audio(self, pcm: bytes) -> None:
        self.audio_bytes += len(pcm)

    async def events(self) -> AsyncIterator[SttEvent]:
        clock = 0.0
        for at, event in self.script:
            if at > clock:
                await asyncio.sleep(at - clock)
                clock = at
            yield event
        self.drained.set()
        await self.closed.wait()

    async def finalise(self) -> None:
        return None

    async def close(self) -> None:
        self.closed.set()


class ToneTTS(TTSService):
    """Streams a tone of `ms` per sentence and records what it was asked."""

    provider = "tone"

    def __init__(self, ms: int = 400) -> None:
        self.ms = ms
        self.requests: list[str] = []

    async def synthesise(self, text: str, config: TtsConfig) -> AsyncIterator[TtsChunk]:
        self.requests.append(text)
        audio = audio_utils.tone(440, self.ms)
        step = audio_utils.FRAME_BYTES * 3
        first = True
        for offset in range(0, len(audio), step):
            yield TtsChunk(audio=audio[offset : offset + step], is_first=first)
            first = False
        yield TtsChunk(audio=b"", is_final=True)

    async def close(self) -> None:
        return None


class Responder:
    """Yields sentences with a gap between them, and records the hooks."""

    resumes_after_backchannel = True

    def __init__(self, sentences: list[str] | None = None, *, gap_s: float = 0.02) -> None:
        self.sentences = sentences or [S1, S2, S3]
        self.gap_s = gap_s
        self.calls: list[str] = []
        self.interruptions: list[str] = []
        self.resumed: list[str] = []
        self.discarded = 0
        self.delay_s = 0.0

    async def respond(self, transcript: str, *, language: str) -> AsyncIterator[str]:
        self.calls.append(transcript)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        for sentence in self.sentences:
            yield sentence + " "
            await asyncio.sleep(self.gap_s)

    def note_interruption(self, heard: str) -> None:
        self.interruptions.append(heard)

    def note_resumed(self, text: str) -> None:
        self.resumed.append(text)

    def discard_pending(self) -> None:
        self.discarded += 1


class Harness:
    def __init__(
        self,
        script: list[tuple[float, SttEvent]],
        responder: Responder,
        *,
        gate: VoiceGate | None = None,
        tts_ms: int = 400,
    ) -> None:
        defaults = get_defaults()
        base = build_speech_stack("hi-IN", Settings(), defaults)
        self.tts = ToneTTS(tts_ms)
        self.stt = TimedSTT(script)
        self.sent: list[bytes] = []
        self.cleared = 0

        async def send(frame: bytes) -> None:
            self.sent.append(frame)

        async def clear() -> None:
            self.cleared += 1

        stack = type(base)(
            language=base.language,
            served_by=base.served_by,
            stt=self.stt,
            stt_config=base.stt_config,
            tts=self.tts,
            tts_config=base.tts_config,
            turn_detector=base.turn_detector,
            quality_tier=base.quality_tier,
            tier_note=base.tier_note,
        )
        self.cache = AudioCache()
        self.pipeline = ConversationPipeline(
            stack=stack,
            sender=PacedSender(send, realtime=True),
            responder=responder,  # type: ignore[arg-type]
            defaults=defaults,
            cache=self.cache,
            clear_playback=clear,
            voice_gate=gate,
        )
        self.task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Harness:
        self.task = asyncio.create_task(self.pipeline.run())
        return self

    async def __aexit__(self, *exc: object) -> None:
        await asyncio.wait_for(self.stt.drained.wait(), timeout=10.0)
        await self.pipeline.wait_idle(timeout_s=10.0)
        self.stt.closed.set()
        if self.task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self.task, timeout=2.0)
        await self.pipeline.close()

    async def feed(self, samples: np.ndarray) -> None:
        step = audio_utils.FRAME_SAMPLES
        for start in range(0, samples.size - step + 1, step):
            await self.pipeline.feed_audio(pcm(samples[start : start + step]))

    def played(self) -> list[str]:
        return [s.text for s in self.pipeline.sender.tracker.segments if s.text]


def end(text: str) -> SttEvent:
    return SttEvent(type=SttEventType.END_OF_TURN, text=text)


async def test_a_voice_stops_the_agent_and_a_nod_lets_it_continue() -> None:
    """The farmer says "हाँ" over sentence one: the agent stops, hears that it
    was only a nod, and says the rest -- without asking the model again."""
    responder = Responder()
    script = [
        (0.0, end(QUESTION)),
        (1.1, SttEvent(type=SttEventType.PARTIAL, text="हाँ")),
        (1.2, end("हाँ")),
    ]
    async with Harness(script, responder, gate=VoiceGate()) as h:
        await asyncio.sleep(0.55)  # into sentence one
        await h.feed(voice(0.4))

    assert h.cleared == 1, "the provider buffer was not cleared"
    first, second = h.pipeline.turns[0], h.pipeline.turns[1]
    assert first.interrupted
    assert first.response.startswith(S1), first.response
    assert S3 in first.response, "the rest of the answer was not drained after the cut"
    assert second.resumed
    assert S3 in second.response and second.transcript == "हाँ"
    assert responder.calls == [QUESTION], "the model was asked twice"
    assert responder.interruptions, "the agent was not told what the farmer heard"
    assert responder.resumed


async def test_a_nod_over_the_last_words_is_not_answered() -> None:
    """The answer had one sentence; the farmer said "हाँ" over its tail. There
    is nothing to resume and nothing to answer -- the agent stays quiet."""
    responder = Responder([S1])
    script = [(0.0, end(QUESTION)), (1.1, end("हाँ जी"))]
    async with Harness(script, responder, gate=VoiceGate(), tts_ms=700) as h:
        await asyncio.sleep(0.5)
        await h.feed(voice(0.4))
        await h.feed(quiet(0.3))

    assert h.cleared == 1
    assert responder.calls == [QUESTION], "the nod was answered"
    assert len(h.pipeline.turns) == 2
    assert h.pipeline.turns[1].response == "" and not h.pipeline.turns[1].resumed


async def test_a_fan_does_not_stop_the_agent() -> None:
    responder = Responder()
    async with Harness([(0.0, end(QUESTION))], responder, gate=VoiceGate()) as h:
        await h.feed(quiet(0.5))
        await asyncio.sleep(0.3)
        await h.feed(fan(1.0, rms=0.05))

    assert h.cleared == 0
    assert not h.pipeline.turns[0].interrupted
    assert h.played() == [text_for_speech(S1), text_for_speech(S2), text_for_speech(S3)]


async def test_a_recogniser_speech_start_with_no_voice_behind_it_is_ignored() -> None:
    """Echo: the recogniser transcribes the agent, the gate heard nobody."""
    responder = Responder()
    script = [(0.0, end(QUESTION)), (0.5, SttEvent(type=SttEventType.SPEECH_STARTED))]
    async with Harness(script, responder, gate=VoiceGate()) as h:
        await h.feed(quiet(0.4))  # the gate has heard the room, and no voice

    assert h.cleared == 0
    assert not h.pipeline.turns[0].interrupted


async def test_a_turn_ending_mid_speech_needs_real_words() -> None:
    """Two words while the agent talks is noise; four is a farmer."""
    responder = Responder()
    script = [(0.0, end(QUESTION)), (0.5, end("हाँ जी")), (0.9, end("यूरिया का रेट भी बताइए"))]
    async with Harness(script, responder, gate=VoiceGate()) as h:
        pass

    assert responder.calls == [QUESTION, "यूरिया का रेट भी बताइए"]
    assert len(h.pipeline.turns) == 2
    assert h.pipeline.turns[0].interrupted
    assert h.cleared == 1


async def test_a_silent_cut_resumes_on_its_own() -> None:
    """The gate heard voice, the recogniser produced nothing: a cough. The
    agent waits for the room to go quiet and picks the answer back up."""
    responder = Responder()
    async with Harness([(0.0, end(QUESTION))], responder, gate=VoiceGate()) as h:
        await asyncio.sleep(0.5)
        await h.feed(voice(0.3))
        # The line keeps carrying frames after the voice stops; the gate
        # needs them to notice the silence.
        await h.feed(quiet(0.5))
        await asyncio.sleep(1.6)

    assert h.cleared == 1
    assert len(h.pipeline.turns) == 2
    assert h.pipeline.turns[1].resumed
    assert responder.calls == [QUESTION]


async def test_nothing_is_said_while_the_model_is_slow() -> None:
    """No "एक क्षण रुकिए": a slow turn is silence, then the answer."""
    responder = Responder([S1])
    responder.delay_s = 1.0
    async with Harness([(0.0, end(QUESTION))], responder) as h:
        pass

    assert h.tts.requests == [text_for_speech(S1)]
    assert h.played() == [text_for_speech(S1)]


async def test_speculation_streams_and_pre_renders_its_first_sentence() -> None:
    """On the eager signal the first sentence is generated *and synthesised*
    before the commit, so the commit finds its audio in the cache."""
    responder = Responder([S1, S2], gap_s=0.3)
    script = [
        (0.0, SttEvent(type=SttEventType.EAGER_END_OF_TURN, text=QUESTION)),
        (0.15, end(QUESTION)),
    ]
    async with Harness(script, responder) as h:
        pass

    turn = h.pipeline.turns[0]
    assert turn.metrics is not None and turn.metrics.speculative_hit
    assert responder.calls == [QUESTION]
    assert h.tts.requests.count(text_for_speech(S1)) == 1, "sentence one was synthesised twice"
    assert h.cache.stats.hits >= 1, "the pre-rendered sentence was not used"
    assert h.played() == [text_for_speech(S1), text_for_speech(S2)]


async def test_a_withdrawn_speculation_tells_the_responder() -> None:
    responder = Responder()
    script = [
        (0.0, SttEvent(type=SttEventType.EAGER_END_OF_TURN, text="डीएपी")),
        (0.05, SttEvent(type=SttEventType.TURN_RESUMED, text="डीएपी और यूरिया")),
        (0.1, end("डीएपी और यूरिया का रेट")),
    ]
    async with Harness(script, responder) as h:
        pass

    assert responder.discarded == 1
    assert responder.calls[-1] == "डीएपी और यूरिया का रेट"
    assert not h.pipeline.turns[0].metrics.speculative_hit  # type: ignore[union-attr]


async def test_the_greeting_can_be_interrupted() -> None:
    responder = Responder([S1])
    script = [(0.9, end(QUESTION))]
    async with Harness(script, responder, gate=VoiceGate()) as h:
        h.pipeline.play_opening("नमस्ते जी! बताइए, क्या मदद करूँ?", audio_utils.tone(300, 1500))
        await asyncio.sleep(0.4)
        await h.feed(voice(0.4))

    assert h.cleared == 1
    opening = h.pipeline.turns[0]
    assert opening.interrupted and opening.response.startswith("नमस्ते")
    assert h.pipeline.turns[1].transcript == QUESTION
    assert responder.calls == [QUESTION]
