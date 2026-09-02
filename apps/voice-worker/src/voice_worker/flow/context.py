"""Context construction for the LLM turn (§6.2).

§6.2 gives a budget and a shape:

===============  =======================================================  ======
Block            Content                                                  Tokens
===============  =======================================================  ======
system           persona, rules, safety, tool contract                    ~900 (cached)
system           caller context: name, village, centre, orders, tickets    ~150
system           dynamic hint: season, active offers, stock-outs           ~120
history          last 8 turns verbatim, older ones as a rolling summary    ~400
tool results     this turn's only, schema-trimmed                          ~200
===============  =======================================================  ======

Target: under 2,000 input tokens per turn.

The number is not an aesthetic preference. At ~9 turns per call, every 500
tokens of avoidable prompt is 4,500 tokens per call, and the same content is
resent on every turn. §6.1 enables prompt caching precisely because the first
block is large and static *within* a call -- so the ordering here is
load-bearing: the cached prefix must come first and must be byte-identical
across turns, or the cache misses and the saving evaporates.

That is why the persona block takes no per-call substitutions. Putting the
caller's name in it would make it unique per call and uncacheable, so the name
lives in the second block where it belongs.

**Only this turn's tool results.** §16.2 forbids quoting a price that did not
come from a tool call in this turn, and carrying old results forward is exactly
how the model ends up with a stale one in front of it and no way to tell.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

from uaagro_domain.enums import Intent

log = structlog.get_logger(__name__)

#: §6.2's ceiling. Estimated, not tokenised -- see :func:`estimate_tokens`.
MAX_INPUT_TOKENS = 2000

#: §6.2: the last eight turns go in verbatim.
VERBATIM_TURNS = 8

#: §6.2: older turns become a rolling summary, refreshed every six turns. Not
#: every turn: the summary is itself an LLM call, and refreshing it per turn
#: would add a round trip to the budget it exists to protect.
SUMMARY_REFRESH_EVERY = 6

#: Per-block soft budgets from §6.2, used to decide what to trim first.
BUDGET = {
    "persona": 900,
    "caller": 150,
    "hint": 120,
    "history": 400,
    "tools": 200,
}


def estimate_tokens(text: str) -> int:
    """Approximate token count, script-aware.

    Shared with the chunker's estimator for the same reason: the budget is a
    soft target, and pulling a tokeniser into the turn path to be precise about
    a soft target would cost more than being 10% wrong about it.
    """
    from ..knowledge.chunking import estimate_tokens as estimate

    return estimate(text)


@dataclass(slots=True)
class Turn:
    """One exchange, as the model sees it."""

    role: str  # user | assistant
    text: str
    #: The farmer cut this reply short. Rendered with a marker so the model
    #: knows the caller did not hear the rest, and answers what interrupted
    #: it before finishing the thought -- if the thought is still wanted.
    interrupted: bool = False

    def render(self) -> str:
        if self.role == "user":
            return f"किसान: {self.text}"
        if self.interrupted:
            return f"सहायक (किसान ने बीच में टोका, आगे नहीं सुना): {self.text}"
        return f"सहायक: {self.text}"


@dataclass
class CallerContext:
    """§6.2's second block. What the agent already knows about this caller.

    Built once per call from ``lookup_farmer`` -- not per turn. It changes only
    when the farmer tells the agent something new, and rebuilding it every turn
    would spend a database round trip to produce the same 150 tokens.
    """

    name: str | None = None
    village: str | None = None
    district: str | None = None
    centre: str | None = None
    language: str = "hi-IN"
    land: str | None = None
    crops: Sequence[str] = ()
    recent_orders: Sequence[Mapping[str, Any]] = ()
    open_tickets: Sequence[Mapping[str, Any]] = ()

    def render(self) -> str:
        if self.name is None:
            # An unknown caller is the normal case on a helpline. Saying so
            # explicitly stops the model inventing a name to be friendly with.
            return "काॅलर: नया काॅलर, कोई रिकॉर्ड नहीं। नाम पूछकर ही इस्तेमाल करें।"

        lines = [f"काॅलर: {self.name}"]
        where = ", ".join(p for p in (self.village, self.district) if p)
        if where:
            lines.append(f"गाँव/ज़िला: {where}")
        if self.centre:
            lines.append(f"केंद्र: {self.centre}")
        if self.land:
            lines.append(f"रकबा: {self.land}")
        if self.crops:
            lines.append(f"फ़सलें: {', '.join(self.crops)}")
        if self.recent_orders:
            orders = "; ".join(
                f"{o.get('order_ref')} ({o.get('status')})" for o in self.recent_orders
            )
            lines.append(f"पिछले ऑर्डर: {orders}")
        if self.open_tickets:
            tickets = "; ".join(
                f"{t.get('ticket_ref')} ({t.get('type')})" for t in self.open_tickets
            )
            lines.append(f"खुले टिकट: {tickets}")
        return "\n".join(lines)


@dataclass
class DynamicHint:
    """§6.2's third block. What is true right now at this centre.

    Stock-outs are here rather than left to a tool call because knowing what is
    *not* available shapes the whole answer: an agent that learns urea is out
    only after quoting it has already made the caller's day worse.
    """

    season: str | None = None
    active_offers: Sequence[str] = ()
    stock_outs: Sequence[str] = ()

    def render(self) -> str:
        lines: list[str] = []
        if self.season:
            lines.append(f"मौसम: {self.season}")
        if self.active_offers:
            lines.append(f"चालू ऑफ़र: {', '.join(self.active_offers)}")
        if self.stock_outs:
            lines.append(
                f"इस केंद्र पर अभी उपलब्ध नहीं: {', '.join(self.stock_outs)} "
                "(कीमत या स्टॉक हमेशा टूल से पुष्टि करें)"
            )
        return "\n".join(lines)


@dataclass
class ConversationMemory:
    """The history block: recent turns verbatim, older ones summarised (§6.2)."""

    turns: list[Turn] = field(default_factory=list)
    summary: str = ""
    _turns_since_summary: int = 0

    def add(self, role: str, text: str) -> None:
        self.turns.append(Turn(role=role, text=text))
        self._turns_since_summary += 1

    def mark_last_interrupted(self, heard: str) -> None:
        """§5.4 step 3: the model must believe it said only what was heard.

        The last assistant turn is cut down to the prefix the farmer actually
        got, and flagged. Without this the model references a price it never
        reached, or repeats itself because it thinks it was interrupted
        earlier than it was.
        """
        for turn in reversed(self.turns):
            if turn.role == "assistant":
                turn.text = heard.strip() or turn.text
                turn.interrupted = True
                return

    def extend_last_assistant(self, text: str) -> None:
        """The agent picked up where it was cut off: join the two halves."""
        for turn in reversed(self.turns):
            if turn.role == "assistant":
                turn.text = f"{turn.text} {text}".strip()
                turn.interrupted = False
                return
        self.add("assistant", text)

    def drop_last_user(self) -> None:
        """A turn the model was asked about and never answered.

        A speculative generation (§5.2) is cancelled when the farmer carries
        on speaking; its transcript was only part of what they said, and left
        in the history it would read as a question the agent ignored.
        """
        if self.turns and self.turns[-1].role == "user":
            self.turns.pop()
            self._turns_since_summary = max(0, self._turns_since_summary - 1)

    @property
    def needs_summary(self) -> bool:
        """§6.2: refresh every six turns, and only once there is something to
        summarise beyond what is already going in verbatim."""
        return (
            len(self.turns) > VERBATIM_TURNS
            and self._turns_since_summary >= SUMMARY_REFRESH_EVERY
        )

    def older_turns(self) -> list[Turn]:
        """The turns a summary would cover."""
        return self.turns[:-VERBATIM_TURNS] if len(self.turns) > VERBATIM_TURNS else []

    def set_summary(self, summary: str) -> None:
        self.summary = summary
        self._turns_since_summary = 0

    def render(self) -> str:
        recent = self.turns[-VERBATIM_TURNS:]
        parts: list[str] = []
        if self.summary:
            parts.append(f"[पहले की बातचीत का सार] {self.summary}")
        parts.extend(t.render() for t in recent)
        return "\n".join(parts)


def render_tool_results(results: Sequence[Mapping[str, Any]]) -> str:
    """§6.2's last block: this turn's results only, already trimmed.

    Trimming happens in the tools themselves -- they return the fields an answer
    would mention rather than a full row -- so this only serialises. A trimming
    step here would mean two places decide what the model sees, and they would
    drift.
    """
    if not results:
        return ""
    lines: list[str] = []
    for result in results:
        name = result.get("tool", "tool")
        if result.get("ok"):
            lines.append(f"{name}: {result.get('data')}")
        else:
            # Failures go in too. An agent told only about successes will
            # confidently answer a question whose lookup failed.
            lines.append(f"{name}: FAILED -- {result.get('error')}")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class BuiltContext:
    """The assembled prompt, with the accounting §6.2 asks for."""

    system_blocks: tuple[str, ...]
    user_message: str
    #: Estimated input tokens. Compared against :data:`MAX_INPUT_TOKENS`.
    tokens: int
    over_budget: bool
    #: The persona block, separately, because §6.1 caches it and the caller
    #: needs to mark it as the cacheable prefix.
    cacheable_prefix: str


@dataclass
class ContextBuilder:
    """Assembles §6.2's five blocks."""

    persona: str
    max_tokens: int = MAX_INPUT_TOKENS

    def build(
        self,
        *,
        transcript: str,
        caller: CallerContext | None = None,
        hint: DynamicHint | None = None,
        memory: ConversationMemory | None = None,
        tool_results: Sequence[Mapping[str, Any]] = (),
        intent: Intent | None = None,
        tool_hints: Sequence[str] = (),
    ) -> BuiltContext:
        """Build one turn's prompt.

        The persona block is emitted first and unmodified. §6.1's prompt caching
        keys on an exact prefix, so any per-call substitution in it would make
        every call a cache miss -- and the block is ~900 of the ~2,000 tokens,
        so that is most of the saving.
        """
        blocks: list[str] = [self.persona]

        if caller is not None:
            blocks.append(caller.render())
        if hint is not None:
            rendered = hint.render()
            if rendered:
                blocks.append(rendered)

        if intent is not None and intent is not Intent.UNKNOWN:
            line = f"इस टर्न का अनुमानित इरादा: {intent.value}"
            if tool_hints:
                # A hint, not an instruction. The allowlist on the agent config
                # decides what may actually be called (§10); this only saves
                # the model a round of exploration.
                line += f" (सुझाए गए टूल: {', '.join(tool_hints)})"
            blocks.append(line)

        history = memory.render() if memory is not None else ""
        if history:
            blocks.append(history)

        tools = render_tool_results(tool_results)
        if tools:
            blocks.append(f"[इस टर्न के टूल परिणाम]\n{tools}")

        tokens = sum(estimate_tokens(b) for b in blocks) + estimate_tokens(transcript)
        over = tokens > self.max_tokens
        if over:
            # Logged, not silently truncated. §6.2's budget is a design target,
            # and quietly dropping history to meet it would make the agent
            # forget mid-call for reasons nobody could see.
            log.warning(
                "context.over_budget", tokens=tokens, budget=self.max_tokens
            )

        return BuiltContext(
            system_blocks=tuple(blocks),
            user_message=transcript,
            tokens=tokens,
            over_budget=over,
            cacheable_prefix=self.persona,
        )


__all__ = (
    "BUDGET",
    "MAX_INPUT_TOKENS",
    "SUMMARY_REFRESH_EVERY",
    "VERBATIM_TURNS",
    "BuiltContext",
    "CallerContext",
    "ContextBuilder",
    "ConversationMemory",
    "DynamicHint",
    "Turn",
    "estimate_tokens",
    "render_tool_results",
)
