"""The post-call pipeline (§11.5).

Runs in the background within 30 seconds of a call ending: consolidate the
transcript, upload the recording, summarise, extract entities, set the outcome,
compute cost and latency, create or update tickets, notify the centre manager,
update the farmer profile, push metrics.

Two properties from §11.5 shape the whole design.

**Every stage is idempotent and retried with backoff.** A stage that ran twice
must produce the same result as running once -- the recording key is derived
from the call reference so a retry overwrites, and the ticket lookup is by call
id so a retry finds the existing row rather than raising a second one. A farmer
called back twice about one complaint learns the system is not paying attention.

**A failure here never loses the call record.** The `calls` row was written
incrementally *during* the call (§1 N8), so everything below is enrichment. That
inverts the usual error handling: a stage that fails is logged and skipped, and
the pipeline continues to the next one, because a summary that failed to
generate must not also cost the operator the cost figures and the ticket.

The stages are ordered by what a person needs soonest. The ticket and the
manager notification come before the summary, because somebody may be waiting
on a callback commitment the agent made out loud -- and a summary nobody has
read yet is worth less than a callback that happens.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.models import Call, CallTurn, CampaignContact, Farmer, Ticket
from uaagro_domain.enums import CallOutcome, ContactStatus, TicketPriority, TicketStatus, TicketType
from uaagro_domain.livefeed import CAMPAIGN_UPDATED, CONTACT_UPDATED, LiveEvent, LiveFeed

from .contacts import contact_result_from_call, panel_status

log = structlog.get_logger(__name__)

#: §11.5's budget. Exceeding it is logged rather than aborted -- a slow pipeline
#: that finishes is better than a fast one that gives up on a call record.
BUDGET_SECONDS = 30.0


@dataclass
class StageResult:
    """What one stage did, for the pipeline's own log line."""

    name: str
    ok: bool
    skipped: bool = False
    detail: str = ""


@dataclass
class PipelineReport:
    call_ref: str
    stages: list[StageResult] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def failed(self) -> list[StageResult]:
        return [s for s in self.stages if not s.ok and not s.skipped]

    def summary(self) -> str:
        ok = sum(1 for s in self.stages if s.ok)
        return (
            f"{self.call_ref}: {ok}/{len(self.stages)} stages in "
            f"{self.elapsed_s:.1f}s"
            + (f", failed: {', '.join(s.name for s in self.failed)}" if self.failed else "")
        )


Summariser = Callable[[str, str], Awaitable[tuple[str, str]]]
"""``(transcript, language) -> (hindi_summary, english_summary)``."""

Notifier = Callable[[uuid.UUID, str], Awaitable[None]]
"""``(centre_id, message) -> None``. WhatsApp or email (§11.5)."""


async def run(
    session: AsyncSession,
    call_id: uuid.UUID,
    *,
    summarise: Summariser | None = None,
    notify: Notifier | None = None,
    upload_recording: Callable[[Call], Awaitable[str | None]] | None = None,
    live_feed: LiveFeed | None = None,
) -> PipelineReport:
    """Run every §11.5 stage for one call.

    Each stage is wrapped so a failure is recorded and the next one still runs.
    That is deliberate and is the opposite of how a request handler should
    behave: here the expensive thing has already happened -- the call -- and
    partial enrichment beats none.
    """
    import time

    started = time.perf_counter()
    # A select, not `session.get`. `calls` is range-partitioned on started_at
    # (§10), so its primary key is the composite (id, started_at) -- and the
    # job that enqueued this has only the id. `session.get` raises on the
    # partial key rather than returning None, so every run would fail.
    call = await session.scalar(select(Call).where(Call.id == call_id))
    if call is None:
        return PipelineReport(call_ref=str(call_id), stages=[
            StageResult("load", ok=False, detail="call not found")
        ])

    report = PipelineReport(call_ref=call.call_ref)

    async def stage(name: str, work: Callable[[], Awaitable[str]]) -> None:
        try:
            detail = await work()
            report.stages.append(StageResult(name, ok=True, detail=detail))
        except Exception as exc:
            # Logged with the type only. A traceback here would carry transcript
            # fragments into the log aggregator (§19, §23-6).
            log.error("postcall.stage_failed", stage=name, error=type(exc).__name__)
            report.stages.append(StageResult(name, ok=False, detail=type(exc).__name__))

    await stage("transcript", lambda: _consolidate_transcript(session, call))
    await stage("outcome", lambda: _set_outcome(session, call))
    # Right after the outcome, which it reads: the campaign card should turn
    # green within seconds of the call ending, not after the summary.
    await stage("campaign", lambda: _finish_campaign_contact(session, call, live_feed))
    # Before the summary: somebody may be waiting on a callback the agent
    # committed to out loud, and a summary nobody has read is worth less.
    await stage("tickets", lambda: _ensure_ticket(session, call))
    await stage("recording", lambda: _upload(call, upload_recording))
    await stage("cost", lambda: _finalise_cost(call))
    await stage("summary", lambda: _summarise(session, call, summarise))
    await stage("farmer", lambda: _update_farmer(session, call))
    await stage("notify", lambda: _notify(call, notify))

    report.elapsed_s = time.perf_counter() - started
    if report.elapsed_s > BUDGET_SECONDS:
        log.warning(
            "postcall.over_budget",
            call_ref=call.call_ref,
            elapsed_s=round(report.elapsed_s, 1),
            budget_s=BUDGET_SECONDS,
        )
    log.info("postcall.complete", summary=report.summary())
    return report


# --------------------------------------------------------------------------- #
# Stages
# --------------------------------------------------------------------------- #


async def _consolidate_transcript(session: AsyncSession, call: Call) -> str:
    """Mark the transcript complete once every turn is written.

    Idempotent: re-running sets an already-true flag. The turns themselves were
    written during the call and are not touched here -- rewriting them would
    risk losing a turn to a race with a late-arriving write.
    """
    turns = int(
        await session.scalar(
            select(CallTurn.turn_index)
            .where(CallTurn.call_id == call.id)
            .order_by(CallTurn.turn_index.desc())
            .limit(1)
        )
        or 0
    )
    call.transcript_ready = True
    return f"{turns + 1} turns"


async def _set_outcome(session: AsyncSession, call: Call) -> str:
    """Fill in the disposition if the call ended without one.

    A call that dropped mid-conversation has no outcome, and leaving it null
    makes it invisible to §15's dashboard -- which reports on outcome. Guessing
    is not the answer either, so the guess is the *conservative* one: a
    transferred call is transferred, and anything else that simply stopped is
    recorded as the caller hanging up rather than as a system failure.
    """
    if call.outcome is not None:
        return f"already {call.outcome.value}"

    if call.was_transferred:
        call.outcome = CallOutcome.TRANSFERRED
    elif call.answered_at is None:
        call.outcome = CallOutcome.NOT_REACHED
    else:
        call.outcome = CallOutcome.CALLER_HUNG_UP

    _ = session
    return call.outcome.value


async def _finish_campaign_contact(
    session: AsyncSession, call: Call, live_feed: LiveFeed | None
) -> str:
    """Close the loop on the campaign contact this call served (§13.1).

    The media path normally writes the contact's result itself. This is the
    backstop for the call that ended before the script could decide -- and
    it is also where the panel is told, because by now the outcome is final.
    """
    if call.campaign_id is None:
        return "not a campaign call"

    contact = await session.scalar(
        select(CampaignContact).where(CampaignContact.call_id == call.id)
    )
    if contact is None:
        hashes = [h for h in (call.from_number_hash, call.to_number_hash) if h]
        if hashes:
            contact = await session.scalar(
                select(CampaignContact)
                .where(
                    CampaignContact.campaign_id == call.campaign_id,
                    CampaignContact.phone_hash.in_(hashes),
                )
                .order_by(CampaignContact.last_attempt_at.desc().nulls_last())
                .limit(1)
            )
    if contact is None:
        return "no contact for this call"

    contact.call_id = call.id
    if contact.status is ContactStatus.DIALING or contact.outcome is None:
        contact.status, contact.outcome = contact_result_from_call(call.outcome)
    await session.flush()

    if live_feed is not None:
        farmer = (
            await session.execute(
                select(Farmer.full_name, Farmer.phone_last4).where(Farmer.id == contact.farmer_id)
            )
        ).first()
        live_feed.publish(
            LiveEvent(
                type=CONTACT_UPDATED,
                payload={
                    "id": str(contact.id),
                    "farmerName": farmer[0] if farmer else None,
                    "last4": str(farmer[1]) if farmer else "",
                    "status": panel_status(contact.status),
                    "outcome": contact.outcome,
                    "dtmf": contact.dtmf_response,
                    "callId": str(call.id),
                    "attempts": contact.attempts,
                },
                campaign_id=str(call.campaign_id),
                call_id=str(call.id),
            )
        )
        live_feed.publish(
            LiveEvent(
                type=CAMPAIGN_UPDATED,
                payload={"id": str(call.campaign_id)},
                campaign_id=str(call.campaign_id),
            )
        )
    return f"{contact.status.value} ({contact.outcome})"


async def _ensure_ticket(session: AsyncSession, call: Call) -> str:
    """§11.4: a call that ended mid-resolution gets a callback ticket.

    Looked up by ``call_id`` before creating, so a retry finds the existing row.
    §11.5 requires every stage to be idempotent, and a duplicate here means a
    farmer is called back twice about one thing.
    """
    existing = await session.scalar(select(Ticket).where(Ticket.call_id == call.id))
    if existing is not None:
        return f"ticket {existing.ticket_ref} already exists"

    # Only for calls that were actually mid-resolution. A resolved call and an
    # unanswered one both end without a ticket, for opposite reasons.
    needs_followup = call.outcome in (
        CallOutcome.ABANDONED_SILENCE,
        CallOutcome.SYSTEM_FAILURE,
    ) or (call.was_transferred and not call.transfer_completed)
    if not needs_followup:
        return "no follow-up needed"

    from datetime import timedelta

    ticket = Ticket(
        organization_id=call.organization_id,
        ticket_ref=f"TKT-{datetime.now(UTC):%y%m%d}-{uuid.uuid4().hex[:6].upper()}",
        centre_id=call.centre_id,
        farmer_id=call.farmer_id,
        call_id=call.id,
        type=TicketType.CALLBACK,
        priority=TicketPriority.P1,
        status=TicketStatus.OPEN,
        subject="Call ended before the farmer's question was resolved",
        # The schema refuses a callback without a commitment (§10), so the due
        # time is set here rather than left for the centre to decide.
        due_at=datetime.now(UTC) + timedelta(hours=24),
    )
    session.add(ticket)
    await session.flush()
    return f"created {ticket.ticket_ref}"


async def _upload(
    call: Call, upload: Callable[[Call], Awaitable[str | None]] | None
) -> str:
    """§11.5's recording upload.

    Idempotent because the object key is derived from the call reference, so a
    retry overwrites rather than accumulating copies -- and an auditor asked
    which copy is real has one answer.
    """
    if upload is None:
        return "no uploader configured"
    if call.recording_object_key:
        return "already uploaded"
    key = await upload(call)
    if key:
        call.recording_object_key = key
    # The key, never a URL. §23-6 treats a recording URL like a phone number.
    return key or "nothing to upload"


async def _finalise_cost(call: Call) -> str:
    """Total the §8 breakdown written during the call.

    Summed in ``Decimal``, not float. The four components of a typical call
    (₹1.2 + ₹2.1 + ₹1.9 + ₹0.9) come to 6.100000000000001 in binary floating
    point, and this figure is not decoration: §8 checks it against
    ``max_cost_per_call_inr`` and accumulates it into the daily spend cap. A
    cap enforced against drifting sums is a cap that reports the wrong number
    on the day somebody asks why calling stopped.

    ``cost_total_inr`` is Numeric in the schema, so a float assigned here comes
    back as a Decimal on the next load -- which is how this surfaced.
    """
    breakdown = call.cost_breakdown or {}
    components = [
        amount
        for key, value in breakdown.items()
        if key != "total" and (amount := _as_decimal(value)) is not None
    ]
    if not components:
        return "no components recorded"

    # Four places: §8 prices per-token and per-second, so a call's components
    # are fractions of a paisa and rounding to two would lose most of them.
    total = sum(components, start=Decimal(0)).quantize(Decimal("0.0001"), ROUND_HALF_UP)
    call.cost_total_inr = total
    # The JSON copy is for the panel's breakdown chart and is float because
    # that is what JSON has. The Numeric column above is the authoritative one.
    call.cost_breakdown = {**breakdown, "total": float(total)}
    return f"₹{total.normalize():f}"


def _as_decimal(value: object) -> Decimal | None:
    """A component as an exact decimal, or None if it is not a number.

    Via ``str``: ``Decimal(1.2)`` is 1.1999999999999999555910790149937383830547,
    which defeats the point of summing in Decimal at all.
    """
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return None if amount.is_nan() or amount.is_infinite() else amount


async def _summarise(
    session: AsyncSession, call: Call, summarise: Summariser | None
) -> str:
    """§11.5: Hindi *and* English summaries.

    Both, because the audiences differ: a centre manager in Barabanki reads the
    Hindi one and a regional report is written from the English one. Generating
    one and translating later loses the detail that made it useful.
    """
    if summarise is None:
        return "no summariser configured"
    if call.summary_hi:
        return "already summarised"

    turns = (
        await session.scalars(
            select(CallTurn).where(CallTurn.call_id == call.id).order_by(CallTurn.turn_index)
        )
    ).all()
    if not turns:
        return "no turns to summarise"

    # The *original* transcript, not the normalised one. Normalisation expands
    # numbers into words for the TTS ("बारह सौ पचास"), and a summary built from
    # that reads as though the farmer spoke in words when they said "1250".
    transcript = "\n".join(
        f"{turn.role.value}: {turn.text_original}"
        for turn in turns
        if turn.text_original
    )
    hindi, english = await summarise(transcript, call.language_final or "hi-IN")
    call.summary_hi = hindi
    call.summary_en = english
    return f"{len(turns)} turns summarised"


async def _update_farmer(session: AsyncSession, call: Call) -> str:
    """§11.5: language, crops and the speech profile.

    The language is the interesting one. §11.1 locks it from the first
    utterance, and persisting it means the *next* call starts in the right
    language instead of guessing again -- which is the difference between a
    farmer being recognised and being re-interrogated.
    """
    if call.farmer_id is None:
        return "unknown caller"
    farmer = await session.get(Farmer, call.farmer_id)
    if farmer is None:
        return "farmer row missing"

    changes: list[str] = []
    if call.language_final and farmer.preferred_language != call.language_final:
        farmer.preferred_language = call.language_final
        changes.append("language")

    farmer.last_contact_at = call.ended_at or datetime.now(UTC)
    changes.append("last_contact")
    return ", ".join(changes)


async def _notify(call: Call, notify: Notifier | None) -> str:
    """§11.5: tell the centre manager when action is required.

    Only when action is required. A notification on every call is a
    notification nobody reads, and the one that mattered is in the middle of
    it.
    """
    if notify is None or call.centre_id is None:
        return "no notifier configured"

    if call.outcome not in (CallOutcome.TRANSFERRED, CallOutcome.TICKET_CREATED):
        return "no action required"

    await notify(
        call.centre_id,
        # No phone number, no transcript. The manager opens the call in the
        # panel, which is where the audited view of a caller's details lives.
        f"Call {call.call_ref} needs follow-up ({call.outcome.value}).",
    )
    return "notified"


__all__ = ("BUDGET_SECONDS", "PipelineReport", "StageResult", "run")
