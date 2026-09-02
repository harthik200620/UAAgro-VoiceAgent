"""The conversational turn loop (§5.2, §5.3, §5.4, §7).

Drives one call: audio in, turns out. What plugs in on either side is deliberately
abstract -- :class:`Responder` is whatever decides *what to say*, and Phase 4
supplies the LLM-backed one. Keeping that seam means the timing machinery here
is testable without a model.

**Speculative generation is the reason this is not a simple loop.** §5.2 has the
recogniser emit an eager end-of-turn while the caller may still be speaking, and
the pipeline starts generating on it. Flux guarantees the eager transcript
matches the eventual final one exactly, so on the roughly 80% of turns that
commit, the answer is already in flight and ~300 ms of the budget is already
spent productively.

The obligation that comes with it is absolute: on ``TurnResumed`` the
speculative work **must** be cancelled and must not reach the caller. §5.2 calls
an orphaned generation that still speaks a severe bug, and it is -- the farmer
hears an answer to a question they were still in the middle of asking. Every
path out of speculation here goes through :meth:`_discard_speculation`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

import structlog

from uaagro_domain.settings import Defaults

from ..adapters.factory import SpeechStack
from ..adapters.stt.base import SttEvent, SttEventType
from ..flow.agent import HOLD_SCRIPT_HI
from ..runtime.audio_cache import AudioCache
from ..runtime.metrics import CallLatency, TurnMetrics
from ..runtime.playback import BargeInPolicy, PacedSender, handle_barge_in
from ..text.speech import SentenceBuffer, split_sentences, text_for_speech
from ..turn.base import TurnDecision, TurnState

log = structlog.get_logger(__name__)

#: How often the turn detector is consulted while the caller is silent.
DETECTOR_TICK_S = 0.05

#: How long the caller may hear nothing before the agent says "one moment".
#: Above §7's 700 ms p50 for a whole turn, so it fires only on turns that
#: are already over budget rather than on every one.
HOLD_AFTER_MS = 750.0

#: Silence the recogniser had already observed when it emitted a final
#: transcript. Mirrors Sarvam's configured ``silence_duration_ms``; the
#: detector needs a real elapsed figure, not a guess.
RECOGNISER_ENDPOINT_SILENCE_MS = 700.0


class Responder(Protocol):
    """Decides what the agent says. Phase 4 supplies the LLM implementation."""

    def respond(self, transcript: str, *, language: str) -> AsyncIterator[str]:
        """Yield the answer as text, in the order it should be spoken.

        Declared as a plain method returning an async iterator rather than as
        ``async def``: an async generator is not a coroutine, and typing it as
        one makes every implementation look wrong to the checker.

        Yielding sentence by sentence lets synthesis start before the whole
        answer exists, which is most of the §7 budget.
        """
        ...


@dataclass
class _Speculation:
    """An answer being generated before the turn has committed."""

    transcript: str
    task: asyncio.Task[list[str]]

    def matches(self, final_transcript: str) -> bool:
        return self.transcript.strip() == final_transcript.strip()


@dataclass
class TurnOutcome:
    """What one turn produced, for the call record and the tests."""

    turn_index: int
    transcript: str = ""
    response: str = ""
    #: What the caller actually heard, which differs from ``response`` whenever
    #: they interrupted (§5.4).
    spoken: str = ""
    interrupted: bool = False
    metrics: TurnMetrics | None = None


@dataclass
class ConversationPipeline:
    """One call's conversational loop."""

    stack: SpeechStack
    sender: PacedSender
    responder: Responder
    defaults: Defaults
    cache: AudioCache = field(default_factory=AudioCache)
    barge_in: BargeInPolicy = field(default_factory=BargeInPolicy)
    clear_playback: Callable[[], Awaitable[None]] | None = None
    slow_speaker: bool = False

    latency: CallLatency = field(default_factory=CallLatency)
    turns: list[TurnOutcome] = field(default_factory=list)

    _speculation: _Speculation | None = field(default=None, repr=False)
    _turn_index: int = 0
    _turn_audio: bytearray = field(default_factory=bytearray, repr=False)
    _transcript: str = ""
    _agent_speaking: bool = False
    _metrics: TurnMetrics | None = field(default=None, repr=False)
    #: Held while anything is being spoken. The hold phrase and the answer
    #: are produced by different tasks, and two utterances interleaved
    #: frame by frame is not slow -- it is unintelligible.
    _speaking: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    #: Set once the answer's first sentence has the floor, so a hold phrase
    #: that was already waiting on the lock stands down instead of
    #: interrupting the answer it was covering for.
    _answer_started: bool = False

    # -- audio in --------------------------------------------------------- #

    async def feed_audio(self, pcm: bytes) -> None:
        """Push caller audio to the recogniser and the turn detector.

        Never blocks: §7.6 makes anything slow on this path a P1.
        """
        self._turn_audio.extend(pcm)
        await self.stack.stt.send_audio(pcm)

    # -- the loop --------------------------------------------------------- #

    async def run(self) -> None:
        """Consume recogniser events until the stream closes."""
        async for event in self.stack.stt.events():
            await self._handle(event)

    async def _handle(self, event: SttEvent) -> None:
        match event.type:
            case SttEventType.SPEECH_STARTED:
                await self._on_speech_started()

            case SttEventType.PARTIAL:
                self._transcript = event.text

            case SttEventType.FINAL:
                # Sarvam segments on silence; the detector in front decides
                # whether this ends the turn.
                self._transcript = event.text
                self._note_speech_end(event)
                if not self.stack.stt.emits_turn_events:
                    await self._maybe_commit_via_detector()

            case SttEventType.EAGER_END_OF_TURN:
                self._note_speech_end(event)
                self._begin_speculation(event.text)

            case SttEventType.TURN_RESUMED:
                await self._discard_speculation("caller resumed speaking")

            case SttEventType.END_OF_TURN:
                self._note_speech_end(event)
                await self._commit(event.text or self._transcript)

            case SttEventType.ERROR:
                log.warning("pipeline.stt_error", detail=event.detail)

    def _note_speech_end(self, event: SttEvent) -> None:
        """Start the §7 clock at end of speech, not at the commit decision.

        Starting it at commit would hide the eager-EOT overlap -- flattering
        the measurement by exactly the amount of the optimisation §5.2 exists
        to provide.
        """
        if self._metrics is None:
            self._metrics = TurnMetrics(turn_index=self._turn_index)
        if self._metrics.speech_ended_at is None:
            self._metrics.mark("speech_ended_at")
        if event.type is SttEventType.EAGER_END_OF_TURN:
            self._metrics.mark("eager_at")

    async def _on_speech_started(self) -> None:
        """The caller started talking. Interrupt if the agent is speaking."""
        if not self._agent_speaking:
            return
        result = await handle_barge_in(
            self.sender,
            policy=self.barge_in,
            clear_playback=self.clear_playback or _noop,
            cancel_synthesis=None,
        )
        if result.interrupted and self.turns:
            # §5.4 step 3: the model must believe it said only what was heard.
            self.turns[-1].spoken = result.spoken_text
            self.turns[-1].interrupted = True

    # -- turn detection for recognisers that do not decide ---------------- #

    async def _maybe_commit_via_detector(self) -> None:
        """Ask the detector whether this utterance ends the turn.

        Reached only for recognisers that segment on their own VAD. By the time
        a final transcript arrives, that VAD has already waited a real silence
        threshold -- so the detector is told so rather than being handed an
        unrelated number from config and asked to guess.
        """
        detector = self.stack.turn_detector
        state = TurnState(
            silence_ms=RECOGNISER_ENDPOINT_SILENCE_MS,
            transcript=self._transcript,
            audio=bytes(self._turn_audio),
            slow_speaker=self.slow_speaker,
            recogniser_endpointed=True,
        )
        result = await detector.evaluate(state)
        if result.decision is TurnDecision.END:
            log.debug("pipeline.turn_ended", reason=result.reason)
            await self._commit(self._transcript)
        else:
            log.debug("pipeline.turn_continues", reason=result.reason)

    # -- speculation ------------------------------------------------------ #

    def _begin_speculation(self, transcript: str) -> None:
        """Start generating on the eager signal (§5.2)."""
        if not transcript.strip() or self._speculation is not None:
            return
        self._speculation = _Speculation(
            transcript=transcript,
            task=asyncio.create_task(self._collect_response(transcript)),
        )
        log.debug("pipeline.speculation_started")

    async def _discard_speculation(self, reason: str) -> None:
        """Cancel speculative work and wait for it to actually stop.

        Awaiting the cancellation matters. Firing ``cancel()`` and moving on
        leaves a task that may still be mid-``yield``, and §5.2 is explicit
        that a generation which still speaks is a severe bug.
        """
        speculation = self._speculation
        self._speculation = None
        if speculation is None:
            return
        speculation.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await speculation.task
        if self._metrics is not None:
            self._metrics.speculative_discarded = True
        log.debug("pipeline.speculation_discarded", reason=reason)

    async def _take_speculation(self, transcript: str) -> list[str] | None:
        """Use the speculative answer if it was for this exact transcript."""
        speculation = self._speculation
        self._speculation = None
        if speculation is None:
            return None
        if not speculation.matches(transcript):
            # The caller said more than the eager signal captured, so the
            # answer would be to the wrong question.
            speculation.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await speculation.task
            if self._metrics is not None:
                self._metrics.speculative_discarded = True
            return None
        try:
            chunks = await speculation.task
        except (asyncio.CancelledError, Exception) as exc:
            log.warning("pipeline.speculation_failed", error=type(exc).__name__)
            return None
        if self._metrics is not None:
            self._metrics.speculative_hit = True
        return chunks

    async def _collect_response(self, transcript: str) -> list[str]:
        return [
            chunk
            async for chunk in self.responder.respond(transcript, language=self.stack.served_by)
        ]

    # -- commit ----------------------------------------------------------- #

    async def _commit(self, transcript: str) -> None:
        """Answer the turn and speak it."""
        transcript = transcript.strip()
        metrics = self._metrics or TurnMetrics(turn_index=self._turn_index)
        metrics.mark("committed_at")
        metrics.mark("transcript_at")

        outcome = TurnOutcome(turn_index=self._turn_index, transcript=transcript)
        self.turns.append(outcome)

        if not transcript:
            self._finish_turn(metrics, outcome)
            return

        metrics.mark("generation_started_at")
        chunks = await self._take_speculation(transcript)
        if chunks is not None:
            # A speculative answer is already complete -- §5.2 generated it
            # during the caller's trailing silence, which is the whole point.
            metrics.mark("first_token_at")
            outcome.response = " ".join(chunks).strip()
            await self._speak(outcome.response, metrics, outcome)
        else:
            await self._stream_answer(transcript, metrics, outcome)
        self._finish_turn(metrics, outcome)

    async def _hold_if_slow(self, metrics: TurnMetrics, outcome: TurnOutcome) -> None:
        """Say "one moment" if the answer is taking too long (§11.4).

        §7 budgets a whole turn at 700 ms p50, but a tool call and a
        generation can each run to their p95 and leave well over a second of
        silence. On a rural GSM line that silence is indistinguishable from a
        dropped call: the farmer says "हैलो? हैलो?", which the recogniser hears
        as speech, which triggers barge-in, which abandons the answer that was
        seconds from arriving. Filling the gap is not politeness -- it stops a
        slow turn from becoming a broken one.

        The phrase is pre-rendered at worker start, so saying it costs a cache
        lookup rather than the synthesiser round trip that would make a slow
        turn slower.
        """
        try:
            await asyncio.sleep(HOLD_AFTER_MS / 1000)
        except asyncio.CancelledError:
            return

        async with self._speaking:
            # Re-checked under the lock: the answer may have arrived while this
            # was waiting, and covering for a delay that is over would talk
            # over the reply.
            if self._answer_started or self.sender.cancelled:
                return
            audio = await self.cache.get(
                HOLD_SCRIPT_HI, self.stack.tts_config, provider=self.stack.tts.provider
            )
            if audio is None:
                # Not synthesised on demand. This exists to cover a slow turn;
                # adding a vendor call to it would deepen the hole.
                log.debug("pipeline.hold_phrase_uncached")
                return
            metrics.spoke_hold_phrase = True
            if not await self.sender.play(HOLD_SCRIPT_HI, audio):
                outcome.interrupted = True

    async def _stream_answer(
        self, transcript: str, metrics: TurnMetrics, outcome: TurnOutcome
    ) -> None:
        """Speak each sentence as the model finishes it.

        This is where most of §7's budget is won or lost. Collecting the whole
        answer before synthesising anything serialises three waits that have no
        reason to be sequential -- the model finishing, the synthesiser
        finishing, and the audio playing -- and the farmer hears silence for the
        sum of them. Releasing a sentence the moment it is complete overlaps
        generation with synthesis and playback, so the pause before the agent
        speaks is the *first* sentence's cost rather than the whole answer's.

        It also makes barge-in cheaper: an interruption during sentence two
        cancels a much smaller amount of committed work.
        """
        buffer = SentenceBuffer()
        spoken: list[str] = []
        self._begin_playback()
        hold = asyncio.create_task(self._hold_if_slow(metrics, outcome))

        try:
            async for fragment in self.responder.respond(
                transcript, language=self.stack.served_by
            ):
                if metrics.first_token_at is None:
                    metrics.mark("first_token_at")
                for sentence in buffer.add(fragment):
                    if not await self._speak_sentence(sentence, metrics, outcome):
                        return
                    spoken.append(sentence)

            # The tail: an answer whose last sentence carries no terminator is
            # still an answer, and dropping it would truncate mid-thought.
            for sentence in buffer.flush():
                if not await self._speak_sentence(sentence, metrics, outcome):
                    return
                spoken.append(sentence)
            metrics.mark("audio_sent_at")
        finally:
            # `outcome.response` is what the agent *intended* to say and is
            # assembled from the sentences actually handed to the synthesiser,
            # so an interrupted turn records the part that existed rather than
            # a whole answer that was never spoken. What the caller *heard* is
            # `outcome.spoken`, which the tracker computes (§5.4).
            hold.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await hold
            outcome.response = " ".join(spoken).strip()
            self._end_playback(outcome)

    def _begin_playback(self) -> None:
        """Open a speaking turn. Shared by the streaming and complete paths."""
        self._agent_speaking = True
        self._answer_started = False
        self.sender.resume()
        self.sender.tracker.reset()

    def _end_playback(self, outcome: TurnOutcome) -> None:
        self._agent_speaking = False
        outcome.spoken = self.sender.tracker.spoken_text()

    async def _speak_sentence(
        self, sentence: str, metrics: TurnMetrics, outcome: TurnOutcome
    ) -> bool:
        """Synthesise one sentence and play it. False when interrupted.

        Returning a bool rather than raising: an interruption is the normal
        end of a turn on a helpline, not an error, and §5.4 wants the caller's
        speech handled promptly rather than after an exception unwinds.
        """
        if self.sender.cancelled:
            outcome.interrupted = True
            return False

        # §5.3: never hand a raw catalogue string to the synthesiser.
        speakable = text_for_speech(sentence, language=self.stack.tts_config.language)
        if not speakable:
            return True

        # Claim the floor. Taken for the whole sentence so a hold phrase cannot
        # start between this sentence's chunks, and `_answer_started` is set
        # inside the lock so a hold already waiting on it stands down rather
        # than talking over the answer it was covering for.
        async with self._speaking:
            self._answer_started = True
            return await self._synthesise_and_play(speakable, metrics, outcome)

    async def _synthesise_and_play(
        self, speakable: str, metrics: TurnMetrics, outcome: TurnOutcome
    ) -> bool:
        """Render one sentence and stream it out. Called holding `_speaking`."""

        if metrics.synthesis_started_at is None:
            metrics.mark("synthesis_started_at")

        cached = await self.cache.get(
            speakable, self.stack.tts_config, provider=self.stack.tts.provider
        )
        if cached is not None:
            metrics.from_cache = True
            if metrics.first_audio_at is None:
                metrics.mark("first_audio_at")
            if not await self.sender.play(speakable, cached):
                outcome.interrupted = True
                return False
            return True

        # Streamed, not collected. `synthesise_all` exists for the cache and
        # for tests; its own docstring says it is never for a live turn,
        # because waiting for the last chunk throws away the streaming benefit
        # the §7 tts_ttfb budget is written against.
        collected = bytearray()
        first_chunk = True
        async for chunk in self.stack.tts.synthesise(speakable, self.stack.tts_config):
            if not chunk.audio:
                continue
            if first_chunk:
                if metrics.first_audio_at is None:
                    metrics.mark("first_audio_at")
                self.sender.tracker.open_segment(speakable)
                first_chunk = False
            collected.extend(chunk.audio)
            if not await self.sender.play_chunk(chunk.audio):
                outcome.interrupted = True
                return False

        # The tail of the sentence: up to one frame that never completed. Held
        # back it would clip the final syllable of every sentence.
        if not await self.sender.flush():
            outcome.interrupted = True
            return False

        if collected:
            # Cached whole, so the next call skips synthesis entirely (§9.3).
            await self.cache.put(
                speakable,
                self.stack.tts_config,
                bytes(collected),
                provider=self.stack.tts.provider,
            )
        return True

    async def _speak(self, response: str, metrics: TurnMetrics, outcome: TurnOutcome) -> None:
        """Speak an answer that is already complete.

        The speculative path (§5.2) and anything replayed from cache arrive
        whole, so there is nothing to stream -- but they still go out a
        sentence at a time so barge-in lands on a natural boundary.
        """
        if not response.strip():
            return

        self._begin_playback()
        try:
            for sentence in split_sentences(response):
                if not await self._speak_sentence(sentence, metrics, outcome):
                    break
            metrics.mark("audio_sent_at")
        finally:
            self._end_playback(outcome)

    async def _synthesise(self, text: str) -> bytes:
        """Cached audio where possible, synthesis otherwise (§5.3, §8)."""
        cached = await self.cache.get(text, self.stack.tts_config, provider=self.stack.tts.provider)
        if cached is not None:
            if self._metrics is not None:
                self._metrics.from_cache = True
            return cached

        audio = await self.stack.tts.synthesise_all(text, self.stack.tts_config)
        if audio:
            await self.cache.put(
                text, self.stack.tts_config, audio, provider=self.stack.tts.provider
            )
        return audio

    def _finish_turn(self, metrics: TurnMetrics, outcome: TurnOutcome) -> None:
        outcome.metrics = metrics
        self.latency.add(metrics)

        breaches = metrics.budget_breaches(self.defaults.latency_budget_ms)
        if breaches:
            # §7 gates on the distribution, not on one turn, so this is a
            # signal rather than a failure -- but an unlogged breach is a
            # regression nobody notices until a farmer does.
            log.info("pipeline.budget_breach", turn=metrics.turn_index, breaches=breaches)

        self._turn_index += 1
        self._metrics = None
        self._transcript = ""
        self._turn_audio.clear()

    # -- shutdown --------------------------------------------------------- #

    async def close(self) -> None:
        await self._discard_speculation("call ended")
        await self.stack.stt.close()
        await self.stack.tts.close()
        await self.stack.turn_detector.close()


async def _noop() -> None:
    return None
