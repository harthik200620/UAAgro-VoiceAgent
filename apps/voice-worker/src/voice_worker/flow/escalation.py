"""The escalation rule engine (§12.1, §12.2).

Two tiers with a real difference between them. §12.1 triggers transfer with **no
further agent turns**; §12.2 lets the agent try, then hands off when a threshold
is crossed. Getting a §12.1 trigger wrong in the §12.2 direction is the specific
failure this file exists to prevent -- the spec is emphatic about the one that
matters most:

    "One request is enough -- never negotiate, never 'let me try to help
    first'."

A farmer who has asked for a person has already judged the agent. Answering with
"मैं आपकी मदद कर सकता हूँ" is the behaviour that makes people hate IVRs, and it
is one line of well-intentioned code away at all times. So the immediate
triggers return :attr:`EscalationDecision.immediate` and the caller has no path
that reads it as advisory.

The counters for §12.2 live on :class:`~voice_worker.flow.state.CallFlow`, which
already tracks them for §11.4; this module reads them rather than keeping a
second copy that could disagree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import pairwise

import structlog

from uaagro_domain.enums import Intent, TransferReason, TransferUrgency

from ..text.script import whole_word

log = structlog.get_logger(__name__)

#: §12.2: rolling mean ASR confidence below this over three turns.
LOW_CONFIDENCE_MEAN = 0.55
CONFIDENCE_WINDOW = 3

#: §12.2: sentiment declining across three turns and below this.
SENTIMENT_FLOOR = -0.3
SENTIMENT_WINDOW = 3

#: §12.2's "configurable ceiling" for a complex or high-value order. A default,
#: not a rule: what counts as bulk differs between a Barabanki centre and a
#: Lucknow one, and §15 puts this in the admin panel.
DEFAULT_HIGH_VALUE_RUPEES = Decimal("25000")
DEFAULT_BULK_UNITS = 20

#: Abuse and sustained anger (§12.1). Deliberately short and unambiguous: a
#: frustrated farmer swearing once is not the same as abuse, and over-matching
#: here transfers exactly the callers who most need the agent to stay calm.
#: §12.1 says transfer calmly and never argue -- so this errs toward letting the
#: sentiment trend in §12.2 catch frustration, and reserves the immediate path
#: for language directed at the listener.
_ABUSE = re.compile(
    whole_word(
        "गाली|बदतमीज़|बदतमीज|बकवास बंद|चुप कर|चूतिया|मादरचोद|भोसड़ी|"
        "fuck|bastard|idiot|shut up|bloody"
    ),
    re.IGNORECASE,
)

#: Legal, dispute or compensation (§12.1). A farmer blaming a product for a
#: crop failure is a liability conversation, and nothing the agent says on it
#: should be its own words.
_LEGAL = re.compile(
    whole_word(
        "मुक़दमा|मुकदमा|कोर्ट|अदालत|वकील|कानूनी|क़ानूनी|मुआवज़ा|मुआवजा|"
        "हर्जाना|उपभोक्ता फोरम|शिकायत दर्ज|फ़सल बर्बाद|फसल बर्बाद|"
        "lawyer|legal|court|compensation|consumer forum|sue|lawsuit|"
        "crop failed|crop failure|ruined my crop"
    ),
    re.IGNORECASE,
)

#: §12.2: the caller says a stated price or stock figure is wrong. Worth its own
#: trigger because the agent cannot adjudicate it -- the tool said what it said,
#: and arguing with a farmer about a price is a conversation with no good end.
_DISPUTES_ANSWER = re.compile(
    whole_word(
        "गलत है|ग़लत है|ऐसा नहीं|इतना नहीं|सस्ता था|पिछली बार|कल तो|"
        "that's wrong|that is wrong|not correct|it was cheaper|last time"
    ),
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class EscalationDecision:
    """Whether to hand off, and how."""

    escalate: bool
    reason: TransferReason | None = None
    urgency: TransferUrgency = TransferUrgency.NORMAL
    #: True for §12.1: transfer now, no further agent turns.
    immediate: bool = False
    #: §12.2 triggers that always accompany the transfer with a ticket.
    ticket_type: str | None = None
    detail: str = ""

    def __bool__(self) -> bool:
        return self.escalate


NO_ESCALATION = EscalationDecision(escalate=False)


@dataclass(frozen=True, slots=True)
class TurnSignals:
    """What one turn tells the escalation engine.

    A single struct rather than a long parameter list because every field is
    optional and a caller that forgets one should get the conservative default,
    not a signature error at the wrong layer.
    """

    text: str = ""
    intent: Intent = Intent.UNKNOWN
    asr_confidence: float | None = None
    sentiment: float | None = None
    #: Set when a tool reported the product is restricted or needs a licence.
    restricted_product: bool = False
    #: Set when every tier came back empty and the caller still wants an answer.
    no_data_available: bool = False
    order_value: Decimal | None = None
    order_units: int | None = None
    #: True when the caller asked about scheme *eligibility* rather than about
    #: the scheme. §12.2 and §16.2: the agent explains, never asserts.
    asserts_scheme_eligibility: bool = False


@dataclass
class EscalationEngine:
    """§12.1 and §12.2, evaluated in that order."""

    high_value_rupees: Decimal = DEFAULT_HIGH_VALUE_RUPEES
    bulk_units: int = DEFAULT_BULK_UNITS
    #: Rolling windows, kept here so the engine is the only thing that has to
    #: know the window sizes.
    _confidences: list[float] = field(default_factory=list, repr=False)
    _sentiments: list[float] = field(default_factory=list, repr=False)

    def evaluate(
        self, signals: TurnSignals, *, repeated_intent: bool = False
    ) -> EscalationDecision:
        """Decide for one turn.

        §12.1 is checked first and returns immediately. Ordering is the whole
        design: a caller who is both angry *and* has a low-confidence transcript
        must be transferred for the anger, not held in a clarification loop
        because the confidence rule happened to be evaluated first.
        """
        self._record(signals)

        immediate = self._immediate(signals)
        if immediate.escalate:
            log.info(
                "escalation.immediate",
                reason=immediate.reason.value if immediate.reason else None,
            )
            return immediate

        conditional = self._conditional(signals, repeated_intent=repeated_intent)
        if conditional.escalate:
            log.info(
                "escalation.conditional",
                reason=conditional.reason.value if conditional.reason else None,
            )
        return conditional

    # -- §12.1 -------------------------------------------------------------- #

    def _immediate(self, signals: TurnSignals) -> EscalationDecision:
        if signals.intent is Intent.SAFETY_EMERGENCY:
            # §16.1 has already run its script by the time this is consulted;
            # this is the transfer half.
            return EscalationDecision(
                escalate=True,
                reason=TransferReason.SAFETY_EMERGENCY,
                urgency=TransferUrgency.CRITICAL,
                immediate=True,
                ticket_type="safety_incident",
                detail="§16.1 emergency",
            )

        if signals.intent is Intent.TALK_TO_HUMAN:
            # §12.1: one request is enough. There is deliberately no branch
            # here that tries once more first.
            return EscalationDecision(
                escalate=True,
                reason=TransferReason.EXPLICIT_REQUEST,
                urgency=TransferUrgency.NORMAL,
                immediate=True,
                detail="caller asked for a person",
            )

        if signals.text and _ABUSE.search(signals.text):
            return EscalationDecision(
                escalate=True,
                reason=TransferReason.ABUSE_OR_ANGER,
                urgency=TransferUrgency.HIGH,
                immediate=True,
                detail="abusive language; transfer calmly, never argue",
            )

        if signals.text and _LEGAL.search(signals.text):
            return EscalationDecision(
                escalate=True,
                reason=TransferReason.LEGAL_OR_DISPUTE,
                urgency=TransferUrgency.HIGH,
                immediate=True,
                ticket_type="complaint",
                detail="legal, compensation or crop-failure claim",
            )

        return NO_ESCALATION

    # -- §12.2 -------------------------------------------------------------- #

    def _conditional(
        self, signals: TurnSignals, *, repeated_intent: bool
    ) -> EscalationDecision:
        if signals.intent is Intent.DEALERSHIP_ENQUIRY:
            # §12.2: always. A franchise enquiry is a high-value sales lead and
            # the agent has nothing useful to say about terms.
            return EscalationDecision(
                escalate=True,
                reason=TransferReason.DEALERSHIP_ENQUIRY,
                urgency=TransferUrgency.HIGH,
                ticket_type="dealership",
                detail="dealership enquiry",
            )

        if signals.intent is Intent.COMPLAINT:
            # §12.2: always -- ticket *plus* transfer, not one or the other.
            return EscalationDecision(
                escalate=True,
                reason=TransferReason.PRODUCT_COMPLAINT,
                urgency=TransferUrgency.HIGH,
                ticket_type="complaint",
                detail="product complaint or damage",
            )

        if signals.restricted_product:
            # §16.2: never recommend a restricted or licensed product. The row
            # is still surfaced -- pretending it does not exist would be a lie
            # -- but a human decides.
            return EscalationDecision(
                escalate=True,
                reason=TransferReason.RESTRICTED_PRODUCT,
                ticket_type=None,
                detail="restricted or licensed product",
            )

        if signals.asserts_scheme_eligibility:
            return EscalationDecision(
                escalate=True,
                reason=TransferReason.SCHEME_ELIGIBILITY,
                detail="eligibility is never asserted by the agent (§16.2)",
            )

        if signals.text and _DISPUTES_ANSWER.search(signals.text):
            return EscalationDecision(
                escalate=True,
                reason=TransferReason.CALLER_DISPUTES_ANSWER,
                detail="caller says the stated price or stock is wrong",
            )

        if self._is_high_value(signals):
            return EscalationDecision(
                escalate=True,
                reason=TransferReason.COMPLEX_OR_HIGH_VALUE_ORDER,
                ticket_type="lead",
                detail="bulk quantity or order value above the ceiling",
            )

        # Confidence before repetition, and the order is not arbitrary. Both
        # fire on the same calls -- an intent goes unresolved twice *because*
        # the audio is bad -- and the reason is what the manager reads on the
        # transfer. "Repeated misunderstanding" tells them the farmer is being
        # unclear and to re-ask; "low recognition confidence" tells them the
        # line is bad and to call back. Those are different actions, and the
        # second one is the true cause whenever confidence has been low.
        if self._confidence_is_low():
            return EscalationDecision(
                escalate=True,
                reason=TransferReason.LOW_RECOGNITION_CONFIDENCE,
                detail=f"rolling ASR confidence below {LOW_CONFIDENCE_MEAN}",
            )

        if repeated_intent and not self._audio_explains_it():
            return EscalationDecision(
                escalate=True,
                reason=TransferReason.REPEATED_MISUNDERSTANDING,
                detail="same intent unresolved after two attempts",
            )

        if self._sentiment_is_falling():
            return EscalationDecision(
                escalate=True,
                reason=TransferReason.NEGATIVE_SENTIMENT,
                urgency=TransferUrgency.HIGH,
                detail="sentiment declining across three turns",
            )

        if signals.no_data_available:
            # §11.4: nothing dead-ends. "I don't know" on its own is a dead end.
            return EscalationDecision(
                escalate=True,
                reason=TransferReason.MISSING_DATA,
                detail="no tier could answer and the caller wants an answer now",
            )

        return NO_ESCALATION

    # -- rolling signals ---------------------------------------------------- #

    def _record(self, signals: TurnSignals) -> None:
        if signals.asr_confidence is not None:
            self._confidences.append(signals.asr_confidence)
            del self._confidences[:-CONFIDENCE_WINDOW]
        if signals.sentiment is not None:
            self._sentiments.append(signals.sentiment)
            del self._sentiments[:-SENTIMENT_WINDOW]

    def _confidence_is_low(self) -> bool:
        """§12.2: rolling *mean* below the floor over a full window.

        The mean and not any single turn: one misheard sentence on a rural GSM
        line is ordinary, and transferring on it would send most of the district
        to a manager.
        """
        if len(self._confidences) < CONFIDENCE_WINDOW:
            return False
        return sum(self._confidences) / len(self._confidences) < LOW_CONFIDENCE_MEAN

    def _audio_explains_it(self) -> bool:
        """Whether every reading so far is below the floor.

        The repetition trigger fires at two turns and the confidence trigger
        needs three, so without this the reason attributed to a bad line is
        always "repeated misunderstanding" -- the earlier threshold wins and the
        manager is told the wrong thing. Holding the repetition trigger back for
        one turn when the audio is uniformly poor lets the accurate reason land.

        Only when *all* readings are low. One bad turn among good ones does not
        explain a repetition, and treating it as though it did would suppress a
        genuine misunderstanding trigger.
        """
        return bool(self._confidences) and all(
            c < LOW_CONFIDENCE_MEAN for c in self._confidences
        )

    def _sentiment_is_falling(self) -> bool:
        """§12.2: declining across the window **and** below the floor.

        Both conditions. A caller who is unhappy and staying level is being
        handled; a caller who started fine and is getting worse is not, and
        that trend is the signal worth acting on before they hang up.
        """
        if len(self._sentiments) < SENTIMENT_WINDOW:
            return False
        # `pairwise`, not `zip(xs, xs[1:], strict=True)`: a sliding window's
        # second sequence is always one shorter, so strict zip raises every
        # time -- which it did, from inside the escalation engine, on the third
        # sentiment reading of a live call.
        falling = all(later < earlier for earlier, later in pairwise(self._sentiments))
        return falling and self._sentiments[-1] < SENTIMENT_FLOOR

    def _is_high_value(self, signals: TurnSignals) -> bool:
        if signals.order_value is not None and signals.order_value >= self.high_value_rupees:
            return True
        return signals.order_units is not None and signals.order_units >= self.bulk_units


def triggers_covered() -> frozenset[TransferReason]:
    """Every §12 reason this engine can produce.

    Compared against :class:`TransferReason` in the tests. §12 lists fourteen
    triggers and an engine that silently implements nine still looks like it
    works -- the missing five simply never fire, and the calls they should have
    caught end some other way.
    """
    return frozenset(
        {
            TransferReason.SAFETY_EMERGENCY,
            TransferReason.EXPLICIT_REQUEST,
            TransferReason.ABUSE_OR_ANGER,
            TransferReason.LEGAL_OR_DISPUTE,
            TransferReason.REPEATED_MISUNDERSTANDING,
            TransferReason.LOW_RECOGNITION_CONFIDENCE,
            TransferReason.NEGATIVE_SENTIMENT,
            TransferReason.COMPLEX_OR_HIGH_VALUE_ORDER,
            TransferReason.DEALERSHIP_ENQUIRY,
            TransferReason.PRODUCT_COMPLAINT,
            TransferReason.RESTRICTED_PRODUCT,
            TransferReason.MISSING_DATA,
            TransferReason.SCHEME_ELIGIBILITY,
            TransferReason.CALLER_DISPUTES_ANSWER,
        }
    )


__all__ = (
    "CONFIDENCE_WINDOW",
    "DEFAULT_BULK_UNITS",
    "DEFAULT_HIGH_VALUE_RUPEES",
    "LOW_CONFIDENCE_MEAN",
    "NO_ESCALATION",
    "SENTIMENT_FLOOR",
    "SENTIMENT_WINDOW",
    "EscalationDecision",
    "EscalationEngine",
    "TurnSignals",
    "triggers_covered",
)
