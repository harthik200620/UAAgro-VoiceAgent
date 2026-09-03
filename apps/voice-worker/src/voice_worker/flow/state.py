"""The inbound call state machine (§11.1) and its failure handling (§11.4).

::

    INIT ─▶ IDENTIFY ─▶ SCREEN ─▶ GREET ─▶ LANG_LOCK ─▶ DISCOVER ⇄ RESOLVE ─▶ WRAP ─▶ END
               │                                │            │
               └─▶ REJECT                       └────────────┴─▶ ESCALATE ─▶ TRANSFER
                                                                     │
                                                                     └─▶ CALLBACK

Modelled explicitly rather than as flags on a call object because §11.4's
central promise -- *nothing dead-ends* -- is a statement about reachability, and
reachability is a property of a graph. Written as booleans it is a claim nobody
can check; written as transitions, :func:`unreachable_terminals` checks it and a
test asserts it on every run.

**IDENTIFY runs beside the greeting, never before it.** §11.1 gives it 150 ms
and marks it explicitly non-blocking. A farmer hearing 150 ms of silence before
"नमस्ते" has already learned this is a machine; the greeting is cached audio and
goes out at ~50 ms, and the lookup lands during it. If it is slow or fails, the
call proceeds with an unknown caller, which is a worse greeting and a working
one.

**SCREEN's thresholds are loose on purpose.** §11.1: false positives on a farmer
helpline are expensive. A wrongly rejected caller does not complain to the
system, they simply stop calling, so the failure is invisible from the inside.
Every rejection is logged with its reason and reversible in the panel.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

import structlog

from uaagro_domain.enums import CallOutcome, Intent

log = structlog.get_logger(__name__)


class CallState(StrEnum):
    """§11.1."""

    INIT = "init"
    IDENTIFY = "identify"
    SCREEN = "screen"
    GREET = "greet"
    LANG_LOCK = "lang_lock"
    DISCOVER = "discover"
    RESOLVE = "resolve"
    WRAP = "wrap"
    END = "end"
    REJECT = "reject"
    ESCALATE = "escalate"
    TRANSFER = "transfer"
    CALLBACK = "callback"


#: Where a call can legitimately finish. Everything else must reach one of
#: these, and :func:`unreachable_terminals` proves it does.
TERMINAL: frozenset[CallState] = frozenset(
    {CallState.END, CallState.REJECT, CallState.TRANSFER, CallState.CALLBACK}
)

#: The graph of §11.1. ESCALATE deliberately has no edge back into the
#: conversation: once the agent has decided it cannot help, returning the caller
#: to the loop that already failed them is the behaviour §12 exists to prevent.
TRANSITIONS: dict[CallState, frozenset[CallState]] = {
    CallState.INIT: frozenset({CallState.IDENTIFY}),
    CallState.IDENTIFY: frozenset({CallState.SCREEN}),
    CallState.SCREEN: frozenset({CallState.GREET, CallState.REJECT}),
    CallState.GREET: frozenset({CallState.LANG_LOCK}),
    CallState.LANG_LOCK: frozenset({CallState.DISCOVER, CallState.ESCALATE}),
    CallState.DISCOVER: frozenset({CallState.RESOLVE, CallState.ESCALATE, CallState.WRAP}),
    CallState.RESOLVE: frozenset({CallState.DISCOVER, CallState.ESCALATE, CallState.WRAP}),
    CallState.WRAP: frozenset({CallState.END, CallState.DISCOVER}),
    # §12.3-6: when the chain is exhausted the caller gets a commitment, not a
    # ring-out. Both edges exist so neither outcome is a dead end.
    CallState.ESCALATE: frozenset({CallState.TRANSFER, CallState.CALLBACK}),
    CallState.TRANSFER: frozenset({CallState.END}),
    CallState.CALLBACK: frozenset({CallState.END}),
    CallState.REJECT: frozenset(),
    CallState.END: frozenset(),
}

#: §11.4's silence ladder, in seconds.
SILENCE_PROMPT_S = 6.0
SILENCE_WARN_S = 15.0
SILENCE_CLOSE_S = 25.0

#: §11.4: escalate after three consecutive low-confidence turns, or after the
#: same intent has failed twice. Both are "the agent is not getting there", and
#: a fourth attempt is not going to be the one that works.
LOW_CONFIDENCE_LIMIT = 3
REPEATED_INTENT_LIMIT = 2

#: §11.3: hold silently for up to this long when the caller asks for a moment,
#: then say one gentle line. Longer than it looks -- a farmer putting the phone
#: down to check a fertiliser bag takes about this.
HOLD_PATIENCE_S = 25.0


class InvalidTransition(RuntimeError):
    """An edge that §11.1 does not have."""

    def __init__(self, source: CallState, target: CallState) -> None:
        super().__init__(f"{source.value} -> {target.value} is not a valid transition")
        self.source = source
        self.target = target


@dataclass
class CallFlow:
    """One call's position in §11.1, and the counters §11.4 escalates on."""

    state: CallState = CallState.INIT
    history: list[CallState] = field(default_factory=lambda: [CallState.INIT])

    #: Consecutive turns below the STT confidence floor (§11.4).
    low_confidence_streak: int = 0
    #: How many times each intent has been attempted without resolving.
    intent_attempts: dict[Intent, int] = field(default_factory=dict)
    #: Set once the caller has been told what will happen next, so §11.4's
    #: "nothing dead-ends" can be asserted rather than assumed.
    commitment_spoken: bool = False
    escalation_reason: str | None = None

    def can(self, target: CallState) -> bool:
        return target in TRANSITIONS[self.state]

    def to(self, target: CallState, *, reason: str | None = None) -> CallState:
        """Move, or refuse.

        Refusing loudly rather than tolerating an unknown edge: a call that
        slides from GREET to TRANSFER without passing ESCALATE has skipped the
        step that decides whether a transfer is even possible, and the caller
        hears a ring-out instead of a commitment.
        """
        if not self.can(target):
            raise InvalidTransition(self.state, target)
        log.info(
            "flow.transition",
            **{"from": self.state.value},
            to=target.value,
            reason=reason,
        )
        self.state = target
        self.history.append(target)
        if reason and target is CallState.ESCALATE:
            self.escalation_reason = reason
        return target

    # -- §11.4 counters ---------------------------------------------------- #

    def record_turn(self, intent: Intent, *, confident: bool, resolved: bool) -> str | None:
        """Update the escalation counters. Returns a reason when one trips.

        Returning the reason rather than escalating here keeps this a decision
        and lets the caller sequence it -- the agent still has to *say*
        something before being handed over, and §12.3 requires the caller be
        told first.
        """
        self.low_confidence_streak = 0 if confident else self.low_confidence_streak + 1
        if self.low_confidence_streak >= LOW_CONFIDENCE_LIMIT:
            return "low_recognition_confidence"

        if resolved:
            self.intent_attempts.pop(intent, None)
            return None

        attempts = self.intent_attempts.get(intent, 0) + 1
        self.intent_attempts[intent] = attempts
        if attempts >= REPEATED_INTENT_LIMIT:
            return "repeated_misunderstanding"
        return None

    def silence_action(self, silent_for: float) -> str | None:
        """§11.4's silence ladder.

        A farmer on a rural line is often not silent because they have gone --
        they are walking to the shed to read a label, or the network dropped a
        second of audio. The ladder prompts twice before closing, and closing is
        recorded as ``abandoned_silence`` rather than as a failure.
        """
        if silent_for >= SILENCE_CLOSE_S:
            return "close"
        if silent_for >= SILENCE_WARN_S:
            return "warn"
        if silent_for >= SILENCE_PROMPT_S:
            return "prompt"
        return None


def unreachable_terminals(start: CallState = CallState.INIT) -> frozenset[CallState]:
    """States from which no terminal state is reachable.

    §11.4's "nothing dead-ends" as something checkable. Any state returned here
    is one where a call can arrive and never legitimately finish -- the caller
    is left on an open line with no answer, no human and no commitment. The test
    asserts this is empty, so adding a state without wiring its exits fails the
    build rather than a call.
    """
    reachable = _reachable_from(start)
    return frozenset(s for s in reachable if not _reaches_terminal(s))


def _reachable_from(start: CallState) -> set[CallState]:
    seen: set[CallState] = set()
    frontier = [start]
    while frontier:
        state = frontier.pop()
        if state in seen:
            continue
        seen.add(state)
        frontier.extend(TRANSITIONS[state] - seen)
    return seen


def _reaches_terminal(state: CallState) -> bool:
    return bool(_reachable_from(state) & TERMINAL)


def outcome_for(state: CallState, *, resolved: bool = False) -> CallOutcome:
    """The §10 disposition a terminal state records.

    Kept here so the state machine and the call record cannot disagree about
    what happened -- a transfer written as ``resolved`` would inflate the
    containment rate §15 reports, which is the number the operator uses to
    judge whether the agent is working.
    """
    if state is CallState.TRANSFER:
        return CallOutcome.TRANSFERRED
    if state is CallState.CALLBACK:
        return CallOutcome.TICKET_CREATED
    if state is CallState.REJECT:
        return CallOutcome.REJECTED_SPAM
    if resolved:
        return CallOutcome.RESOLVED
    return CallOutcome.CALLER_HUNG_UP


def describe(path: Iterable[CallState]) -> str:
    """A call's path, for a log line or a test failure message."""
    return " -> ".join(s.value for s in path)


__all__ = (
    "HOLD_PATIENCE_S",
    "LOW_CONFIDENCE_LIMIT",
    "REPEATED_INTENT_LIMIT",
    "SILENCE_CLOSE_S",
    "SILENCE_PROMPT_S",
    "SILENCE_WARN_S",
    "TERMINAL",
    "TRANSITIONS",
    "CallFlow",
    "CallState",
    "InvalidTransition",
    "describe",
    "outcome_for",
    "unreachable_terminals",
)
