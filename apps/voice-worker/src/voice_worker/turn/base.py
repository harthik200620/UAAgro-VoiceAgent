"""Turn detection (§5.2).

Deciding when a farmer has finished speaking is the single most felt piece of
the system. Cut them off and the agent is rude; wait too long and it is slow.
§19.2 weights the two errors unequally on purpose — a false cut is the more
damaging one — and the thresholds here reflect that.

Four strategies, selected by the §5.1 language table. The first two are the
recogniser's own decision; the last two run locally in front of a recogniser
that only transcribes.

``soniox_endpoint``
    Soniox's semantic endpointing. Nothing to do here.

``flux_semantic``
    Deepgram Flux's fused end-of-turn. Nothing to do here either;
    :class:`DelegatedTurnDetector` exists so the pipeline has a uniform shape
    rather than an ``if``.

``smart_turn_v3``
    A local ONNX model over the waveform. Covers 23 languages including Hindi
    and Marathi.

``vad_silence``
    Silence endpointing, with a Hindi-aware grace period. The weakest option,
    used where no semantic model exists, and labelled tier C in the admin panel
    rather than presented as equivalent (§5.1).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum, auto

from uaagro_domain.enums import TurnStrategy

from ..text.script import words as script_words


class TurnDecision(StrEnum):
    #: The speaker is still going.
    CONTINUE = auto()
    #: Probably finished. Start the LLM speculatively (§5.2).
    EAGER_END = auto()
    #: Finished. Commit the turn.
    END = auto()


@dataclass(frozen=True, slots=True)
class TurnState:
    """Everything a detector may look at for one decision.

    Passed as a value rather than accumulated inside the detector so the same
    inputs always produce the same decision -- which is what makes the §19.2
    false-cut and dead-air evaluation reproducible.
    """

    #: Milliseconds of continuous silence since the last speech frame.
    silence_ms: float
    #: The transcript so far this turn. May be interim.
    transcript: str = ""
    #: Milliseconds since the caller started this turn.
    turn_duration_ms: float = 0.0
    #: Raw audio for this turn, for detectors that read the waveform.
    audio: bytes = b""
    #: Persisted on the farmer record after two observed long pauses (§5.2).
    slow_speaker: bool = False
    #: True when the recogniser has already applied its own VAD and decided
    #: this utterance ended -- a Sarvam ``transcript.final``. It means a real
    #: silence threshold has demonstrably elapsed, so a rule-based detector
    #: should defer to it rather than invent a second, longer wait and add
    #: dead air for no new information.
    recogniser_endpointed: bool = False


@dataclass(frozen=True, slots=True)
class TurnResult:
    decision: TurnDecision
    #: 0-1 where the detector produces one; ``None`` for rule-based detectors,
    #: which should not invent a number that looks like a model score.
    probability: float | None = None
    #: Why, in a form the call event log can carry. §12 tunes thresholds
    #: against real data, which needs the reason and not just the outcome.
    reason: str = ""


# --------------------------------------------------------------------------- #
# Hindi endpointing cues
# --------------------------------------------------------------------------- #

#: Words that mean the speaker is mid-sentence. §5.2 grants a trailing
#: conjunction or postposition another 400 ms before endpointing.
#:
#: Conjunctions carry the strongest signal: nobody ends a sentence on "और".
#: Postpositions are nearly as strong -- "बीघे में" is waiting for a verb.
TRAILING_PARTICLES: frozenset[str] = frozenset(
    {
        # conjunctions
        "और",
        "या",
        "तो",
        "लेकिन",
        "पर",
        "मगर",
        "क्योंकि",
        "अगर",
        "जब",
        "फिर",
        "इसलिए",
        "ताकि",
        "कि",
        # postpositions
        "का",
        "की",
        "के",
        "को",
        "में",
        "से",
        "पे",
        "तक",
        "पास",
        "लिए",
        "वाला",
        # hesitation
        "मतलब",
        "यानी",
        "जैसे",
        "वो",
        "वह",
        "ये",
        "यह",
    }
)

#: Words that usually *do* end a Hindi utterance, which shortens the wait.
TERMINAL_WORDS: frozenset[str] = frozenset(
    {"है", "हैं", "था", "थे", "थी", "चाहिए", "नहीं", "हाँ", "ठीक", "बस", "धन्यवाद", "जी"}
)


def last_word(transcript: str) -> str:
    """Final token, punctuation excluded.

    The danda lives inside the Devanagari letter block, so a naive class
    makes "चाहिए।" a different word from "चाहिए" and the particle check
    silently stops firing. See :mod:`voice_worker.text.script`.
    """
    tokens = script_words(transcript)
    return tokens[-1] if tokens else ""


def ends_mid_sentence(transcript: str) -> bool:
    """True when the transcript trails off on a particle."""
    return last_word(transcript) in TRAILING_PARTICLES


def ends_on_a_terminal_word(transcript: str) -> bool:
    return last_word(transcript) in TERMINAL_WORDS


# --------------------------------------------------------------------------- #
# Detectors
# --------------------------------------------------------------------------- #


class TurnDetector(ABC):
    """Decides whether the caller has finished speaking."""

    strategy: TurnStrategy

    @abstractmethod
    async def evaluate(self, state: TurnState) -> TurnResult:
        """Judge the current state. Called on each silence tick."""

    async def reset(self) -> None:
        """Forget per-turn state. Called at the start of every turn."""
        return None

    async def close(self) -> None:
        return None


class DelegatedTurnDetector(TurnDetector):
    """A detector for recognisers that decide for themselves (Flux, Soniox).

    Always returns CONTINUE: the real decision arrives as an STT event. Present
    so the pipeline has one shape for every language rather than branching on
    whether a detector exists.

    It still carries *which* recogniser is deciding, because §5.1 requires the
    admin panel to name the engine behind a language's quality tier. Reporting
    ``flux_semantic`` for a route Soniox serves would put a vendor's name
    against another vendor's behaviour, which is the kind of small inaccuracy
    that survives until someone debugs a turn-taking complaint with it.
    """

    def __init__(self, strategy: TurnStrategy = TurnStrategy.FLUX_SEMANTIC) -> None:
        self.strategy = strategy

    async def evaluate(self, state: TurnState) -> TurnResult:
        return TurnResult(
            decision=TurnDecision.CONTINUE,
            reason="recogniser emits its own end-of-turn events",
        )


@dataclass
class VadSilenceTurnDetector(TurnDetector):
    """Silence endpointing with Hindi-aware grace (§5.2).

    Deliberately generous. §3 is explicit that rural callers pause mid-sentence
    to think far more than a model's training distribution expects, and cutting
    them off is the commonest way an Indian voice agent feels rude. The
    thresholds here err toward waiting.
    """

    strategy: TurnStrategy = TurnStrategy.VAD_SILENCE
    #: §5.2 default, deliberately long.
    stop_secs: float = 0.85
    #: Extra grace when the transcript trails off on a conjunction.
    trailing_particle_grace_ms: int = 400
    #: Applied to callers observed to pause; §5.2 raises the backstop for them.
    slow_speaker_multiplier: float = 1.4
    #: Hard backstop, so a detector that never fires cannot hold a call open.
    max_silence_ms: float = 8000.0

    async def evaluate(self, state: TurnState) -> TurnResult:
        if state.silence_ms >= self.max_silence_ms:
            return TurnResult(TurnDecision.END, reason="backstop: maximum silence reached")

        # A trailing conjunction outranks everything short of the backstop: the
        # farmer is audibly mid-sentence, whatever the recogniser concluded.
        if ends_mid_sentence(state.transcript):
            threshold_ms = self.stop_secs * 1000 + self.trailing_particle_grace_ms
            if state.slow_speaker:
                threshold_ms *= self.slow_speaker_multiplier
            if state.silence_ms >= threshold_ms:
                return TurnResult(
                    TurnDecision.END, reason="silence, extended for a trailing particle"
                )
            return TurnResult(TurnDecision.CONTINUE, reason="trailing particle; still waiting")

        if state.recogniser_endpointed:
            # The recogniser's VAD already waited a real threshold. Waiting
            # again would be dead air bought with nothing.
            return TurnResult(TurnDecision.END, reason="recogniser endpointed the utterance")

        threshold_ms = self.stop_secs * 1000
        if state.slow_speaker:
            threshold_ms *= self.slow_speaker_multiplier
        reason = "silence"
        if ends_on_a_terminal_word(state.transcript):
            threshold_ms *= 0.8
            reason = "silence after a sentence-final word"

        if state.silence_ms >= threshold_ms:
            return TurnResult(TurnDecision.END, reason=reason)
        return TurnResult(TurnDecision.CONTINUE, reason="still within the silence window")


def build_particle_report(transcript: str) -> dict[str, object]:
    """Diagnostics for the §19.2 turn-detection evaluation."""
    return {
        "last_word": last_word(transcript),
        "ends_mid_sentence": ends_mid_sentence(transcript),
        "ends_terminal": ends_on_a_terminal_word(transcript),
    }
