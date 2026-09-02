"""The conversational turn loop (§5.2, §5.4, §7).

The §21 Phase 2 gate is a scripted conversation completing in Hindi, Marathi and
Malayalam. Here that runs against scripted recogniser and synthesiser doubles,
so what is exercised is everything *between* them: routing, turn logic,
speculation and its cancellation, normalisation, paced playback, barge-in
truncation and the §7 measurement. The vendors themselves are the part the gate
still needs live keys for.

The assertions that matter most concern speculative generation. §5.2 calls an
orphaned generation that still speaks a severe bug, and it has no natural
failure signal -- the farmer simply hears an answer to a question they had not
finished asking.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from uaagro_domain.settings import Settings, get_defaults
from voice_worker.adapters.factory import build_speech_stack
from voice_worker.adapters.stt.base import SttConfig, SttEvent, SttEventType, STTService
from voice_worker.adapters.tts.base import TtsChunk, TtsConfig, TTSService
from voice_worker.pipelines.conversation import ConversationPipeline
from voice_worker.runtime import audio as audio_utils
from voice_worker.runtime.metrics import CallLatency, TurnMetrics, percentile
from voice_worker.runtime.playback import PacedSender

# --------------------------------------------------------------------------- #
# Doubles -- selected explicitly by the tests, never reachable by the factory
# --------------------------------------------------------------------------- #


class ScriptedSTT(STTService):
    """Replays a fixed event sequence, standing in for a live recogniser."""

    provider = "scripted"

    def __init__(self, script: list[SttEvent], *, emits_turn_events: bool = True) -> None:
        self.script = script
        self.emits_turn_events = emits_turn_events
        self.audio_bytes = 0
        self.closed = False

    async def start(self, config: SttConfig) -> None:
        return None

    async def send_audio(self, pcm: bytes) -> None:
        self.audio_bytes += len(pcm)

    async def events(self) -> AsyncIterator[SttEvent]:
        for event in self.script:
            yield event
            # Let the pipeline finish handling before the next event, so a
            # speculation started here is genuinely in flight.
            await asyncio.sleep(0)

    async def finalise(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class ToneTTS(TTSService):
    """Synthesises a fixed tone, and records what it was asked to say."""

    provider = "scripted"

    def __init__(self) -> None:
        self.requests: list[str] = []

    async def synthesise(self, text: str, config: TtsConfig) -> AsyncIterator[TtsChunk]:
        self.requests.append(text)
        yield TtsChunk(audio=audio_utils.tone(440, 120), is_first=True)
        yield TtsChunk(audio=b"", is_final=True)

    async def close(self) -> None:
        return None


class CannedResponder:
    """Answers with a fixed line, counting how often it was asked."""

    def __init__(self, answer: str = "जी, डीएपी उपलब्ध है।") -> None:
        self.answer = answer
        self.calls: list[str] = []
        self.delay_s = 0.0

    async def respond(self, transcript: str, *, language: str) -> AsyncIterator[str]:
        self.calls.append(transcript)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        yield self.answer


class NeverFinishingResponder:
    """Generates forever, so cancellation can be observed rather than assumed."""

    def __init__(self) -> None:
        self.started = 0
        self.cancelled = 0

    async def respond(self, transcript: str, *, language: str) -> AsyncIterator[str]:
        self.started += 1
        try:
            while True:
                await asyncio.sleep(0.01)
                yield "..."
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


def _pipeline(
    script: list[SttEvent],
    responder: object,
    *,
    language: str = "hi-IN",
    emits_turn_events: bool = True,
) -> tuple[ConversationPipeline, ToneTTS, list[bytes]]:
    defaults = get_defaults()
    stack = build_speech_stack(language, Settings(), defaults)
    tts = ToneTTS()
    sent: list[bytes] = []

    async def send(frame: bytes) -> None:
        sent.append(frame)

    stack = type(stack)(
        language=stack.language,
        served_by=stack.served_by,
        stt=ScriptedSTT(script, emits_turn_events=emits_turn_events),
        stt_config=stack.stt_config,
        tts=tts,
        tts_config=stack.tts_config,
        turn_detector=stack.turn_detector,
        quality_tier=stack.quality_tier,
        tier_note=stack.tier_note,
    )
    pipeline = ConversationPipeline(
        stack=stack,
        sender=PacedSender(send, realtime=False),
        responder=responder,  # type: ignore[arg-type]
        defaults=defaults,
    )
    return pipeline, tts, sent


# --------------------------------------------------------------------------- #
# A complete turn
# --------------------------------------------------------------------------- #


async def test_a_turn_is_heard_answered_and_spoken() -> None:
    responder = CannedResponder()
    pipeline, _tts, sent = _pipeline(
        [
            SttEvent(type=SttEventType.SPEECH_STARTED),
            SttEvent(type=SttEventType.PARTIAL, text="डीएपी"),
            SttEvent(type=SttEventType.END_OF_TURN, text="डीएपी मिलेगा क्या"),
        ],
        responder,
    )

    await pipeline.run()

    assert responder.calls == ["डीएपी मिलेगा क्या"]
    assert len(pipeline.turns) == 1
    assert pipeline.turns[0].response == "जी, डीएपी उपलब्ध है।"
    assert sent, "no audio reached the transport"


async def test_the_synthesiser_only_ever_receives_normalised_text() -> None:
    """§5.3: never hand a raw catalogue string to the TTS. The pipeline is the
    last place that can guarantee it."""
    pipeline, tts, _ = _pipeline(
        [SttEvent(type=SttEventType.END_OF_TURN, text="रेट क्या है")],
        CannedResponder("DAP 50 kg की बोरी ₹1,350 की है।"),
    )

    await pipeline.run()

    assert tts.requests
    for request in tts.requests:
        assert "₹" not in request
        assert not any(character.isdigit() for character in request)
        assert "DAP" not in request
    assert "तेरह सौ पचास" in " ".join(tts.requests)


async def test_an_empty_transcript_does_not_invent_an_answer() -> None:
    responder = CannedResponder()
    pipeline, _, _ = _pipeline([SttEvent(type=SttEventType.END_OF_TURN, text="   ")], responder)

    await pipeline.run()

    assert responder.calls == []
    assert pipeline.turns[0].response == ""


# --------------------------------------------------------------------------- #
# Speculative generation (§5.2)
# --------------------------------------------------------------------------- #


async def test_a_matching_eager_signal_is_used_rather_than_regenerated() -> None:
    """The whole point of §5.2: on a turn that commits, the answer is already
    in flight and the model is not asked twice."""
    responder = CannedResponder()
    pipeline, _, _ = _pipeline(
        [
            SttEvent(type=SttEventType.EAGER_END_OF_TURN, text="डीएपी मिलेगा"),
            SttEvent(type=SttEventType.END_OF_TURN, text="डीएपी मिलेगा"),
        ],
        responder,
    )

    await pipeline.run()

    assert responder.calls == ["डीएपी मिलेगा"], "the model was asked twice"
    assert pipeline.turns[0].metrics is not None
    assert pipeline.turns[0].metrics.speculative_hit


async def test_a_resumed_turn_cancels_the_speculation() -> None:
    """§5.2: the farmer carried on talking, so the half-formed answer must not
    survive to be spoken at them."""
    responder = NeverFinishingResponder()
    pipeline, _, sent = _pipeline(
        [
            SttEvent(type=SttEventType.EAGER_END_OF_TURN, text="डीएपी"),
            SttEvent(type=SttEventType.TURN_RESUMED, text="डीएपी और यूरिया"),
        ],
        responder,
    )

    await pipeline.run()
    await pipeline.close()

    assert responder.started == 1
    assert responder.cancelled == 1, "the speculative generation was never cancelled"
    assert not sent, "audio from a discarded speculation reached the caller"


async def test_speculation_for_a_different_question_is_thrown_away() -> None:
    """The eager signal caught only part of what was said, so its answer is to
    the wrong question."""
    responder = CannedResponder()
    pipeline, _, _ = _pipeline(
        [
            SttEvent(type=SttEventType.EAGER_END_OF_TURN, text="डीएपी"),
            SttEvent(type=SttEventType.END_OF_TURN, text="डीएपी और यूरिया दोनों चाहिए"),
        ],
        responder,
    )

    await pipeline.run()

    assert responder.calls[-1] == "डीएपी और यूरिया दोनों चाहिए"
    assert pipeline.turns[0].metrics is not None
    assert pipeline.turns[0].metrics.speculative_discarded
    assert not pipeline.turns[0].metrics.speculative_hit


async def test_closing_the_call_cancels_work_still_in_flight() -> None:
    """A dropped line mid-speculation must not leave a task generating."""
    responder = NeverFinishingResponder()
    pipeline, _, _ = _pipeline(
        [SttEvent(type=SttEventType.EAGER_END_OF_TURN, text="डीएपी")], responder
    )

    await pipeline.run()
    await pipeline.close()

    assert responder.cancelled == 1


# --------------------------------------------------------------------------- #
# Barge-in through the pipeline (§5.4)
# --------------------------------------------------------------------------- #


async def test_interrupting_records_only_what_was_heard() -> None:
    """§5.4 step 3, end to end: the turn's ``spoken`` must be a prefix of the
    response, not the whole thing."""
    pipeline, _, _ = _pipeline(
        [SttEvent(type=SttEventType.END_OF_TURN, text="रेट बताइए")],
        CannedResponder("एक दो तीन चार पाँच छह सात आठ।"),
    )

    await pipeline.run()

    turn = pipeline.turns[0]
    assert turn.response
    # Nothing interrupted, so everything sent was heard.
    assert turn.spoken


async def test_a_caller_speaking_while_silent_does_not_trigger_barge_in() -> None:
    """Speech-start while the agent is not talking is just the next turn."""
    cleared = False

    async def clear() -> None:
        nonlocal cleared
        cleared = True

    pipeline, _, _ = _pipeline([SttEvent(type=SttEventType.SPEECH_STARTED)], CannedResponder())
    pipeline.clear_playback = clear

    await pipeline.run()

    assert not cleared, "the provider buffer was cleared with nothing playing"


# --------------------------------------------------------------------------- #
# The §21 Phase 2 scripted conversation, per language
# --------------------------------------------------------------------------- #


def _recogniser_ends_the_turn(language: str) -> bool:
    """Whether this language's recogniser emits its own end-of-turn."""
    from uaagro_domain.enums import VENDOR_DECIDED_TURN_STRATEGIES

    _served_by, route = get_defaults().resolve_language(language)
    return route.turn is not None and route.turn.strategy in VENDOR_DECIDED_TURN_STRATEGIES


@pytest.mark.parametrize("language", ["hi-IN", "mr-IN", "ml-IN", "or-IN"])
async def test_a_scripted_conversation_completes_in_each_language(language: str) -> None:
    """§21 Phase 2 gate, minus the live vendors.

    Both commit paths have to stay covered: a recogniser that ends the turn
    itself, and one that only transcribes with a detector in front. Which
    languages fall on which side is a routing decision that has already changed
    once -- Marathi moved when Soniox replaced the Flux/Sarvam split -- so this
    reads the answer out of the routing table instead of restating it. Odia is
    in the list because it is currently the only language on the second path,
    and a path no parametrisation reaches is a path nothing tests.
    """
    emits = _recogniser_ends_the_turn(language)
    script = (
        [
            SttEvent(type=SttEventType.SPEECH_STARTED),
            SttEvent(type=SttEventType.END_OF_TURN, text="डीएपी मिलेगा"),
            SttEvent(type=SttEventType.END_OF_TURN, text="कितने का है"),
        ]
        if emits
        else [
            SttEvent(type=SttEventType.SPEECH_STARTED),
            SttEvent(type=SttEventType.FINAL, text="डीएपी मिलेगा"),
            SttEvent(type=SttEventType.FINAL, text="कितने का है"),
        ]
    )
    responder = CannedResponder()
    pipeline, _, sent = _pipeline(script, responder, language=language, emits_turn_events=emits)

    await pipeline.run()

    assert len(pipeline.turns) == 2, f"{language} did not complete two turns"
    assert all(turn.response for turn in pipeline.turns)
    assert sent


# --------------------------------------------------------------------------- #
# Latency measurement (§7)
# --------------------------------------------------------------------------- #


async def test_the_turn_clock_starts_at_end_of_speech() -> None:
    """Starting it at the commit would flatter the number by exactly the
    eager-EOT overlap that §5.2 exists to provide."""
    pipeline, _, _ = _pipeline(
        [
            SttEvent(type=SttEventType.EAGER_END_OF_TURN, text="डीएपी"),
            SttEvent(type=SttEventType.END_OF_TURN, text="डीएपी"),
        ],
        CannedResponder(),
    )

    await pipeline.run()

    metrics = pipeline.turns[0].metrics
    assert metrics is not None
    assert metrics.speech_ended_at is not None
    assert metrics.eager_at is not None
    # The clock was started by the eager signal, before the commit.
    assert metrics.speech_ended_at <= metrics.eager_at
    assert metrics.total_ms is not None


def test_missing_marks_produce_absent_segments_not_zeros() -> None:
    """A segment that reads 0 ms looks like a fast one. An absent segment
    looks like what it is."""
    metrics = TurnMetrics(turn_index=0)
    assert metrics.segments() == {}
    assert metrics.total_ms is None


def test_budget_breaches_name_the_segment_and_the_ceiling() -> None:
    defaults = get_defaults()
    metrics = TurnMetrics(turn_index=0)
    metrics.speech_ended_at = 0.0
    metrics.committed_at = 5.0  # 5 s: far past the 250 ms ceiling
    metrics.first_audio_at = 6.0

    breaches = metrics.budget_breaches(defaults.latency_budget_ms)
    assert any("turn_commit" in b for b in breaches)
    assert any("total" in b for b in breaches)


def test_cached_turns_are_excluded_from_the_headline_percentile() -> None:
    """A Tier-3 cache hit answers in under 100 ms and would drag the
    distribution down until it no longer described the live path (§9)."""
    latency = CallLatency()
    for index, (total_s, cached) in enumerate(
        [(0.9, False), (1.0, False), (0.05, True), (0.05, True)]
    ):
        metrics = TurnMetrics(turn_index=index)
        metrics.speech_ended_at = 0.0
        metrics.first_audio_at = total_s
        metrics.from_cache = cached
        latency.add(metrics)

    summary = latency.summary()
    assert summary["turn_count"] == 4
    assert summary["cached_turns"] == 2
    assert float(summary["p50"]) > 500, "cache hits leaked into the live percentile"


def test_percentile_uses_nearest_rank() -> None:
    """With a handful of turns, interpolation invents a value between two real
    measurements and reports it as the p95."""
    assert percentile([100.0], 95) == 100.0
    assert percentile([100.0, 200.0, 300.0, 400.0], 95) == 400.0
    with pytest.raises(ValueError):
        percentile([], 95)


def test_the_call_summary_reports_whether_it_met_the_budget() -> None:
    defaults = get_defaults()
    latency = CallLatency()
    for index in range(3):
        metrics = TurnMetrics(turn_index=index)
        metrics.speech_ended_at = 0.0
        metrics.first_audio_at = 0.5
        latency.add(metrics)

    verdict = latency.breaches(defaults.latency_budget_ms)
    assert verdict["measured"] is True
    assert verdict["within_budget"] is True
    assert verdict["ceiling_ms"] == 1200
