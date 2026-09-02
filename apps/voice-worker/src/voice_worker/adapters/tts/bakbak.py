"""Raya's Bakbak streaming synthesis (§5.3, §23-8).

Bakbak is built for conversational Indic speech rather than narration, which is
what §5.3 actually needs: short turns, code-mixed Hindi and English, and a first
byte fast enough that playback starts before the sentence is finished. It also
synthesises **directly at 8 kHz**, so the telephony leg needs no resampling --
§23-8's requirement, and the difference between an agent that sounds like a
phone call and one that sounds tinny.

Protocol, verified 2 September 2026 against docs.litwizlabs.com:

==================  ===========================================================
Base                ``https://hub.getraya.app/v1``
Auth                ``X-API-Key: raya_…``
Stream              ``POST /text-to-speech/stream`` -- Server-Sent Events
Batch               ``POST /text-to-speech`` -- whole file, ``codec`` of
                    ``pcm`` | ``wav`` | ``mp3`` | ``mulaw``
Voices              ``GET /v1/voices`` -- ``{id, name, language, model}``
Body                ``text``, ``voice_id``, ``model`` (``standard`` | ``m1``),
                    ``language``, ``sample_rate`` (8000 | 16000 | 22050 |
                    24000), ``speed`` (0.5-1.5)
Events              ``event: chunk`` with ``{"data": "<base64>", "step_time":
                    …}``, then ``event: done``
==================  ===========================================================

**The streaming endpoint returns PCM F32LE regardless of ``codec``**, which the
batch endpoint's format list does not apply to. So this adapter converts 32-bit
float samples to the 16-bit integers the rest of the worker speaks. That is a
sample-*format* conversion at an unchanged 8 kHz -- not the resampling §23-8
forbids, which is the operation that costs quality and CPU. The rate is
requested from the vendor and rejected here if it is not the telephony rate,
rather than fixed up afterwards.

A voice id is required and has no default. §5.3 chooses the voice in a bake-off
over a real phone line; a documentation example silently becoming production's
voice is exactly what that rule exists to prevent.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import time
from collections.abc import AsyncIterator
from typing import Any

import numpy as np
import structlog

from uaagro_domain import fastjson
from uaagro_domain.enums import AudioCodec
from uaagro_domain.errors import (
    ConfigurationError,
    MissingCredentialError,
    VendorTimeoutError,
    VendorUnavailableError,
)
from uaagro_domain.settings import Settings

from ..http import build_client, is_shared, prewarm_connection
from ..resilience import breaker_for
from .base import TtsChunk, TtsConfig, TTSService

log = structlog.get_logger(__name__)


async def _noop() -> None:
    """Nothing. Lets the breaker act as an admission check around a
    generator, which cannot be wrapped in `call` without consuming it."""
    return None

DEFAULT_BASE_URL = "https://hub.getraya.app/v1"
AUTH_HEADER = "X-API-Key"
STREAM_PATH = "/text-to-speech/stream"
VOICES_PATH = "/voices"

#: §7 allows 350 ms at p95 for time-to-first-byte; Bakbak advertises <150 ms.
#: This is the hard ceiling after which the pipeline abandons the utterance and
#: falls back to a cached phrase (§11.4), not a target.
FIRST_BYTE_TIMEOUT_S = 3.0
#: Whole-utterance ceiling, so a synthesiser that stops mid-sentence cannot
#: hold the turn open indefinitely.
TOTAL_TIMEOUT_S = 20.0
CONNECT_TIMEOUT_S = 3.0

#: The rates the vendor will accept. Asking for anything else is a
#: configuration error, because the alternative is resampling (§23-8).
SUPPORTED_SAMPLE_RATES = (8000, 16000, 22050, 24000)

#: Vendor speed multiplier bounds.
MIN_SPEED, MAX_SPEED = 0.5, 1.5

#: Route language (BCP-47, as the §5.1 table and the audio cache key use it) to
#: the vendor's own code. The mapping lives here because §4.1 puts vendor
#: dialects in the adapter -- ``en-in`` in a routing table would leak one
#: vendor's spelling into the panel, the cache and every other adapter.
_LANGUAGE_CODES: dict[str, str] = {
    "hi-IN": "hi",
    "en-IN": "en-in",
    "en-US": "en-us",
    "mr-IN": "mr",
    "ml-IN": "ml",
    "bn-IN": "bn",
    "ta-IN": "ta",
    "te-IN": "te",
    "kn-IN": "kn",
    "gu-IN": "gu",
    "as-IN": "as",
    "ne-NP": "ne",
}


class BakbakTTS(TTSService):
    """Streaming synthesis over Bakbak's SSE endpoint."""

    provider = "bakbak"

    def __init__(
        self,
        settings: Settings,
        *,
        base_url: str | None = None,
        client: Any = None,
    ) -> None:
        # §0 rule 4: name the variable rather than degrading at the first turn.
        self._api_key = settings.require(
            "bakbak_api_key", needed_for="Bakbak text-to-speech synthesis"
        )
        self._base_url = (base_url or settings.bakbak_base_url or DEFAULT_BASE_URL).rstrip("/")
        self._model = settings.bakbak_model
        self._client = client
        # No lock. Sarvam's adapter serialises on one because it holds a single
        # shared WebSocket; here each utterance is its own HTTP request over a
        # pooled client, so a lock would buy nothing and cost the concurrency
        # `audio_prewarm` relies on -- it renders the §16.1 fixed phrases with
        # `asyncio.gather`, and serialised that is a slow worker boot.

    # -- connection ------------------------------------------------------- #

    def _ensure_client(self) -> Any:
        """One pooled client for the process, held warm.

        Measured: 741 ms to first byte on a cold connection, 303 ms on a warm
        one. §7 allows 350 ms at p95, so the handshake alone is the difference
        between meeting that budget and doubling it -- see
        :mod:`voice_worker.adapters.http` for why "cold" was the normal case.
        """
        if self._client is None:
            self._client = build_client(
                base_url=self._base_url,
                headers={AUTH_HEADER: self._api_key, "Content-Type": "application/json"},
                total_timeout_s=TOTAL_TIMEOUT_S,
                connect_timeout_s=CONNECT_TIMEOUT_S,
            )
        return self._client

    async def prewarm(self) -> bool:
        """Open the connection before the first caller does (§7.5).

        Uses the voice listing, which is free. The first utterance of the first
        call would otherwise pay the handshake, and the first utterance of a
        call is the greeting -- the one place §11.1 wants audio inside ~50 ms.
        """
        return await prewarm_connection(self._ensure_client(), VOICES_PATH)

    # -- request building ------------------------------------------------- #

    def _body(self, text: str, config: TtsConfig) -> dict[str, Any]:
        if config.codec is not AudioCodec.LINEAR16:
            # The worker's audio bus is linear16 throughout; mulaw is applied
            # once, at the wire, by `MulawSerializer`. A synthesiser asked for
            # mulaw as well would have it encoded twice, which is the white
            # noise §23-8 warns about.
            raise ConfigurationError(
                f"Bakbak was asked for {config.codec.value} audio, but the worker's "
                "audio path carries linear16 and the telephony serializer applies "
                "mulaw at the wire. Encoding it twice puts white noise on the line.",
                remedy="Set TTS_OUTPUT_CODEC=linear16. Plivo and Twilio still receive "
                "mulaw -- MulawSerializer converts on the way out.",
                context={"codec": config.codec.value},
            )

        if config.sample_rate not in SUPPORTED_SAMPLE_RATES:
            raise ConfigurationError(
                f"Bakbak does not synthesise at {config.sample_rate} Hz.",
                remedy="Set TTS_OUTPUT_SAMPLE_RATE to one of "
                f"{', '.join(str(rate) for rate in SUPPORTED_SAMPLE_RATES)}. Telephony "
                "is 8000, and asking for another rate would mean resampling, which "
                "§23-8 forbids.",
                context={"sample_rate": config.sample_rate},
            )

        language = _LANGUAGE_CODES.get(config.language)
        if language is None:
            raise ConfigurationError(
                f"Bakbak has no voice for language {config.language!r}.",
                remedy="Route this language to a provider that covers it in "
                "config/defaults.yaml, or add it to _LANGUAGE_CODES once the vendor "
                "supports it.",
                context={"language": config.language},
            )

        if not config.speaker:
            raise MissingCredentialError(
                "BAKBAK_VOICE_HI",
                needed_for=f"choosing a Bakbak voice for {config.language}; list the "
                "account's voices with `uv run python scripts/bakbak_voices.py` and "
                "pick one in the §5.3 bake-off rather than from the documentation",
            )

        return {
            "text": text,
            "voice_id": config.speaker,
            "model": config.model or self._model,
            "language": language,
            # Asked for, not converted to. §23-8.
            "sample_rate": config.sample_rate,
            # §5.3: slightly slower than default, never faster.
            "speed": round(min(MAX_SPEED, max(MIN_SPEED, config.pace)), 2),
        }

    # -- synthesis -------------------------------------------------------- #

    async def synthesise(self, text: str, config: TtsConfig) -> AsyncIterator[TtsChunk]:
        """Stream audio for one utterance.

        Cancelling this iterator is normal control flow -- it happens on every
        barge-in (§5.4) -- and closing the response is enough here: unlike a
        shared WebSocket, an abandoned HTTP stream cannot deliver its tail into
        the next turn.
        """
        if not text.strip():
            return

        body = self._body(text, config)
        client = self._ensure_client()
        # Admission check before the request. A synthesiser that is down would
        # otherwise cost 3 s per sentence, on every sentence, of every call.
        # §16.1's fixed phrases are already in the audio cache, so refusing in
        # microseconds leaves a degraded agent rather than a silent one.
        breaker = breaker_for(self.provider, "tts")
        await breaker.call(_noop)
        started = time.perf_counter()
        first = True
        residue = b""

        try:
            async with client.stream("POST", STREAM_PATH, json=body) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise VendorUnavailableError(
                        vendor=self.provider,
                        service="tts",
                        detail=_error_detail(response),
                    )

                deadline = started + TOTAL_TIMEOUT_S
                lines = response.aiter_lines()
                while True:
                    budget = (
                        FIRST_BYTE_TIMEOUT_S
                        if first
                        else max(0.1, deadline - time.perf_counter())
                    )
                    try:
                        line = await asyncio.wait_for(anext(lines), timeout=budget)
                    except StopAsyncIteration:
                        break
                    except TimeoutError as exc:
                        raise VendorTimeoutError(
                            vendor=self.provider,
                            service="tts",
                            timeout_ms=int(
                                (FIRST_BYTE_TIMEOUT_S if first else TOTAL_TIMEOUT_S) * 1000
                            ),
                        ) from exc

                    if not line.startswith("data:"):
                        # `event:` lines and the blank separators carry no
                        # audio; the payload type is in the JSON itself.
                        continue

                    message = _decode(line[len("data:") :].strip())
                    if message is None:
                        continue

                    if message.get("type") == "done" or message.get("done") is True:
                        # Counted, so an isolated failure hours ago cannot
                        # accumulate into an outage that never happened.
                        breaker.record_success()
                        yield TtsChunk(audio=b"", is_final=True)
                        return

                    floats, residue = _f32_payload(message.get("data"), residue)
                    pcm = _to_linear16(floats)
                    if not pcm:
                        continue

                    if first:
                        log.debug(
                            "tts.first_byte",
                            provider=self.provider,
                            ttfb_ms=round((time.perf_counter() - started) * 1000, 1),
                        )
                    yield TtsChunk(audio=pcm, is_first=first)
                    first = False

                # The stream ended without a `done` event -- unusual but
                # harmless. Close the utterance so playback is not left
                # waiting for a final chunk that will never arrive.
                breaker.record_success()
                yield TtsChunk(audio=b"", is_final=True)
        except (VendorTimeoutError, VendorUnavailableError) as exc:
            breaker.record_failure(exc)
            raise
        except ConfigurationError:
            # Ours, not theirs -- an unset voice, a language they do not speak.
            # Never counted against the vendor's health.
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = VendorUnavailableError(
                vendor=self.provider, service="tts", detail=str(exc)[:200]
            )
            breaker.record_failure(failure)
            raise failure from exc

    # -- voices ----------------------------------------------------------- #

    async def voices(self) -> list[dict[str, str]]:
        """Every voice this account can use.

        Voice ids are account-scoped UUIDs, so there is no list to hard-code and
        no default to fall back to; this is what ``scripts/bakbak_voices.py``
        prints for the §5.3 bake-off.
        """
        client = self._ensure_client()
        response = await client.get(VOICES_PATH)
        if response.status_code >= 400:
            raise VendorUnavailableError(
                vendor=self.provider, service="tts", detail=_error_detail(response)
            )
        payload = response.json()
        voices = payload.get("voices") if isinstance(payload, dict) else None
        return [voice for voice in voices or [] if isinstance(voice, dict)]

    async def close(self) -> None:
        """Release this adapter. The shared connection stays open on purpose.

        Every call builds its own `BakbakTTS`, and the pre-warm at boot builds
        another. If each closed the pooled connection on the way out, the next
        call would re-handshake -- 741 ms to first byte instead of 303 ms, and
        the pre-warm would be actively self-defeating.
        """
        if self._client is not None and not is_shared(self._client):
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None


# --------------------------------------------------------------------------- #
# Decoding
# --------------------------------------------------------------------------- #


def _decode(body: str) -> dict[str, Any] | None:
    if not body:
        return None
    try:
        payload = fastjson.loads(body)
    except fastjson.JsonError:
        # A malformed frame mid-stream is not worth ending a live call over.
        log.warning("tts.undecodable_message", provider="bakbak")
        return None
    return payload if isinstance(payload, dict) else None


def _error_detail(response: Any) -> str:
    """The vendor's message, never its response headers.

    §23-6: an error path is the easiest place for a key to reach a log line,
    and this one is called with the authenticated client's own response.
    """
    with contextlib.suppress(Exception):
        payload = response.json()
        if isinstance(payload, dict) and payload.get("detail"):
            return f"{response.status_code}: {str(payload['detail'])[:200]}"
    return f"HTTP {response.status_code}"


def _f32_payload(encoded: object, residue: bytes) -> tuple[np.ndarray, bytes]:
    """Decode one base64 chunk into float samples, carrying a partial sample.

    Nothing guarantees a chunk holds a whole number of 4-byte floats. Dropping
    the remainder would delete a sample from every chunk boundary -- inaudible
    once, a rising click track over a whole answer -- so the tail is carried
    into the next chunk instead.
    """
    if not isinstance(encoded, str) or not encoded:
        return np.empty(0, dtype="<f4"), residue
    try:
        raw = residue + base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        log.warning("tts.undecodable_audio", provider="bakbak")
        return np.empty(0, dtype="<f4"), residue

    usable = len(raw) - (len(raw) % 4)
    return np.frombuffer(raw[:usable], dtype="<f4"), raw[usable:]


def _to_linear16(samples: np.ndarray) -> bytes:
    """Float samples in [-1, 1] to signed 16-bit little-endian.

    Clipped rather than scaled to fit: a synthesiser occasionally overshoots by
    a fraction, and normalising the whole utterance to its loudest sample would
    make the agent's volume wander between sentences. 32767 rather than 32768 so
    a full-scale positive sample cannot wrap to silence.
    """
    if samples.size == 0:
        return b""
    clipped = np.clip(samples, -1.0, 1.0)
    encoded: bytes = (clipped * 32767.0).astype("<i2").tobytes()
    return encoded


__all__ = ("DEFAULT_BASE_URL", "SUPPORTED_SAMPLE_RATES", "BakbakTTS")
