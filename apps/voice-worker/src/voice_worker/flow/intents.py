"""Intent classification for the fifteen intents of §11.2.

Two layers, and the order matters.

**Rules first.** A handful of intents must not depend on a model call: a safety
emergency (§16.1), an explicit request for a human (§12.1), and an opt-out
(§13.2, which must be honoured synchronously). Each is a phrase set a farmer
uses directly and unmistakably, and each carries a consequence the system is not
allowed to get wrong because a gateway was slow. Routing those through an LLM
would make a legal obligation and a poisoning response contingent on an
inference completing.

**Model second.** Everything else is genuinely ambiguous -- "इसमें क्या है?"
could be a composition question or a stock question depending on three turns of
context -- and that is what a model is for. The rules layer returns
:data:`Intent.UNKNOWN` rather than guessing, and the caller escalates to the
classifier.

The rules are also the reason the system degrades usefully. With the LLM
unavailable, an agent that can still recognise "मुझे आदमी से बात करनी है" and
transfer is far better than one that cannot do anything.

§11.2's last line is the loop that makes this improve: every turn calls
``log_intent``, and unrecognised intents accumulate in an admin queue so the
operator can add answers. :data:`Intent.UNKNOWN` is a signal, not a failure.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

import structlog

from uaagro_domain.enums import Intent

from ..text.script import whole_word
from .safety import detect as detect_safety

log = structlog.get_logger(__name__)

#: Below this the classifier's answer is not trusted and the agent asks rather
#: than acting. §11.4 escalates after the same intent fails twice, so an
#: uncertain guess acted on twice becomes a transfer -- which is the right
#: outcome, but asking once first is cheaper for everyone.
MIN_CONFIDENCE = 0.55


def _rule(*terms: str) -> re.Pattern[str]:
    """Whole-word alternation. See :func:`whole_word` on why not ``\\b``."""
    return re.compile(whole_word("|".join(terms)), re.IGNORECASE)


#: §12.1 and KB §10: an explicit request for a person. Immediate transfer, no
#: further agent turns -- a farmer who has asked for a human has already decided
#: the agent is not helping, and another attempt reads as being stonewalled.
_TALK_TO_HUMAN = _rule(
    "आदमी", "इंसान", "मैनेजर", "manager", "किसी से बात", "human", "person",
    "बात कराओ", "बात करा दो", "बात करनी है", "जोड़ दो", "जोड़िए",
)

#: §13.2: honoured synchronously during the call, never deferred to a job.
_OPT_OUT = _rule(
    "मत करो कॉल", "कॉल मत", "फ़ोन मत", "फोन मत", "परेशान", "नंबर हटाओ",
    "नंबर हटा", "दोबारा मत", "बंद करो", "stop calling", "remove my number",
    "do not call", "unsubscribe", "opt out",
)

#: KB §10 complaint vocabulary. Always a ticket plus a transfer (§12.2).
_COMPLAINT = _rule(
    "शिकायत", "ख़राब", "खराब", "नक़ली", "नकली", "काम नहीं किया", "नुक़सान",
    "नुकसान", "घाटा", "गलत माल", "ग़लत माल", "टूटा", "complaint", "damaged",
    "fake", "did not work", "didn't work",
)

_DEALERSHIP = _rule(
    "डीलरशिप", "एजेंसी", "फ्रैंचाइज़ी", "फ्रैंचाइजी", "डिस्ट्रीब्यूटर",
    "dealership", "franchise", "distributor", "agency", "partnership",
)

#: §11.2 marks this **strict**: explain the scheme, never assert eligibility.
_SCHEME = _rule(
    "सब्सिडी", "सब्सिडी", "योजना", "पीएम किसान", "पी एम किसान", "किसान सम्मान",
    "केसीसी", "के सी सी", "क्रेडिट कार्ड", "अनुदान", "subsidy", "scheme",
    "pm kisan", "pm-kisan", "kcc", "kisan credit",
)

_ORDER_STATUS = _rule(
    "ऑर्डर", "आर्डर", "order", "भेजा", "कब आएगा", "कब मिलेगा", "डिलीवरी",
    "delivery", "पहुँचा", "पहुंचा",
)

_CENTRE_LOCATION = _rule(
    "दुकान", "केंद्र", "सेंटर", "कहाँ है", "कहां है", "पता", "address",
    "कितने बजे", "खुलता", "बंद होता", "timing", "location", "shop",
)

#: Price and stock. Kept as rules because they are the two most common intents
#: on an agri helpline and both route to Tier 1 -- a model call to decide
#: "यूरिया का रेट?" is a round trip spent on a settled question.
_PRICE = _rule(
    "रेट", "दाम", "क़ीमत", "कीमत", "भाव", "मूल्य", "price", "rate", "cost",
    "कितने का", "कितने की", "कितने रुपये",
)
_AVAILABILITY = _rule(
    "मिलेगा", "मिल जाएगा", "मिलेगी", "है क्या", "स्टॉक", "उपलब्ध", "available",
    "in stock", "मौजूद",
    # "X चाहिए" is one of the commonest ways a farmer asks for a product, and
    # leaving it out sent "ग्लाइफोसेट चाहिए" to UNKNOWN -- so no tool ran, so the
    # restricted-product check never saw the row it exists to catch.
    "चाहिए", "चाहिये", "दे दीजिए", "दे दो", "लेना है",
)

_DOSAGE = _rule(
    "कितना डालना", "कितनी डालनी", "कितना डालें", "मात्रा", "डोज़", "खुराक",
    "प्रति एकड़", "प्रति बीघा", "how much", "dose", "dosage", "per acre",
)
_COMPOSITION = _rule(
    "क्या क्या है", "क्या-क्या है", "कितना नाइट्रोजन", "तत्व", "कंपोज़िशन",
    "composition", "ingredients", "what is in", "एनपीके", "en pi ke",
)
_SERVICE = _rule(
    "मिट्टी जाँच", "मिट्टी जांच", "सॉइल टेस्ट", "ड्रोन", "छिड़काव सेवा",
    "फील्ड विज़िट", "soil test", "soil testing", "drone", "spraying service",
    "field visit",
)
_PROBLEM = _rule(
    "पीली", "पीला", "सूख", "मुरझा", "कीड़ा", "कीड़े", "इल्ली", "सुंडी",
    "धब्बा", "धब्बे", "रोग", "बीमारी", "लग गया", "लग गई", "खराब हो रही",
    "yellowing", "wilting", "pest", "disease", "blight", "rust",
)
_CROP_RECOMMENDATION = _rule(
    "क्या डालें", "क्या डालूँ", "क्या डालना", "कौन सी दवा", "कौन सा खाद",
    "क्या डाल सकते", "सलाह", "what should i", "recommend", "which fertiliser",
    "which pesticide",
)


#: Ordered. The first match wins, so the order encodes precedence and a
#: reordering changes behaviour -- safety before everything, then the intents
#: that carry an obligation, then the ordinary ones.
_RULES: tuple[tuple[Intent, re.Pattern[str]], ...] = (
    (Intent.TALK_TO_HUMAN, _TALK_TO_HUMAN),
    (Intent.COMPLAINT, _COMPLAINT),
    (Intent.DEALERSHIP_ENQUIRY, _DEALERSHIP),
    (Intent.SCHEME_QUERY, _SCHEME),
    (Intent.SERVICE_REQUEST, _SERVICE),
    (Intent.ORDER_STATUS, _ORDER_STATUS),
    (Intent.DOSAGE_QUERY, _DOSAGE),
    (Intent.PRODUCT_COMPOSITION, _COMPOSITION),
    (Intent.CROP_RECOMMENDATION, _CROP_RECOMMENDATION),
    (Intent.PROBLEM_DIAGNOSIS, _PROBLEM),
    (Intent.PRICE_ENQUIRY, _PRICE),
    (Intent.PRODUCT_AVAILABILITY, _AVAILABILITY),
    (Intent.CENTRE_LOCATION, _CENTRE_LOCATION),
)

#: Intents whose consequence must not depend on a model call completing.
DETERMINISTIC = frozenset(
    {Intent.SAFETY_EMERGENCY, Intent.TALK_TO_HUMAN, Intent.COMPLAINT}
)

#: Which tools §11.2 routes each intent to. Not a hard allowlist -- that lives
#: on ``agent_configs.tool_allowlist`` (§10) -- but the hint the prompt carries,
#: so the model reaches for the right tool without exploring.
TOOL_HINTS: dict[Intent, tuple[str, ...]] = {
    Intent.PRODUCT_AVAILABILITY: ("search_products", "check_availability"),
    Intent.PRICE_ENQUIRY: ("search_products", "check_availability"),
    Intent.CROP_RECOMMENDATION: ("recommend_for_crop",),
    Intent.PROBLEM_DIAGNOSIS: ("recommend_for_crop", "search_knowledge"),
    Intent.DOSAGE_QUERY: ("recommend_for_crop", "calculate_dose"),
    Intent.PRODUCT_COMPOSITION: ("search_products", "get_product_details"),
    Intent.CENTRE_LOCATION: ("find_nearest_centre",),
    Intent.ORDER_STATUS: ("get_order_status",),
    Intent.SERVICE_REQUEST: ("find_nearest_centre", "create_ticket"),
    Intent.SCHEME_QUERY: ("search_knowledge",),
    Intent.COMPLAINT: ("create_ticket", "transfer_to_human"),
    Intent.TALK_TO_HUMAN: ("transfer_to_human",),
    Intent.DEALERSHIP_ENQUIRY: ("create_ticket", "transfer_to_human"),
    Intent.SAFETY_EMERGENCY: ("transfer_to_human", "create_ticket"),
}


@dataclass(frozen=True, slots=True)
class IntentResult:
    """One turn's classification."""

    intent: Intent
    confidence: float
    #: ``rule``, ``classifier`` or ``safety``. Recorded so §19 can measure how
    #: much of the traffic the deterministic layer handles -- if it is most of
    #: it, the model is being paid for very little.
    source: str
    #: Products, crops, quantities the turn mentioned. Passed to ``log_intent``.
    entities: dict[str, str] | None = None

    @property
    def is_confident(self) -> bool:
        return self.confidence >= MIN_CONFIDENCE

    @property
    def tool_hints(self) -> tuple[str, ...]:
        return TOOL_HINTS.get(self.intent, ())

    @property
    def bypasses_llm(self) -> bool:
        """Whether this intent acts without waiting for a model."""
        return self.intent in DETERMINISTIC


def classify_by_rule(text: str) -> IntentResult:
    """Deterministic classification. :data:`Intent.UNKNOWN` when unsure.

    Safety is checked first and separately, because §16.1 makes it the one
    branch that abandons all other logic -- including this function's own
    ordering.
    """
    if not text or not text.strip():
        return IntentResult(Intent.UNKNOWN, 0.0, "rule")

    verdict = detect_safety(text)
    if verdict.triggered:
        # Confidence 1.0 is not a claim about certainty. It marks the branch as
        # non-negotiable: nothing downstream may weigh this against an
        # alternative reading of the sentence.
        return IntentResult(Intent.SAFETY_EMERGENCY, 1.0, "safety")

    if _OPT_OUT.search(text):
        return IntentResult(Intent.OUT_OF_SCOPE, 0.9, "rule", {"opt_out": "true"})

    for intent, pattern in _RULES:
        match = pattern.search(text)
        if match is not None:
            # 0.75, not 1.0. A keyword match is strong evidence and not proof:
            # "मेरा ऑर्डर कहाँ है" and "ऑर्डर कैसे करूँ" both contain ऑर्डर and
            # are different intents. Above MIN_CONFIDENCE so it acts, below
            # certainty so a classifier that disagrees can still be heard.
            return IntentResult(intent, 0.75, "rule", {"matched": match.group(0)})

    return IntentResult(Intent.UNKNOWN, 0.0, "rule")


def reconcile(rule: IntentResult, model: IntentResult | None) -> IntentResult:
    """Combine the two layers.

    The deterministic intents win outright. For everything else the rules are a
    prior and the model has the context -- three turns of it -- so it takes
    precedence when it is confident. A rule that fired on a single keyword
    should not override a model that has read the conversation.
    """
    if rule.bypasses_llm or rule.intent is Intent.SAFETY_EMERGENCY:
        return rule
    if model is None:
        return rule
    if model.intent is Intent.SAFETY_EMERGENCY:
        # §16.1: the classifier is allowed to *add* an emergency the keyword
        # list missed. It is never allowed to remove one.
        return model
    if model.is_confident:
        return model
    return rule if rule.intent is not Intent.UNKNOWN else model


def unhandled(results: Sequence[IntentResult]) -> int:
    """How many turns fell through to UNKNOWN.

    §11.2: these accumulate in an admin queue so the operator can add answers.
    Counted rather than hidden, because a rising number is the clearest signal
    that the agent is meeting questions nobody anticipated.
    """
    return sum(1 for r in results if r.intent is Intent.UNKNOWN)


__all__ = (
    "DETERMINISTIC",
    "MIN_CONFIDENCE",
    "TOOL_HINTS",
    "IntentResult",
    "classify_by_rule",
    "reconcile",
    "unhandled",
)
