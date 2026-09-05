"""The conversational turn loop (§5.2, §5.3, §5.4, §7).

Drives one call: audio in, turns out. What plugs in on either side is deliberately
abstract -- :class:`Responder` is whatever decides *what to say*, and Phase 4
supplies the LLM-backed one. Keeping that seam means the timing machinery here
is testable without a model.

**The answer is spoken in its own task.** The first version awaited playback
inside the recogniser's event loop, which meant that while the agent was
talking, nothing was listening: a speech-start from the recogniser sat in the
queue until the answer finished, and "barge-in" happened a sentence too late
-- or, as the first browser test put it, "it does not stop when I talk". Now
the loop keeps consuming events during playback, and an interruption reaches
the sender within a frame.

**Interruption is decided on the audio, not on the words.** A local voice
gate (:mod:`voice_worker.runtime.vad`) says when the caller starts speaking,
within about 200 ms and without mistaking a fan for a person. The
recogniser's own speech-start is honoured only when the gate has heard voice
too, because a recogniser will happily transcribe the agent's echo. A turn
that ends while the agent is still speaking is an interruption if it carries
real words and noise if it does not.

**What was cut off is not thrown away.** After a cut the model is still read
for the rest of its answer, so when the interruption turns out to be a "हाँ"
or a cough the agent picks up where it stopped instead of answering a
non-question -- and when it turns out to be a real question, the model is
told exactly how much the farmer heard (§5.4 step 3).

**Speculative generation streams.** §5.2 has the recogniser emit an eager
end-of-turn while the caller may still be speaking, and the pipeline starts
generating on it. Sentences produced during the speculation are queued, and
the first one is synthesised into the cache as soon as it exists, so on the
turns that commit the reply is in flight and its first audio is often already
rendered. On ``TurnResumed`` the speculative work **must** be cancelled and
must not reach the caller; every path out of speculation goes through
:meth:`_discard_speculation`.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from uaagro_domain.enums import CallOutcome
from uaagro_domain.settings import CallHandlingSettings, Defaults

from ..adapters.factory import SpeechStack
from ..adapters.stt.base import SttEvent, SttEventType
from ..adapters.tts.base import TtsConfig
from ..flow.address import is_backchannel
from ..flow.closing import CLOSING_LINE_HI, SILENCE_PROMPT_HI, SILENCE_WARN_HI
from ..runtime.audio_cache import AudioCache
from ..runtime.metrics import CallLatency, TurnMetrics
from ..runtime.playback import BargeInPolicy, PacedSender, handle_barge_in
from ..runtime.vad import VoiceEvent, VoiceGate
from ..text.script import words as script_words
from ..text.speech import SentenceBuffer, split_sentences, text_for_speech
from ..turn.base import TurnDecision, TurnState

log = structlog.get_logger(__name__)

#: How often the turn detector is consulted while the caller is silent.
DETECTOR_TICK_S = 0.05

#: Silence the recogniser had already observed when it emitted a final
#: transcript. Mirrors Sarvam's configured ``silence_duration_ms``; the
#: detector needs a real elapsed figure, not a guess.
RECOGNISER_ENDPOINT_SILENCE_MS = 700.0

#: After an interruption, how long the model is still read for the rest of
#: its answer. Bounded, because the model is billed for every token and the
#: farmer may be asking something new.
REMAINDER_DRAIN_S = 2.5

#: A recogniser speech-start interrupts the agent only if the voice gate
#: heard the caller this recently. Otherwise it is echo or noise.
VOICE_CORROBORATION_S = 1.2

#: After a voice-triggered cut, silence this long with no transcript means
#: it was not a turn -- a cough, a "हाँ" the recogniser dropped -- and the
#: agent picks up where it stopped.
RESUME_AFTER_SILENCE_S = 0.9
#: The watchdog gives up after this long and leaves the floor to the caller.
RESUME_WATCHDOG_MAX_S = 6.0

#: A turn that ends while the agent is still speaking is an interruption only
#: if the recogniser heard this many words. One or two is echo or noise.
MIN_WORDS_TO_INTERRUPT_LATE = 3

#: The opening clause is released to the synthesiser once this many words
#: precede a comma. Set on the first sentence of every turn. Three, because
#: Devanagari costs ~3.5 tokens a word and every word waited for is ~60 ms.
FIRST_CLAUSE_WORDS = 3

#: A sentence the farmer heard at least this much of is not repeated when
#: the agent resumes; anything less is spoken again from the start.
HEARD_ENOUGH = 0.6


@dataclass(frozen=True, slots=True)
class SilenceLadder:
    """§11.4: what happens when the caller goes quiet, in seconds of silence.

    Prompt, prompt again, then say goodbye and hang up. Rural callers walk
    off to read a label and lines drop a second of audio, so the first two
    rungs are patient; the last one exists because a call nobody is on
    still costs a line and a worker slot.
    """

    prompt_s: float = 6.0
    warn_s: float = 15.0
    close_s: float = 25.0

    @classmethod
    def from_settings(cls, handling: CallHandlingSettings) -> SilenceLadder:
        return cls(
            prompt_s=float(handling.silence_first_prompt_s),
            warn_s=float(handling.silence_second_prompt_s),
            close_s=float(handling.silence_abandon_s),
        )

    def stages(self) -> tuple[tuple[float, str | None], ...]:
        """(seconds of silence, what to say) -- ``None`` means hang up."""
        return (
            (self.prompt_s, SILENCE_PROMPT_HI),
            (self.warn_s, SILENCE_WARN_HI),
            (self.close_s, None),
        )


class Responder(Protocol):
    """Decides what the agent says. Phase 4 supplies the LLM implementation.

    Optional hooks, looked up by name so a scripted responder need not carry
    them: ``note_interruption(heard)``, ``note_resumed(text)``,
    ``discard_pending()``, and ``resumes_after_backchannel``.
    """

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
    queue: asyncio.Queue[str | None]
    task: asyncio.Task[None] | None = None
    prefetch: asyncio.Task[None] | None = None
    failed: bool = False

    def matches(self, final_transcript: str) -> bool:
        """The same words. The recogniser adds a "?" or a danda at the
        endpoint, and a question mark is not a different question."""
        return script_words(self.transcript) == script_words(final_transcript)

    async def cancel(self) -> None:
        """Stop generating and wait for it to actually stop.

        Awaiting the cancellation matters. Firing ``cancel()`` and moving on
        leaves a task that may still be mid-``yield``, and §5.2 is explicit
        that a generation which still speaks is a severe bug.
        """
        for task in (self.task, self.prefetch):
            if task is None or task.done():
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def sentences(self) -> AsyncIterator[str]:
        """What was generated so far, then the rest as it arrives."""
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item


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
    #: The agent said the rest of an interrupted answer rather than a new one.
    resumed: bool = False
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
    #: Decides, on the audio, whether the caller is talking. ``None`` means
    #: the recogniser's speech-start is trusted on its own -- the shape the
    #: scripted tests use, and the shape a route without audio access has.
    voice_gate: VoiceGate | None = None
    #: What to do when the caller goes quiet. ``None`` means wait forever,
    #: which is what the scripted tests want and no live call does.
    silence: SilenceLadder | None = None
    #: Set by the pipeline when it ended the call itself -- the silence
    #: ladder ran out. The session prefers it to the responder's verdict.
    call_outcome: CallOutcome | None = None

    latency: CallLatency = field(default_factory=CallLatency)
    turns: list[TurnOutcome] = field(default_factory=list)

    #: Told about each finished turn -- the session persists it and feeds the
    #: live view. Synchronous and must return fast; it runs on the audio path.
    on_turn: Callable[[TurnOutcome], None] | None = None
    #: Told when the agent starts thinking, speaking or listening (§15.1's
    #: live view shows which). Same contract as ``on_turn``.
    on_activity: Callable[[str], None] | None = None
    #: Awaited once the responder has said its last line and wants the call
    #: ended -- the outbound script's closing. Inbound responders never ask.
    on_call_over: Callable[[], Awaitable[None]] | None = None
    #: A hand-over the responder prepared (§12.3). Called once the transfer
    #: line has been spoken, with the request; the session owns the phone line.
    on_transfer: Callable[[Any], Awaitable[None]] | None = None

    _speculation: _Speculation | None = field(default=None, repr=False)
    _turn_index: int = 0
    _turn_audio: bytearray = field(default_factory=bytearray, repr=False)
    _transcript: str = ""
    _agent_speaking: bool = False
    _metrics: TurnMetrics | None = field(default=None, repr=False)
    #: Held while anything is being spoken, so two utterances produced by
    #: different tasks cannot interleave frame by frame.
    _speaking: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    #: The answer being spoken, so the event loop is free to hear the caller.
    _answer_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _opening_task: asyncio.Task[None] | None = field(default=None, repr=False)
    #: Sentences of an interrupted answer the farmer has not heard.
    _resumable: list[str] = field(default_factory=list, repr=False)
    _resume_watchdog: asyncio.Task[None] | None = field(default=None, repr=False)
    #: Sentences being rendered ahead of their turn to be spoken, by
    #: speakable text. The synthesiser's first byte is 300 ms on a good call
    #: and 1.5 s on a bad one, and paying it *between* sentences put that
    #: much silence in the middle of every answer.
    _ahead: dict[str, asyncio.Task[bytes | None]] = field(default_factory=dict, repr=False)
    #: Fixed lines rendering into the cache; held so they are not collected.
    _background: set[asyncio.Task[bytes | None]] = field(default_factory=set, repr=False)
    _partial_since_cut: bool = False
    #: The current recognition began by interrupting the agent. Decides
    #: what a bare "हाँ" means when it arrives.
    _cut_pending: bool = False
    _silence_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _silence_stage: int = 0
    #: The goodbye is being said; nothing else starts.
    _ending: bool = False
    _closed: bool = False
    #: The language the caller was last heard in, as a route code, when it
    #: differs from the one the call was set up for (§11.1: follow them).
    _turn_language: str = ""
    _unvoiced_logged: set[str] = field(default_factory=set, repr=False)

    # -- language --------------------------------------------------------- #

    def _language(self) -> str:
        return self._turn_language or self.stack.served_by

    def _tts_config(self) -> TtsConfig:
        return self.stack.voices.get(self._language(), self.stack.tts_config)

    def _follow_language(self, heard: str | None) -> None:
        """Follow a caller who switches language, when there is a voice for it.

        The recogniser labels each turn with the language it heard. A turn in
        a language this deployment can speak switches the reply and the voice
        from the next answer on; one it cannot is answered in the language
        being served, and said so once in the log rather than every turn.
        """
        if not heard:
            return
        route = self.stack.language_for(heard)
        if route is None:
            bare = heard.split("-")[0].lower()
            if bare not in self._unvoiced_logged:
                self._unvoiced_logged.add(bare)
                log.info("pipeline.language_unvoiced", heard=bare, serving=self._language())
            return
        if route != self._language():
            log.info("pipeline.language_followed", previous=self._language(), now=route)
            self._turn_language = route

    # -- audio in --------------------------------------------------------- #

    async def feed_audio(self, pcm: bytes) -> None:
        """Push caller audio to the voice gate and the recogniser.

        Never blocks: §7.6 makes anything slow on this path a P1. The gate is
        one FFT per frame; the recogniser send is a socket write.
        """
        self._turn_audio.extend(pcm)
        if self.voice_gate is not None:
            for event in self.voice_gate.feed(pcm):
                if event is VoiceEvent.SPEECH_START:
                    self._cancel_silence_watch()
                    if self._agent_speaking:
                        await self._interrupt("voice")
                elif event is VoiceEvent.SPEECH_END and self._listening():
                    # The caller made a sound and stopped, and the recogniser
                    # may make nothing of it. The clock on their silence
                    # starts again from here rather than never.
                    self._start_silence_watch()
        await self.stack.stt.send_audio(pcm)

    # -- the loop --------------------------------------------------------- #

    async def run(self) -> None:
        """Consume recogniser events until the stream closes.

        The answer in flight is awaited before returning: the loop ends
        when the last thing the agent had to say has been said, not when
        the recogniser stopped talking.
        """
        async for event in self.stack.stt.events():
            await self._handle(event)
        await self.wait_idle()

    async def _handle(self, event: SttEvent) -> None:
        if event.type is not SttEventType.ERROR:
            # Anything the recogniser heard is the caller, not silence.
            self._cancel_silence_watch()
        match event.type:
            case SttEventType.SPEECH_STARTED:
                await self._on_speech_started()

            case SttEventType.PARTIAL:
                self._transcript = event.text
                self._partial_since_cut = True

            case SttEventType.FINAL:
                # Sarvam segments on silence; the detector in front decides
                # whether this ends the turn.
                self._transcript = event.text
                self._partial_since_cut = True
                self._note_speech_end(event)
                if not self.stack.stt.emits_turn_events:
                    await self._maybe_commit_via_detector()

            case SttEventType.EAGER_END_OF_TURN:
                self._note_speech_end(event)
                self._follow_language(event.language)
                self._begin_speculation(event.text)

            case SttEventType.TURN_RESUMED:
                await self._discard_speculation("caller resumed speaking")

            case SttEventType.END_OF_TURN:
                self._note_speech_end(event)
                self._follow_language(event.language)
                await self._on_end_of_turn(event.text or self._transcript)

            case SttEventType.ERROR:
                log.warning("pipeline.stt_error", detail=event.detail)

    async def _on_end_of_turn(self, transcript: str) -> None:
        """The recogniser closed a turn. Whether it *was* one is decided here.

        A turn that ends while the agent is still speaking never went through
        the voice gate -- if it had, the agent would have stopped. Real words
        from the caller mean the gate missed them and this is a late
        interruption; a word or two means the recogniser heard the agent's
        own echo, or the fan, and the turn is dropped rather than answered.
        """
        if self._agent_speaking:
            word_count = len(script_words(transcript))
            if word_count >= MIN_WORDS_TO_INTERRUPT_LATE:
                await self._interrupt("late transcript")
            else:
                log.info("pipeline.turn_ignored_while_speaking", words=word_count)
                await self._discard_speculation("noise while speaking")
                self._reset_recognition()
                return
        await self._commit(transcript)

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

    # -- interruption ------------------------------------------------------ #

    async def _on_speech_started(self) -> None:
        """The recogniser heard the caller start. Interrupt if corroborated."""
        if not self._agent_speaking:
            return
        gate = self.voice_gate
        if (
            gate is not None
            and gate.frames > 0
            and not gate.heard_voice_within(VOICE_CORROBORATION_S)
        ):
            # Words with no voice behind them: the agent's own echo, or the
            # room. If the caller really is speaking, the gate will say so
            # within a frame or two and the cut happens then.
            log.info("bargein.uncorroborated", floor_db=round(gate.floor_db, 1))
            return
        await self._interrupt("recogniser")

    async def _interrupt(self, source: str) -> None:
        """Execute the §5.4 sequence and remember why."""
        if not self._agent_speaking:
            return
        result = await handle_barge_in(
            self.sender,
            policy=self.barge_in,
            clear_playback=self.clear_playback or _noop,
            cancel_synthesis=None,
        )
        if not result.interrupted:
            return
        log.info("bargein.source", source=source)
        self._cut_pending = True
        # The answer task sees the cancelled sender and finishes: it drains
        # the rest of the model's answer for a resume, then `_end_playback`
        # records what was heard. Nothing else to do here except decide
        # whether to wait for a transcript.
        self._partial_since_cut = False
        self._cancel_resume_watchdog()
        if source == "voice":
            self._resume_watchdog = asyncio.create_task(self._watch_for_resume())

    async def _watch_for_resume(self) -> None:
        """After a voice cut with no words behind it, pick up again.

        The gate said the caller spoke; if the recogniser then produces
        nothing and the room goes quiet, it was a cough or a "हाँ" the
        recogniser dropped, and the honest thing is to continue rather than
        to sit in silence waiting for a question that is not coming.
        """
        started = time.perf_counter()
        gate = self.voice_gate
        try:
            while time.perf_counter() - started < RESUME_WATCHDOG_MAX_S:
                await asyncio.sleep(0.1)
                if self._partial_since_cut or self._closed:
                    return
                if gate is None:
                    return
                quiet_for = (
                    time.perf_counter() - gate.last_voice_at
                    if gate.last_voice_at is not None
                    else RESUME_AFTER_SILENCE_S
                )
                if not gate.speaking and quiet_for >= RESUME_AFTER_SILENCE_S:
                    await self._resume("silence after a voice cut")
                    return
        except asyncio.CancelledError:
            raise

    def _cancel_resume_watchdog(self) -> None:
        if self._resume_watchdog is not None and not self._resume_watchdog.done():
            self._resume_watchdog.cancel()
        self._resume_watchdog = None

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
            await self._on_end_of_turn(self._transcript)
        else:
            log.debug("pipeline.turn_continues", reason=result.reason)

    # -- speculation ------------------------------------------------------ #

    def _begin_speculation(self, transcript: str) -> None:
        """Start generating on the eager signal (§5.2)."""
        if not transcript.strip() or self._speculation is not None or self._agent_speaking:
            return
        if self._cut_pending and is_backchannel(transcript):
            # "हाँ" over the agent's last words is not going to be answered
            # (see `_commit`), so it is not worth a generation either.
            return
        speculation = _Speculation(transcript=transcript, queue=asyncio.Queue())
        speculation.task = asyncio.create_task(self._speculate(speculation))
        self._speculation = speculation
        log.info("pipeline.speculation_started", words=len(script_words(transcript)))

    async def _speculate(self, speculation: _Speculation) -> None:
        """Generate into the queue; render the first sentence into the cache."""
        first = True
        try:
            async for chunk in self.responder.respond(
                speculation.transcript, language=self._language()
            ):
                await speculation.queue.put(chunk)
                if first and chunk.strip():
                    first = False
                    speculation.prefetch = asyncio.create_task(self._prefetch(chunk))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("pipeline.speculation_failed", error=type(exc).__name__)
            speculation.failed = True
        finally:
            with contextlib.suppress(Exception):
                speculation.queue.put_nowait(None)

    async def _prefetch(self, chunk: str) -> None:
        """Synthesise the opening of a speculative answer ahead of the commit.

        On the ~80% of turns that commit, the synthesiser's first byte -- some
        300 ms -- has already been paid by the time the turn needs it. On the
        rest it is a few characters of synthesis nobody hears, which is the
        cheaper mistake.
        """
        sentences = split_sentences(chunk)
        if not sentences:
            return
        speakable = text_for_speech(sentences[0], language=self._tts_config().language)
        if not speakable:
            return
        try:
            already = await self.cache.get(
                speakable, self._tts_config(), provider=self.stack.tts.provider
            )
            if already:
                return
            audio = await self.stack.tts.synthesise_all(speakable, self._tts_config())
            if audio:
                await self.cache.put(
                    speakable, self._tts_config(), audio, provider=self.stack.tts.provider
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("pipeline.prefetch_failed", error=type(exc).__name__)

    async def _discard_speculation(self, reason: str) -> None:
        """Cancel speculative work and wait for it to actually stop."""
        speculation = self._speculation
        self._speculation = None
        if speculation is None:
            return
        await speculation.cancel()
        self._discard_pending_turn()
        if self._metrics is not None:
            self._metrics.speculative_discarded = True
        log.info("pipeline.speculation_discarded", reason=reason)

    def _discard_pending_turn(self) -> None:
        """Tell the responder its half-answered question was withdrawn."""
        hook = getattr(self.responder, "discard_pending", None)
        if hook is None:
            return
        try:
            hook()
        except Exception as exc:  # pragma: no cover - a hook must never end a call
            log.warning("pipeline.discard_hook_failed", error=type(exc).__name__)

    async def _take_speculation(self, transcript: str, metrics: TurnMetrics) -> _Speculation | None:
        """Use the speculative answer if it was for this exact transcript."""
        speculation = self._speculation
        self._speculation = None
        if speculation is None:
            return None
        if not speculation.matches(transcript) or speculation.failed:
            # The caller said more than the eager signal captured, so the
            # answer would be to the wrong question.
            await speculation.cancel()
            self._discard_pending_turn()
            metrics.speculative_discarded = True
            log.info("pipeline.speculation_discarded", reason="transcript differs")
            return None
        metrics.speculative_hit = True
        log.info("pipeline.speculation_hit", queued=speculation.queue.qsize())
        return speculation

    # -- commit ----------------------------------------------------------- #

    async def _commit(self, transcript: str) -> None:
        """Decide what this turn is, and start speaking it in the background."""
        transcript = transcript.strip()
        self._cancel_resume_watchdog()
        await self._settle_answer()

        metrics = self._metrics or TurnMetrics(turn_index=self._turn_index)
        metrics.mark("committed_at")
        metrics.mark("transcript_at")
        outcome = TurnOutcome(turn_index=self._turn_index, transcript=transcript)
        self.turns.append(outcome)
        self._turn_index += 1
        self._reset_recognition()

        remainder = self._resumable
        self._resumable = []
        cut = self._cut_pending
        self._cut_pending = False
        nod = not transcript or is_backchannel(transcript)
        if cut and nod and not remainder and self._may_resume():
            # The farmer said "हाँ" over the last words of an answer that
            # had nothing left. They were agreeing, not asking; answering a
            # nod is how an agent ends up saying "जी बताइए" to itself.
            log.info("pipeline.nod_after_cut", words=len(script_words(transcript)))
            await self._discard_speculation("nod")
            self._finish_turn(metrics, outcome)
            self._start_silence_watch()
            return
        if remainder and self._may_resume() and nod:
            # "हाँ" while the agent was mid-answer is a listener, not a
            # question. Pick up the rest rather than replying to a nod.
            await self._discard_speculation("resuming an interrupted answer")
            log.info("pipeline.resumed", reason="backchannel", sentences=len(remainder))
            metrics.resumed = True
            outcome.resumed = True
            outcome.response = " ".join(remainder)
            self._answer_task = asyncio.create_task(self._speak_resumed(outcome, metrics))
            return

        if not transcript:
            self._finish_turn(metrics, outcome)
            self._start_silence_watch()
            return

        self._activity("thinking")
        metrics.mark("generation_started_at")
        speculation = await self._take_speculation(transcript, metrics)
        source = (
            speculation.sentences()
            if speculation is not None
            else self.responder.respond(transcript, language=self._language())
        )
        self._answer_task = asyncio.create_task(self._answer(source, metrics, outcome))

    async def _answer(
        self, source: AsyncIterator[str], metrics: TurnMetrics, outcome: TurnOutcome
    ) -> None:
        try:
            await self._stream_answer(source, metrics, outcome)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("pipeline.answer_failed", error=type(exc).__name__)
        finally:
            self._finish_turn(metrics, outcome)
        await self._transfer_if_asked()
        await self._end_call_if_asked()

    async def _speak_resumed(self, outcome: TurnOutcome, metrics: TurnMetrics) -> None:
        try:
            metrics.mark("generation_started_at")
            metrics.mark("first_token_at")
            await self._speak(outcome.response, metrics, outcome)
            if not outcome.interrupted:
                hook = getattr(self.responder, "note_resumed", None)
                if hook is not None:
                    hook(outcome.spoken or outcome.response)
        finally:
            self._finish_turn(metrics, outcome)

    async def _resume(self, reason: str) -> None:
        """Continue an interrupted answer without a farmer turn in front."""
        await self._settle_answer()
        remainder = self._resumable
        self._resumable = []
        if not remainder or not self._may_resume():
            return
        log.info("pipeline.resumed", reason=reason)
        metrics = TurnMetrics(turn_index=self._turn_index)
        metrics.resumed = True
        outcome = TurnOutcome(turn_index=self._turn_index, resumed=True)
        outcome.response = " ".join(remainder)
        self.turns.append(outcome)
        self._turn_index += 1
        self._answer_task = asyncio.create_task(self._speak_resumed(outcome, metrics))

    def _may_resume(self) -> bool:
        return bool(getattr(self.responder, "resumes_after_backchannel", True))

    async def _settle_answer(self) -> None:
        """Wait for the previous answer task; after a cut that is quick."""
        task = self._answer_task
        self._answer_task = None
        if task is None or task.done():
            return
        if self._agent_speaking and not self.sender.cancelled:
            # Should not happen -- a commit while speaking goes through
            # `_on_end_of_turn`, which interrupts first -- but a task left
            # speaking under a new turn would be two voices at once. A
            # sender already cancelled is an answer already cut and still
            # draining the model; that needs no second clear.
            await self._interrupt("new turn")
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    def _reset_recognition(self) -> None:
        self._metrics = None
        self._transcript = ""
        self._turn_audio.clear()

    async def on_dtmf(self, digit: str) -> None:
        """A keypress, for responders that treat one as a turn (§13.2).

        The outbound script offers "press one or say yes" and must treat the
        two identically, so a digit arrives here as a turn the recogniser
        could never have produced. A press while the agent is still talking
        interrupts it first -- the farmer who presses one halfway through the
        question has answered it.
        """
        if not getattr(self.responder, "accepts_dtmf", False):
            return
        await self._interrupt("keypress")
        await self._discard_speculation("keypress")
        self._metrics = TurnMetrics(turn_index=self._turn_index)
        self._metrics.mark("speech_ended_at")
        self._resumable = []
        await self._commit(f"[dtmf {digit}]")

    async def _transfer_if_asked(self) -> None:
        """Join the caller to a person once they have heard it is happening (§12.3-1)."""
        if self.on_transfer is None:
            return
        request = getattr(self.responder, "pending_transfer", None)
        if request is None:
            return
        await self.on_transfer(request)

    async def announce(self, text: str) -> None:
        """Speak a line that answers nothing: a hand-over that could not be made.

        Recorded as a turn of its own so the transcript shows what the caller
        was promised, with no farmer text in front of it.
        """
        metrics = TurnMetrics(turn_index=self._turn_index)
        for mark in ("committed_at", "transcript_at", "generation_started_at", "first_token_at"):
            metrics.mark(mark)
        outcome = TurnOutcome(turn_index=self._turn_index, response=text)
        self.turns.append(outcome)
        self._turn_index += 1
        # The ladder is the caller's silence clock, and it speaks too. Two
        # speakers on one paced sender collided -- the ladder's prompt ended
        # and reset the pacing slot under this line mid-play. The ladder's own
        # prompts come through here as well; those must not cancel the ladder.
        if self._silence_task is not asyncio.current_task():
            self._cancel_silence_watch()
        await self._speak(text, metrics, outcome)
        self._finish_turn(metrics, outcome)

    def play_opening(self, text: str, pcm: bytes, *, protect_disclosure: bool = False) -> None:
        """Speak the greeting through the same sender as every answer.

        Sent this way rather than blasted at the transport so the caller can
        interrupt it -- a returning farmer who already knows the greeting says
        their question over it, and used to be talked over for five seconds.
        Runs as a task, because the socket loop must keep reading frames for
        that interruption to be heard.
        """
        if not pcm:
            return
        outcome = TurnOutcome(turn_index=self._turn_index, response=text)
        self.turns.append(outcome)
        self._turn_index += 1
        self._opening_task = asyncio.create_task(
            self._play_opening(text, pcm, outcome, protect_disclosure=protect_disclosure)
        )

    async def _play_opening(
        self, text: str, pcm: bytes, outcome: TurnOutcome, *, protect_disclosure: bool
    ) -> None:
        metrics = TurnMetrics(turn_index=outcome.turn_index)
        if protect_disclosure:
            self.barge_in.begin_disclosure()
        try:
            async with self._speaking:
                self._begin_playback()
                try:
                    metrics.mark("first_audio_at")
                    speakable = text_for_speech(text, language=self._tts_config().language)
                    if not await self.sender.play(speakable or text, pcm):
                        outcome.interrupted = True
                    metrics.mark("audio_sent_at")
                finally:
                    self._end_playback(outcome)
        finally:
            if protect_disclosure:
                self.barge_in.end_disclosure()
            self._finish_turn(metrics, outcome)

    async def _end_call_if_asked(self) -> None:
        """The responder said its last line and it has been played: hang up.

        Reached from the answer task after `_stream_answer` returns, which
        is after the sender has paced out the last frame of the goodbye --
        the farmer hears all of "धन्यवाद, नमस्ते" and *then* the line ends.
        """
        if not getattr(self.responder, "call_over", False):
            return
        self._ending = True
        self._cancel_silence_watch()
        if self.on_call_over is None:
            return
        log.info("pipeline.call_over", by="responder")
        await self.on_call_over()

    # -- the silence ladder (§11.4) ---------------------------------------- #

    def _listening(self) -> bool:
        """Nothing is being said and nothing is being prepared."""
        if self._agent_speaking or self._ending:
            return False
        task = self._answer_task
        return task is None or task.done()

    def _start_silence_watch(self) -> None:
        """Start the clock on the caller's silence, if it is not running."""
        if self.silence is None or self._closed or self._ending:
            return
        if self._silence_task is not None and not self._silence_task.done():
            return
        self._silence_task = asyncio.create_task(self._watch_silence())

    def _cancel_silence_watch(self) -> None:
        """The caller did something: the ladder starts over next time."""
        task = self._silence_task
        self._silence_task = None
        self._silence_stage = 0
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _watch_silence(self) -> None:
        """Prompt, prompt again, then say goodbye and hang up.

        One task for the whole ladder. Its own prompts do not reset it --
        the agent talking is not the caller talking -- and it runs on to the
        next rung after each one. Anything the caller does cancels it.
        """
        assert self.silence is not None
        stages = self.silence.stages()
        elapsed = 0.0
        try:
            while self._silence_stage < len(stages):
                at, phrase = stages[self._silence_stage]
                wait = at - elapsed
                if wait > 0:
                    await asyncio.sleep(wait)
                elapsed = at
                if self._agent_speaking:
                    # Somebody is talking after all (a hand-over line, a
                    # resumed answer). The clock restarts when they finish.
                    return
                self._silence_stage += 1
                if phrase is not None:
                    log.info("pipeline.silence_prompt", stage=self._silence_stage, after_s=at)
                    await self.announce(phrase)
                    continue
                await self._close_for_silence(at)
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - a watchdog must never end a call
            log.warning("pipeline.silence_watch_failed", error=type(exc).__name__)

    async def _close_for_silence(self, after_s: float) -> None:
        """Nobody is there: say goodbye and end the call (§11.4)."""
        if self._ending or self._closed:
            return
        self._ending = True
        self.call_outcome = CallOutcome.ABANDONED_SILENCE
        log.info("pipeline.call_over", by="silence", after_s=after_s)
        closing = getattr(self.responder, "closing_line", None) or CLOSING_LINE_HI
        await self.announce(str(closing))
        if self.on_call_over is not None:
            await self.on_call_over()

    def _activity(self, state: str) -> None:
        if self.on_activity is None:
            return
        try:
            self.on_activity(state)
        except Exception as exc:  # pragma: no cover - a hook must never end a call
            log.warning("pipeline.activity_hook_failed", error=type(exc).__name__)

    # -- speaking --------------------------------------------------------- #

    async def _stream_answer(
        self, source: AsyncIterator[str], metrics: TurnMetrics, outcome: TurnOutcome
    ) -> None:
        """Speak each sentence as the model finishes it.

        This is where most of §7's budget is won or lost. Collecting the whole
        answer before synthesising anything serialises three waits that have no
        reason to be sequential -- the model finishing, the synthesiser
        finishing, and the audio playing -- and the farmer hears silence for the
        sum of them. Releasing a sentence the moment it is complete overlaps
        generation with synthesis and playback, and releasing the *first
        clause* even earlier means the pause before the agent speaks is the
        cost of a few words rather than of a sentence.

        The model is read by its own task. Reading it from the speaking loop
        meant the model was throttled to playback: sentence two was not even
        requested until sentence one had finished playing, so every sentence
        boundary paid the synthesiser's first byte in silence. Now every
        sentence after the first is rendered the moment it exists, while the
        one before it is still playing.

        After an interruption the producer is still read, briefly, so the part
        the farmer did not hear is known and can be resumed.
        """
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        producer = asyncio.create_task(self._produce(source, queue, metrics))
        spoken: list[str] = []
        unspoken: list[str] = []
        self._begin_playback()
        cut_at: float | None = None

        try:
            while True:
                if cut_at is None:
                    sentence = await queue.get()
                else:
                    budget = cut_at + REMAINDER_DRAIN_S - time.perf_counter()
                    if budget <= 0:
                        break
                    try:
                        sentence = await asyncio.wait_for(queue.get(), timeout=budget)
                    except TimeoutError:
                        break
                if sentence is None:
                    break
                if cut_at is not None:
                    unspoken.append(sentence)
                    continue
                if await self._speak_sentence(sentence, metrics, outcome):
                    spoken.append(sentence)
                    continue
                unspoken.append(sentence)
                cut_at = time.perf_counter()
            if cut_at is None:
                metrics.mark("audio_sent_at")
        finally:
            if not producer.done():
                producer.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await producer
            self._cancel_ahead()
            # `outcome.response` is what the agent *intended* to say: the
            # sentences spoken plus the ones drained after the cut. What the
            # caller *heard* is `outcome.spoken`, which the tracker computes.
            outcome.response = " ".join([*spoken, *unspoken]).strip()
            self._end_playback(outcome)

    async def _produce(
        self, source: AsyncIterator[str], queue: asyncio.Queue[str | None], metrics: TurnMetrics
    ) -> None:
        """Read the model into sentences; render every one after the first."""
        buffer = SentenceBuffer(first_clause_words=FIRST_CLAUSE_WORDS)
        count = 0

        def release(sentence: str) -> None:
            nonlocal count
            count += 1
            if count > 1:
                self._render_ahead(sentence)
            queue.put_nowait(sentence)

        try:
            async for fragment in source:
                if metrics.first_token_at is None:
                    metrics.mark("first_token_at")
                for sentence in buffer.add(fragment):
                    release(sentence)
            # The tail: an answer whose last sentence carries no terminator is
            # still an answer, and dropping it would truncate mid-thought.
            for sentence in buffer.flush():
                release(sentence)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("pipeline.generation_failed", error=type(exc).__name__)
        finally:
            queue.put_nowait(None)
            with contextlib.suppress(Exception):
                aclose = getattr(source, "aclose", None)
                if aclose is not None:
                    await aclose()

    def _begin_playback(self) -> None:
        """Open a speaking turn. Shared by the streaming and complete paths."""
        self._agent_speaking = True
        self.sender.resume()
        self.sender.tracker.reset()
        self._activity("speaking")

    def _end_playback(self, outcome: TurnOutcome) -> None:
        self._agent_speaking = False
        self._start_silence_watch()
        outcome.spoken = self.sender.tracker.spoken_text()
        if self.sender.cancelled:
            outcome.interrupted = True
        if outcome.interrupted:
            self._resumable = self._remainder(outcome)
            hook = getattr(self.responder, "note_interruption", None)
            if hook is not None and not outcome.resumed:
                try:
                    hook(outcome.spoken)
                except Exception as exc:  # pragma: no cover
                    log.warning("pipeline.interruption_hook_failed", error=type(exc).__name__)
        self._activity("listening")

    def _remainder(self, outcome: TurnOutcome) -> list[str]:
        """The sentences of an interrupted answer the farmer did not hear.

        Compared on the synthesised form, because the tracker records what
        went to the synthesiser -- "बारह सौ पचास", not "1250".
        """
        sentences = split_sentences(outcome.response)
        heard = script_words(outcome.spoken)
        if not heard:
            return sentences
        position = 0
        for index, sentence in enumerate(sentences):
            spoken_form = text_for_speech(sentence, language=self._tts_config().language)
            tokens = script_words(spoken_form or sentence)
            if not tokens:
                continue
            matched = 0
            for token in tokens:
                if position + matched < len(heard) and heard[position + matched] == token:
                    matched += 1
                else:
                    break
            if matched == len(tokens):
                position += matched
                continue
            if matched / len(tokens) >= HEARD_ENOUGH:
                return sentences[index + 1 :]
            return sentences[index:]
        return []

    def render_later(self, *lines: str) -> None:
        """Render fixed lines into the cache in the background.

        For the goodbye and the silence prompts: known at build time, said
        once if at all, and wanted instantly when they are. Kept separate
        from `_render_ahead` so that ending an answer does not cancel them.
        """
        for line in lines:
            for sentence in split_sentences(line):
                speakable = text_for_speech(sentence, language=self._tts_config().language)
                if speakable:
                    task = asyncio.create_task(self._render(speakable))
                    self._background.add(task)
                    task.add_done_callback(self._background.discard)

    def _render_ahead(self, sentence: str) -> None:
        """Start synthesising a sentence that will be spoken after the current one.

        The model produces sentence two while sentence one is being spoken,
        and sentence one takes seconds to play. Rendering two in that time
        turns the synthesiser's first byte from a pause the farmer hears into
        one nobody does. Bounded by the answer: `_cancel_ahead` stops anything
        still in flight when the turn ends or is interrupted.
        """
        speakable = text_for_speech(sentence, language=self._tts_config().language)
        if not speakable or speakable in self._ahead:
            return
        self._ahead[speakable] = asyncio.create_task(self._render(speakable))

    async def _render(self, speakable: str) -> bytes | None:
        try:
            cached = await self.cache.get(
                speakable, self._tts_config(), provider=self.stack.tts.provider
            )
            if cached is not None:
                return cached
            audio = await self.stack.tts.synthesise_all(speakable, self._tts_config())
            if audio:
                await self.cache.put(
                    speakable, self._tts_config(), audio, provider=self.stack.tts.provider
                )
            return audio or None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("pipeline.render_ahead_failed", error=type(exc).__name__)
            return None

    async def _rendered_ahead(self, speakable: str) -> bytes | None:
        """The audio of a sentence rendered ahead, waiting for it if need be."""
        task = self._ahead.pop(speakable, None)
        if task is None:
            return None
        with contextlib.suppress(asyncio.CancelledError, Exception):
            return await task
        return None

    def _cancel_ahead(self) -> None:
        for task in self._ahead.values():
            if not task.done():
                task.cancel()
        self._ahead.clear()

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
        speakable = text_for_speech(sentence, language=self._tts_config().language)
        if not speakable:
            return True

        async with self._speaking:
            return await self._synthesise_and_play(speakable, metrics, outcome)

    async def _synthesise_and_play(
        self, speakable: str, metrics: TurnMetrics, outcome: TurnOutcome
    ) -> bool:
        """Render one sentence and stream it out. Called holding `_speaking`."""

        if metrics.synthesis_started_at is None:
            metrics.mark("synthesis_started_at")

        cached = await self._rendered_ahead(speakable)
        if cached is None:
            cached = await self.cache.get(
                speakable, self._tts_config(), provider=self.stack.tts.provider
            )
        if cached is not None:
            metrics.from_cache = metrics.from_cache or False
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
        async for chunk in self.stack.tts.synthesise(speakable, self._tts_config()):
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
                self._tts_config(),
                bytes(collected),
                provider=self.stack.tts.provider,
            )
        return True

    async def _speak(self, response: str, metrics: TurnMetrics, outcome: TurnOutcome) -> None:
        """Speak an answer that is already complete.

        A resumed remainder and anything replayed from cache arrive whole, so
        there is nothing to stream -- but they still go out a sentence at a
        time so barge-in lands on a natural boundary.
        """
        if not response.strip():
            return

        self._begin_playback()
        try:
            sentences = split_sentences(response)
            for later in sentences[1:]:
                self._render_ahead(later)
            for sentence in sentences:
                if not await self._speak_sentence(sentence, metrics, outcome):
                    break
            metrics.mark("audio_sent_at")
        finally:
            self._cancel_ahead()
            self._end_playback(outcome)

    async def _synthesise(self, text: str) -> bytes:
        """Cached audio where possible, synthesis otherwise (§5.3, §8)."""
        cached = await self.cache.get(text, self._tts_config(), provider=self.stack.tts.provider)
        if cached is not None:
            if self._metrics is not None:
                self._metrics.from_cache = True
            return cached

        audio = await self.stack.tts.synthesise_all(text, self._tts_config())
        if audio:
            await self.cache.put(text, self._tts_config(), audio, provider=self.stack.tts.provider)
        return audio

    def _finish_turn(self, metrics: TurnMetrics, outcome: TurnOutcome) -> None:
        if outcome.metrics is not None:
            return
        outcome.metrics = metrics
        self.latency.add(metrics)

        breaches = metrics.budget_breaches(self.defaults.latency_budget_ms)
        if breaches:
            # §7 gates on the distribution, not on one turn, so this is a
            # signal rather than a failure -- but an unlogged breach is a
            # regression nobody notices until a farmer does.
            log.info("pipeline.budget_breach", turn=metrics.turn_index, breaches=breaches)

        if self.on_turn is not None:
            try:
                self.on_turn(outcome)
            except Exception as exc:  # pragma: no cover - a hook must never end a call
                log.warning("pipeline.turn_hook_failed", error=type(exc).__name__)

    async def wait_idle(self, timeout_s: float = 30.0) -> None:
        """Wait until nothing is being spoken. For tests and the simulator."""
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            tasks = [
                t
                for t in (self._answer_task, self._opening_task, self._resume_watchdog)
                if t is not None
            ]
            if self._agent_speaking and self._silence_task is not None:
                tasks.append(self._silence_task)
            if not any(not t.done() for t in tasks):
                return
            await asyncio.sleep(0.005)

    # -- shutdown --------------------------------------------------------- #

    async def close(self) -> None:
        self._closed = True
        self._cancel_resume_watchdog()
        self._cancel_silence_watch()
        self._cancel_ahead()
        await self._discard_speculation("call ended")
        for task in (self._answer_task, self._opening_task, self._silence_task):
            if task is not None and not task.done():
                self.sender.cancel()
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._answer_task = None
        self._opening_task = None
        await self.stack.stt.close()
        await self.stack.tts.close()
        await self.stack.turn_detector.close()


async def _noop() -> None:
    return None
