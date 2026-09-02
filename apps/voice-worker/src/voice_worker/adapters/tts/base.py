"""Speech-synthesis adapter interface (§4.1, §5.3).

Three properties the interface exists to enforce:

**Streaming.** §7 allows 350 ms at p95 for time-to-first-byte. Playback starts
on the first synthesised chunk, so :meth:`TTSService.synthesise` is an async
iterator rather than a coroutine returning bytes.

**No resampling.** §23-8 forbids it outright. The synthesiser is asked for
8 kHz in the telephony provider's own codec — linear16 for Exotel — which
deletes a resample stage from the hot path and removes the artefacts that make
an agent sound tinny on a phone line. An adapter that cannot emit the requested
format must fail loudly rather than convert quietly.

**Cancellation.** Barge-in has to stop generation *and* clear what the provider
has buffered (§5.4). This interface owns the first half; the telephony
serializer owns the second. Doing only one leaves the agent talking over the
farmer for another second, which is the failure everyone ships.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import TracebackType

from uaagro_domain.enums import AudioCodec


@dataclass(frozen=True, slots=True)
class TtsConfig:
    """How to synthesise one utterance.

    Built by the language router from the §5.1 table.
    """

    language: str
    model: str = "bulbul:v3"
    #: Chosen by the Phase 2 bake-off, not from documentation (§5.3).
    speaker: str | None = None
    #: §5.3: slightly slower than default, never faster.
    pace: float = 0.97
    sample_rate: int = 8000
    codec: AudioCodec = AudioCodec.LINEAR16


@dataclass(frozen=True, slots=True)
class TtsChunk:
    """One piece of synthesised audio."""

    audio: bytes
    #: True for the first chunk of an utterance, which is what the time-to-
    #: first-byte metric is measured to.
    is_first: bool = False
    #: True for the last chunk, so the caller knows playback is complete
    #: rather than merely paused.
    is_final: bool = False


class TTSService(ABC):
    """A streaming speech synthesiser."""

    provider: str

    @abstractmethod
    def synthesise(self, text: str, config: TtsConfig) -> AsyncIterator[TtsChunk]:
        """Stream audio for ``text``.

        ``text`` must already have been through
        :func:`voice_worker.text.speech.text_for_speech`. §5.3 forbids handing a
        raw catalogue string to the synthesiser, and an adapter is not the place
        to notice that -- by then the digits have already been read in English.

        Cancelling the iterator stops generation. Implementations must treat
        cancellation as normal control flow, not an error: it happens on every
        barge-in.
        """

    @abstractmethod
    async def close(self) -> None:
        """Release the connection."""

    async def synthesise_all(self, text: str, config: TtsConfig) -> bytes:
        """Collect a whole utterance.

        For the audio cache and for tests -- never for a live turn, where
        waiting for the last chunk throws away the entire streaming benefit.
        """
        chunks = [chunk.audio async for chunk in self.synthesise(text, config)]
        return b"".join(chunks)

    async def __aenter__(self) -> TTSService:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()


def characters_billed(text: str) -> int:
    """Characters a vendor will charge for.

    §8 makes TTS the largest controllable line item — larger than STT and far
    larger than the LLM — so this is counted per utterance and attributed to
    the call rather than estimated monthly.
    """
    return len(text)
