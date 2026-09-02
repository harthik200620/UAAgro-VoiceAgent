"""Sarvam ``bulbul:v3`` streaming synthesis (§5.3).

Chosen for four reasons, in order of weight (§5.3): it covers all eleven
required languages including Malayalam and Marathi, which no Western TTS does
at acceptable quality; it emits **8 kHz directly in the telephony codec**, which
deletes a resample stage from the hot path; it streams, so playback starts on
the first chunk; and it is India-hosted, so the round trip is tens of
milliseconds rather than a transatlantic hop.

Protocol, per Sarvam's streaming WebSocket documentation (verified 31 Aug 2026):

===============  ==============================================================
Client config    ``{"type":"config","data":{speaker, language_code, pace,
                 output_audio_codec, ...}}`` -- must be sent first
Client text      ``{"type":"text","data":{"text": "..."}}`` (1-2500 chars)
Client flush     ``{"type":"flush"}``
Server audio     ``{"type":"audio","data":{"audio":"<base64>"}}``
Server event     ``{"type":"event","data":{"event_type":"final"}}``
Codecs           mp3, wav, aac, opus, flac, **linear16**, **mulaw**, alaw
===============  ==============================================================

The WebSocket URL and the exact auth header are not stated on that page. Both
are configurable here and default to the documented API host and subscription
header; confirming them against a live key is a Phase 2 gate item.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import time
from collections.abc import AsyncIterator

import structlog
import websockets

from uaagro_domain import fastjson
from uaagro_domain.enums import AudioCodec
from uaagro_domain.errors import (
    MissingCredentialError,
    VendorTimeoutError,
    VendorUnavailableError,
)
from uaagro_domain.settings import Settings

from .base import TtsChunk, TtsConfig, TTSService

log = structlog.get_logger(__name__)

DEFAULT_WS_URL = "wss://api.sarvam.ai/text-to-speech/ws"
AUTH_HEADER = "api-subscription-key"

#: §7 allows 350 ms at p95 for time-to-first-byte. This is the hard ceiling
#: after which the pipeline gives up on this utterance and falls back to a
#: cached phrase (§11.4).
FIRST_BYTE_TIMEOUT_S = 3.0
#: Whole-utterance ceiling. A synthesiser that stops mid-sentence must not hold
#: the turn open indefinitely.
TOTAL_TIMEOUT_S = 20.0

#: Sarvam accepts 1-2500 characters per text message. Answers are capped at ~35
#: words by §5.3 so this is a guard rail, not a routine path.
MAX_TEXT_CHARS = 2500

_CODEC_NAMES: dict[AudioCodec, str] = {
    AudioCodec.LINEAR16: "linear16",
    AudioCodec.MULAW: "mulaw",
}


class SarvamTTS(TTSService):
    """Streaming synthesis over Sarvam's WebSocket API."""

    provider = "sarvam"

    def __init__(
        self,
        settings: Settings,
        *,
        url: str = DEFAULT_WS_URL,
        connect_timeout_s: float = 5.0,
    ) -> None:
        # §0 rule 4: fail loudly naming the variable rather than degrading.
        self._api_key = settings.require(
            "sarvam_api_key", needed_for="Sarvam text-to-speech synthesis"
        )
        self._url = url
        self._connect_timeout_s = connect_timeout_s
        self._socket: websockets.ClientConnection | None = None
        self._configured_for: TtsConfig | None = None
        self._lock = asyncio.Lock()

    # -- connection ------------------------------------------------------- #

    async def _connect(self, config: TtsConfig) -> websockets.ClientConnection:
        """Open a socket, or reuse the open one if its config still matches.

        §7.5 wants persistent sockets held warm rather than dialled per turn --
        a TLS handshake inside the 350 ms first-byte budget is most of it.
        """
        if self._socket is not None and self._configured_for == config:
            return self._socket

        await self._disconnect()

        codec = _CODEC_NAMES.get(config.codec)
        if codec is None:
            raise VendorUnavailableError(
                vendor=self.provider,
                service="tts",
                detail=f"codec {config.codec.value} is not offered by bulbul",
            )

        url = f"{self._url}?model={config.model}"
        try:
            socket = await asyncio.wait_for(
                websockets.connect(
                    url,
                    additional_headers={AUTH_HEADER: self._api_key},
                    max_size=None,
                ),
                timeout=self._connect_timeout_s,
            )
        except TimeoutError as exc:
            raise VendorTimeoutError(
                vendor=self.provider,
                service="tts",
                timeout_ms=int(self._connect_timeout_s * 1000),
            ) from exc
        except Exception as exc:
            raise VendorUnavailableError(
                vendor=self.provider, service="tts", detail=str(exc)[:200]
            ) from exc

        if config.speaker is None:
            raise MissingCredentialError(
                "SARVAM_TTS_SPEAKER_HI",
                needed_for="choosing a bulbul voice; pick one in the Phase 2 bake-off "
                "rather than from the documentation",
            )

        await socket.send(
            fastjson.dumps(
                {
                    "type": "config",
                    "data": {
                        "speaker": config.speaker,
                        "language_code": config.language,
                        "pace": config.pace,
                        "output_audio_codec": codec,
                        # 8 kHz in the telephony codec, so nothing downstream
                        # resamples (§23-8).
                        "output_audio_bitrate": str(config.sample_rate),
                    },
                }
            )
        )

        self._socket = socket
        self._configured_for = config
        return socket

    async def _disconnect(self) -> None:
        if self._socket is not None:
            with contextlib.suppress(Exception):
                await self._socket.close()
        self._socket = None
        self._configured_for = None

    # -- synthesis -------------------------------------------------------- #

    async def synthesise(self, text: str, config: TtsConfig) -> AsyncIterator[TtsChunk]:
        """Stream audio for one utterance.

        Cancelling this iterator is normal control flow -- it happens on every
        barge-in -- so the socket is dropped rather than reused, because a
        half-drained stream would deliver the abandoned utterance's tail into
        the next turn.
        """
        if not text.strip():
            return

        if len(text) > MAX_TEXT_CHARS:
            raise VendorUnavailableError(
                vendor=self.provider,
                service="tts",
                detail=f"text is {len(text)} characters; the limit is {MAX_TEXT_CHARS}",
            )

        async with self._lock:
            socket = await self._connect(config)
            started = time.perf_counter()
            first = True
            cancelled = False

            try:
                await socket.send(fastjson.dumps({"type": "text", "data": {"text": text}}))
                await socket.send(fastjson.dumps({"type": "flush"}))

                deadline = started + TOTAL_TIMEOUT_S
                while True:
                    timeout = (
                        FIRST_BYTE_TIMEOUT_S if first else max(0.1, deadline - time.perf_counter())
                    )
                    try:
                        raw = await asyncio.wait_for(socket.recv(), timeout=timeout)
                    except TimeoutError as exc:
                        raise VendorTimeoutError(
                            vendor=self.provider,
                            service="tts",
                            timeout_ms=int(
                                (FIRST_BYTE_TIMEOUT_S if first else TOTAL_TIMEOUT_S) * 1000
                            ),
                        ) from exc

                    message = _decode(raw)
                    if message is None:
                        continue

                    kind = message.get("type")
                    if kind == "audio":
                        audio = _extract_audio(message)
                        if not audio:
                            continue
                        if first:
                            log.debug(
                                "tts.first_byte",
                                provider=self.provider,
                                ttfb_ms=round((time.perf_counter() - started) * 1000, 1),
                            )
                        yield TtsChunk(audio=audio, is_first=first)
                        first = False
                    elif kind == "event" and _event_type(message) == "final":
                        yield TtsChunk(audio=b"", is_final=True)
                        return
                    elif kind == "error":
                        raise VendorUnavailableError(
                            vendor=self.provider,
                            service="tts",
                            detail=str(message.get("data"))[:200],
                        )
            except asyncio.CancelledError:
                # Barge-in. Drop the socket: whatever is still queued belongs
                # to an utterance the farmer interrupted (§5.4).
                cancelled = True
                raise
            finally:
                if cancelled:
                    await self._disconnect()

    async def close(self) -> None:
        await self._disconnect()


def _decode(raw: str | bytes) -> dict[str, object] | None:
    try:
        payload = fastjson.loads(raw)
    except fastjson.JsonError:
        log.warning("tts.undecodable_message", provider="sarvam")
        return None
    return payload if isinstance(payload, dict) else None


def _extract_audio(message: dict[str, object]) -> bytes:
    data = message.get("data")
    if not isinstance(data, dict):
        return b""
    encoded = data.get("audio")
    if not isinstance(encoded, str) or not encoded:
        return b""
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        log.warning("tts.undecodable_audio", provider="sarvam")
        return b""


def _event_type(message: dict[str, object]) -> str:
    data = message.get("data")
    if isinstance(data, dict):
        value = data.get("event_type")
        if isinstance(value, str):
            return value
    return ""
