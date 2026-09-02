"""Contract tests for every vendor message shape (§19).

§19 asks for "recorded fixtures for every vendor API; the suite runs offline".

**These fixtures are derived from the vendor protocol documentation and from
§4.3/§5.1, not captured from a live session.** That distinction matters enough
to state at the top rather than bury: a fixture that claims to be recorded and
is not gives exactly the false confidence this tier of testing exists to
prevent. They are the right *shape* and they pin our parsing of it; they cannot
prove the vendor still sends that shape. Replace them with captures from the
first real call and the assertions below keep working.

What they do catch is the failure that is otherwise silent. Every one of these
parsers returns ``None`` for a message it does not recognise -- which is
correct, because a vendor that adds an event type must not crash a live call.
The cost of that choice is that a *renamed* field produces no events at all:
the recogniser goes quiet, the caller hears nothing, and no exception is raised
anywhere. These tests are what turns that into a red build.
"""

from __future__ import annotations

import json

import pytest

from uaagro_domain.settings import get_settings
from voice_worker.adapters.stt.base import SttEventType
from voice_worker.adapters.stt.deepgram_flux import DeepgramFluxSTT
from voice_worker.adapters.stt.sarvam import SarvamSTT

# --------------------------------------------------------------------------- #
# Deepgram Flux (§5.1 tier A: Hindi)
# --------------------------------------------------------------------------- #

FLUX_START_OF_TURN = {"event": "StartOfTurn", "request_id": "req-1"}

FLUX_UPDATE = {
    "event": "Update",
    "transcript": "डीएपी का",
    "words": [
        {"word": "डीएपी", "confidence": 0.97},
        {"word": "का", "confidence": 0.91},
    ],
}

FLUX_EAGER = {
    "event": "EagerEndOfTurn",
    "transcript": "डीएपी का रेट क्या है",
    "end_of_turn_confidence": 0.82,
    "words": [
        {"word": "डीएपी", "confidence": 0.97},
        {"word": "का", "confidence": 0.91},
        {"word": "रेट", "confidence": 0.95},
        {"word": "क्या", "confidence": 0.93},
        {"word": "है", "confidence": 0.89},
    ],
}

FLUX_TURN_RESUMED = {"event": "TurnResumed", "transcript": ""}

FLUX_END_OF_TURN = {
    "event": "EndOfTurn",
    "transcript": "डीएपी का रेट क्या है",
    "end_of_turn_confidence": 0.96,
    "trigger": "silence",
    "words": [
        {"word": "डीएपी", "confidence": 0.97},
        {"word": "का", "confidence": 0.91},
        {"word": "रेट", "confidence": 0.95},
        {"word": "क्या", "confidence": 0.93},
        {"word": "है", "confidence": 0.89},
    ],
}


@pytest.fixture
def flux() -> DeepgramFluxSTT:
    return DeepgramFluxSTT(get_settings())


def parse(adapter: object, payload: dict[str, object]):  # type: ignore[no-untyped-def]
    return adapter._parse(json.dumps(payload))  # type: ignore[attr-defined]


def test_every_flux_event_maps_to_a_pipeline_event(flux: DeepgramFluxSTT) -> None:
    """The five events §5.2's speculative loop is built on.

    If any one stops mapping, the loop degrades silently: no StartOfTurn means
    no barge-in, no EagerEndOfTurn means speculation never starts, no
    TurnResumed means a speculation is never cancelled -- and §5.2 calls that
    last one a severe bug, because the farmer hears an answer to a question
    they were still asking.
    """
    assert parse(flux, FLUX_START_OF_TURN).type is SttEventType.SPEECH_STARTED
    assert parse(flux, FLUX_UPDATE).type is SttEventType.PARTIAL
    assert parse(flux, FLUX_EAGER).type is SttEventType.EAGER_END_OF_TURN
    assert parse(flux, FLUX_TURN_RESUMED).type is SttEventType.TURN_RESUMED
    assert parse(flux, FLUX_END_OF_TURN).type is SttEventType.END_OF_TURN


def test_confidence_comes_from_words_not_from_end_of_turn_confidence(
    flux: DeepgramFluxSTT,
) -> None:
    """§11.4 escalates on a rolling recognition confidence below 0.55.

    ``end_of_turn_confidence`` measures how sure the model is that the turn
    *ended* -- a different quantity. Substituting it would make a decisive
    end-of-turn look like clear speech, and would silence the escalation
    exactly on the noisy calls it exists for.
    """
    event = parse(flux, FLUX_END_OF_TURN)

    mean_word = (0.97 + 0.91 + 0.95 + 0.93 + 0.89) / 5
    assert event.confidence == pytest.approx(mean_word)
    assert event.confidence != FLUX_END_OF_TURN["end_of_turn_confidence"]


def test_a_message_with_no_words_reports_no_confidence(flux: DeepgramFluxSTT) -> None:
    """None, not zero. §11.4 must see "not reported" honestly -- a stand-in
    zero would escalate every call."""
    event = parse(flux, {"event": "EndOfTurn", "transcript": "हाँ"})
    assert event.confidence is None


def test_an_empty_partial_is_dropped(flux: DeepgramFluxSTT) -> None:
    """Update fires roughly every 250 ms and is frequently empty. An empty
    partial carries nothing and would only add queue churn on the audio path."""
    assert parse(flux, {"event": "Update", "transcript": ""}) is None


def test_an_unknown_event_is_ignored_rather_than_fatal(flux: DeepgramFluxSTT) -> None:
    """A vendor that adds an event type must not end a live call.

    This is also the reason the tests above exist: the same tolerance means a
    *renamed* event produces silence rather than an error.
    """
    assert parse(flux, {"event": "SomethingNewInV3", "transcript": "x"}) is None


def test_malformed_json_does_not_raise(flux: DeepgramFluxSTT) -> None:
    assert flux._parse("not json at all") is None
    assert flux._parse(b"\xff\xfe binary") is None
    assert flux._parse(json.dumps(["a", "list"])) is None


def test_the_transcript_survives_devanagari_round_tripping(
    flux: DeepgramFluxSTT,
) -> None:
    """Sounds trivial and is not: a parser that decoded as latin-1 anywhere
    would produce mojibake that still passes a truthiness check, and the agent
    would answer a question made of question marks."""
    event = parse(flux, FLUX_END_OF_TURN)
    assert event.text == "डीएपी का रेट क्या है"


# --------------------------------------------------------------------------- #
# Sarvam (§5.1 tier B: Marathi, Malayalam and the rest)
# --------------------------------------------------------------------------- #

# `language_code` here, and the adapter accepts `language` too -- which of
# the two Sarvam actually sends is one of the things a real capture settles.
# See `_language` in the adapter for why it takes both rather than guessing.
SARVAM_PARTIAL = {
    "event": "transcript.partial",
    "text": "डीएपीची किंमत",
    "language_code": "mr-IN",
    "language_confidence": 0.88,
}

SARVAM_FINAL = {
    "event": "transcript.final",
    "text": "डीएपीची किंमत काय आहे",
    "language_code": "mr-IN",
    "language_confidence": 0.94,
}


@pytest.fixture
def sarvam() -> SarvamSTT:
    return SarvamSTT(get_settings())


def test_sarvam_final_is_not_an_end_of_turn(sarvam: SarvamSTT) -> None:
    """The distinction §5.1 turns on.

    Sarvam segments on silence; it does not decide whether the farmer has
    finished their thought. Emitting END_OF_TURN here would bypass the Smart
    Turn detector in front and cut off every Marathi caller who paused --
    which is the tier-B routing quietly becoming worse than tier A.
    """
    assert parse(sarvam, SARVAM_FINAL).type is SttEventType.FINAL
    assert parse(sarvam, SARVAM_PARTIAL).type is SttEventType.PARTIAL


def test_sarvam_reports_language_confidence_but_not_recognition_confidence(
    sarvam: SarvamSTT,
) -> None:
    """§11.4's escalation reads `confidence`; §11.1's LANG_LOCK reads
    `language_confidence`. Sarvam reports only the second, and filling the
    first with it would make a confidently-identified language look like
    clearly-heard speech."""
    event = parse(sarvam, SARVAM_FINAL)

    assert event.language_confidence == pytest.approx(0.94)
    assert event.confidence is None
    assert event.language == "mr-IN"


def test_an_empty_sarvam_partial_is_dropped(sarvam: SarvamSTT) -> None:
    assert parse(sarvam, {"event": "transcript.partial", "text": ""}) is None


def test_an_unknown_sarvam_event_is_ignored(sarvam: SarvamSTT) -> None:
    assert parse(sarvam, {"event": "transcript.speculative", "text": "x"}) is None


def test_malformed_sarvam_json_does_not_raise(sarvam: SarvamSTT) -> None:
    assert sarvam._parse("<html>gateway timeout</html>") is None
    assert sarvam._parse(json.dumps(42)) is None
