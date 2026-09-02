"""Word error rate, per condition (§19.1).

§19 wants WER measured across clean Hindi, noisy Hindi, elderly and fast
speech, Bhojpuri- and Awadhi-inflected Hindi, code-mixing, Marathi, Malayalam,
phone numbers, quantities and product names -- and tracked per release, because
a headline WER hides exactly the condition that is getting worse.

**Normalisation is most of the work, and getting it wrong makes the number
meaningless.** Two Devanagari strings that a person would read aloud
identically compare unequal for reasons that have nothing to do with the
recogniser:

* Nukta composition. ``क़`` exists as one code point (U+0958) and as ``क`` +
  U+093C. Sarvam emits one, Deepgram the other, and a corpus transcribed by
  hand contains both. NFC converges them -- see :func:`normalise`, where
  the direction is the opposite of what it looks like.
* Zero-width joiners. Invisible, and they change nothing about the sound.
* Danda and Latin punctuation. ``।`` versus ``.`` is a transcription
  convention, not a recognition error.
* Digits. A recogniser that writes ``50`` where the reference says ``५०`` --
  or ``पचास`` -- heard the number correctly. Which of the three appears is a
  formatting decision downstream of recognition.

Counting those as errors inflates Hindi WER by a wide margin and, worse, makes
the inflation *uneven* across vendors -- so a bake-off measures transcription
conventions rather than accuracy. The normalisation here is deliberately
aggressive about form and conservative about content: it never merges two words
that sound different.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field

#: Devanagari digits, mapped to ASCII so ``५०`` and ``50`` compare equal.
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

#: Invisible marks that never change how a word sounds.
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍﻿"))

#: Punctuation that is a transcription convention rather than speech.
#:
#: Listed as plain characters and escaped by ``re``, rather than hand-written
#: as a character class. Half of these are regex metacharacters and two are
#: dashes indistinguishable from a hyphen in a diff; writing the class by hand
#: is how one silently stops matching.
_PUNCTUATION_CHARS = (
    "।॥"  # danda, double danda
    # En dash and em dash as escapes: in a diff they are indistinguishable
    # from the ASCII hyphen already in the class below, so a literal one can
    # be "cleaned up" into a duplicate and silently stop matching.
    "\u2013\u2014"
    ".,;:!?\"'`~()[]{}<>/\\|@#$%^&*_+=-"
)
_PUNCTUATION = re.compile(f"[{re.escape(_PUNCTUATION_CHARS)}]+")


def normalise(text: str) -> str:
    """Fold a transcript to the form WER should be measured on.

    NFC first, and it is worth being precise about what that does here, because
    the obvious reading is backwards. U+0958..U+095F -- the precomposed nukta
    letters -- are Unicode *composition exclusions*, so NFC does not build them:
    it **decomposes** ``क़`` into ``क`` + U+093C. Both spellings therefore
    converge on the decomposed form, which is all this needs. Reaching for NFD
    instead would work equally well for the nukta and would gratuitously split
    every other matra in the string.
    """
    folded = unicodedata.normalize("NFC", text)
    folded = folded.translate(_ZERO_WIDTH).translate(_DEVANAGARI_DIGITS)
    folded = _PUNCTUATION.sub(" ", folded)
    return " ".join(folded.lower().split())


def tokenise(text: str) -> list[str]:
    return normalise(text).split()


@dataclass(frozen=True, slots=True)
class Alignment:
    """One reference/hypothesis pair, scored."""

    substitutions: int
    deletions: int
    insertions: int
    reference_length: int

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    @property
    def wer(self) -> float:
        """Errors per reference word.

        Can exceed 1.0, and is left uncapped on purpose: a recogniser that
        hallucinates a sentence onto a two-word utterance has a WER above one,
        and clamping it to 100% would hide how badly.
        """
        if self.reference_length == 0:
            # An empty reference with any output is all insertions. Reporting
            # 0.0 would make a hallucination on silence look perfect.
            return 0.0 if self.insertions == 0 else float(self.insertions)
        return self.errors / self.reference_length


def align(reference: str, hypothesis: str) -> Alignment:
    """Levenshtein alignment over words.

    Two rows rather than a full matrix: a corpus is thousands of utterances and
    the full table is not needed once the counts are separated. The
    back-pointer is carried as a triple of counts, which is what lets
    substitutions, deletions and insertions be reported apart -- and they
    matter differently. Deletions mean the recogniser dropped words the farmer
    said; insertions mean it invented some.
    """
    ref = tokenise(reference)
    hyp = tokenise(hypothesis)

    # (cost, substitutions, deletions, insertions)
    previous: list[tuple[int, int, int, int]] = [(j, 0, 0, j) for j in range(len(hyp) + 1)]

    for i in range(1, len(ref) + 1):
        current: list[tuple[int, int, int, int]] = [(i, 0, i, 0)]
        for j in range(1, len(hyp) + 1):
            if ref[i - 1] == hyp[j - 1]:
                cost, subs, dels, ins = previous[j - 1]
                current.append((cost, subs, dels, ins))
                continue
            sub_cost, sub_s, sub_d, sub_i = previous[j - 1]
            del_cost, del_s, del_d, del_i = previous[j]
            ins_cost, ins_s, ins_d, ins_i = current[j - 1]
            best = min(sub_cost, del_cost, ins_cost)
            if best == sub_cost:
                current.append((best + 1, sub_s + 1, sub_d, sub_i))
            elif best == del_cost:
                current.append((best + 1, del_s, del_d + 1, del_i))
            else:
                current.append((best + 1, ins_s, ins_d, ins_i + 1))
        previous = current

    _, subs, dels, ins = previous[-1]
    return Alignment(
        substitutions=subs, deletions=dels, insertions=ins, reference_length=len(ref)
    )


@dataclass(frozen=True, slots=True)
class Utterance:
    """One corpus item.

    ``condition`` is the axis §19 wants WER reported along -- "noisy_hindi",
    "elderly", "code_mixed", "marathi". A corpus without it produces one number
    that cannot be acted on.
    """

    audio_path: str
    reference: str
    condition: str
    language: str = "hi-IN"
    speaker_id: str | None = None
    notes: str = ""


@dataclass
class ConditionResult:
    condition: str
    utterances: int = 0
    errors: int = 0
    reference_words: int = 0
    substitutions: int = 0
    deletions: int = 0
    insertions: int = 0

    @property
    def wer(self) -> float:
        if self.reference_words == 0:
            return 0.0
        return self.errors / self.reference_words


@dataclass
class WerReport:
    """WER overall and per condition."""

    by_condition: dict[str, ConditionResult] = field(default_factory=dict)

    @property
    def overall(self) -> float:
        words = sum(r.reference_words for r in self.by_condition.values())
        errors = sum(r.errors for r in self.by_condition.values())
        return errors / words if words else 0.0

    @property
    def worst(self) -> ConditionResult | None:
        """The condition to fix next.

        Reported because the headline number is not actionable: a corpus that
        is 80% clean Hindi averages away a recogniser that cannot hear a
        tractor, and the tractor is what a farmer calls from.
        """
        scored = [r for r in self.by_condition.values() if r.reference_words]
        return max(scored, key=lambda r: r.wer) if scored else None

    def add(self, condition: str, alignment: Alignment) -> None:
        result = self.by_condition.setdefault(condition, ConditionResult(condition))
        result.utterances += 1
        result.errors += alignment.errors
        result.reference_words += alignment.reference_length
        result.substitutions += alignment.substitutions
        result.deletions += alignment.deletions
        result.insertions += alignment.insertions

    def table(self) -> str:
        rows = ["condition            n    WER    sub   del   ins"]
        for result in sorted(
            self.by_condition.values(), key=lambda r: r.wer, reverse=True
        ):
            rows.append(
                f"{result.condition:<20} {result.utterances:>3} "
                f"{result.wer:>6.1%} {result.substitutions:>5} "
                f"{result.deletions:>5} {result.insertions:>5}"
            )
        rows.append(f"{'OVERALL':<20} {'':>3} {self.overall:>6.1%}")
        return "\n".join(rows)


def score(pairs: Sequence[tuple[Utterance, str]]) -> WerReport:
    """Score a corpus. ``pairs`` is ``(utterance, what the recogniser said)``."""
    report = WerReport()
    for utterance, hypothesis in pairs:
        report.add(utterance.condition, align(utterance.reference, hypothesis))
    return report


#: What §19.1 asks the corpus to cover. Listed as data so a corpus can be
#: checked for gaps rather than assumed complete -- and so the gap is
#: reportable before anybody trusts the number.
REQUIRED_CONDITIONS: tuple[str, ...] = (
    "clean_hindi",
    "noisy_hindi_tractor",
    "noisy_hindi_market",
    "noisy_hindi_wind",
    "elderly_slow",
    "fast_speech",
    "bhojpuri_inflected",
    "awadhi_inflected",
    "code_mixed",
    "marathi",
    "malayalam",
    "phone_numbers",
    "quantities",
    "product_names",
)

#: §19.1's floor. Below this the per-condition numbers are anecdotes.
MINIMUM_CORPUS_SIZE = 120


def missing_conditions(corpus: Sequence[Utterance]) -> tuple[str, ...]:
    present = {utterance.condition for utterance in corpus}
    return tuple(c for c in REQUIRED_CONDITIONS if c not in present)


__all__ = (
    "MINIMUM_CORPUS_SIZE",
    "REQUIRED_CONDITIONS",
    "Alignment",
    "ConditionResult",
    "Utterance",
    "WerReport",
    "align",
    "missing_conditions",
    "normalise",
    "score",
    "tokenise",
)
