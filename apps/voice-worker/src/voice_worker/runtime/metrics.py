"""Per-turn latency measurement against the §7 budget.

§7 defines one number that matters: **end-of-user-speech to the first byte of
agent audio reaching the telephony provider**. Everything else is a breakdown of
where that number went.

Measured, not estimated. §7 makes the p95 a build-failing gate, and a gate you
cannot measure is a wish. Each segment is timestamped as it happens and the turn
is scored against the budget when it closes, so a regression shows up as a
failing test rather than as a farmer waiting.

One subtlety worth stating: the clock starts at **end of speech**, not at the
turn-commit decision. On the Flux path those differ by the eager-EOT overlap,
which is the whole point of §5.2 -- the LLM has already been running. Starting
the clock at commit would flatter the measurement by exactly the amount of the
optimisation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from statistics import median

import structlog

from uaagro_domain.settings import LatencyBudget

log = structlog.get_logger(__name__)


class Segment(StrEnum):
    """The §7 breakdown. Names match the budget table exactly."""

    NETWORK_IN = "network_in"
    TURN_COMMIT = "turn_commit"
    STT_FINAL = "stt_final"
    TOOL_EXECUTION = "tool_execution"
    LLM_TTFT = "llm_ttft"
    TTS_TTFB = "tts_ttfb"
    NETWORK_OUT = "network_out"


@dataclass
class TurnMetrics:
    """Timings for one turn.

    Marks are absolute perf-counter readings; segments are derived from them at
    the end so a missing mark shows up as an absent segment rather than as a
    plausible-looking zero.
    """

    turn_index: int
    #: End of the caller's speech. The clock everything is measured from.
    speech_ended_at: float | None = None
    #: The eager end-of-turn signal, where the recogniser offers one (§5.2).
    eager_at: float | None = None
    #: The turn commit.
    committed_at: float | None = None
    #: Final transcript in hand.
    transcript_at: float | None = None
    tool_started_at: float | None = None
    tool_finished_at: float | None = None
    generation_started_at: float | None = None
    first_token_at: float | None = None
    synthesis_started_at: float | None = None
    first_audio_at: float | None = None
    audio_sent_at: float | None = None

    used_tools: bool = False
    #: True when the answer came from the §9 Tier-3 cache, skipping the model
    #: and often the synthesiser too. Reported separately because a cached turn
    #: is not evidence the live path meets the budget.
    from_cache: bool = False
    speculative_hit: bool = False
    speculative_discarded: bool = False
    #: True when the turn was not generated at all: the farmer interrupted,
    #: said something that was only a listener's "हाँ", and the agent picked
    #: up the rest of what it had been saying.
    resumed: bool = False

    def mark(self, field_name: str, at: float | None = None) -> None:
        setattr(self, field_name, at if at is not None else time.perf_counter())

    # -- derived ---------------------------------------------------------- #

    @property
    def total_ms(self) -> float | None:
        """End of speech to first agent audio out. The §7 headline."""
        if self.speech_ended_at is None or self.first_audio_at is None:
            return None
        return (self.first_audio_at - self.speech_ended_at) * 1000

    @property
    def eager_lead_ms(self) -> float | None:
        """How far ahead of the commit the eager signal fired.

        This is the §5.2 saving, made visible. A lead of zero across a call
        means eager mode is configured but never firing, which is worth
        knowing before concluding the budget cannot be met.
        """
        if self.eager_at is None or self.committed_at is None:
            return None
        return (self.committed_at - self.eager_at) * 1000

    def segments(self) -> dict[str, float]:
        """Per-segment durations, omitting any that were not measured."""
        out: dict[str, float] = {}
        pairs = (
            (Segment.TURN_COMMIT, self.speech_ended_at, self.committed_at),
            (Segment.STT_FINAL, self.committed_at, self.transcript_at),
            (Segment.TOOL_EXECUTION, self.tool_started_at, self.tool_finished_at),
            (Segment.LLM_TTFT, self.generation_started_at, self.first_token_at),
            (Segment.TTS_TTFB, self.synthesis_started_at, self.first_audio_at),
            (Segment.NETWORK_OUT, self.first_audio_at, self.audio_sent_at),
        )
        for name, start, end in pairs:
            if start is not None and end is not None and end >= start:
                out[name.value] = round((end - start) * 1000, 1)
        return out

    def to_dict(self) -> dict[str, object]:
        """Written to ``call_turns.latency_ms`` (§10)."""
        payload: dict[str, object] = {"turn_index": self.turn_index, **self.segments()}
        if self.total_ms is not None:
            payload["total"] = round(self.total_ms, 1)
        if self.eager_lead_ms is not None:
            payload["eager_lead"] = round(self.eager_lead_ms, 1)
        payload["used_tools"] = self.used_tools
        payload["from_cache"] = self.from_cache
        if self.speculative_hit:
            payload["speculative_hit"] = True
        if self.speculative_discarded:
            payload["speculative_discarded"] = True
        return payload

    def budget_breaches(self, budget: LatencyBudget) -> list[str]:
        """Segments that exceeded their §7 p95 ceiling.

        Returned rather than raised: one slow turn is not a failure, and it is
        the distribution over a call that §7 gates on.
        """
        ceilings = {
            Segment.TURN_COMMIT.value: budget.turn_commit.p95,
            Segment.STT_FINAL.value: budget.stt_final.p95,
            Segment.TOOL_EXECUTION.value: budget.tool_execution.p95,
            Segment.LLM_TTFT.value: budget.llm_ttft.p95,
            Segment.TTS_TTFB.value: budget.tts_ttfb.p95,
            Segment.NETWORK_OUT.value: budget.network_out.p95,
        }
        breaches = [
            f"{name} {value:.0f}ms > {ceilings[name]}ms"
            for name, value in self.segments().items()
            if name in ceilings and value > ceilings[name]
        ]

        total = self.total_ms
        if total is not None:
            ceiling = budget.total_with_tool.p95 if self.used_tools else budget.total_no_tool.p95
            if total > ceiling:
                label = "with tool" if self.used_tools else "no tool"
                breaches.append(f"total ({label}) {total:.0f}ms > {ceiling}ms")
        return breaches


@dataclass
class CallLatency:
    """Latency across a whole call, summarised for ``calls.latency_stats``."""

    turns: list[TurnMetrics] = field(default_factory=list)

    def add(self, turn: TurnMetrics) -> None:
        self.turns.append(turn)

    def _totals(self, *, exclude_cached: bool = True) -> list[float]:
        return [
            t.total_ms
            for t in self.turns
            if t.total_ms is not None and not (exclude_cached and t.from_cache)
        ]

    def summary(self) -> dict[str, object]:
        """The §10 ``latency_stats`` payload.

        Cached turns are excluded from the headline percentiles and counted
        separately: a Tier-3 cache hit answers in under 100 ms and would drag
        the distribution down until it no longer described the live path.
        """
        live = sorted(self._totals())
        cached = len([t for t in self.turns if t.from_cache])

        payload: dict[str, object] = {
            "turn_count": len(self.turns),
            "cached_turns": cached,
            "tool_turns": len([t for t in self.turns if t.used_tools]),
            "speculative_hits": len([t for t in self.turns if t.speculative_hit]),
            "speculative_discards": len([t for t in self.turns if t.speculative_discarded]),
        }
        if live:
            payload |= {
                "p50": round(median(live), 1),
                "p95": round(percentile(live, 95), 1),
                "max": round(max(live), 1),
            }
            payload["per_segment"] = self._segment_medians()
        return payload

    def _segment_medians(self) -> dict[str, float]:
        collected: dict[str, list[float]] = {}
        for turn in self.turns:
            if turn.from_cache:
                continue
            for name, value in turn.segments().items():
                collected.setdefault(name, []).append(value)
        return {name: round(median(values), 1) for name, values in collected.items()}

    def breaches(self, budget: LatencyBudget) -> dict[str, object]:
        """Whether this call met §7, and where it did not."""
        live = sorted(self._totals())
        if not live:
            return {"measured": False}
        p95 = percentile(live, 95)
        ceiling = budget.total_no_tool.p95
        return {
            "measured": True,
            "p95_ms": round(p95, 1),
            "ceiling_ms": ceiling,
            "within_budget": p95 <= ceiling,
            "per_turn": [b for turn in self.turns for b in turn.budget_breaches(budget)],
        }


def percentile(sorted_values: list[float], p: float) -> float:
    """Nearest-rank percentile.

    Nearest-rank rather than interpolated: with the handful of turns in a
    typical call, interpolation invents a value between two real measurements
    and reports it as the p95.
    """
    if not sorted_values:
        raise ValueError("percentile of an empty sample")
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = max(1, min(len(sorted_values), round(p / 100 * len(sorted_values) + 0.5)))
    return sorted_values[rank - 1]
