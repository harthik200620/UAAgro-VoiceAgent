"""Telephony adapter interface (§4.1).

Everything vendor-facing sits behind an interface so swapping Exotel for Plivo
is a config change plus one adapter file, never a refactor.

The adapters are deliberately **separate implementations rather than one
parameterised class**. §4.3 is explicit about why: Exotel carries base64 raw PCM
(linear16) and Twilio and Plivo carry base64 mu-law. Conflating them produces
white noise on the line, and a shared class with a codec flag is exactly how
that mistake gets made.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from uaagro_domain.enums import AudioCodec, TelephonyProvider


class InboundEventType(StrEnum):
    """Normalised event vocabulary. Every provider maps onto these."""

    CONNECTED = "connected"
    START = "start"
    MEDIA = "media"
    DTMF = "dtmf"
    STOP = "stop"
    #: Anything the provider sent that this adapter does not model. Kept rather
    #: than dropped so an unexpected frame shows up in the call event log
    #: instead of vanishing.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CallMetadata:
    """Identity of a call, taken from the provider's ``start`` message.

    Exotel includes the to/from numbers and account details in the WebSocket
    ``start`` frame, so no companion webhook is needed to identify the caller
    (§4.3). Twilio does need one, which is why this is part of the adapter
    contract rather than assumed.
    """

    stream_sid: str
    call_sid: str
    from_number: str | None = None
    to_number: str | None = None
    account_sid: str | None = None
    #: Anything else the provider sent, kept verbatim for the call event log.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InboundEvent:
    """One decoded frame from the provider."""

    type: InboundEventType
    #: Decoded PCM bytes for MEDIA events, already in the worker's own format.
    audio: bytes | None = None
    digit: str | None = None
    metadata: CallMetadata | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class TelephonySerializer(ABC):
    """Translates between a provider's WebSocket messages and worker frames."""

    provider: TelephonyProvider
    codec: AudioCodec
    sample_rate: int

    @abstractmethod
    def decode(self, message: str | bytes) -> InboundEvent:
        """Decode one inbound WebSocket message.

        Must never raise on malformed input from the network: an unparseable
        frame becomes :attr:`InboundEventType.UNKNOWN` so the call survives and
        the frame is logged, rather than the socket dying mid-conversation.
        """

    @abstractmethod
    def encode_audio(self, pcm: bytes) -> str:
        """Wrap outbound audio in the provider's media message."""

    @abstractmethod
    def encode_clear(self) -> str:
        """The buffer-flush control message.

        §5.4: cancelling TTS without clearing the provider's buffer leaves the
        agent talking for another second after the farmer interrupts. This is
        the message that stops that, and it is not optional.
        """

    @abstractmethod
    def bind(self, metadata: CallMetadata) -> None:
        """Attach the stream identity learned from the ``start`` frame."""

    @property
    @abstractmethod
    def is_bound(self) -> bool:
        """Whether a ``start`` frame has been seen and audio may be sent."""


class TelephonyAdapter(ABC):
    """Control-plane operations: dialling, transferring, hanging up.

    Separate from the serializer because the media path and the control path
    have different lifetimes -- the serializer lives for one call, the adapter
    for the process.
    """

    provider: TelephonyProvider

    @abstractmethod
    async def originate(
        self, *, to: str, from_: str, callback_url: str, custom_field: str | None = None
    ) -> str:
        """Place an outbound call. Returns the provider call SID.

        §17: the destination must come from an operator-approved campaign list
        or the ``centres``/``users`` tables. A caller-supplied value must never
        reach here -- that is how toll fraud happens.

        ``custom_field`` rides along to the media stream's ``start`` frame,
        where the worker reads it to find the campaign contact the call is
        for. Providers that cannot carry it are still usable: the worker falls
        back to matching the dialled number against contacts being dialled.
        """

    @abstractmethod
    async def transfer(self, *, call_sid: str, to: str, whisper_text: str | None = None) -> None:
        """Warm-transfer a live call (§12.3), whispering context first."""

    @abstractmethod
    async def hangup(self, *, call_sid: str) -> None:
        """End a live call."""
