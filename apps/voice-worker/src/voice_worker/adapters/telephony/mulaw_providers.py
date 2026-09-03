"""Plivo and Twilio serializers -- the µ-law providers (§4.3).

§4.3 requires these to be **separate implementations**, not one class with a
codec flag, and the reason is worth restating because the shared-class version
is genuinely tempting: the message envelopes are near-identical and only the
payload codec differs. That single difference is the one that matters. A codec
flag set wrong emits linear16 bytes into a µ-law stream, and the caller hears
white noise -- not silence, not distortion, noise -- on a provider that was
working yesterday.

Keeping them apart makes the codec a class attribute chosen at import time
rather than a runtime value that can be threaded wrong. :class:`MulawSerializer`
holds what is genuinely common: the envelope shapes. The subclasses hold the
identity and the small differences that are real.

The Plivo adapter is §21's *portability proof*: the point is to demonstrate that
swapping the telephony vendor is one adapter file, not a rewrite. If writing it
had required touching the pipeline, the abstraction in §4.1 would have been
wrong.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

import structlog

from uaagro_domain import fastjson
from uaagro_domain.enums import AudioCodec, TelephonyProvider

from .base import CallMetadata, InboundEvent, InboundEventType, TelephonySerializer
from .mulaw import mulaw_to_pcm, pcm_to_mulaw

log = structlog.get_logger(__name__)

SAMPLE_RATE = 8000


class MulawSerializer(TelephonySerializer):
    """Shared envelope handling for the µ-law providers.

    Only the parts that are genuinely identical live here. The codec stays on
    the subclass, deliberately unset on this base so a provider that forgets to
    declare one fails at class definition rather than at the first frame of a
    live call.
    """

    codec = AudioCodec.MULAW
    sample_rate = SAMPLE_RATE

    def __init__(self) -> None:
        self._stream_sid: str | None = None
        self._call_sid: str | None = None

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
        """Tolerant on parse, strict on emit -- as with Exotel.

        A malformed frame becomes ``UNKNOWN`` and is logged. It never raises,
        because an exception here drops a live call over one bad packet.
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
                # Bound as `provider_event`, not `event`: structlog owns that
                # key, and a collision raises -- on an unrecognised frame, mid
                # call. That bug shipped once already.
                log.info(
                    "telephony.unknown_event",
                    provider_event=event,
                    provider=self.provider.value,
                )
                return InboundEvent(type=InboundEventType.UNKNOWN, raw=payload)

    def _decode_start(self, payload: dict[str, Any]) -> InboundEvent:
        start = payload.get("start")
        start = start if isinstance(start, dict) else {}

        # `streamSid` and `stream_sid` both accepted: Plivo and Twilio differ on
        # the casing and a formatting change on either side would otherwise
        # leave the call unidentified.
        stream_sid = (
            _text(start.get("streamSid"))
            or _text(start.get("stream_sid"))
            or _text(payload.get("streamSid"))
            or _text(payload.get("stream_sid"))
        )
        if stream_sid is None:
            log.warning("telephony.start_without_stream_sid", provider=self.provider.value)
            return InboundEvent(type=InboundEventType.UNKNOWN, raw=payload)

        metadata = CallMetadata(
            stream_sid=stream_sid,
            call_sid=_text(start.get("callSid") or start.get("call_id")) or "",
            from_number=self._from_number(start),
            to_number=self._to_number(start),
            account_sid=_text(start.get("accountSid") or start.get("account_id")),
            extra=payload,
        )
        # Deliberately *not* bound here. `runtime.session` binds after it has
        # written the `calls` row, and the Exotel serializer has the same
        # contract -- a serializer that bound itself would work with one call
        # site and silently not with the other.
        return InboundEvent(type=InboundEventType.START, metadata=metadata, raw=payload)

    def _decode_media(self, payload: dict[str, Any]) -> InboundEvent:
        media = payload.get("media")
        media = media if isinstance(media, dict) else {}
        encoded = media.get("payload")
        if not isinstance(encoded, str):
            return InboundEvent(type=InboundEventType.UNKNOWN, raw=payload)

        try:
            mulaw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            log.warning("telephony.bad_media_payload", provider=self.provider.value)
            return InboundEvent(type=InboundEventType.UNKNOWN, raw=payload)

        # Decoded to PCM at the boundary, so nothing downstream has to know
        # which provider this call came in on. That is the whole point of the
        # serializer: the pipeline sees one audio format.
        return InboundEvent(type=InboundEventType.MEDIA, audio=mulaw_to_pcm(mulaw), raw=payload)

    def _decode_dtmf(self, payload: dict[str, Any]) -> InboundEvent:
        dtmf = payload.get("dtmf")
        dtmf = dtmf if isinstance(dtmf, dict) else {}
        digit = _text(dtmf.get("digit"))
        if digit is None:
            return InboundEvent(type=InboundEventType.UNKNOWN, raw=payload)
        return InboundEvent(type=InboundEventType.DTMF, digit=digit, raw=payload)

    # -- outbound --------------------------------------------------------- #

    def encode_audio(self, pcm: bytes) -> str:
        """Wrap outbound audio, converting PCM to µ-law.

        The conversion is here and nowhere else. §23-8 forbids resampling in the
        output path; this is a *codec* change at the same rate, which is what
        the provider requires and is not the same thing.
        """
        payload = base64.b64encode(pcm_to_mulaw(pcm)).decode("ascii")
        return fastjson.dumps(
            {
                "event": "media",
                "streamSid": self._stream_sid,
                "media": {"payload": payload},
            }
        )

    def encode_clear(self) -> str:
        """§5.4's buffer flush.

        Cancelling TTS without this leaves the agent talking for another second
        after the farmer interrupts, which is the single most irritating thing a
        voice agent does.
        """
        return fastjson.dumps({"event": "clear", "streamSid": self._stream_sid})

    # -- provider differences ---------------------------------------------- #

    def _from_number(self, start: dict[str, Any]) -> str | None:
        return _text(start.get("from"))

    def _to_number(self, start: dict[str, Any]) -> str | None:
        return _text(start.get("to"))


class PlivoSerializer(MulawSerializer):
    """Plivo AudioStream.

    Plivo puts the caller's numbers directly on the ``start`` object, like
    Exotel and unlike Twilio.
    """

    provider = TelephonyProvider.PLIVO


class TwilioSerializer(MulawSerializer):
    """Twilio Media Streams.

    Twilio does **not** put the caller's number in the stream. §4.3 records
    this: identity arrives on a separate webhook, and the ``start`` frame
    carries only custom parameters the TwiML author chose to pass through. So
    the numbers are read from ``customParameters`` when they are there and are
    ``None`` when they are not -- rather than being invented, which would give
    every Twilio call the same fabricated caller.
    """

    provider = TelephonyProvider.TWILIO

    def _custom(self, start: dict[str, Any]) -> dict[str, Any]:
        params = start.get("customParameters")
        return params if isinstance(params, dict) else {}

    def _from_number(self, start: dict[str, Any]) -> str | None:
        return _text(self._custom(start).get("from") or start.get("from"))

    def _to_number(self, start: dict[str, Any]) -> str | None:
        return _text(self._custom(start).get("to") or start.get("to"))


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


__all__ = ("MulawSerializer", "PlivoSerializer", "TwilioSerializer")
