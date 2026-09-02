"""Post-generation validation of every LLM output (§16.3, §11.3).

§16.3 puts a validator between the model and the TTS. It rejects and regenerates
**once** if the output contains a price or quantity with no supporting tool
result this turn, uses ``तुम``, exceeds the length cap, makes a guarantee claim,
or contains English filler like "As an AI". Two failures fall back to a cached
safe phrase and escalate.

The check that matters most is the **grounding** one, and it is worth being
precise about what it can and cannot do. §1 N1 says every factual claim must
trace to a tool call. This validator cannot verify that a sentence is *true*; it
can verify that a number the agent is about to speak appeared in a tool result
this turn. That is a narrower claim and a much stronger one than asking a model
to self-report its confidence: a price the model invented has no matching tool
result, and no amount of fluency changes that.

It is a *last* line, not the only one. The tools refuse to answer without data,
the prompt says not to guess, and this catches what gets through. A validator
doing all the work would mean the layers above it were not.

Numbers are compared after Devanagari-digit folding and thousands separators are
stripped, because the model may write ``1,350`` where the tool returned ``1350``
and ``१३५०`` where it returned ``1350``. A validator that failed on those would
be rejected constantly for being right.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

from ..text.script import whole_word

log = structlog.get_logger(__name__)

#: §11.3. Normal answers cap at 26 words; dosage instructions may run to ~60.
#: Words, not characters: Devanagari characters per word vary enormously and a
#: character cap would clip Hindi far earlier than English for the same content.
#: The prompt asks for fifteen to twenty; twenty-six -- two short sentences --
#: is where the validator steps in. On the streaming path that is a quiet
#: stop at the sentence boundary, never a regeneration.
MAX_WORDS = 26
MAX_WORDS_DOSAGE = 60

#: §16.3: reject and regenerate once. Two failures end in a cached phrase.
MAX_REGENERATIONS = 1

#: The cached phrase spoken when generation cannot be made safe. Not composed by
#: a model, for the same reason the safety script is not.
FALLBACK_PHRASE_KEY = "fallback.cannot_answer.hi"
FALLBACK_SCRIPT_HI = (
    "जी, यह जानकारी मेरे पास पक्की नहीं है। "
    "मैं आपको हमारे केंद्र प्रबंधक से जोड़ देता हूँ।"
)

#: §11.3: always आप, never तुम. The informal second person from a stranger in a
#: service call is not casual, it is rude, and in central UP it lands as
#: talking down to the caller.
_TUM = re.compile(whole_word("तुम|तुमने|तुम्हें|तुम्हारा|तुम्हारी|तुमको|तेरा|तेरी|तुझे"))

#: §16.2 and §11.3: no yield, profit or guarantee claims. A promise of doubled
#: output is both unprovable and, from a seller, close to mis-selling.
_GUARANTEE = re.compile(
    whole_word(
        "गारंटी|गॅरंटी|दोगुना|दुगुना|दोगुनी|पक्का फ़ायदा|पक्का फायदा|"
        "guarantee|guaranteed|double your|will definitely"
    ),
    re.IGNORECASE,
)
#: "मुनाफ़ा"/"पैदावार" are legitimate words; only a *promise* about them is not.
_YIELD_PROMISE = re.compile(
    r"(पैदावार|उपज|मुनाफ़ा|मुनाफा|yield|profit)[^।.!?]{0,40}"
    r"(बढ़ जाएगी|बढ़ जाएगा|दोगुनी|दोगुना|guaranteed|will increase)",
    re.IGNORECASE,
)

#: §16.3: no "As an AI" filler. Disclosure when *asked* is required (§11.3) and
#: has its own phrasing; this catches the model volunteering a disclaimer in
#: English in the middle of a Hindi call.
_AI_FILLER = re.compile(
    r"\b(as an ai|as a language model|i'?m an ai|i am an ai|i cannot|"
    r"i'?m sorry,? but i)\b",
    re.IGNORECASE,
)

#: A number the agent is about to speak. Devanagari digits included -- the model
#: will produce them in Hindi output and a Latin-only pattern would skip exactly
#: the prices this check exists for.
# noqa RUF001: the Devanagari digits are the point. The linter flags ० as
# confusable with a Latin o, which is true and is exactly why the range has to
# be written out rather than left to \d.
_NUMBER = re.compile(r"[0-9०-९][0-9०-९,.]*")  # noqa: RUF001

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

#: Numbers that are never a factual claim about stock. A year, a phone number
#: read back, a percentage in a composition the tool did return, and the small
#: counting numbers that appear in ordinary speech ("एक मिनट", "दो बात").
_ALWAYS_ALLOWED = frozenset({"1", "2", "3", "4", "5", "6", "7", "8", "9", "10"})


def _fold(number: str) -> str:
    """``१,३५०`` and ``1,350`` both become ``1350``.

    Trailing ``.00`` is dropped too: a tool returns ``Decimal('1350.00')`` and
    the model says ``1350``, and those are the same price.
    """
    plain = number.translate(_DEVANAGARI_DIGITS).replace(",", "").strip(".")
    if "." in plain:
        whole, _, frac = plain.partition(".")
        if frac.strip("0") == "":
            return whole
    return plain


@dataclass(frozen=True, slots=True)
class Violation:
    """One rule the output broke."""

    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.rule}: {self.detail}"


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    """Whether this output may be spoken."""

    ok: bool
    violations: tuple[Violation, ...] = ()

    @property
    def rules(self) -> tuple[str, ...]:
        return tuple(v.rule for v in self.violations)

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class OutputValidator:
    """§16.3's post-generation validator."""

    max_words: int = MAX_WORDS
    max_words_dosage: int = MAX_WORDS_DOSAGE

    def validate(
        self,
        text: str,
        *,
        tool_results: Sequence[Mapping[str, Any]] = (),
        is_dosage: bool = False,
    ) -> ValidationOutcome:
        """Check one candidate response.

        Args:
            text: What the model produced, before TTS.
            tool_results: Every tool result from **this turn**. Not the call --
                §16.2 forbids quoting a price that did not come from a tool call
                in this turn, because a price fetched four turns ago may have
                changed and the caller cannot tell the difference.
            is_dosage: Relaxes the length cap to §11.3's 60 words. Dosage
                answers carry a pre-harvest interval and a precaution, and
                clipping either is a §16.2 violation in the other direction.
        """
        violations: list[Violation] = []
        stripped = text.strip()

        if not stripped:
            return ValidationOutcome(
                ok=False, violations=(Violation("empty", "no text to speak"),)
            )

        if _TUM.search(stripped):
            violations.append(Violation("register", "uses तुम; §11.3 requires आप"))
        # "सर" is not rejected here. It used to be, and every reply that said
        # it cost a second generation inside the caller's turn. The customer
        # wants it -- once or twice a call, the way a shopkeeper says it --
        # and that is a budget, not a ban: `flow.address` enforces the count.

        cap = self.max_words_dosage if is_dosage else self.max_words
        words = len(stripped.split())
        if words > cap:
            violations.append(Violation("length", f"{words} words over a cap of {cap}"))

        if _GUARANTEE.search(stripped) or _YIELD_PROMISE.search(stripped):
            violations.append(
                Violation("guarantee", "makes a yield, profit or guarantee claim (§16.2)")
            )
        if _AI_FILLER.search(stripped):
            violations.append(Violation("filler", "contains English AI filler (§16.3)"))

        ungrounded = self._ungrounded_numbers(stripped, tool_results)
        if ungrounded:
            violations.append(
                Violation(
                    "ungrounded",
                    f"states {', '.join(sorted(ungrounded))} with no tool result this turn",
                )
            )

        if violations:
            # The text itself is not logged: it may contain the caller's name,
            # village or order reference (§19, PII redacted at the logger).
            log.info("validator.rejected", rules=[v.rule for v in violations])
        return ValidationOutcome(ok=not violations, violations=tuple(violations))

    def _ungrounded_numbers(
        self, text: str, tool_results: Sequence[Mapping[str, Any]]
    ) -> set[str]:
        """Numbers in the output that no tool result this turn supports.

        The core §1 N1 check. It cannot tell whether a sentence is true, but it
        can tell whether a figure the agent is about to speak came from
        anywhere -- and an invented price has no matching tool result no matter
        how confidently it is phrased.
        """
        spoken = {_fold(m.group(0)) for m in _NUMBER.finditer(text)}
        spoken -= _ALWAYS_ALLOWED
        spoken.discard("")
        if not spoken:
            return set()

        grounded = {_fold(n) for n in _numbers_in(tool_results)}
        return spoken - grounded


def _numbers_in(value: Any) -> Iterable[str]:
    """Every number anywhere in a tool result, at any depth.

    Walks the whole structure rather than reading named fields: a tool may nest
    a price under ``alternatives[0].price`` or return it as part of a formatted
    string like ``"1350 प्रति बोरी"``, and a validator that only understood the
    shapes it was told about would reject correct answers whenever a tool grew
    a field.
    """
    if isinstance(value, str):
        yield from (m.group(0) for m in _NUMBER.finditer(value))
    elif isinstance(value, int | float):
        yield _fold(str(value))
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _numbers_in(item)
    elif isinstance(value, Sequence):
        for item in value:
            yield from _numbers_in(item)
    elif value is not None:
        yield from (m.group(0) for m in _NUMBER.finditer(str(value)))


@dataclass
class ValidatedGeneration:
    """Generate, validate, retry once, then fall back (§16.3).

    The retry is not a loop. §16.3 allows exactly one regeneration, and the
    reason is latency: a second retry would put a third LLM round trip inside a
    turn that §7 gives 1,500 ms end to end. Two failures mean the model is not
    going to produce something speakable for this input, and a cached phrase
    plus an escalation serves the caller better than a third attempt.
    """

    validator: OutputValidator = field(default_factory=OutputValidator)
    max_regenerations: int = MAX_REGENERATIONS

    async def run(
        self,
        generate: Any,
        *,
        tool_results: Sequence[Mapping[str, Any]] = (),
        is_dosage: bool = False,
    ) -> tuple[str, ValidationOutcome, bool]:
        """Produce a speakable answer.

        Args:
            generate: Called with ``None`` first, then with the violation text
                so the second attempt knows what to fix. Feeding the reason back
                matters: a bare "try again" regenerates the same class of
                output, and the one retry available is then wasted.

        Returns:
            ``(text, outcome, escalate)``. When ``escalate`` is set the text is
            the cached fallback script and the caller must be handed to a human
            -- §11.4 does not allow this to end in silence.
        """
        feedback: str | None = None
        outcome = ValidationOutcome(ok=False)
        text = ""

        for attempt in range(self.max_regenerations + 1):
            text = await generate(feedback)
            outcome = self.validator.validate(
                text, tool_results=tool_results, is_dosage=is_dosage
            )
            if outcome.ok:
                return text, outcome, False
            feedback = "; ".join(str(v) for v in outcome.violations)
            log.info("validator.regenerating", attempt=attempt + 1, rules=outcome.rules)

        log.warning("validator.exhausted", rules=outcome.rules)
        return FALLBACK_SCRIPT_HI, outcome, True


__all__ = (
    "FALLBACK_PHRASE_KEY",
    "FALLBACK_SCRIPT_HI",
    "MAX_REGENERATIONS",
    "MAX_WORDS",
    "MAX_WORDS_DOSAGE",
    "OutputValidator",
    "ValidatedGeneration",
    "ValidationOutcome",
    "Violation",
)
