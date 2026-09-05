"""Deepgram Flux recognition with fused end-of-turn (§5.1, §5.2).

Flux is chosen for the Hindi and English path because it decides turn-taking
*and* transcribes in one pass, and because its eager end-of-turn signal is what
lets the LLM start while the caller is still finishing (§5.2). It covers ten
languages -- English, Spanish, French, German, Hindi, Russian, Portuguese,
Japanese, Italian, Dutch -- and **Marathi and Malayalam are not among them**
(re-verified 31 August 2026), which is why §5.1 routes those elsewhere.

Protocol, verified 31 August 2026:

==================  ===========================================================
URL                 ``wss://api.deepgram.com/v2/listen``
Auth                ``Authorization: Token <key>``
Parameters          ``model``, ``encoding``, ``sample_rate``, ``eot_threshold``,
                    ``eager_eot_threshold``, ``eot_timeout_ms``,
                    ``language_hint`` (repeatable)
Sample rates        8000 is supported -- so the telephony leg needs no
                    resampling on the way in either
Events              ``Update``, ``StartOfTurn``, ``EagerEndOfTurn``,
                    ``TurnResumed``, ``EndOfTurn``
Common fields       ``event``, ``turn_index``, ``transcript``,
                    ``end_of_turn_confidence``, ``words[]``
EndOfTurn extra     ``trigger``: ``model`` | ``manual`` | ``timeout``
==================  ===========================================================

The eager path is the reason this adapter exists, and it carries an obligation:
on ``TurnResumed`` the speculative generation **must** actually be cancelled.
An orphaned generation that still speaks is a severe bug (§5.2), so the event is
surfaced explicitly rather than folded into a transcript update.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from urllib.parse import urlencode

import structlog
import websockets

from uaagro_domain import fastjson
from uaagro_domain.errors import VendorTimeoutError, VendorUnavailableError
from uaagro_domain.settings import Settings

from .base import SttConfig, SttEvent, SttEventType, STTService

log = structlog.get_logger(__name__)

DEFAULT_WS_URL = "wss://api.deepgram.com/v2/listen"

CONNECT_TIMEOUT_S = 5.0

#: Bounded so a stalled consumer cannot grow memory without limit during a
#: seasonal peak. Dropping the oldest event is wrong -- an EndOfTurn must not be
#: lost -- so the queue is generous and a full queue is logged as a fault.
EVENT_QUEUE_MAX = 256

_EVENT_MAP: dict[str, SttEventType] = {
    "StartOfTurn": SttEventType.SPEECH_STARTED,
    "Update": SttEventType.PARTIAL,
    "EagerEndOfTurn": SttEventType.EAGER_END_OF_TURN,
    "TurnResumed": SttEventType.TURN_RESUMED,
    "EndOfTurn": SttEventType.END_OF_TURN,
}


class DeepgramFluxSTT(STTService):
    """Streaming recognition with vendor-side turn detection."""

    provider = "deepgram"
    emits_turn_events = True

    def __init__(self, settings: Settings, *, url: str = DEFAULT_WS_URL) -> None:
        self._api_key = settings.require(
            "deepgram_api_key", needed_for="Deepgram Flux speech recognition"
        )
        self._url = url
        self._socket: websockets.ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[SttEvent | None] = asyncio.Queue(maxsize=EVENT_QUEUE_MAX)
        self._turn_started_at: float | None = None

    #: How many keyterms to send.
    #:
    #: §5.5 wants the whole ~500-term catalogue, and the limit here is the URL
    #: rather than the vendor: these are query parameters, Devanagari costs
    #: ~9 bytes a character once percent-encoded, and a request line has to fit
    #: in the server's header buffer. 200 terms of realistic length lands
    #: around 4 KB, which is comfortable; the whole catalogue is not.
    #:
    #: The rest is not lost. §5.5 pairs boosting with a post-ASR fuzzy match
    #: against the full lexicon, which is what catches everything below the cut.
    MAX_KEYTERMS = 200

    def _keyterms(self, config: SttConfig) -> tuple[str, ...]:
        """The terms to bias the decode toward, longest first.

        Longest first because a multi-word product name is both the hardest to
        recognise and the most useful to get right: "एनपीके बारह बत्तीस सोलह"
        decoded as four unrelated numbers is a failed turn, while a single
        mis-heard "यूरिया" is recoverable by the fuzzy match.
        """
        ordered = sorted(dict.fromkeys(config.keyterms), key=len, reverse=True)
        if len(ordered) > self.MAX_KEYTERMS:
            # Logged rather than silently truncated: an operator who added
            # eighty products and saw no accuracy change deserves to know the
            # list was cut.
            log.info(
                "stt.keyterms_truncated",
                sent=self.MAX_KEYTERMS,
                available=len(ordered),
            )
        return tuple(ordered[: self.MAX_KEYTERMS])

    # -- lifecycle -------------------------------------------------------- #

    async def start(self, config: SttConfig) -> None:
        params: list[tuple[str, str]] = [
            ("model", config.model or "flux-general-multi"),
            ("encoding", "linear16"),
            ("sample_rate", str(config.sample_rate)),
            ("eot_threshold", f"{config.eot_threshold}"),
            ("eager_eot_threshold", f"{config.eager_eot_threshold}"),
            ("eot_timeout_ms", str(self._eot_timeout(config))),
        ]
        # language_hint repeats rather than taking a list, so code-mixed Hindi
        # and English are both biased for on the same stream (§5.1).
        params.extend(("language_hint", hint) for hint in config.language_hints)

        # §5.5's vocabulary boosting. The catalogue's spoken forms --
        # "डीएपी", "इमिडाक्लोप्रिड", "एनपीके बारह बत्तीस सोलह" -- are exactly
        # what a general recogniser mangles, and biasing the decode is
        # cheaper and less lossy than correcting the transcript afterwards.
        keyterms = self._keyterms(config)
        params.extend(("keyterm", term) for term in keyterms)

        url = f"{self._url}?{urlencode(params)}"
        try:
            self._socket = await asyncio.wait_for(
                websockets.connect(
                    url,
                    additional_headers={"Authorization": f"Token {self._api_key}"},
                    max_size=None,
                ),
                timeout=CONNECT_TIMEOUT_S,
            )
        except TimeoutError as exc:
            raise VendorTimeoutError(
                vendor=self.provider, service="stt", timeout_ms=int(CONNECT_TIMEOUT_S * 1000)
            ) from exc
        except Exception as exc:
            raise VendorUnavailableError(
                vendor=self.provider, service="stt", detail=str(exc)[:200]
            ) from exc

        self._reader = asyncio.create_task(self._read_loop())
        log.info(
            "stt.started",
            provider=self.provider,
            model=config.model,
            language=config.language,
            eager=config.eager_eot_threshold,
        )

    @staticmethod
    def _eot_timeout(config: SttConfig) -> int:
        """§5.2 raises the backstop for callers observed to pause.

        Rural callers pause mid-sentence to think far more than the model's
        training distribution expects; cutting them off is the commonest way an
        Indian voice agent feels rude.
        """
        if config.slow_speaker:
            return max(config.eot_timeout_ms, 8000)
        return config.eot_timeout_ms

    async def send_audio(self, pcm: bytes) -> None:
        """Send raw linear16. Deepgram takes audio as binary WebSocket frames."""
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
        """Ask Flux to close the current turn immediately.

        Used when the pipeline knows the turn is over before the model does --
        a DTMF keypress, or a transfer being triggered mid-utterance.
        """
        if self._socket is None:
            return
        with contextlib.suppress(Exception):
            await self._socket.send(fastjson.dumps({"type": "ForceEndTurn"}))

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        if self._socket is not None:
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
                event = self._parse(raw)
                if event is None:
                    continue
                try:
                    self._queue.put_nowait(event)
                except asyncio.QueueFull:
                    # Never silently drop: losing an EndOfTurn strands the call.
                    log.warning("stt.event_queue_full", provider=self.provider)
                    await self._queue.put(event)
        except asyncio.CancelledError:
            raise
        except websockets.ConnectionClosed:
            log.info("stt.stream_closed", provider=self.provider)
        except Exception as exc:
            log.warning("stt.read_failed", provider=self.provider, error=type(exc).__name__)
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(SttEvent(type=SttEventType.ERROR, detail=type(exc).__name__))
        finally:
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(None)

    def _parse(self, raw: str | bytes) -> SttEvent | None:
        try:
            payload = fastjson.loads(raw)
        except fastjson.JsonError:
            return None
        if not isinstance(payload, dict):
            return None

        name = str(payload.get("event", ""))
        kind = _EVENT_MAP.get(name)
        if kind is None:
            return None

        transcript = str(payload.get("transcript", ""))

        # `Update` fires about every 250 ms and is frequently empty; an empty
        # partial carries nothing and would only add queue churn.
        if kind is SttEventType.PARTIAL and not transcript:
            return None

        latency_ms: float | None = None
        if self._turn_started_at is not None and kind in (
            SttEventType.EAGER_END_OF_TURN,
            SttEventType.END_OF_TURN,
        ):
            latency_ms = (time.perf_counter() - self._turn_started_at) * 1000
            if kind is SttEventType.END_OF_TURN:
                self._turn_started_at = None

        return SttEvent(
            type=kind,
            text=transcript,
            confidence=_word_confidence(payload),
            language=_detected_language(payload),
            latency_ms=latency_ms,
            detail=str(payload.get("trigger", "")),
            raw=payload,
        )


def _word_confidence(payload: dict[str, object]) -> float | None:
    """Mean word confidence for the turn.

    §11.4 escalates on a rolling mean below 0.55, and ``end_of_turn_confidence``
    is a different quantity -- how sure the model is that the *turn* ended, not
    how well it heard. Using it as a recognition score would make a decisive
    end-of-turn look like clear speech.
    """
    words = payload.get("words")
    if not isinstance(words, list) or not words:
        return None
    scores = [
        float(w["confidence"])
        for w in words
        if isinstance(w, dict) and isinstance(w.get("confidence"), int | float)
    ]
    return sum(scores) / len(scores) if scores else None


def _detected_language(payload: dict[str, object]) -> str | None:
    """First detected language, for ``flux-general-multi``.

    §11.1 follows a caller who switches language mid-call, and this is the
    signal that makes that possible.
    """
    languages = payload.get("languages")
    if isinstance(languages, list) and languages:
        first = languages[0]
        if isinstance(first, str):
            return first
    return None
