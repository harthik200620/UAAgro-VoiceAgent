"""Soniox realtime recognition with semantic endpointing (§5.1, §5.2, §5.5).

Soniox replaces the split that §5.1 was forced into. Deepgram Flux fuses
recognition and end-of-turn but covers ten languages, so Marathi, Malayalam and
the rest were routed to Sarvam behind a *local* turn detector -- a second model,
a second failure mode, and a measurably worse turn feel. Soniox covers all of
them in one engine and decides the turn itself, so every language the helpline
serves gets vendor-fused endpointing from the same code path.

It also takes 8 kHz linear16 directly, which keeps §23-8 satisfied on the way
in: the telephony frames go to the recogniser as they arrive, unconverted.

Protocol, verified 2 September 2026 against soniox.com/docs:

==================  ===========================================================
URL                 ``wss://stt-rt.soniox.com/transcribe-websocket``
Auth                ``api_key`` inside the opening JSON config message
Config              ``model``, ``audio_format`` (``pcm_s16le``),
                    ``sample_rate``, ``num_channels``, ``language_hints``,
                    ``context``, ``enable_endpoint_detection``,
                    ``endpoint_latency_adjustment_level`` (0-3),
                    ``endpoint_sensitivity`` (-1.0..1.0),
                    ``max_endpoint_delay_ms`` (500..3000)
Audio               raw binary WebSocket frames
Responses           ``{"tokens": [{text, is_final, confidence, language,
                    start_ms, end_ms}], "final_audio_proc_ms": …,
                    "total_audio_proc_ms": …}``
End of turn         a token whose text is ``<end>``, always final
Force finalise      ``{"type": "finalize"}``
Close               an empty frame; the server answers ``{"finished": true}``
==================  ===========================================================

**The one thing Soniox does not give us is §5.2's eager end-of-turn.** Flux
publishes a probability that the turn is over and lets us start generating at
0.45 while still listening; Soniox publishes only the commit. Losing that would
cost the ~300 ms overlap on the ~80% of turns that do commit, which is most of
the margin in §7's 1,200 ms budget.

What it publishes instead is *text that stops changing*. A stretch where
no new word has arrived is the same evidence Flux turns into a probability,
and it comes in two strengths: every token already final (Soniox finalises
after a pause, so this is close to a real endpoint) and merely stable
provisional text (a pause between words as often as between sentences). So
the eager signal here is a timer armed when the text last changed --
:attr:`SttConfig.eager_after_final_ms` in the first case, the longer
:attr:`SttConfig.eager_after_stable_ms` in the second -- a heuristic,
stated as one, not a vendor signal wearing a vendor's name. It is safe
because it is *only* a speculation trigger: when the caller carries on, the
adapter emits ``TURN_RESUMED`` and §5.2 requires the speculative generation
be cancelled. A false eager costs tokens; it never speaks.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

import structlog
import websockets

from uaagro_domain import fastjson
from uaagro_domain.errors import VendorTimeoutError, VendorUnavailableError
from uaagro_domain.settings import Settings

from ...text.script import words as script_words
from ..resilience import breaker_for
from .base import SttConfig, SttEvent, SttEventType, STTService

log = structlog.get_logger(__name__)


async def _noop() -> None:
    """Nothing. Lets the breaker act purely as an admission check."""
    return None

DEFAULT_WS_URL = "wss://stt-rt.soniox.com/transcribe-websocket"

CONNECT_TIMEOUT_S = 5.0

#: See :class:`~voice_worker.adapters.stt.deepgram_flux.DeepgramFluxSTT`; the
#: reasoning for a bounded queue is the same, and an ``END_OF_TURN`` must never
#: be the event that gets dropped.
EVENT_QUEUE_MAX = 256

#: The marker token Soniox emits when it decides the speaker has finished.
END_MARKER = "<end>"
#: Emitted when a *finalize* request drains the stream. Carries no text.
FIN_MARKER = "<fin>"

#: §5.5 vocabulary boosting. The ceiling here is the vendor's context field
#: rather than a URL -- these travel in the JSON config, so there is room for
#: the whole catalogue where Flux had room for 200 terms. Both caps are kept
#: because an unbounded context is a slow handshake on every call.
MAX_KEYTERMS = 400
MAX_CONTEXT_CHARS = 6000


class SonioxSTT(STTService):
    """Streaming recognition with vendor-side semantic endpointing."""

    provider = "soniox"
    emits_turn_events = True

    def __init__(self, settings: Settings, *, url: str | None = None) -> None:
        self._api_key = settings.require(
            "soniox_api_key", needed_for="Soniox realtime speech recognition"
        )
        self._model = settings.soniox_stt_model
        self._url = url or settings.soniox_ws_url or DEFAULT_WS_URL
        self._socket: websockets.ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[SttEvent | None] = asyncio.Queue(maxsize=EVENT_QUEUE_MAX)
        self._overflow: set[asyncio.Task[None]] = set()

        # -- per-turn state ------------------------------------------------ #
        self._turn_started_at: float | None = None
        self._final_text = ""
        self._pending_text = ""
        self._confidences: list[float] = []
        self._languages: Counter[str] = Counter()
        self._speech_started = False
        self._eager_sent = False
        self._eager_text = ""
        self._last_heard = ""
        self._eager_handle: asyncio.TimerHandle | None = None
        self._eager_delay_s = 0.16
        self._eager_stable_delay_s = 0.3

    # -- lifecycle -------------------------------------------------------- #

    async def start(self, config: SttConfig) -> None:
        self._eager_delay_s = max(config.eager_after_final_ms, 0) / 1000
        self._eager_stable_delay_s = max(config.eager_after_stable_ms, 0) / 1000
        # There is no degraded mode for "cannot hear the caller" -- a dead
        # recogniser fails the call either way (§11.4). What this buys is
        # failing in microseconds instead of after a 5 s connect timeout, so
        # the caller reaches a person while they are still on the line.
        breaker = breaker_for(self.provider, "stt")
        await breaker.call(_noop)
        try:
            self._socket = await asyncio.wait_for(
                websockets.connect(self._url, max_size=None),
                timeout=CONNECT_TIMEOUT_S,
            )
        except TimeoutError as exc:
            timed_out = VendorTimeoutError(
                vendor=self.provider, service="stt", timeout_ms=int(CONNECT_TIMEOUT_S * 1000)
            )
            breaker.record_failure(timed_out)
            raise timed_out from exc
        except Exception as exc:
            failure = VendorUnavailableError(
                vendor=self.provider, service="stt", detail=str(exc)[:200]
            )
            breaker.record_failure(failure)
            raise failure from exc

        try:
            await self._socket.send(fastjson.dumps(self._config_message(config)))
        except Exception as exc:
            await self.close()
            raise VendorUnavailableError(
                vendor=self.provider, service="stt", detail=str(exc)[:200]
            ) from exc

        # Without this the failure count only ever rises: three unrelated
        # blips hours apart would open the circuit on a vendor that has been
        # answering perfectly in between.
        breaker.record_success()

        self._reader = asyncio.create_task(self._read_loop())
        log.info(
            "stt.started",
            provider=self.provider,
            model=self._model,
            language=config.language,
            endpoint_level=config.endpoint_latency_adjustment_level,
            max_endpoint_delay_ms=self._endpoint_delay(config),
        )

    def _config_message(self, config: SttConfig) -> dict[str, Any]:
        """The opening message. The API key travels in it, never in a log line."""
        message: dict[str, Any] = {
            "api_key": self._api_key,
            "model": config.model or self._model,
            # §23-8: the telephony leg is already 8 kHz linear16, so it is
            # handed over untouched rather than resampled to suit a vendor.
            "audio_format": "pcm_s16le",
            "sample_rate": config.sample_rate,
            "num_channels": 1,
            "enable_endpoint_detection": True,
            "endpoint_latency_adjustment_level": config.endpoint_latency_adjustment_level,
            "endpoint_sensitivity": config.endpoint_sensitivity,
            "max_endpoint_delay_ms": self._endpoint_delay(config),
            # §11.1 follows a caller who switches language mid-call, which
            # needs the recogniser to say which language it heard.
            "enable_language_identification": True,
        }
        if config.language_hints:
            message["language_hints"] = list(config.language_hints)
        terms = self._keyterms(config)
        if terms:
            message["context"] = {"terms": list(terms)}
        return message

    @staticmethod
    def _endpoint_delay(config: SttConfig) -> int:
        """The backstop, clamped to the range the vendor accepts.

        §5.2 raises it for callers observed to pause: rural callers stop
        mid-sentence to think far more than the model's training distribution
        expects, and cutting them off is the commonest way an Indian voice
        agent feels rude. The vendor ceiling is 3000 ms, so a slow speaker gets
        the ceiling rather than §5.2's 8 s -- the ceiling is the vendor's, and
        silently sending 8000 would be rejected at connect time.
        """
        wanted = 3000 if config.slow_speaker else config.max_endpoint_delay_ms
        return max(500, min(3000, wanted))

    def _keyterms(self, config: SttConfig) -> tuple[str, ...]:
        """The catalogue terms to bias the decode toward, longest first.

        Longest first for the reason §5.5 gives: a multi-word product name is
        both the hardest to recognise and the most useful to get right.
        "एनपीके बारह बत्तीस सोलह" heard as four unrelated numbers is a failed
        turn; a single mis-heard "यूरिया" is recovered by the post-ASR fuzzy
        match against the full lexicon.
        """
        ordered = sorted(dict.fromkeys(config.keyterms), key=len, reverse=True)
        kept: list[str] = []
        budget = MAX_CONTEXT_CHARS
        for term in ordered[:MAX_KEYTERMS]:
            if len(term) > budget:
                break
            kept.append(term)
            budget -= len(term)
        if len(kept) < len(ordered):
            # Logged rather than silently truncated: an operator who added
            # eighty products and saw no accuracy change deserves to know the
            # list was cut.
            log.info(
                "stt.keyterms_truncated",
                provider=self.provider,
                sent=len(kept),
                available=len(ordered),
            )
        return tuple(kept)

    async def send_audio(self, pcm: bytes) -> None:
        """Send raw 8 kHz linear16 as a binary frame."""
        if self._socket is None:
            raise VendorUnavailableError(
                vendor=self.provider, service="stt", detail="stream is not open"
            )
        if self._turn_started_at is None:
            self._turn_started_at = time.perf_counter()
        try:
            await self._socket.send(pcm)
        except websockets.ConnectionClosed as exc:
            raise VendorUnavailableError(
                vendor=self.provider, service="stt", detail="stream closed while sending"
            ) from exc

    async def events(self) -> AsyncIterator[SttEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event

    async def finalise(self) -> None:
        """Force every pending token final.

        Used when the pipeline knows the turn is over before the recogniser
        does -- a DTMF keypress, or a transfer triggered mid-utterance.
        """
        if self._socket is None:
            return
        with contextlib.suppress(Exception):
            await self._socket.send(fastjson.dumps({"type": "finalize"}))

    async def close(self) -> None:
        self._cancel_eager()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        if self._socket is not None:
            # An empty frame is Soniox's end-of-stream. Best effort: on a
            # dropped call the socket is already gone and the close below is
            # what actually matters.
            with contextlib.suppress(Exception):
                await self._socket.send(b"")
            with contextlib.suppress(Exception):
                await self._socket.close()
            self._socket = None
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)

    # -- receive ---------------------------------------------------------- #

    async def _read_loop(self) -> None:
        assert self._socket is not None
        try:
            async for raw in self._socket:
                for event in self._handle(raw):
                    self._emit(event)
        except asyncio.CancelledError:
            raise
        except websockets.ConnectionClosed:
            log.info("stt.stream_closed", provider=self.provider)
        except Exception as exc:
            log.warning("stt.read_failed", provider=self.provider, error=type(exc).__name__)
            self._emit(SttEvent(type=SttEventType.ERROR, detail=type(exc).__name__))
        finally:
            self._cancel_eager()
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(None)

    def _emit(self, event: SttEvent) -> None:
        """Queue an event from the reader or from the eager timer.

        Synchronous because :meth:`_fire_eager` is a plain loop callback and
        cannot await. A full queue means a stalled consumer, and the event that
        would be lost is an ``END_OF_TURN`` -- which strands the call -- so
        overflow is handed to the loop rather than dropped.
        """
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            log.error("stt.event_queue_full", provider=self.provider)
            task = asyncio.get_running_loop().create_task(self._queue.put(event))
            # Held so the loop's weak reference cannot collect it mid-flight.
            self._overflow.add(task)
            task.add_done_callback(self._overflow.discard)

    def _handle(self, raw: str | bytes) -> list[SttEvent]:
        """Fold one server message into the turn state and return what changed."""
        try:
            payload = fastjson.loads(raw)
        except fastjson.JsonError:
            return []
        if not isinstance(payload, dict):
            return []

        if payload.get("error_code") is not None:
            return [
                SttEvent(
                    type=SttEventType.ERROR,
                    detail=str(payload.get("error_message", ""))[:200],
                    raw=payload,
                )
            ]

        tokens = payload.get("tokens")
        if not isinstance(tokens, list):
            return []

        events: list[SttEvent] = []
        ended = False
        new_final = ""
        pending = ""

        for token in tokens:
            if not isinstance(token, dict):
                continue
            text = str(token.get("text", ""))
            if text == FIN_MARKER:
                continue
            if text == END_MARKER:
                ended = True
                continue
            if token.get("is_final"):
                new_final += text
                confidence = token.get("confidence")
                if isinstance(confidence, int | float):
                    self._confidences.append(float(confidence))
                language = token.get("language")
                if isinstance(language, str) and language:
                    self._languages[language] += 1
            else:
                pending += text

        if new_final:
            self._final_text += new_final
        self._pending_text = pending

        heard = (self._final_text + self._pending_text).strip()
        if heard and not self._speech_started:
            self._speech_started = True
            events.append(SttEvent(type=SttEventType.SPEECH_STARTED))

        if ended:
            self._cancel_eager()
            events.append(self._end_of_turn_event(payload))
            self._reset_turn()
            return events

        if self._eager_sent and script_words(heard) != script_words(self._eager_text):
            # §5.2: the caller carried on. Whatever was generated on the eager
            # signal must be cancelled -- an orphaned generation that still
            # speaks is a severe bug, so this is surfaced as its own event
            # rather than folded into a transcript update. The comparison
            # is on the words: a token flipping from provisional to final
            # changes nothing the model was asked about.
            self._eager_sent = False
            events.append(SttEvent(type=SttEventType.TURN_RESUMED, text=heard, raw=payload))

        if heard:
            events.append(
                SttEvent(
                    type=SttEventType.PARTIAL,
                    text=heard,
                    language=self._dominant_language(),
                    raw=payload,
                )
            )

        # The timer runs from the last time the words changed. Everything
        # final is the stronger signal and gets the short delay; provisional
        # text that has merely stopped growing gets the longer one, because
        # a pause between words looks exactly like this for a moment.
        changed = heard != self._last_heard or self._eager_handle is None
        if heard and not self._eager_sent and changed:
            self._arm_eager(self._eager_stable_delay_s if pending else self._eager_delay_s)
        self._last_heard = heard

        return events

    def _end_of_turn_event(self, payload: dict[str, Any]) -> SttEvent:
        latency_ms: float | None = None
        if self._turn_started_at is not None:
            latency_ms = (time.perf_counter() - self._turn_started_at) * 1000
        return SttEvent(
            type=SttEventType.END_OF_TURN,
            text=(self._final_text + self._pending_text).strip(),
            confidence=self._mean_confidence(),
            language=self._dominant_language(),
            latency_ms=latency_ms,
            detail="endpoint",
            raw=payload,
        )

    def _mean_confidence(self) -> float | None:
        """Mean token confidence for the turn.

        §11.4 escalates on a rolling mean below 0.55, so this has to be *how
        well the words were heard*. The ``<end>`` token's own confidence is
        excluded above: it scores the turn boundary, not the speech, and
        including it would make a decisive endpoint look like clear audio.
        """
        if not self._confidences:
            return None
        return sum(self._confidences) / len(self._confidences)

    def _dominant_language(self) -> str | None:
        if not self._languages:
            return None
        return self._languages.most_common(1)[0][0]

    def _reset_turn(self) -> None:
        self._final_text = ""
        self._pending_text = ""
        self._confidences.clear()
        self._languages.clear()
        self._speech_started = False
        self._eager_sent = False
        self._eager_text = ""
        self._last_heard = ""
        self._turn_started_at = None

    # -- the eager heuristic ---------------------------------------------- #

    def _arm_eager(self, delay_s: float) -> None:
        """Start the speculation timer, replacing any timer already running."""
        self._cancel_eager()
        if delay_s <= 0:
            self._fire_eager()
            return
        loop = asyncio.get_running_loop()
        self._eager_handle = loop.call_later(delay_s, self._fire_eager)

    def _cancel_eager(self) -> None:
        if self._eager_handle is not None:
            self._eager_handle.cancel()
            self._eager_handle = None

    def _fire_eager(self) -> None:
        """The recogniser has been quiet with everything final. Speculate.

        A plain timer callback rather than a task: it runs on the loop between
        socket reads, so it cannot interleave with :meth:`_handle` and cannot
        be left orphaned when the stream closes.
        """
        self._eager_handle = None
        if self._eager_sent:
            return
        text = (self._final_text + self._pending_text).strip()
        if not text:
            return
        self._eager_sent = True
        self._eager_text = text
        self._emit(
            SttEvent(
                type=SttEventType.EAGER_END_OF_TURN,
                text=text,
                confidence=self._mean_confidence(),
                language=self._dominant_language(),
                detail="stable_final" if not self._pending_text else "stable_text",
            )
        )
