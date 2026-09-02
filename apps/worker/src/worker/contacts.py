"""What a finished call means for the campaign contact behind it (§13.1, §13.3).

The media path writes the contact's result when the script decides it. Two
paths need the same reading from the *call* instead: the post-call pipeline,
for a call that ended before the script could decide (the line dropped, the
worker restarted), and the reconciliation job, for a dial that never became
a call at all. Both derive the contact's status from the call outcome here,
so the panel's card and the call's outcome cannot disagree by construction.
"""

from __future__ import annotations

from uaagro_domain.enums import CallOutcome, ContactStatus

#: The panel's outcome vocabulary, by what the call recorded.
_BY_CALL_OUTCOME: dict[CallOutcome, tuple[ContactStatus, str]] = {
    CallOutcome.OFFER_ACCEPTED: (ContactStatus.COMPLETED, "pressed_1"),
    CallOutcome.OFFER_DECLINED: (ContactStatus.COMPLETED, "talked"),
    CallOutcome.OPTED_OUT: (ContactStatus.OPTED_OUT, "opted_out"),
    CallOutcome.NOT_REACHED: (ContactStatus.NO_ANSWER, "no_answer"),
    CallOutcome.SYSTEM_FAILURE: (ContactStatus.FAILED, "failed"),
    CallOutcome.REJECTED_SPAM: (ContactStatus.FAILED, "failed"),
    CallOutcome.ABANDONED_SILENCE: (ContactStatus.NO_ANSWER, "no_answer"),
}


def contact_result_from_call(outcome: CallOutcome | None) -> tuple[ContactStatus, str]:
    """The contact's status and panel outcome for a call that ended this way.

    A call that connected and ended without a decision -- the farmer hung up
    mid-message, a transfer, a resolved question -- counts as talked: the
    farmer heard a person-shaped voice and the retry policy should not ring
    them again for this campaign.
    """
    if outcome is None:
        return ContactStatus.NO_ANSWER, "no_answer"
    return _BY_CALL_OUTCOME.get(outcome, (ContactStatus.COMPLETED, "talked"))


def panel_status(status: ContactStatus) -> str:
    """The contract's four-state vocabulary for a stored contact status."""
    match status:
        case ContactStatus.PENDING:
            return "waiting"
        case ContactStatus.DIALING:
            return "in_call"
        case ContactStatus.COMPLETED | ContactStatus.OPTED_OUT:
            return "done"
        case ContactStatus.NO_ANSWER | ContactStatus.BUSY | ContactStatus.FAILED:
            return "no_answer"
        case _:
            return "removed"


__all__ = ("contact_result_from_call", "panel_status")
