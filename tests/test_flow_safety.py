"""The §16.1 safety path, with the adversarial phrasings §21 gates on.

This is the highest-severity path in the system and the one where a silent
regression is worst: every other failure produces a bad answer, this one
produces a farmer who swallowed pesticide being read a dosage table.

The suite is deliberately lopsided. The recall cases are exhaustive and the
precision cases are few, because the two errors are not comparable: a false
positive costs one unnecessary transfer that a manager resolves in ten seconds,
and a false negative can cost a life. Where a phrasing is genuinely ambiguous
the test asserts it fires.
"""

from __future__ import annotations

import pytest

from uaagro_domain.enums import Intent, TransferReason, TransferUrgency
from voice_worker.flow.safety import (
    REVIEW_WITHIN_HOURS,
    SAFETY_PHRASE_KEY,
    SafetyVerdict,
    combine,
    detect,
)

# --------------------------------------------------------------------------- #
# Recall -- what must always fire
# --------------------------------------------------------------------------- #

INGESTION = [
    "मेरे बेटे ने दवा पी ली है",
    "गलती से दवा पी ली",
    "उसने खा लिया है, क्या करें",
    "बच्चे ने निगल लिया",
    "my son swallowed the pesticide",
    "he drank it by mistake",
    "she ingested some of the spray",
]

EXPOSURE = [
    "छिड़काव करते समय आँख में चला गया",
    "दवा आँखों में पड़ गई",
    "स्प्रे करते समय साँस में चला गया",
    "पूरी बोतल शरीर पर गिर गई",
    "it splashed on my skin",
    "the spray got in my eye",
    "I breathed in the fumes while spraying",
]

SYMPTOMS = [
    "छिड़काव के बाद चक्कर आ रहे हैं",
    "उल्टी हो रही है और बेहोश जैसा लग रहा है",
    "मुँह से झाग आ रहा है",
    "साँस नहीं आ रही",
    "he is unconscious",
    "she is vomiting after spraying",
    "I feel dizzy and my skin is burning",
    "we are going to the hospital now",
]

#: Phrasings that do not name a chemical, do not use the obvious verb, or bury
#: the emergency inside an ordinary-sounding question. §16.1 requires a
#: dedicated adversarial suite, and this is it.
ADVERSARIAL = [
    # The emergency arrives as a product question.
    "यूरिया का रेट बताइए, और हाँ मेरे भाई ने दवा पी ली है",
    # No chemical named at all.
    "मेरी पत्नी बेहोश हो गई है, खेत में थी",
    # Past tense, understated.
    "कल छिड़काव किया था, आज से उल्टी हो रही है",
    # Third person, indirect.
    "पड़ोसी का लड़का अस्पताल में है, दवा सूँघ लिया था",
    # English inside a Hindi call.
    "sir he is vomiting, spray ke baad",
    # Politely phrased, still an emergency.
    "एक बात पूछनी थी, आँख में दवा चली जाए तो क्या करें, अभी हुआ है",
]


@pytest.mark.parametrize(
    "utterance", [*INGESTION, *EXPOSURE, *SYMPTOMS, *ADVERSARIAL]
)
def test_the_safety_path_fires(utterance: str) -> None:
    verdict = detect(utterance)
    assert verdict.triggered, f"missed: {utterance!r}"
    assert verdict.source == "keyword"


def test_detection_does_not_need_a_model() -> None:
    """§16.1: the keyword path exists so detection does not depend on a model
    call succeeding. The LLM gateway is the component most likely to be timing
    out at the moment a distressed caller is on the line."""
    verdict = detect("बच्चे ने दवा पी ली")
    assert verdict.triggered
    assert combine(verdict, classifier_says_emergency=None).triggered


def test_the_classifier_can_add_an_emergency_but_never_remove_one() -> None:
    """§16.1 backs the list with LLM classification. Requiring both would let
    the model veto the deterministic path -- exactly backwards, since the model
    is the part that can be wrong, slow, or absent."""
    keyword_hit = detect("उल्टी हो रही है")
    assert combine(keyword_hit, classifier_says_emergency=False).triggered

    no_keyword = detect("तबीयत कुछ ठीक नहीं लग रही छिड़काव के बाद से")
    combined = combine(no_keyword, classifier_says_emergency=True)
    assert combined.triggered
    assert combined.source == "classifier"


# --------------------------------------------------------------------------- #
# Precision -- the few things that must not fire
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "utterance",
    [
        # The single most common sentence in agronomy advice.
        "दवा को पानी में मिलाकर छिड़काव करें",
        "एक बीघे में कितनी दवा डालनी है",
        "डीएपी का रेट क्या है",
        "मेरे खेत में कीड़ा लग गया है",
        # A product-safety question, asked by someone doing the right thing.
        "क्या यह दवा ज़हरीली है",
        "how toxic is this pesticide",
    ],
)
def test_ordinary_advisory_talk_does_not_fire(utterance: str) -> None:
    """A product mention is not an emergency. Matching "दवा" alone would route
    the district's dosage questions to the emergency line, and an emergency path
    that fires on everything is one that gets switched off."""
    assert not detect(utterance).triggered, f"false positive: {utterance!r}"


def test_a_toxicity_question_with_actual_exposure_still_fires() -> None:
    """The negation list is not an override. Someone asking whether the spray is
    toxic *because it is on their skin* is an emergency."""
    assert detect("क्या यह दवा ज़हरीली है, मेरे हाथ पर गिर गई है").triggered


def test_empty_input_is_not_an_emergency() -> None:
    assert not detect("").triggered
    assert not detect("   ").triggered


# --------------------------------------------------------------------------- #
# What firing actually does
# --------------------------------------------------------------------------- #


def test_the_instruction_covers_every_step_of_16_1() -> None:
    """§16.1 lists four numbered actions. A path that speaks the script and
    forgets the P0 ticket looks correct on the call and leaves nobody following
    up."""
    instruction = detect("बच्चे ने दवा पी ली").instruction()

    assert instruction["abandon_other_logic"] is True
    assert instruction["speak_cached"] == SAFETY_PHRASE_KEY

    transfer = instruction["transfer"]
    assert transfer["reason"] == TransferReason.SAFETY_EMERGENCY.value
    assert transfer["urgency"] == TransferUrgency.CRITICAL.value
    # §16.1-2: past the hours check, the busy check and the daily cap.
    assert transfer["bypass_availability"] is True

    assert instruction["ticket"] == {"type": "safety_incident", "priority": "p0"}
    assert set(instruction["alert"]) == {"centre_manager", "regional_manager"}
    assert set(instruction["alert_channels"]) == {"whatsapp", "sms"}
    assert instruction["flag_for_human_review_within_hours"] == REVIEW_WITHIN_HOURS
    assert instruction["intent"] == Intent.SAFETY_EMERGENCY.value


def test_the_agent_never_composes_medical_advice() -> None:
    """§16.1: never an antidote, never "drink milk", never a folk remedy --
    several of which are actively harmful for organophosphates.

    The script is a fixed string reached by a cache key, so there is no path by
    which a model writes this sentence.
    """
    instruction = detect("दवा पी ली").instruction()
    script = str(instruction["script"])

    assert "अस्पताल" in script, "must send them to a hospital"
    assert "लेबल" in script, "must tell them to take the label"
    for remedy in ("दूध", "पानी पिला", "नमक", "उल्टी करा", "milk", "induce"):
        assert remedy not in script, f"script contains a remedy: {remedy}"


def test_a_verdict_that_did_not_fire_has_no_matches() -> None:
    verdict = SafetyVerdict(triggered=False)
    assert verdict.matched == ()
    assert verdict.urgency is TransferUrgency.CRITICAL
