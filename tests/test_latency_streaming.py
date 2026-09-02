"""Time to first audio, under a realistic model and synthesiser (§7, §5.2).

§7 budgets 700 ms p50 for a whole turn without a tool call, of which the model
gets 260 ms to its first token and the synthesiser 180 ms to its first byte.
Those are *first*-token and *first*-byte budgets, and they only mean anything if
the pipeline actually starts speaking on the first sentence rather than waiting
for the last one.

The doubles here are deliberately slow in the way real vendors are slow: the
model emits tokens over time, and the synthesiser streams audio chunks. Neither
is instant, and a pipeline that collects everything before it speaks turns both
of those streams into their totals.

The number these assert is **speech-ended to first-audio-out**, which is what
the farmer experiences as the pause before the agent answers.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable

from uaagro_domain.settings import Settings, get_defaults
from voice_worker.adapters.factory import build_speech_stack
from voice_worker.adapters.stt.base import SttConfig, SttEvent, SttEventType, STTService
from voice_worker.adapters.tts.base import TtsChunk, TtsConfig, TTSService
from voice_worker.pipelines.conversation import ConversationPipeline
from voice_worker.runtime import audio as audio_utils
from voice_worker.runtime.playback import PacedSender

#: How long a word takes to arrive from the model.
#:
#: 25 ms, and the exact value matters for a reason that has nothing to do with
#: any model: Windows' monotonic clock has ~15.6 ms granularity, and an
#: `asyncio.sleep` shorter than that can return immediately. A 12 ms delay here
#: measured as ~0.02 ms -- the "slow" model was not slow, and every number
#: derived from it was a measurement of the harness. Anything comfortably above
#: the tick simulates a real stream on every platform.
TOKEN_DELAY_S = 0.025

#: Time to the synthesiser's first audio chunk, and between chunks after it.
TTS_FIRST_CHUNK_S = 0.05
TTS_CHUNK_S = 0.02

#: A three-sentence answer of the length §11.3 asks for (~35 words).
ANSWER = (
    "जी, डीएपी की बोरी बारह सौ पचास रुपये की है। "
    "अभी सत्ताईस बोरी स्टॉक में हैं। "
    "आप कब तक आ सकते हैं?"
)


class SlowResponder:
    """Streams an answer a few characters at a time, as a model does."""

    def __init__(self, answer: str = ANSWER) -> None:
        self.answer = answer
        self.chunks_yielded = 0

    async def respond(self, transcript: str, *, language: str) -> AsyncIterator[str]:
        # Word by word, which is roughly a token.
        for word in self.answer.split(" "):
            await asyncio.sleep(TOKEN_DELAY_S)
            self.chunks_yielded += 1
            yield word + " "


class SlowTTS(TTSService):
    """Streams audio chunks, with a first-byte delay like a real voice API."""

    provider = "slow"

    def __init__(self) -> None:
        self.requests: list[str] = []

    async def synthesise(self, text: str, config: TtsConfig) -> AsyncIterator[TtsChunk]:
        self.requests.append(text)
        await asyncio.sleep(TTS_FIRST_CHUNK_S)
        yield TtsChunk(audio=audio_utils.tone(440, 60), is_first=True)
        for _ in range(3):
            await asyncio.sleep(TTS_CHUNK_S)
            yield TtsChunk(audio=audio_utils.tone(440, 60))
        yield TtsChunk(audio=b"", is_final=True)

    async def close(self) -> None:
        return None


class OneTurnSTT(STTService):
    """Emits one finished turn, then idles."""

    provider = "scripted"

    def __init__(self, transcript: str = "डीएपी का रेट क्या है") -> None:
        self.transcript = transcript

    async def start(self, config: SttConfig) -> None:
        return None

    async def send_audio(self, pcm: bytes) -> None:
        return None

    async def events(self) -> AsyncIterator[SttEvent]:
        yield SttEvent(type=SttEventType.SPEECH_STARTED)
        yield SttEvent(
            type=SttEventType.END_OF_TURN, text=self.transcript, confidence=0.95
        )
        await asyncio.sleep(3600)

    async def finalise(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _pipeline(
    responder: object | None = None,
) -> tuple[ConversationPipeline, SlowTTS, list[float]]:
    """A pipeline whose sender records when each frame went out."""
    defaults = get_defaults()
    base = build_speech_stack("hi-IN", Settings(), defaults)
    tts = SlowTTS()
    frame_times: list[float] = []

    async def send(frame: bytes) -> None:
        frame_times.append(time.perf_counter())

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
    pipeline = ConversationPipeline(
        stack=stack,
        sender=PacedSender(send, realtime=False),
        responder=responder or SlowResponder(),  # type: ignore[arg-type]
        defaults=defaults,
    )
    return pipeline, tts, frame_times


async def _drive(
    until: Callable[[ConversationPipeline, SlowTTS, list[float]], bool],
    # Named `deadline_s` rather than `timeout`: this is how long to poll
    # for a condition, not a cancellation budget for an awaited call.
    deadline_s: float = 20.0,
    responder: object | None = None,
) -> tuple[float, SlowTTS, ConversationPipeline, list[float]]:
    """Run the pipeline until `until` holds, then stop it.

    The recogniser idles after its one turn, exactly as a live socket does
    between utterances, so `run()` never returns on its own. Driving it as a
    task and stopping on a condition is what lets this measure a turn rather
    than wait for a call to end.
    """
    pipeline, tts, frame_times = _pipeline(responder)
    started = time.perf_counter()
    task = asyncio.create_task(pipeline.run())
    try:
        deadline = started + deadline_s
        while not until(pipeline, tts, frame_times):
            if task.done():
                await task  # surface whatever it raised
                break
            if time.perf_counter() > deadline:
                raise AssertionError("the pipeline produced nothing in time")
            await asyncio.sleep(0.001)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            # The pipeline is being stopped mid-turn on purpose; whatever
            # the cancelled task raises is not a test result.
            await task
        await pipeline.close()

    assert frame_times, "the caller never heard anything"
    return (frame_times[0] - started) * 1000, tts, pipeline, frame_times


async def _run_one_turn() -> tuple[float, SlowTTS, ConversationPipeline]:
    """Milliseconds from turn start to the first audio frame leaving."""
    first_ms, tts, pipeline, _ = await _drive(lambda _p, _t, frames: bool(frames))
    return first_ms, tts, pipeline


async def test_the_agent_starts_speaking_before_the_model_has_finished(
    record_property,
) -> None:
    """The property that decides whether §7's budget is reachable at all.

    An answer of §11.3's length takes the model about a fifth of a second to
    stream and the synthesiser another fifth per sentence. A pipeline that
    waits for the last token and then the last audio chunk pays *all* of it
    before the farmer hears a syllable -- and no amount of tuning the model or
    the voice recovers that, because the cost is the waiting, not the work.

    Speaking the first sentence as soon as it is complete overlaps the two.
    """
    first_audio_ms, tts, _ = await _run_one_turn()
    record_property("first_audio_ms", round(first_audio_ms, 1))

    whole_answer_tokens = len(ANSWER.split(" "))
    generation_ms = whole_answer_tokens * TOKEN_DELAY_S * 1000
    synthesis_ms = (TTS_FIRST_CHUNK_S + 3 * TTS_CHUNK_S) * 1000

    # The ceiling a collect-everything pipeline cannot beat: it must wait for
    # the whole generation, then a whole sentence of synthesis.
    collect_everything_ms = generation_ms + synthesis_ms

    assert first_audio_ms < collect_everything_ms * 0.75, (
        f"first audio took {first_audio_ms:.0f} ms; collecting the whole "
        f"response before speaking would cost about {collect_everything_ms:.0f} ms, "
        "so this is not streaming"
    )
    # The synthesiser was asked for the first sentence on its own, not the
    # whole answer in one call.
    assert tts.requests, "nothing was synthesised"
    assert len(tts.requests[0]) < len(ANSWER) * 0.7, (
        f"the first synthesis request was {tts.requests[0]!r} -- the whole "
        "answer, rather than its first sentence"
    )


async def test_first_audio_leaves_room_inside_the_turn_budget(
    record_property,
) -> None:
    """§7 gives a no-tool turn 700 ms p50 end to end.

    This double is slower than the real thing on purpose, so the assertion is
    against the budget rather than a tuned number: if a deliberately sluggish
    model and synthesiser still get first audio out inside the budget, the real
    ones have room for the network and the recogniser.
    """
    first_audio_ms, _, _ = await _run_one_turn()
    record_property("first_audio_ms", round(first_audio_ms, 1))

    budget = get_defaults().latency_budget_ms.total_no_tool.p50
    assert first_audio_ms < budget, (
        f"first audio at {first_audio_ms:.0f} ms against a {budget} ms p50 "
        "budget for the whole turn"
    )


async def test_every_sentence_is_still_spoken_in_order(record_property) -> None:
    """Streaming must not drop or reorder anything.

    The failure this guards against is subtle: a sentence buffer that flushes
    on the wrong boundary speaks the answer in fragments, and a farmer hears a
    confident half-sentence followed by the rest of it.
    """
    # Waits for all three sentences rather than the first frame.
    _, tts, pipeline, _ = await _drive(lambda p, _t, _f: bool(p.latency.turns))

    spoken = " ".join(tts.requests)
    for fragment in ("डीएपी", "बारह सौ पचास", "सत्ताईस", "कब तक"):
        assert fragment in spoken, f"{fragment!r} was never spoken"

    assert pipeline.turns, "no turn was recorded"
    assert pipeline.turns[0].response.strip(), "the turn recorded no response"


async def test_the_first_token_mark_is_the_first_token(record_property) -> None:
    """`llm_ttft` has to measure time to the *first* token.

    Marked after the last one, the segment reports the whole generation and the
    §7 breakdown blames the model for time the pipeline spent waiting. That
    makes the one number an operator would use to choose a model actively
    misleading.
    """
    # Driven to the end of the turn: metrics are recorded when the turn
    # finishes, not when its first frame goes out.
    _, _tts, pipeline, _ = await _drive(
        lambda p, _t, _f: bool(p.latency.turns), deadline_s=25.0
    )

    assert pipeline.latency.turns, "no turn metrics recorded"
    metrics = pipeline.latency.turns[0]
    segments = metrics.segments()

    ttft = segments.get("llm_ttft")
    assert ttft is not None, "llm_ttft was not measured"
    record_property("llm_ttft_ms", round(ttft, 1))

    whole_generation_ms = len(ANSWER.split(" ")) * TOKEN_DELAY_S * 1000
    assert ttft < whole_generation_ms * 0.6, (
        f"llm_ttft is {ttft:.0f} ms against a {whole_generation_ms:.0f} ms "
        "full generation -- it is measuring the last token, not the first"
    )


class CollectingResponder:
    """Drains the model, then yields the whole answer at once.

    Reproduces exactly what this pipeline used to do -- and what any future
    refactor might quietly reintroduce by awaiting the response before speaking
    it. A single chunk means the sentence buffer sees the whole answer in one
    go, so nothing is synthesised until generation has finished.
    """

    async def respond(self, transcript: str, *, language: str) -> AsyncIterator[str]:
        inner = SlowResponder()
        whole = "".join(
            [chunk async for chunk in inner.respond(transcript, language=language)]
        )
        yield whole


async def _median_first_audio(
    make_responder: Callable[[], object], runs: int = 5
) -> float:
    """Median time to first audio over `runs`, with a given responder.

    The responder is passed in rather than swapped on the module. An earlier
    version rebound the name globally, which made `CollectingResponder`
    construct itself and recurse until the stack ran out -- a reminder that a
    test double reaching for a global is the same mistake as production code
    doing it.
    """
    samples: list[float] = []
    for _ in range(runs):
        first_ms, _tts, _pipeline, _frames = await _drive(
            lambda _p, _t, frames: bool(frames),
            deadline_s=30.0,
            responder=make_responder(),
        )
        samples.append(first_ms)
    samples.sort()
    return samples[len(samples) // 2]


async def test_streaming_beats_collecting_the_whole_answer(record_property) -> None:
    """The regression this change exists to prevent, measured both ways.

    Identical doubles, identical answer: the only difference is whether the
    pipeline waits for the model to finish before it starts synthesising. On
    this machine that is ~400 ms against ~210 ms, and the gap widens with the
    length of the answer -- it is generation time for everything after the
    first sentence, which the farmer would otherwise spend in silence.

    Asserted as a ratio rather than a fixed millisecond figure, because the
    absolute numbers move with the host's timer resolution and the assertion
    should fail on a *behaviour* change, not on a faster laptop.
    """
    streaming = await _median_first_audio(SlowResponder)
    collecting = await _median_first_audio(CollectingResponder)

    record_property("first_audio_streaming_ms", round(streaming, 1))
    record_property("first_audio_collecting_ms", round(collecting, 1))
    record_property("saved_ms", round(collecting - streaming, 1))

    assert streaming < collecting * 0.8, (
        f"streaming first audio {streaming:.0f} ms vs collecting "
        f"{collecting:.0f} ms -- the pipeline is waiting for the whole answer "
        "before it speaks"
    )
