"""Sarvam ``saaras:v3-realtime`` recognition (§5.1).

Carries every Indic language the platform serves, including the two Deepgram
Flux does not: **Marathi and Malayalam**. That is the whole reason §5.1 is a
two-engine table rather than one.

Unlike Flux, this recogniser does **not** decide turns semantically. It
segments on its own VAD and emits a final transcript per utterance, so
:attr:`emits_turn_events` is False and a separate detector sits in front --
Smart Turn v3.1 where it covers the language, plain silence endpointing where
it does not (Malayalam, which is why that route is labelled tier C).

Protocol, verified 31 August 2026:

==================  ===========================================================
URL                 ``wss://api.sarvam.ai/speech-to-text-realtime/ws``
Auth                ``api-subscription-key: <key>``
Required query      ``language_code`` (BCP-47, or ``auto``)
Optional query      ``model``, ``mode``, ``endpointing``, ``encoding``,
                    ``sample_rate``, ``threshold``, ``silence_duration_ms``,
                    ``min_speech_duration_ms``, ``prompt``
Audio in            ``{"event":"audio_input","audio":"<base64>"}``
Partial out         ``{"event":"transcript.partial","text":...,"language":...,
                    "language_confidence":...}``
Final out           ``{"event":"transcript.final", ... ,"start_s","end_s"}``
Reconfigure         ``{"event":"config.update", ...}`` -- no reconnect needed
==================  ===========================================================

``config.update`` matters more than it looks: §11.1 requires following a caller
who switches language mid-call by reconfiguring the stream **in place** rather
than reconnecting, and this is what makes that possible without dropping audio.
"""

from __future__ import annotations

import asyncio
import base64
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

DEFAULT_WS_URL = "wss://api.sarvam.ai/speech-to-text-realtime/ws"
AUTH_HEADER = "api-subscription-key"

CONNECT_TIMEOUT_S = 5.0
EVENT_QUEUE_MAX = 256

#: Sarvam's VAD defaults. ``silence_duration_ms`` is raised from the vendor
#: default of 500 because §3 is explicit that rural callers pause mid-sentence
#: far more than a model expects, and §19.2 weights a false cut above dead air.
#: The turn detector in front makes the final call either way; this only
#: controls how Sarvam segments utterances.
DEFAULT_VAD_THRESHOLD = 0.3
DEFAULT_SILENCE_MS = 700
DEFAULT_MIN_SPEECH_MS = 250

#: Longest ``prompt`` biasing string worth sending. The catalogue lexicon is
#: ~300 terms; sending all of them on every stream would bloat the handshake
#: for diminishing benefit, so the most distinctive are sent.
MAX_PROMPT_CHARS = 900


class SarvamSTT(STTService):
    """Streaming recognition for the Indic languages Flux does not cover."""

    provider = "sarvam"
    #: Sarvam segments on VAD, not on meaning. A semantic detector sits in front.
    emits_turn_events = False

    def __init__(self, settings: Settings, *, url: str = DEFAULT_WS_URL) -> None:
        self._api_key = settings.require(
            "sarvam_api_key", needed_for="Sarvam real-time speech recognition"
        )
        self._url = url
        self._socket: websockets.ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[SttEvent | None] = asyncio.Queue(maxsize=EVENT_QUEUE_MAX)
        self._utterance_started_at: float | None = None

    # -- lifecycle -------------------------------------------------------- #

    async def start(self, config: SttConfig) -> None:
        params: dict[str, str] = {
            "language_code": config.language,
            "model": config.model or "saaras:v3-realtime",
            "encoding": "linear16",
            "sample_rate": str(config.sample_rate),
            "endpointing": "vad",
            "threshold": str(DEFAULT_VAD_THRESHOLD),
            "silence_duration_ms": str(DEFAULT_SILENCE_MS),
            "min_speech_duration_ms": str(DEFAULT_MIN_SPEECH_MS),
        }
        prompt = _build_prompt(config.keyterms)
        if prompt:
            # §5.5: bias the decode toward the catalogue rather than correcting
            # it afterwards. Sarvam takes this as a free-text `prompt`.
            params["prompt"] = prompt

        url = f"{self._url}?{urlencode(params)}"
        try:
            self._socket = await asyncio.wait_for(
                websockets.connect(
                    url,
                    additional_headers={AUTH_HEADER: self._api_key},
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
            model=params["model"],
            language=config.language,
            keyterms=len(config.keyterms),
        )

    async def reconfigure(self, *, language: str | None = None, prompt: str | None = None) -> None:
        """Change the stream's configuration without reconnecting.

        §11.1: when a caller switches language mid-call, follow them. Tearing
        the socket down and redialling would lose the audio spoken during the
        reconnect, which is exactly the moment the caller is mid-sentence.
        """
        if self._socket is None:
            return
        payload: dict[str, str] = {"event": "config.update"}
        if language:
            payload["language_code"] = language
        if prompt:
            payload["prompt"] = prompt[:MAX_PROMPT_CHARS]
        with contextlib.suppress(Exception):
            await self._socket.send(fastjson.dumps(payload))
            log.info("stt.reconfigured", provider=self.provider, language=language)

    async def send_audio(self, pcm: bytes) -> None:
        """Send linear16 as base64 inside a JSON envelope.

        Sarvam takes audio in JSON rather than as binary frames, which costs
        roughly a third in bandwidth to base64. At 8 kHz mono that is about
        21 kB/s -- irrelevant next to the latency budget, and not worth
        deviating from the documented protocol to avoid.
        """
        if self._socket is None:
            raise VendorUnavailableError(
                vendor=self.provider, service="stt", detail="stream is not open"
            )
        if self._utterance_started_at is None:
            self._utterance_started_at = time.perf_counter()
        try:
            await self._socket.send(
                fastjson.dumps(
                    {
                        "event": "audio_input",
                        "audio": base64.b64encode(pcm).decode("ascii"),
                    }
                )
            )
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
        """Sarvam finalises on its own VAD; there is no force-flush event.

        Left as a no-op rather than faking one: the turn detector in front owns
        the decision, and pretending to flush would hide that.
        """
        return None

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
                    log.error("stt.event_queue_full", provider=self.provider)
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
        text = str(payload.get("text", ""))

        if name == "transcript.partial":
            if not text:
                return None
            return SttEvent(
                type=SttEventType.PARTIAL,
                text=text,
                language=_language(payload),
                # Recognition confidence is deliberately left unset: Sarvam
                # does not report one, and the §11.4 escalation must see that
                # honestly rather than a stand-in.
                language_confidence=_language_confidence(payload),
                raw=payload,
            )

        if name == "transcript.final":
            latency_ms: float | None = None
            if self._utterance_started_at is not None:
                latency_ms = (time.perf_counter() - self._utterance_started_at) * 1000
                self._utterance_started_at = None
            # FINAL, not END_OF_TURN: this recogniser segments on silence, and
            # only the turn detector in front knows whether the farmer has
            # actually finished their thought.
            return SttEvent(
                type=SttEventType.FINAL,
                text=text,
                language=_language(payload),
                language_confidence=_language_confidence(payload),
                latency_ms=latency_ms,
                raw=payload,
            )

        if name in ("error", "transcript.error"):
            return SttEvent(type=SttEventType.ERROR, detail=str(payload)[:200], raw=payload)

        return None


def _build_prompt(keyterms: tuple[str, ...]) -> str:
    """Pack catalogue terms into Sarvam's biasing prompt, longest first.

    Longest first because a distinctive multi-word product name carries far
    more disambiguating value than a common short one, and the budget is
    character-bound.
    """
    if not keyterms:
        return ""
    chosen: list[str] = []
    used = 0
    for term in sorted(keyterms, key=len, reverse=True):
        if used + len(term) + 2 > MAX_PROMPT_CHARS:
            break
        chosen.append(term)
        used += len(term) + 2
    return ", ".join(chosen)


def _language(payload: dict[str, object]) -> str | None:
    """The detected language tag, under either field name.

    Both are accepted because which one Sarvam sends is **unverified against a
    live session** -- the adapter was written from the protocol description,
    and the contract fixtures in `tests/test_vendor_contracts.py` are derived
    the same way rather than captured.

    Reading only one and guessing wrong does not fail loudly: the field comes
    back None, §11.1's LANG_LOCK never fires, and every call runs in the
    default language while the transcripts look fine. Accepting both costs a
    dictionary lookup and removes the failure mode. Confirm the real name at
    the Phase 2 gate and this can narrow.
    """
    for key in ("language", "language_code"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _language_confidence(payload: dict[str, object]) -> float | None:
    """Confidence in the *detected language*, which is not a recognition score.

    Carried in its own field. §11.4 escalates a call when recognition
    confidence stays low, and a confidently-identified language says nothing
    about whether the words were clear -- substituting one for the other would
    silence that escalation on exactly the noisy calls it exists for.

    **Known gap:** the Sarvam paths therefore have no recognition-confidence
    signal, so §11.4's low-confidence escalation is inert for Marathi,
    Malayalam and the other Indic routes. Those calls still escalate on
    repeated misunderstanding and on sentiment; this is recorded rather than
    papered over, and is a Phase 2 finding for the tier B/C languages.
    """
    value = payload.get("language_confidence")
    return float(value) if isinstance(value, int | float) else None
