"""Turn-detection evaluation: false cuts and dead air (§19.2).

§19 asks for both rates per language, and is explicit that **false cuts are the
more damaging error** and should be weighted accordingly. That asymmetry is the
whole design of this module, so it is worth saying why it is true rather than
just encoding it.

A dead-air error costs the caller a second of silence. They wait, the agent
answers, the call continues. A false cut talks over a farmer mid-sentence --
and on a phone line, with no visual channel, the farmer does not know whether
they were heard. They start again. The agent, now answering the first half of a
question, gives a confidently wrong answer to something nobody asked. §11.3
makes interrupting the single most damaging register failure, and §5.2 calls an
orphaned generation that still speaks a severe bug.

So the composite score weights a false cut several times a dead-air event, and
the regression gate blocks on the false-cut rate rising even when the mean
looks better.

The evaluation is over **labelled events**, not audio. Each fixture says where
the speaker actually finished; the detector says where it thought they did. That
keeps this runnable without a recording studio and makes the arithmetic
checkable by hand.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

#: How much worse a false cut is than a dead-air wait, in the composite score.
#: Not tuned -- chosen from the asymmetry above and stated so a reader can
#: disagree with the number rather than reverse-engineer it.
FALSE_CUT_WEIGHT = 5.0

#: Silence beyond this, after the speaker has actually finished, is dead air.
#: §7 budgets 700 ms of endpoint silence before the recogniser finalises, so
#: this is that plus the turn the caller is waiting through.
DEAD_AIR_THRESHOLD_MS = 1_200.0

#: Cutting in this far *before* the speaker finished is a false cut. A few tens
#: of milliseconds either side of the true boundary is measurement noise, not
#: an interruption -- and treating it as one would make the rate a report on
#: the labelling rather than on the detector.
FALSE_CUT_TOLERANCE_MS = 120.0


class Verdict(StrEnum):
    CORRECT = "correct"
    FALSE_CUT = "false_cut"
    DEAD_AIR = "dead_air"


@dataclass(frozen=True, slots=True)
class TurnFixture:
    """One labelled turn boundary.

    ``true_end_ms`` is when the speaker actually stopped, measured from the
    start of their utterance. ``detected_end_ms`` is when the detector said so.
    """

    utterance_id: str
    language: str
    true_end_ms: float
    detected_end_ms: float
    condition: str = "clean"
    #: True when the speaker paused mid-sentence -- "एक मिनट रुकिए", a farmer
    #: turning to check a label. These are where detectors fail, and a corpus
    #: without them measures the easy case.
    has_mid_utterance_pause: bool = False


def judge(fixture: TurnFixture) -> Verdict:
    delta = fixture.detected_end_ms - fixture.true_end_ms
    if delta < -FALSE_CUT_TOLERANCE_MS:
        return Verdict.FALSE_CUT
    if delta > DEAD_AIR_THRESHOLD_MS:
        return Verdict.DEAD_AIR
    return Verdict.CORRECT


@dataclass
class LanguageResult:
    language: str
    total: int = 0
    false_cuts: int = 0
    dead_air: int = 0
    #: Latency past the true end, over the turns that were not false cuts.
    #: Averaging in the cuts would let interruptions improve the number.
    wait_ms: list[float] = field(default_factory=list)

    @property
    def false_cut_rate(self) -> float:
        return self.false_cuts / self.total if self.total else 0.0

    @property
    def dead_air_rate(self) -> float:
        return self.dead_air / self.total if self.total else 0.0

    @property
    def mean_wait_ms(self) -> float:
        return sum(self.wait_ms) / len(self.wait_ms) if self.wait_ms else 0.0

    @property
    def score(self) -> float:
        """Weighted error rate. Lower is better; 0 is perfect.

        A detector that never cuts anybody off but waits two seconds every turn
        scores worse than one that waits 600 ms and cuts one turn in a hundred
        -- which is the trade a helpline should make.
        """
        if not self.total:
            return 0.0
        return (FALSE_CUT_WEIGHT * self.false_cuts + self.dead_air) / self.total


@dataclass
class TurnReport:
    by_language: dict[str, LanguageResult] = field(default_factory=dict)

    @property
    def overall_false_cut_rate(self) -> float:
        total = sum(r.total for r in self.by_language.values())
        cuts = sum(r.false_cuts for r in self.by_language.values())
        return cuts / total if total else 0.0

    @property
    def overall_dead_air_rate(self) -> float:
        total = sum(r.total for r in self.by_language.values())
        dead = sum(r.dead_air for r in self.by_language.values())
        return dead / total if total else 0.0

    @property
    def worst_language(self) -> LanguageResult | None:
        """The language to fix next.

        §5.1 routes languages to different recognisers with different turn
        strategies, so a single mean is the average of things that do not share
        an implementation -- and it hides the tier-C language that is failing.
        """
        scored = [r for r in self.by_language.values() if r.total]
        return max(scored, key=lambda r: r.score) if scored else None

    def table(self) -> str:
        rows = ["language      n   false-cut  dead-air   mean wait   score"]
        for result in sorted(
            self.by_language.values(), key=lambda r: r.score, reverse=True
        ):
            rows.append(
                f"{result.language:<12} {result.total:>3} "
                f"{result.false_cut_rate:>9.1%} {result.dead_air_rate:>9.1%} "
                f"{result.mean_wait_ms:>9.0f}ms {result.score:>7.3f}"
            )
        return "\n".join(rows)


def evaluate(fixtures: Sequence[TurnFixture]) -> TurnReport:
    report = TurnReport()
    for fixture in fixtures:
        result = report.by_language.setdefault(
            fixture.language, LanguageResult(fixture.language)
        )
        result.total += 1
        verdict = judge(fixture)
        if verdict is Verdict.FALSE_CUT:
            result.false_cuts += 1
        else:
            if verdict is Verdict.DEAD_AIR:
                result.dead_air += 1
            result.wait_ms.append(fixture.detected_end_ms - fixture.true_end_ms)
    return report


@dataclass(frozen=True, slots=True)
class GateResult:
    passed: bool
    reasons: tuple[str, ...]


def regression_gate(
    baseline: TurnReport, candidate: TurnReport, *, tolerance: float = 0.005
) -> GateResult:
    """§19.5: a change that raises the false-cut rate blocks the merge.

    Asymmetric on purpose, and this is the part that matters. A candidate is
    allowed to trade *dead air* for a better composite score, because waiting
    less is what everyone wants. It is not allowed to buy that with more false
    cuts, in any language, even if the overall score improves -- because the
    overall score can improve while the one tier-C language the change was
    supposed to help gets worse.

    The tolerance absorbs a single fixture flipping in a small corpus. It does
    not absorb a trend.
    """
    reasons: list[str] = []

    for language, after in candidate.by_language.items():
        before = baseline.by_language.get(language)
        if before is None or not before.total:
            continue
        if after.false_cut_rate > before.false_cut_rate + tolerance:
            reasons.append(
                f"{language}: false cuts {before.false_cut_rate:.1%} -> "
                f"{after.false_cut_rate:.1%}"
            )

    if (
        candidate.overall_dead_air_rate
        > baseline.overall_dead_air_rate + max(tolerance, 0.05)
    ):
        # Dead air gets a looser bound than false cuts: it is a worse
        # experience, not a wrong answer.
        reasons.append(
            f"dead air {baseline.overall_dead_air_rate:.1%} -> "
            f"{candidate.overall_dead_air_rate:.1%}"
        )

    return GateResult(passed=not reasons, reasons=tuple(reasons))


__all__ = (
    "DEAD_AIR_THRESHOLD_MS",
    "FALSE_CUT_TOLERANCE_MS",
    "FALSE_CUT_WEIGHT",
    "GateResult",
    "LanguageResult",
    "TurnFixture",
    "TurnReport",
    "Verdict",
    "evaluate",
    "judge",
    "regression_gate",
)
