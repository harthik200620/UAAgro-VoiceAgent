"""Agrochemical safety detection -- the highest-severity path (§16.1).

When a caller says they swallowed something, or cannot breathe, or has spray in
their eyes, the agent abandons every other line of logic: it speaks a cached
sentence telling them to get to a hospital and take the label, and it transfers
immediately, past the availability checks, the hours check and the daily
transfer cap.

Three properties of this module follow from that, and each is a deliberate
choice against the more obvious design.

**Keywords first, model second.** §16.1 requires detection to be a keyword list
in every supported language *plus* LLM classification, in that order. The
keyword path exists so that detection does not depend on a model call
succeeding: the LLM gateway is the single most likely component to be timing
out at the moment a distressed caller is on the line, and a safety path that
needs it is a safety path that fails exactly when it is needed. The classifier
widens recall; it is never the only thing standing between a poisoning and a
product enquiry.

**The threshold is deliberately loose.** A false positive costs one unnecessary
transfer to a manager, who says "no, they were asking about a spray schedule"
and moves on. A false negative is somebody who swallowed pesticide being read a
dosage table. Those are not comparable, so this errs heavily toward firing.
§19's eval measures the false-positive rate rather than tuning it away.

**No medical content, ever.** §16.1 forbids the agent suggesting an antidote,
saying "drink milk", or offering any folk remedy -- several of which are
actively harmful for organophosphates. This module returns a *cached phrase key*
and a transfer instruction, never generated text, so there is no path by which a
model composes medical advice on this branch.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

import structlog

from uaagro_domain.enums import Intent, TransferReason, TransferUrgency

from ..text.script import whole_word

log = structlog.get_logger(__name__)

#: The cached recording spoken before the transfer. A key, not text: §16.1 wants
#: this delivered calmly and slowly from a recording, and a phrase this
#: important is not re-synthesised on the fly at the moment TTS is most likely
#: to be struggling.
SAFETY_PHRASE_KEY = "safety.emergency.hi"

#: What that recording says, kept here so the audio and the transcript cannot
#: drift apart, and so the post-call record shows what the caller was told.
SAFETY_SCRIPT_HI = (
    "जी, यह गंभीर बात है। तुरंत नज़दीकी अस्पताल या डॉक्टर के पास जाइए। "
    "दवा का डिब्बा या लेबल साथ ले जाइए। मैं अभी आपको हमारे विशेषज्ञ से जोड़ रहा हूँ।"
)

#: §16.1-4: mandatory human review inside this window.
REVIEW_WITHIN_HOURS = 1


# --------------------------------------------------------------------------- #
# Keyword lexicons
# --------------------------------------------------------------------------- #
#
# Two lists, because one list cannot express the difference between "मैंने दवा
# पी ली" (drank the spray) and "दवा पानी में मिलाकर छिड़काव करें" (mix the spray
# with water and apply). The first is an emergency; the second is the single
# most common sentence in agronomy advice, and a naive match on "दवा" would fire
# on every dosage call in the district.
#
# So: an EXPOSURE term (something entered a person) or a SYMPTOM term (a person
# is in distress) is required. A bare product mention is not enough.

#: Something got into someone. Verbs and phrases, not nouns.
_EXPOSURE_HI = (
    "पी लिया",
    "पी ली",
    "पी लिये",
    "पी लिए",  # drank it
    "खा लिया",
    "खा ली",
    "निगल",
    "निगल लिया",  # swallowed
    "मुँह में चला गया",
    "मुंह में चला गया",
    "आँख में",
    "आंख में",
    "आँखों में",
    "आंखों में",  # in the eyes
    "साँस में",
    "सांस में",
    "सूँघ लिया",
    "सुंघ लिया",  # inhaled
    "छिड़कते समय गिर",
    "ऊपर गिर गई",
    "ऊपर गिर गया",
    "चमड़ी पर",
    "त्वचा पर लग",
)

#: Something spilled onto a person. A *pattern*, not a list of phrases: the
#: first version of this file enumerated "शरीर पर गिर" and "बदन पर गिर" and
#: missed "हाथ पर गिर गई" -- and a farmer says whichever body part it landed on.
#: Enumerating body parts is a losing game where losing means a caller with
#: pesticide on their skin is read a dosage table, so the shape is
#: "<body part> पर <spill verb>" with the parts as the open set.
_BODY_PART = (
    "हाथ|हाथों|पैर|पाँव|पांव|मुँह|मुंह|चेहरे|चेहरा|शरीर|बदन|सिर|गर्दन|"
    "छाती|पीठ|टाँग|टांग|कपड़ों|कपड़े|चमड़ी|त्वचा|body|hand|face|skin|leg|arm"
)
_SPILL_VERB = "गिर|लग|पड़|छलक|spill|splash|fall"
_SPILLED_ON_PERSON = re.compile(
    rf"(?:{_BODY_PART})\s*(?:पर|में|on)?\s*(?:{_SPILL_VERB})",
    re.IGNORECASE,
)
_EXPOSURE_EN = (
    "swallowed",
    "drank",
    "ingested",
    "inhaled",
    "breathed in",
    "in my eye",
    "in his eye",
    "in her eye",
    "in the eyes",
    "in my eyes",
    "spilled on",
    "splashed on",
    "on my skin",
    "got into my mouth",
)

#: A person is in distress. These fire on their own -- someone unconscious after
#: handling a product does not need the sentence to also contain a verb.
_SYMPTOM_HI = (
    "बेहोश",
    "बेहोशी",  # unconscious
    "उल्टी",
    "उलटी",
    "क़ै",
    "कै",  # vomiting
    "चक्कर",  # dizzy
    "झाग",  # foaming
    "दौरा",
    "मिर्गी",  # seizure
    "साँस नहीं",
    "सांस नहीं",
    "दम घुट",  # cannot breathe
    "जलन हो रही",
    "जल रही है",
    "तड़प",  # writhing
    "अस्पताल",
    "हॉस्पिटल",  # already going to hospital
    "ज़हर",
    "जहर",
    "विष",  # poison
)
_SYMPTOM_EN = (
    "unconscious",
    "fainted",
    "passed out",
    "vomiting",
    "throwing up",
    "seizure",
    "convulsion",
    "foaming",
    "cannot breathe",
    "can't breathe",
    "difficulty breathing",
    "poisoned",
    "poisoning",
    "hospital",
    "emergency",
    "burning skin",
    "dizzy",
)

#: Marathi and Malayalam, per §16.1's "every supported language". Thin on
#: purpose and flagged as such: these were assembled without a native speaker,
#: and §16.1 requires the emergency list to be reviewed by a human before
#: go-live. They widen recall; they are not a substitute for that review.
_SYMPTOM_MR = ("विष", "बेशुद्ध", "उलटी", "चक्कर", "श्वास", "रुग्णालय")
_EXPOSURE_MR = ("प्यायले", "गिळले", "डोळ्यात", "खाल्ले")
_SYMPTOM_ML = ("വിഷം", "ബോധം", "ഛർദ്ദി", "ശ്വാസം", "ആശുപത്രി")
_EXPOSURE_ML = ("കുടിച്ചു", "വിഴുങ്ങി", "കണ്ണിൽ")

#: Phrases that look like an emergency and are not. Checked *before* the
#: keyword scan: "ज़हरीली दवा" is how a farmer describes a strong insecticide,
#: and "क्या यह ज़हरीला है?" is a product-safety question, not a poisoning.
_NEGATIONS = (
    "ज़हरीली दवा",
    "जहरीली दवा",
    "ज़हरीला है",
    "जहरीला है",
    "ज़हरीली है",
    "जहरीली है",
    "कितना ज़हरीला",
    "कितना जहरीला",
    "is it toxic",
    "how toxic",
    "toxicity",
)


def _compile(terms: Sequence[str]) -> re.Pattern[str]:
    """Whole-word alternation over the terms.

    :func:`whole_word` rather than ``\\b``: half of these end in a matra --
    ``उल्टी``, ``बेहोशी``, ``चक्कर`` is fine but ``पी ली`` is not -- and ``\\b``
    silently fails to match those, which would leave the safety list
    half-working in the way that looks like it works.
    """
    return re.compile(whole_word("|".join(re.escape(t) for t in terms)), re.IGNORECASE)


_EXPOSURE = _compile((*_EXPOSURE_HI, *_EXPOSURE_EN, *_EXPOSURE_MR, *_EXPOSURE_ML))
_SYMPTOM = _compile((*_SYMPTOM_HI, *_SYMPTOM_EN, *_SYMPTOM_MR, *_SYMPTOM_ML))
_NEGATION = _compile(_NEGATIONS)


@dataclass(frozen=True, slots=True)
class SafetyVerdict:
    """Whether this turn is a §16.1 emergency, and why."""

    triggered: bool
    #: ``keyword`` or ``classifier``. Recorded on the call so §19 can measure
    #: which path is actually catching emergencies -- if the classifier never
    #: adds anything the keyword list does not, that is worth knowing.
    source: str = ""
    matched: tuple[str, ...] = ()

    @property
    def transfer_reason(self) -> TransferReason:
        return TransferReason.SAFETY_EMERGENCY

    @property
    def urgency(self) -> TransferUrgency:
        return TransferUrgency.CRITICAL

    def instruction(self) -> dict[str, object]:
        """What the call loop must do next. §16.1, in order.

        Returned as data rather than executed here so the state machine owns
        sequencing and this module stays a detector -- and so the whole
        instruction is visible in one place in a test.
        """
        return {
            "abandon_other_logic": True,
            # Cached, never synthesised: this is the one phrase that must not
            # depend on TTS being healthy.
            "speak_cached": SAFETY_PHRASE_KEY,
            "script": SAFETY_SCRIPT_HI,
            # §16.1-2: past the hours check, the busy check and the daily cap.
            "transfer": {
                "reason": TransferReason.SAFETY_EMERGENCY.value,
                "urgency": TransferUrgency.CRITICAL.value,
                "bypass_availability": True,
            },
            # §16.1-3.
            "ticket": {"type": "safety_incident", "priority": "p0"},
            "alert": ["centre_manager", "regional_manager"],
            "alert_channels": ["whatsapp", "sms"],
            # §16.1-4.
            "flag_for_human_review_within_hours": REVIEW_WITHIN_HOURS,
            "intent": Intent.SAFETY_EMERGENCY.value,
        }


def detect(text: str) -> SafetyVerdict:
    """Keyword detection over one utterance (§16.1).

    Fires when the caller reports **exposure** (something entered a person) or a
    **symptom** (a person is in distress). A product name alone is not enough:
    "दवा" appears in nearly every advisory call, and matching it would route the
    district's dosage questions to the emergency line.

    Returns:
        A verdict. ``triggered`` is deliberately over-eager -- see the module
        docstring on why the two error directions are not comparable.
    """
    if not text or not text.strip():
        return SafetyVerdict(triggered=False)

    # Checked first: "क्या यह दवा ज़हरीली है?" is a product-safety question and
    # a common one. Treating it as a poisoning would transfer a farmer who was
    # doing exactly the right thing by asking.
    if (
        _NEGATION.search(text)
        and not _EXPOSURE.search(text)
        and not _SPILLED_ON_PERSON.search(text)
    ):
        return SafetyVerdict(triggered=False)

    exposure = tuple(m.group(0) for m in _EXPOSURE.finditer(text))
    exposure += tuple(m.group(0) for m in _SPILLED_ON_PERSON.finditer(text))
    symptom = tuple(m.group(0) for m in _SYMPTOM.finditer(text))

    if exposure or symptom:
        matched = (*exposure, *symptom)
        # Logged without the utterance: a transcript at this moment is medical
        # information about a named caller (§17, §19).
        log.warning("safety.keyword_match", terms=len(matched), source="keyword")
        return SafetyVerdict(triggered=True, source="keyword", matched=matched)

    return SafetyVerdict(triggered=False)


def combine(keyword: SafetyVerdict, classifier_says_emergency: bool | None) -> SafetyVerdict:
    """Merge the keyword verdict with the LLM classifier's (§16.1).

    **Or**, never **and**. The classifier is there to catch phrasings the list
    does not have -- a regional word for "fainted", a description with no
    keyword in it at all -- so requiring both would make the model able to veto
    the deterministic path, which is exactly backwards: the model is the part
    that can be wrong, be slow, or be unavailable.

    ``None`` means the classifier did not answer in time. That is not a "no".
    """
    if keyword.triggered:
        return keyword
    if classifier_says_emergency:
        log.warning("safety.classifier_match", source="classifier")
        return SafetyVerdict(triggered=True, source="classifier")
    return SafetyVerdict(triggered=False)


__all__ = (
    "REVIEW_WITHIN_HOURS",
    "SAFETY_PHRASE_KEY",
    "SAFETY_SCRIPT_HI",
    "SafetyVerdict",
    "combine",
    "detect",
)
