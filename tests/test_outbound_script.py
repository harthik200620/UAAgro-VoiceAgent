"""The outbound script, the responder that walks it, and how a call is
recognised as one we placed (§13.2, §15.1).

The responder replaced a state machine that existed but was never wired into
a live call. These tests are the contract the panel's script editor and the
media path both build to: the legal lines cannot be edited, a "no" ends the
call politely, a keypress and a spoken yes are the same answer, opt-out is
written before it is confirmed, and questions go to the knowledge agent
without derailing the script.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

from uaagro_domain.enums import CallDirection, CallOutcome, ContactStatus, InterestLevel
from uaagro_domain.script import DISCLOSURE, OPT_OUT, WHATSAPP_SENT, OutboundScript
from voice_worker.adapters.telephony.base import CallMetadata
from voice_worker.outbound.responder import OutboundResponder, Stage, dtmf_of
from voice_worker.runtime.direction import OurNumbers, classify_direction

# --------------------------------------------------------------------------- #
# The script
# --------------------------------------------------------------------------- #


def test_an_empty_config_speaks_the_defaults() -> None:
    script = OutboundScript.from_config({})
    assert script.ask_time
    assert script.then_ask
    assert script.closing
    assert script.message == ""


def test_the_legal_lines_cannot_be_edited_through_the_panel() -> None:
    """The panel may send anything; the disclosure and the opt-out stay."""
    script = OutboundScript.from_panel(
        {"opening": "hello", "optOut": "fine, whatever", "message": "एक संदेश"}
    )
    assert script.opening.startswith(DISCLOSURE)
    assert script.opt_out == OPT_OUT
    assert script.message == "एक संदेश"


def test_panel_and_storage_shapes_round_trip() -> None:
    original = OutboundScript(message="बैठक चौदह अक्टूबर को है", closing="नमस्ते जी")
    stored = original.to_config()
    assert OutboundScript.from_config(stored) == original
    panel = original.to_panel()
    assert panel["onPress2"] == "knowledge_base"
    assert OutboundScript.from_panel(panel) == original


def test_placeholders_fill_from_either_spelling() -> None:
    rendered = OutboundScript.render(
        "{नाम} जी, {centre} पर मिलिए।", name="राम", centre="सीतापुर केंद्र"
    )
    assert rendered == "राम जी, सीतापुर केंद्र पर मिलिए।"
    # An unknown farmer gets no dangling brace, and no orphaned "जी".
    assert "{" not in OutboundScript.render("{नाम} जी", name=None, centre=None)


def test_an_empty_message_is_a_problem_and_a_long_one_too() -> None:
    assert OutboundScript().problems()
    assert not OutboundScript(message="छूट है").problems()
    assert OutboundScript(message="शब्द " * 200).problems()


# --------------------------------------------------------------------------- #
# The responder
# --------------------------------------------------------------------------- #


class _Knowledge:
    """A stand-in for the LLM agent behind press two."""

    def __init__(self) -> None:
        self.questions: list[str] = []

    async def respond(self, transcript: str, *, language: str) -> AsyncIterator[str]:
        self.questions.append(transcript)
        yield "डीएपी की बोरी बारह सौ रुपये की है।"


def _responder(**overrides: object) -> OutboundResponder:
    kwargs: dict[str, object] = {
        "script": OutboundScript(message="डीएपी पर पचास रुपये की छूट है।"),
        "farmer_name": "कमला देवी",
        "centre_name": "सीतापुर",
    }
    kwargs.update(overrides)
    return OutboundResponder(**kwargs)  # type: ignore[arg-type]


async def _say(responder: OutboundResponder, transcript: str) -> str:
    return " ".join([chunk async for chunk in responder.respond(transcript, language="hi-IN")])


def test_the_opening_names_the_farmer_and_discloses_the_machine() -> None:
    opening = _responder().opening()
    assert opening.startswith(DISCLOSURE)
    assert "कमला देवी जी" in opening
    assert "सही व्यक्ति" in _responder(farmer_name=None).opening()


async def test_the_happy_path_pitches_then_confirms_on_a_keypress() -> None:
    responder = _responder()
    assert "दो मिनट" in await _say(responder, "हाँ बोल रही हूँ")
    pitched = await _say(responder, "हाँ बोलो")
    assert "पचास रुपये" in pitched
    assert "एक दबाइए" in pitched
    assert responder.stage is Stage.PITCHED

    closing = await _say(responder, "[dtmf 1]")
    assert responder.call_over
    assert responder.result.outcome == "pressed_1"
    assert responder.result.dtmf == "1"
    assert responder.result.interest is InterestLevel.INTERESTED
    assert responder.result.call_outcome is CallOutcome.OFFER_ACCEPTED
    assert responder.result.status is ContactStatus.COMPLETED
    # No WhatsApp is configured, so nothing claims a message was sent.
    assert WHATSAPP_SENT not in closing
    assert responder.script.closing in closing


async def test_a_spoken_yes_is_the_same_as_pressing_one() -> None:
    responder = _responder()
    await _say(responder, "हाँ")
    await _say(responder, "हाँ")
    await _say(responder, "हाँ, ले लेंगे")
    assert responder.result.outcome == "pressed_1"
    assert responder.result.dtmf is None


async def test_a_no_is_accepted_the_first_time() -> None:
    responder = _responder()
    await _say(responder, "हाँ")
    await _say(responder, "हाँ")
    said = await _say(responder, "नहीं चाहिए")
    assert responder.call_over
    assert responder.result.outcome == "talked"
    assert responder.result.call_outcome is CallOutcome.OFFER_DECLINED
    assert "कोई बात नहीं" in said
    # No second attempt at persuading: the closing follows directly.
    assert responder.script.closing in said


async def test_the_wrong_person_hears_no_offer() -> None:
    responder = _responder()
    said = await _say(responder, "वो घर पर नहीं हैं")
    assert responder.call_over
    assert "छूट" not in said
    assert responder.result.outcome == "wrong_person"
    assert responder.result.call_outcome is CallOutcome.NOT_REACHED


async def test_opt_out_is_written_before_it_is_confirmed() -> None:
    order: list[str] = []

    async def suppress() -> None:
        order.append("suppressed")

    responder = _responder(suppress=suppress)
    await _say(responder, "हाँ")
    lines = [chunk async for chunk in responder.respond("दोबारा मत करना", language="hi-IN")]
    order.append("spoken")
    assert order == ["suppressed", "spoken"]
    assert lines[0] == OPT_OUT
    assert responder.result.outcome == "opted_out"
    assert responder.result.status is ContactStatus.OPTED_OUT
    assert responder.call_over


async def test_press_two_and_a_question_go_to_the_knowledge_agent() -> None:
    knowledge = _Knowledge()
    responder = _responder(questions=knowledge)
    await _say(responder, "हाँ")
    await _say(responder, "हाँ")
    said = await _say(responder, "डीएपी का रेट क्या है?")
    assert knowledge.questions == ["डीएपी का रेट क्या है?"]
    assert "बारह सौ" in said
    # The script resumes: the interest question is asked again.
    assert "एक दबाइए" in said
    assert not responder.call_over

    await _say(responder, "[dtmf 2]")
    assert responder.result.outcome == "pressed_2"
    # A press-two that ends in a yes is a yes.
    await _say(responder, "[dtmf 1]")
    assert responder.result.outcome == "pressed_1"


async def test_press_two_that_ends_in_a_no_stays_pressed_two() -> None:
    responder = _responder(questions=_Knowledge())
    await _say(responder, "हाँ")
    await _say(responder, "हाँ")
    await _say(responder, "[dtmf 2]")
    await _say(responder, "नहीं")
    assert responder.result.outcome == "pressed_2"
    assert responder.call_over


async def test_silence_gets_one_more_chance_then_the_closing() -> None:
    responder = _responder()
    await _say(responder, "हाँ")
    await _say(responder, "हाँ")
    first = await _say(responder, "")
    assert not responder.call_over
    assert "एक दबाइए" in first
    second = await _say(responder, "")
    assert responder.call_over
    assert responder.script.closing in second
    assert responder.result.interest is InterestLevel.NO_RESPONSE


async def test_a_whatsapp_send_is_claimed_only_when_it_happened() -> None:
    async def sent() -> bool:
        return True

    async def failed() -> bool:
        raise RuntimeError("provider down")

    ok = _responder(send_whatsapp=sent)
    await _say(ok, "हाँ")
    await _say(ok, "हाँ")
    assert WHATSAPP_SENT in await _say(ok, "[dtmf 1]")
    assert ok.result.whatsapp_sent

    broken = _responder(send_whatsapp=failed)
    await _say(broken, "हाँ")
    await _say(broken, "हाँ")
    said = await _say(broken, "[dtmf 1]")
    assert WHATSAPP_SENT not in said
    # The message is read aloud instead, so the farmer leaves with the facts.
    assert "पचास रुपये" in said
    assert not broken.result.whatsapp_sent


async def test_nothing_is_said_once_the_call_is_over() -> None:
    responder = _responder()
    await _say(responder, "वो नहीं हैं")
    assert await _say(responder, "हाँ") == ""


def test_dtmf_markers_are_recognised_and_speech_is_not() -> None:
    assert dtmf_of("[dtmf 1]") == "1"
    assert dtmf_of(" [dtmf 9] ") == "9"
    assert dtmf_of("एक") is None


# --------------------------------------------------------------------------- #
# Which way is the call going?
# --------------------------------------------------------------------------- #

OURS = OurNumbers(inbound=frozenset({"1800123456"}), outbound=frozenset({"1409876543"}))


def _start(**fields: object) -> CallMetadata:
    return CallMetadata(stream_sid="s", call_sid="c", **fields)  # type: ignore[arg-type]


def test_a_call_to_the_helpline_is_inbound_and_the_caller_is_the_farmer() -> None:
    decided = classify_direction(_start(from_number="+919876500001", to_number="01800123456"), OURS)
    assert decided.direction is CallDirection.INBOUND
    assert decided.farmer_number == "+919876500001"
    assert decided.contact_id is None


@pytest.mark.parametrize(
    ("from_number", "to_number"),
    [
        ("+911409876543", "+919876500001"),
        ("+919876500001", "1409876543"),
    ],
)
def test_a_call_on_our_outbound_line_is_outbound_whichever_side_we_are_on(
    from_number: str, to_number: str
) -> None:
    decided = classify_direction(_start(from_number=from_number, to_number=to_number), OURS)
    assert decided.direction is CallDirection.OUTBOUND
    assert decided.farmer_number == next(
        n for n in (from_number, to_number) if n.endswith("500001")
    )


def test_the_dialers_custom_field_names_the_contact() -> None:
    contact = uuid.uuid4()
    decided = classify_direction(
        _start(
            from_number="+919876500001",
            to_number="+919876500002",
            extra={"custom_parameters": {"CustomField": f"contact:{contact}"}},
        ),
        None,
    )
    assert decided.direction is CallDirection.OUTBOUND
    assert decided.contact_id == contact


def test_a_panel_test_call_names_the_draft_to_speak() -> None:
    draft = uuid.uuid4()
    decided = classify_direction(
        _start(from_number="+919876500001", extra={"CustomField": f"test:{draft}"}), OURS
    )
    assert decided.direction is CallDirection.OUTBOUND
    assert decided.config_id == draft
    assert decided.contact_id is None


def test_with_nothing_configured_every_call_is_inbound() -> None:
    decided = classify_direction(_start(from_number="+919876500001"), None)
    assert decided.direction is CallDirection.INBOUND
