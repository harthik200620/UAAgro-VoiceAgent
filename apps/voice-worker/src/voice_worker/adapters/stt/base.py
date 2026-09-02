"""Speech-recognition adapter interface (§4.1, §5.1).

Recognition and end-of-turn detection are **separable**, and §5 is explicit
about why that matters here. Deepgram Flux fuses them, which is what makes it
good — but Flux covers ten languages and Marathi and Malayalam are not among
them. So the interface models both shapes:

* a recogniser that emits its own turn decisions (Flux), and
* a recogniser that only transcribes, with a separate turn detector in front
  (Sarvam plus Smart Turn v3.1, or plus VAD).

:attr:`STTService.emits_turn_events` is how the pipeline knows which it has.
Getting that wrong means either two turn detectors fighting, or none at all.

Every implementation streams. §7 gives the whole recognition segment 200 ms at
p95, which is not a budget a batch API can meet.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum, auto
from types import TracebackType


class SttEventType(StrEnum):
    """What the recogniser is telling us."""

    #: Interim text. Unstable and may be revised; never acted on.
    PARTIAL = auto()
    #: Stable text for a stretch of speech. May still not be a complete turn.
    FINAL = auto()
    #: §5.2: the model believes the turn is *probably* over. Start the LLM
    #: speculatively here -- on the ~80% of turns that commit, this is where
    #: the 300 ms saving comes from.
    EAGER_END_OF_TURN = auto()
    #: The speaker carried on after an eager signal. Any speculative
    #: generation must be cancelled, and cancelled properly: an orphaned
    #: generation that still speaks is a severe bug (§5.2).
    TURN_RESUMED = auto()
    #: The turn is over. Commit.
    END_OF_TURN = auto()
    #: The recogniser detected the caller starting to speak. Drives barge-in.
    SPEECH_STARTED = auto()
    #: Recoverable trouble; the pipeline decides whether to fall back.
    ERROR = auto()


@dataclass(frozen=True, slots=True)
class SttEvent:
    """One event from the recogniser."""

    type: SttEventType
    text: str = ""
    #: How well the words were heard, 0-1. §11.4 escalates on a rolling mean
    #: below 0.55 over three turns.
    #:
    #: ``None`` when the recogniser does not report one, and it must stay
    #: ``None`` rather than being filled with a nearby-looking number. Sarvam
    #: reports ``language_confidence``, which is a different quantity: a
    #: confidently-identified language says nothing about whether the words
    #: were clear. Substituting it would silence the §11.4 escalation exactly
    #: on the noisy calls it exists for.
    confidence: float | None = None
    language: str | None = None
    #: How sure the recogniser is about *which language* was spoken. Drives
    #: §11.1 LANG_LOCK, never the confidence escalation.
    language_confidence: float | None = None
    #: Milliseconds from the audio arriving to this event being emitted.
    latency_ms: float | None = None
    detail: str = ""
    raw: dict[str, object] = field(default_factory=dict)

    @property
    def is_terminal_for_turn(self) -> bool:
        return self.type is SttEventType.END_OF_TURN


@dataclass(frozen=True, slots=True)
class SttConfig:
    """How to configure one recognition stream.

    Built by the language router from the §5.1 table, never hand-assembled at a
    call site -- that is how a language ends up silently on the wrong engine.
    """

    language: str
    #: Hints the recogniser biases toward. Flux takes ``[hi, en]`` for the
    #: Hindi path so code-mixed speech is not fought.
    language_hints: tuple[str, ...] = ()
    model: str = ""
    sample_rate: int = 8000
    #: §5.5: the catalogue lexicon, injected where the vendor supports keyterm
    #: boosting. Biasing the decode beats correcting it afterwards.
    keyterms: tuple[str, ...] = ()
    #: §5.2 thresholds. Only meaningful when ``emits_turn_events``.
    #:
    #: Flux takes a pair of probabilities; Soniox takes three dials and no
    #: eager probability at all. Both sets live here rather than in a
    #: per-vendor config object because the language router builds one of these
    #: from the §5.1 table without knowing which engine will read it, and an
    #: adapter ignoring a field it has no use for is cheaper than a second
    #: routing table that can disagree with the first.
    eager_eot_threshold: float = 0.45
    eot_threshold: float = 0.75
    eot_timeout_ms: int = 6000
    #: Soniox endpointing: 0-3, higher ends the turn sooner.
    endpoint_latency_adjustment_level: int = 0
    #: Soniox endpointing: -1.0 to 1.0, higher makes an endpoint more likely.
    endpoint_sensitivity: float = 0.0
    #: Soniox endpointing backstop, 500-3000 ms after speech stops.
    max_endpoint_delay_ms: int = 2000
    #: How long every-token-final may persist before the pipeline generates
    #: speculatively. Soniox's stand-in for Flux's eager EOT (§5.2).
    eager_after_final_ms: int = 160
    #: Set for callers flagged ``elderly_or_slow``. Rural callers pause
    #: mid-sentence far more than the model's training distribution expects,
    #: and cutting them off is the commonest way an Indian voice agent feels
    #: rude (§5.2).
    slow_speaker: bool = False


class STTService(ABC):
    """A streaming speech recogniser.

    Used as an async context manager so the socket is always closed, including
    when a call drops mid-turn:

    .. code-block:: python

        async with build_stt(config) as stt:
            async for event in stt.events():
                ...
    """

    #: Vendor name, for cost attribution and logging.
    provider: str
    #: True when this recogniser emits its own EOT events (Flux). False when a
    #: separate turn detector is required (Sarvam).
    emits_turn_events: bool

    @abstractmethod
    async def start(self, config: SttConfig) -> None:
        """Open the stream. Raises :class:`VendorUnavailableError` on failure."""

    @abstractmethod
    async def send_audio(self, pcm: bytes) -> None:
        """Feed 8 kHz linear16 audio. Must not block the audio loop (§7.6)."""

    @abstractmethod
    def events(self) -> AsyncIterator[SttEvent]:
        """Yield recogniser events until the stream closes."""

    @abstractmethod
    async def finalise(self) -> None:
        """Ask the recogniser to flush and emit any pending final transcript."""

    @abstractmethod
    async def close(self) -> None:
        """Close the stream and release the socket."""

    async def __aenter__(self) -> STTService:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
