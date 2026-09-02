"""The §16.3 output validator, §11.2 intents, §11.1 state machine, §12 escalation.

Four modules, one file, because they are the four halves of one decision: what
the agent is allowed to say, what it thinks the caller wants, where the call is,
and when to stop trying. Testing them apart is fine; the interesting failures
are where they disagree.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from uaagro_domain.enums import Intent, TransferReason, TransferUrgency
from voice_worker.flow.escalation import (
    CONFIDENCE_WINDOW,
    SENTIMENT_WINDOW,
    EscalationEngine,
    TurnSignals,
    triggers_covered,
)
from voice_worker.flow.intents import (
    DETERMINISTIC,
    IntentResult,
    classify_by_rule,
    reconcile,
    unhandled,
)
from voice_worker.flow.state import (
    LOW_CONFIDENCE_LIMIT,
    REPEATED_INTENT_LIMIT,
    SILENCE_CLOSE_S,
    SILENCE_PROMPT_S,
    SILENCE_WARN_S,
    TERMINAL,
    TRANSITIONS,
    CallFlow,
    CallState,
    InvalidTransition,
    outcome_for,
    unreachable_terminals,
)
from voice_worker.flow.validator import (
    FALLBACK_SCRIPT_HI,
    MAX_WORDS,
    OutputValidator,
    ValidatedGeneration,
)

# --------------------------------------------------------------------------- #
# §16.3 -- the output validator
# --------------------------------------------------------------------------- #


@pytest.fixture
def validator() -> OutputValidator:
    return OutputValidator()


def test_a_price_with_no_tool_result_is_rejected(validator: OutputValidator) -> None:
    """The §1 N1 check, and the reason this module exists. The validator cannot
    tell whether a sentence is true; it can tell that a figure the agent is
    about to speak came from nowhere."""
    outcome = validator.validate("जी, डीएपी तेरह सौ पचास रुपये की बोरी है — 1350 रुपये।")
    assert not outcome.ok
    assert "ungrounded" in outcome.rules


def test_the_same_price_passes_when_a_tool_returned_it(
    validator: OutputValidator,
) -> None:
    outcome = validator.validate(
        "जी, डीएपी 1350 रुपये की बोरी है।",
        tool_results=[{"sku": "FRT-DAP-50", "price": "1350.00"}],
    )
    assert outcome.ok, outcome.violations


@pytest.mark.parametrize(
    ("spoken", "returned"),
    [
        ("1,350", "1350"),          # the model adds a thousands separator
        ("१३५०", 1350),             # the model writes Devanagari digits
        ("1350", "1350.00"),        # the tool returns a Decimal
        ("१,३५०", "1350.00"),       # both at once
    ],
)
def test_number_formats_that_mean_the_same_thing_are_accepted(
    validator: OutputValidator, spoken: str, returned: object
) -> None:
    """A validator that rejected these would be rejecting correct answers
    constantly, and the first response to a validator that cries wolf is to
    switch it off."""
    outcome = validator.validate(
        f"जी, {spoken} रुपये की बोरी है।", tool_results=[{"price": returned}]
    )
    assert outcome.ok, outcome.violations


def test_a_price_from_an_earlier_turn_does_not_count(
    validator: OutputValidator,
) -> None:
    """§16.2: never quote a price that did not come from a tool call **in this
    turn**. A price fetched four turns ago may have changed, and the caller
    cannot tell the difference."""
    outcome = validator.validate("जी, 1350 रुपये।", tool_results=[])
    assert not outcome.ok
    assert "ungrounded" in outcome.rules


def test_a_number_nested_anywhere_in_a_tool_result_grounds_it(
    validator: OutputValidator,
) -> None:
    """Tools nest: a price can arrive under ``alternatives[0].price`` or inside
    a formatted string. A validator that only understood the shapes it was told
    about would reject correct answers whenever a tool grew a field."""
    outcome = validator.validate(
        "जी, विकल्प में 890 रुपये वाला मिल जाएगा।",
        tool_results=[{"available": False, "alternatives": [{"sku": "X", "price": 890}]}],
    )
    assert outcome.ok, outcome.violations


@pytest.mark.parametrize(
    "text",
    [
        "तुम्हें क्या चाहिए",
        "तुम कहाँ से बोल रहे हो",
        "तेरा ऑर्डर तैयार है",
    ],
)
def test_the_informal_second_person_is_rejected(
    validator: OutputValidator, text: str
) -> None:
    """§11.3: always आप, never तुम. From a stranger in a service call this is
    not casual, it lands as talking down to the caller."""
    outcome = validator.validate(text)
    assert not outcome.ok
    assert "register" in outcome.rules


def test_sir_is_rejected_in_hindi(validator: OutputValidator) -> None:
    """§11.3: never सर. It reads as a call-centre script, not a neighbour."""
    outcome = validator.validate("जी सर, बताइए")
    assert not outcome.ok
    assert "register" in outcome.rules


@pytest.mark.parametrize(
    "text",
    [
        "इससे आपकी पैदावार दोगुनी हो जाएगी",
        "पक्का फ़ायदा होगा, गारंटी है",
        "this will double your yield",
    ],
)
def test_yield_and_guarantee_claims_are_rejected(
    validator: OutputValidator, text: str
) -> None:
    """§16.2 and §11.3. Unprovable, and from a seller close to mis-selling."""
    outcome = validator.validate(text)
    assert not outcome.ok
    assert "guarantee" in outcome.rules


def test_talking_about_yield_without_promising_is_allowed(
    validator: OutputValidator,
) -> None:
    """"पैदावार" is an ordinary agronomy word. Only a promise about it is not,
    and a validator that banned the noun would make the agent unable to discuss
    farming."""
    assert validator.validate("यह खाद पैदावार के लिए इस्तेमाल होती है।").ok


def test_english_ai_filler_is_rejected(validator: OutputValidator) -> None:
    """§16.3. Disclosure when *asked* is required and has its own phrasing;
    this catches the model volunteering a disclaimer mid-Hindi-call."""
    outcome = validator.validate("As an AI, I cannot help with that.")
    assert not outcome.ok
    assert "filler" in outcome.rules


def test_the_length_cap_relaxes_for_dosage(validator: OutputValidator) -> None:
    """§11.3: 35 words normally, 60 for dosage. A dosage answer carries a
    pre-harvest interval and a precaution, and clipping either is a §16.2
    violation in the other direction."""
    long_answer = "शब्द " * (MAX_WORDS + 10)
    assert not validator.validate(long_answer).ok
    assert validator.validate(long_answer, is_dosage=True).ok


def test_empty_output_is_rejected(validator: OutputValidator) -> None:
    assert not validator.validate("   ").ok


async def test_generation_retries_once_then_falls_back() -> None:
    """§16.3: reject and regenerate once; two failures end in a cached phrase
    and an escalation. Not a loop -- a third LLM round trip inside a turn §7
    gives 1,500 ms is not available."""
    attempts: list[str | None] = []

    async def always_bad(feedback: str | None) -> str:
        attempts.append(feedback)
        return "तुम्हें 9999 रुपये देने होंगे"

    text, outcome, escalate = await ValidatedGeneration().run(always_bad)

    assert len(attempts) == 2, "exactly one regeneration"
    assert attempts[0] is None
    assert attempts[1], "the retry must be told what was wrong"
    assert escalate
    assert text == FALLBACK_SCRIPT_HI
    assert not outcome.ok


async def test_a_corrected_second_attempt_is_spoken() -> None:
    async def improves(feedback: str | None) -> str:
        return "तुम्हें बताता हूँ" if feedback is None else "जी, मैं बताता हूँ।"

    text, outcome, escalate = await ValidatedGeneration().run(improves)
    assert outcome.ok
    assert not escalate
    assert text == "जी, मैं बताता हूँ।"


# --------------------------------------------------------------------------- #
# §11.2 -- intents
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("मुझे किसी आदमी से बात करनी है", Intent.TALK_TO_HUMAN),
        ("मैनेजर से बात कराओ", Intent.TALK_TO_HUMAN),
        ("यह माल ख़राब निकला, शिकायत दर्ज करो", Intent.COMPLAINT),
        ("डीलरशिप लेनी है", Intent.DEALERSHIP_ENQUIRY),
        ("पीएम किसान की सब्सिडी के बारे में बताइए", Intent.SCHEME_QUERY),
        ("मिट्टी जाँच करानी है", Intent.SERVICE_REQUEST),
        ("मेरा ऑर्डर कब आएगा", Intent.ORDER_STATUS),
        ("एक बीघे में कितना डालना है", Intent.DOSAGE_QUERY),
        ("इस खाद में क्या क्या है", Intent.PRODUCT_COMPOSITION),
        ("आलू में क्या डालें", Intent.CROP_RECOMMENDATION),
        ("गेहूँ की पत्ती पीली हो रही है", Intent.PROBLEM_DIAGNOSIS),
        ("यूरिया का रेट क्या है", Intent.PRICE_ENQUIRY),
        ("डीएपी मिल जाएगा क्या", Intent.PRODUCT_AVAILABILITY),
        ("सबसे पास की दुकान कहाँ है", Intent.CENTRE_LOCATION),
        ("बच्चे ने दवा पी ली", Intent.SAFETY_EMERGENCY),
    ],
)
def test_the_rules_layer_covers_the_common_phrasings(
    utterance: str, expected: Intent
) -> None:
    assert classify_by_rule(utterance).intent is expected


def test_an_unrecognised_turn_returns_unknown_rather_than_guessing() -> None:
    """§11.2: unrecognised intents accumulate in an admin queue so the operator
    can add answers. A guess would fill that queue with nothing."""
    result = classify_by_rule("कल बारिश होगी क्या")
    assert result.intent is Intent.UNKNOWN
    assert unhandled([result]) == 1


def test_safety_outranks_every_other_rule() -> None:
    """The utterance contains a price question *and* an emergency. §16.1
    abandons all other logic, including this module's own ordering."""
    result = classify_by_rule("यूरिया का रेट बताइए, और भाई ने दवा पी ली है")
    assert result.intent is Intent.SAFETY_EMERGENCY
    assert result.source == "safety"


def test_the_deterministic_intents_do_not_wait_for_a_model() -> None:
    """A poisoning response and a legal obligation must not be contingent on an
    inference completing."""
    assert Intent.SAFETY_EMERGENCY in DETERMINISTIC
    assert Intent.TALK_TO_HUMAN in DETERMINISTIC
    for intent in DETERMINISTIC:
        assert IntentResult(intent, 1.0, "rule").bypasses_llm


def test_a_confident_model_overrides_a_keyword_prior() -> None:
    """A rule that fired on one keyword should not beat a model that has read
    three turns of context."""
    rule = classify_by_rule("मेरा ऑर्डर कब आएगा")
    model = IntentResult(Intent.PRODUCT_AVAILABILITY, 0.9, "classifier")
    assert reconcile(rule, model).intent is Intent.PRODUCT_AVAILABILITY


def test_a_model_can_never_override_the_safety_path() -> None:
    """§16.1: the classifier may *add* an emergency the keyword list missed. It
    is never allowed to remove one."""
    rule = classify_by_rule("बच्चे ने दवा पी ली")
    model = IntentResult(Intent.PRODUCT_AVAILABILITY, 0.99, "classifier")
    assert reconcile(rule, model).intent is Intent.SAFETY_EMERGENCY


def test_a_model_may_add_an_emergency_the_rules_missed() -> None:
    rule = classify_by_rule("तबीयत ठीक नहीं लग रही")
    model = IntentResult(Intent.SAFETY_EMERGENCY, 0.8, "classifier")
    assert reconcile(rule, model).intent is Intent.SAFETY_EMERGENCY


def test_the_rules_still_work_with_no_model_at_all() -> None:
    """Degraded usefully: an agent that can still recognise "मुझे आदमी से बात
    करनी है" and transfer beats one that can do nothing."""
    rule = classify_by_rule("किसी आदमी से बात कराइए")
    assert reconcile(rule, None).intent is Intent.TALK_TO_HUMAN


# --------------------------------------------------------------------------- #
# §11.1 -- the state machine
# --------------------------------------------------------------------------- #


def test_no_state_is_a_dead_end() -> None:
    """§11.4's central promise, as something checkable rather than asserted.

    Every failure path must terminate in a resolved answer, a human, or a ticket
    with a callback commitment. Any state listed here is one where a call can
    arrive and never legitimately finish.
    """
    assert unreachable_terminals() == frozenset()


def test_every_state_is_reachable_from_init() -> None:
    """A state nothing can reach is dead code pretending to be a code path."""
    reachable = {CallState.INIT}
    frontier = [CallState.INIT]
    while frontier:
        for nxt in TRANSITIONS[frontier.pop()]:
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    assert reachable == set(CallState)


def test_escalate_has_no_way_back_into_the_conversation() -> None:
    """Once the agent has decided it cannot help, returning the caller to the
    loop that already failed them is what §12 exists to prevent."""
    assert TRANSITIONS[CallState.ESCALATE] == frozenset(
        {CallState.TRANSFER, CallState.CALLBACK}
    )


def test_an_invalid_transition_is_refused() -> None:
    """A call that slides from GREET to TRANSFER has skipped the step that
    decides whether a transfer is possible, and the caller hears a ring-out
    instead of a commitment."""
    flow = CallFlow(state=CallState.GREET)
    with pytest.raises(InvalidTransition):
        flow.to(CallState.TRANSFER)


def test_the_happy_path_walks_end_to_end() -> None:
    flow = CallFlow()
    for state in (
        CallState.IDENTIFY,
        CallState.SCREEN,
        CallState.GREET,
        CallState.LANG_LOCK,
        CallState.DISCOVER,
        CallState.RESOLVE,
        CallState.WRAP,
        CallState.END,
    ):
        flow.to(state)
    assert flow.state in TERMINAL


def test_three_low_confidence_turns_escalate() -> None:
    """§11.4."""
    flow = CallFlow()
    reasons = [
        flow.record_turn(Intent.PRICE_ENQUIRY, confident=False, resolved=False)
        for _ in range(LOW_CONFIDENCE_LIMIT)
    ]
    assert reasons[-1] == "low_recognition_confidence"
    assert reasons[0] is None


def test_one_good_turn_clears_the_confidence_streak() -> None:
    """One misheard sentence on a rural GSM line is ordinary."""
    flow = CallFlow()
    flow.record_turn(Intent.PRICE_ENQUIRY, confident=False, resolved=False)
    flow.record_turn(Intent.PRICE_ENQUIRY, confident=True, resolved=True)
    assert flow.low_confidence_streak == 0


def test_the_same_intent_failing_twice_escalates() -> None:
    """§11.4. A third attempt is not going to be the one that works."""
    flow = CallFlow()
    reason = None
    for _ in range(REPEATED_INTENT_LIMIT):
        reason = flow.record_turn(
            Intent.PROBLEM_DIAGNOSIS, confident=True, resolved=False
        )
    assert reason == "repeated_misunderstanding"


def test_resolving_forgets_the_failed_attempts() -> None:
    flow = CallFlow()
    flow.record_turn(Intent.PRICE_ENQUIRY, confident=True, resolved=False)
    flow.record_turn(Intent.PRICE_ENQUIRY, confident=True, resolved=True)
    assert Intent.PRICE_ENQUIRY not in flow.intent_attempts


@pytest.mark.parametrize(
    ("silent_for", "expected"),
    [
        (2.0, None),
        (SILENCE_PROMPT_S, "prompt"),
        (SILENCE_WARN_S, "warn"),
        (SILENCE_CLOSE_S, "close"),
        (40.0, "close"),
    ],
)
def test_the_silence_ladder(silent_for: float, expected: str | None) -> None:
    """§11.4. A farmer walking to the shed to read a label is not gone, so the
    ladder prompts twice before closing."""
    assert CallFlow().silence_action(silent_for) == expected


def test_a_transfer_is_never_recorded_as_resolved() -> None:
    """A transfer written as resolved inflates the containment rate §15 reports
    — the number the operator uses to judge whether the agent works."""
    assert outcome_for(CallState.TRANSFER, resolved=True).value == "transferred"
    assert outcome_for(CallState.END, resolved=True).value == "resolved"
    assert outcome_for(CallState.REJECT).value == "rejected_spam"


# --------------------------------------------------------------------------- #
# §12 -- escalation
# --------------------------------------------------------------------------- #


def test_every_trigger_in_the_spec_is_implemented() -> None:
    """§12 lists fourteen. An engine that implements nine still looks like it
    works: the missing five never fire, and the calls they should have caught
    end some other way."""
    assert triggers_covered() == set(TransferReason)


def test_one_request_for_a_human_is_enough() -> None:
    """§12.1, emphatically: never negotiate, never "let me try to help first".
    A farmer who has asked for a person has already judged the agent."""
    decision = EscalationEngine().evaluate(
        TurnSignals(text="आदमी से बात कराओ", intent=Intent.TALK_TO_HUMAN)
    )
    assert decision.escalate
    assert decision.immediate, "no further agent turns"
    assert decision.reason is TransferReason.EXPLICIT_REQUEST


def test_a_safety_emergency_is_immediate_and_critical() -> None:
    decision = EscalationEngine().evaluate(
        TurnSignals(intent=Intent.SAFETY_EMERGENCY)
    )
    assert decision.immediate
    assert decision.urgency is TransferUrgency.CRITICAL
    assert decision.ticket_type == "safety_incident"


def test_anger_is_transferred_before_a_clarification_loop() -> None:
    """Ordering is the design. A caller who is both angry and hard to hear must
    be transferred for the anger, not held in a confidence loop because that
    rule was evaluated first."""
    engine = EscalationEngine()
    decision = engine.evaluate(
        TurnSignals(text="बकवास बंद करो", asr_confidence=0.2)
    )
    assert decision.reason is TransferReason.ABUSE_OR_ANGER
    assert decision.immediate


def test_a_compensation_claim_goes_straight_to_a_human() -> None:
    """§12.1. A farmer blaming a product for a crop failure is a liability
    conversation, and nothing the agent says on it should be its own words."""
    decision = EscalationEngine().evaluate(
        TurnSignals(text="आपकी दवा से फसल बर्बाद हो गई, मुआवजा चाहिए")
    )
    assert decision.immediate
    assert decision.reason is TransferReason.LEGAL_OR_DISPUTE


def test_a_dealership_enquiry_always_escalates_but_not_immediately() -> None:
    """§12.2: always, because it is a high-value lead — but the agent may take
    the details first."""
    decision = EscalationEngine().evaluate(
        TurnSignals(intent=Intent.DEALERSHIP_ENQUIRY)
    )
    assert decision.escalate
    assert not decision.immediate
    assert decision.ticket_type == "dealership"


def test_a_complaint_gets_both_a_ticket_and_a_transfer() -> None:
    """§12.2 says ticket **plus** transfer, not one or the other."""
    decision = EscalationEngine().evaluate(TurnSignals(intent=Intent.COMPLAINT))
    assert decision.escalate
    assert decision.ticket_type == "complaint"


def test_a_restricted_product_routes_to_a_human() -> None:
    """§16.2: never recommend it. The row is still surfaced — pretending it
    does not exist would be a lie — but a person decides."""
    decision = EscalationEngine().evaluate(TurnSignals(restricted_product=True))
    assert decision.reason is TransferReason.RESTRICTED_PRODUCT


def test_scheme_eligibility_is_never_asserted() -> None:
    """§16.2: explain the scheme generally, direct to the official channel."""
    decision = EscalationEngine().evaluate(
        TurnSignals(intent=Intent.SCHEME_QUERY, asserts_scheme_eligibility=True)
    )
    assert decision.reason is TransferReason.SCHEME_ELIGIBILITY


def test_a_disputed_price_is_not_argued_about() -> None:
    decision = EscalationEngine().evaluate(
        TurnSignals(text="यह रेट गलत है, कल तो सस्ता था")
    )
    assert decision.reason is TransferReason.CALLER_DISPUTES_ANSWER


def test_a_bulk_order_escalates() -> None:
    engine = EscalationEngine()
    assert not engine.evaluate(TurnSignals(order_units=2)).escalate
    assert engine.evaluate(TurnSignals(order_units=50)).escalate
    assert engine.evaluate(TurnSignals(order_value=Decimal("80000"))).escalate


def test_low_confidence_needs_a_full_window_and_the_mean() -> None:
    """§12.2: the rolling *mean* over three turns. Transferring on one misheard
    sentence would send most of the district to a manager."""
    engine = EscalationEngine()
    assert not engine.evaluate(TurnSignals(asr_confidence=0.2)).escalate
    assert not engine.evaluate(TurnSignals(asr_confidence=0.2)).escalate
    decision = engine.evaluate(TurnSignals(asr_confidence=0.2))
    assert decision.reason is TransferReason.LOW_RECOGNITION_CONFIDENCE

    steady = EscalationEngine()
    for _ in range(CONFIDENCE_WINDOW):
        assert not steady.evaluate(TurnSignals(asr_confidence=0.9)).escalate


def test_sentiment_must_be_both_falling_and_low() -> None:
    """§12.2. A caller who is unhappy and level is being handled; one who
    started fine and is getting worse is not."""
    falling = EscalationEngine()
    decision = None
    for value in (0.3, -0.1, -0.6):
        decision = falling.evaluate(TurnSignals(sentiment=value))
    assert decision is not None
    assert decision.reason is TransferReason.NEGATIVE_SENTIMENT

    low_but_steady = EscalationEngine()
    for _ in range(SENTIMENT_WINDOW):
        result = low_but_steady.evaluate(TurnSignals(sentiment=-0.5))
    assert not result.escalate


def test_no_answer_anywhere_is_a_transfer_not_a_shrug() -> None:
    """§11.4: nothing dead-ends, and "I don't know" on its own is a dead end."""
    decision = EscalationEngine().evaluate(TurnSignals(no_data_available=True))
    assert decision.reason is TransferReason.MISSING_DATA


def test_an_ordinary_turn_does_not_escalate() -> None:
    """The engine must be quiet most of the time, or the transfer rate §15
    reports is meaningless."""
    decision = EscalationEngine().evaluate(
        TurnSignals(
            text="डीएपी का रेट क्या है",
            intent=Intent.PRICE_ENQUIRY,
            asr_confidence=0.95,
            sentiment=0.1,
        )
    )
    assert not decision.escalate
    assert not decision
