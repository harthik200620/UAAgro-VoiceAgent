"""Exotel AgentStream serializer (§4.3).

Verified protocol details:

===================  ==========================================================
Audio payload        base64-encoded **raw PCM / linear16** -- *not* mu-law
Sample rate          8000 Hz
Inbound events       ``connected``, ``start``, ``media`` (``media.payload``),
                     ``dtmf`` (``dtmf.digit``), ``stop``
Outbound audio       ``{"event":"media","stream_sid":<sid>,
                     "media":{"payload":<b64>}}``
Interrupt / flush    ``{"event":"clear","stream_sid":<sid>}``
Caller identity      present in the ``start`` frame -- no companion webhook
===================  ==========================================================

The single most expensive mistake available here is copying a Twilio example:
Twilio carries mu-law, Exotel carries linear16, and feeding one to the other
produces white noise on a live farmer's phone (§23-8). Hence a dedicated class
with the codec fixed as a class attribute rather than a constructor argument.

Configure on Exotel's side with a **Voicebot** applet (not "Stream", not
"Passthru") pointed at ``wss://<host>/ws/voice``, followed by a Hangup applet.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

import structlog

from uaagro_domain import fastjson
from uaagro_domain.enums import AudioCodec, TelephonyProvider

from .base import CallMetadata, InboundEvent, InboundEventType, TelephonySerializer

log = structlog.get_logger(__name__)

#: Exotel streams 8 kHz. Everything downstream assumes it, and the TTS is asked
#: to synthesise at this rate so no resampling stage exists in the hot path
#: (§5.3, §23-8).
SAMPLE_RATE = 8000

#: 20 ms of 16-bit mono PCM at 8 kHz. The standard telephony frame size.
FRAME_BYTES = 320

_VALID_DTMF = frozenset("0123456789*#")


class ExotelSerializer(TelephonySerializer):
    """Encodes and decodes one call's worth of Exotel AgentStream messages."""

    provider = TelephonyProvider.EXOTEL
    codec = AudioCodec.LINEAR16
    sample_rate = SAMPLE_RATE

    def __init__(self) -> None:
        self._stream_sid: str | None = None
        self._call_sid: str | None = None

    # -- lifecycle -------------------------------------------------------- #

    def bind(self, metadata: CallMetadata) -> None:
        self._stream_sid = metadata.stream_sid
        self._call_sid = metadata.call_sid

    @property
    def is_bound(self) -> bool:
        return self._stream_sid is not None

    @property
    def stream_sid(self) -> str | None:
        return self._stream_sid

    # -- inbound ---------------------------------------------------------- #

    def decode(self, message: str | bytes) -> InboundEvent:
        """Decode one inbound message.

        Tolerant on parse, strict on emit. A malformed frame from the network
        becomes ``UNKNOWN`` and is logged; it never raises, because an exception
        here would drop a live call over one bad packet.
        """
        try:
            payload = fastjson.loads(message)
        except fastjson.JsonError:
            log.warning("telephony.decode_failed", provider=self.provider.value)
            return InboundEvent(type=InboundEventType.UNKNOWN)

        if not isinstance(payload, dict):
            return InboundEvent(type=InboundEventType.UNKNOWN, raw={"payload": payload})

        event = str(payload.get("event", "")).lower()

        match event:
            case "connected":
                return InboundEvent(type=InboundEventType.CONNECTED, raw=payload)
            case "start":
                return self._decode_start(payload)
            case "media":
                return self._decode_media(payload)
            case "dtmf":
                return self._decode_dtmf(payload)
            case "stop":
                return InboundEvent(type=InboundEventType.STOP, raw=payload)
            case _:
                # `event` is structlog's own name for the message, so the
                # provider's event name is bound under a different key -- a
                # collision here raises, and raising on an unrecognised frame
                # would drop a live call.
                log.info(
                    "telephony.unknown_event",
                    provider_event=event,
                    provider=self.provider.value,
                )
                return InboundEvent(type=InboundEventType.UNKNOWN, raw=payload)

    def _decode_start(self, payload: dict[str, Any]) -> InboundEvent:
        """Extract stream identity and the caller's number.

        Exotel places most fields inside a nested ``start`` object but also
        repeats ``stream_sid`` at the top level. Both are checked so a
        formatting change on their side does not leave the call unidentified.
        """
        start = payload.get("start")
        start = start if isinstance(start, dict) else {}

        stream_sid = str(payload.get("stream_sid") or start.get("stream_sid") or "")
        call_sid = str(start.get("call_sid") or payload.get("call_sid") or stream_sid)

        if not stream_sid:
            log.warning("telephony.start_without_stream_sid", provider=self.provider.value)
            return InboundEvent(type=InboundEventType.UNKNOWN, raw=payload)

        metadata = CallMetadata(
            stream_sid=stream_sid,
            call_sid=call_sid,
            from_number=_optional_str(start.get("from")),
            to_number=_optional_str(start.get("to")),
            account_sid=_optional_str(start.get("account_sid")),
            extra={
                key: value
                for key, value in start.items()
                if key not in {"from", "to", "account_sid", "call_sid", "stream_sid"}
            },
        )
        return InboundEvent(type=InboundEventType.START, metadata=metadata, raw=payload)

    def _decode_media(self, payload: dict[str, Any]) -> InboundEvent:
        media = payload.get("media")
        media = media if isinstance(media, dict) else {}
        encoded = media.get("payload")
        if not isinstance(encoded, str) or not encoded:
            return InboundEvent(type=InboundEventType.UNKNOWN, raw=payload)
        try:
            audio = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            log.warning("telephony.media_decode_failed", provider=self.provider.value)
            return InboundEvent(type=InboundEventType.UNKNOWN, raw=payload)
        # Already linear16 at 8 kHz -- no transcoding, by design.
        return InboundEvent(type=InboundEventType.MEDIA, audio=audio, raw=payload)

    def _decode_dtmf(self, payload: dict[str, Any]) -> InboundEvent:
        dtmf = payload.get("dtmf")
        dtmf = dtmf if isinstance(dtmf, dict) else {}
        digit = str(dtmf.get("digit", ""))
        if digit not in _VALID_DTMF:
            log.warning("telephony.invalid_dtmf", provider=self.provider.value)
            return InboundEvent(type=InboundEventType.UNKNOWN, raw=payload)
        return InboundEvent(type=InboundEventType.DTMF, digit=digit, raw=payload)

    # -- outbound --------------------------------------------------------- #

    def encode_audio(self, pcm: bytes) -> str:
        """Wrap linear16 PCM in Exotel's media message.

        Raises:
            RuntimeError: if called before the ``start`` frame bound a stream
                SID. Sending audio to an unbound stream silently goes nowhere,
                which is far harder to diagnose than a loud failure.
        """
        if self._stream_sid is None:
            raise RuntimeError(
                "Cannot send audio before the Exotel start frame has been received. "
                "Wait for InboundEventType.START and call bind() first."
            )
        # Compact by construction -- `fastjson.dumps` never emits the standard
        # library's whitespace, so the `separators` argument this used to carry
        # has no counterpart and needs none.
        return fastjson.dumps(
            {
                "event": "media",
                "stream_sid": self._stream_sid,
                "media": {"payload": base64.b64encode(pcm).decode("ascii")},
            }
        )

    def encode_clear(self) -> str:
        """Flush audio Exotel has buffered but not yet played (§5.4)."""
        if self._stream_sid is None:
            raise RuntimeError("Cannot clear an unbound stream; no start frame received.")
        return fastjson.dumps({"event": "clear", "stream_sid": self._stream_sid})


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
