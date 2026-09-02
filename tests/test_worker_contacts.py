"""What a finished call means for its campaign contact (§13.1, §13.3)."""

from __future__ import annotations

from uaagro_domain.enums import CallOutcome, ContactStatus
from worker.contacts import contact_result_from_call, panel_status


def test_every_call_outcome_maps_to_a_card_state() -> None:
    for outcome in CallOutcome:
        status, label = contact_result_from_call(outcome)
        assert isinstance(status, ContactStatus)
        assert label


def test_the_decisive_outcomes_keep_their_meaning() -> None:
    assert contact_result_from_call(CallOutcome.OFFER_ACCEPTED) == (
        ContactStatus.COMPLETED,
        "pressed_1",
    )
    assert contact_result_from_call(CallOutcome.OPTED_OUT) == (ContactStatus.OPTED_OUT, "opted_out")
    assert contact_result_from_call(CallOutcome.NOT_REACHED) == (
        ContactStatus.NO_ANSWER,
        "no_answer",
    )
    assert contact_result_from_call(None) == (ContactStatus.NO_ANSWER, "no_answer")


def test_a_connected_call_without_a_decision_counts_as_talked() -> None:
    """The farmer heard a voice; the retry policy must not ring them again."""
    assert contact_result_from_call(CallOutcome.CALLER_HUNG_UP)[1] == "talked"
    assert contact_result_from_call(CallOutcome.TRANSFERRED)[1] == "talked"


def test_stored_statuses_collapse_to_the_panels_four_colours() -> None:
    assert panel_status(ContactStatus.PENDING) == "waiting"
    assert panel_status(ContactStatus.DIALING) == "in_call"
    assert panel_status(ContactStatus.COMPLETED) == "done"
    assert panel_status(ContactStatus.OPTED_OUT) == "done"
    assert panel_status(ContactStatus.NO_ANSWER) == "no_answer"
    assert panel_status(ContactStatus.FAILED) == "no_answer"
    assert panel_status(ContactStatus.SCRUBBED_OUT) == "removed"
    assert panel_status(ContactStatus.EXCLUDED_CONSENT) == "removed"
