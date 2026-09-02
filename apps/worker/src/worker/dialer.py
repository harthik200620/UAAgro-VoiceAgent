"""The outbound dialer (§13.1 RUNNING).

Respects ``max_concurrent_calls``, the calling window and per-minute pacing; can
be paused instantly, where a pause stops new dials and lets in-flight calls
finish.

**The gate is re-evaluated at dial time, per contact, every time.** Not once at
campaign start. §13.1 is explicit that the calling window is enforced by the
dialer rather than by the schedule, and the reason generalises to every other
check: a campaign approved at 09:00 with 400 eligible contacts is still running
at 21:05, and by then some of those contacts have opted out on another call,
some have hit the weekly cap, and the window has closed. A gate evaluated once
is a gate that was true once.

That costs a query per dial. It is the correct trade: a wrong dial is a
regulatory violation and a database round trip is a millisecond.

**Pacing is a floor on the gap between dials, not a token bucket.** A bucket
lets a paused-then-resumed campaign burst its accumulated allowance, which is
exactly the shape of a complaint — forty farmers phoned in the same minute.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog

from uaagro_domain.compliance import (
    Check,
    Contact,
    next_retry,
    within_calling_window,
)
from uaagro_domain.timezone import now_ist

log = structlog.get_logger(__name__)


class DialerState:
    """Shared, mutable, and deliberately simple.

    A pause has to take effect between one dial and the next, so it is a flag
    checked in the loop rather than a message the loop might not read for a
    minute. §13.1: "It can be paused instantly."
    """

    def __init__(self) -> None:
        self.paused = False
        self.cancelled = False


@dataclass
class DialOutcome:
    contact_id: uuid.UUID
    dialled: bool
    #: Set when the gate refused this contact at dial time.
    skipped_by: Check | None = None
    call_sid: str | None = None
    error: str | None = None


@dataclass
class CampaignRun:
    """One campaign's dialling session."""

    campaign_id: uuid.UUID
    outcomes: list[DialOutcome] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    stopped_reason: str | None = None

    @property
    def dialled(self) -> int:
        return sum(1 for o in self.outcomes if o.dialled)

    @property
    def skipped(self) -> int:
        return sum(1 for o in self.outcomes if o.skipped_by is not None)

    def summary(self) -> str:
        return (
            f"campaign {self.campaign_id}: {self.dialled} dialled, "
            f"{self.skipped} skipped at dial time"
            + (f", stopped: {self.stopped_reason}" if self.stopped_reason else "")
        )


Dial = Callable[[Contact], Awaitable[str]]
"""Places one call and returns the provider SID."""

Recheck = Callable[[Contact], Awaitable[Check | None]]
#: The panel's pause/stop, read from outside the process: "paused",
#: "cancelled" or None.
Control = Callable[[], Awaitable[str | None]]
"""Re-runs the per-contact checks. ``None`` means still eligible."""


@dataclass
class Dialer:
    """§13.1's RUNNING state."""

    dial: Dial
    recheck: Recheck
    max_concurrent: int = 10
    calls_per_minute: int = 20
    state: DialerState = field(default_factory=DialerState)
    #: Asked before every dial for an operator's instruction from outside this
    #: process -- "paused" or "cancelled" from the panel, None to carry on.
    #: The in-memory state above is what a test drives; this is what a
    #: running campaign in another container listens to.
    control: Control | None = None

    @property
    def _min_gap_s(self) -> float:
        return 60.0 / max(1, self.calls_per_minute)

    async def run(
        self, campaign_id: uuid.UUID, contacts: list[Contact]
    ) -> CampaignRun:
        """Dial a campaign's contacts, respecting every §13.1 constraint."""
        run = CampaignRun(campaign_id=campaign_id)
        semaphore = asyncio.Semaphore(self.max_concurrent)
        in_flight: list[asyncio.Task[None]] = []

        for contact in contacts:
            if self.control is not None:
                await self._apply_control()
            if self.state.cancelled:
                run.stopped_reason = "cancelled"
                break
            if self.state.paused:
                # §13.1: a pause stops new dials and lets in-flight calls
                # finish. Breaking rather than sleeping-and-polling means the
                # in-flight ones are awaited below and nothing is abandoned
                # mid-conversation.
                run.stopped_reason = "paused"
                break

            # Checked here, not only at approval. A campaign approved at 09:00
            # is still running at 21:05, and only a dial-time check catches it.
            if not within_calling_window(now_ist()):
                run.stopped_reason = "outside the calling window"
                log.warning("dialer.window_closed", campaign_id=str(campaign_id))
                break

            failure = await self.recheck(contact)
            if failure is not None:
                run.outcomes.append(
                    DialOutcome(contact_id=contact.farmer_id, dialled=False, skipped_by=failure)
                )
                continue

            async def one(target: Contact = contact) -> None:
                async with semaphore:
                    try:
                        sid = await self.dial(target)
                        run.outcomes.append(
                            DialOutcome(
                                contact_id=target.farmer_id, dialled=True, call_sid=sid
                            )
                        )
                    except Exception as exc:
                        # One failed dial does not stop a campaign. The retry
                        # policy in §13.3 decides whether it is tried again.
                        log.warning("dialer.dial_failed", error=type(exc).__name__)
                        run.outcomes.append(
                            DialOutcome(
                                contact_id=target.farmer_id,
                                dialled=False,
                                error=type(exc).__name__,
                            )
                        )

            in_flight.append(asyncio.create_task(one()))
            # A floor on the gap, not a bucket. A bucket lets a resumed campaign
            # burst its accumulated allowance -- forty farmers phoned in one
            # minute, which is the shape of a complaint.
            await asyncio.sleep(self._min_gap_s)

        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)

        log.info("dialer.run_complete", summary=run.summary())
        return run

    async def _apply_control(self) -> None:
        """Read the operator's instruction.

        A control channel that is down is treated as silence: the campaign
        carries on under the rules it was approved with, which are checked
        per contact anyway.
        """
        assert self.control is not None
        try:
            signal = await self.control()
        except Exception as exc:
            log.warning("dialer.control_unreachable", error=type(exc).__name__)
            return
        if signal == "paused" and not self.state.paused:
            self.pause("panel")
        elif signal == "cancelled" and not self.state.cancelled:
            self.cancel()

    def pause(self, reason: str = "operator") -> None:
        self.state.paused = True
        log.info("dialer.paused", reason=reason)

    def resume(self) -> None:
        self.state.paused = False
        log.info("dialer.resumed")

    def cancel(self) -> None:
        self.state.cancelled = True
        log.info("dialer.cancelled")


def schedule_retry(
    outcome: str, *, last_attempt: datetime, attempts: int
) -> datetime | None:
    """§13.3's retry table, re-exported so the dialer and the API agree.

    One implementation, two callers. Two copies of a retry policy is how "opted
    out" eventually gets retried by whichever copy nobody updated.
    """
    return next_retry(outcome, last_attempt=last_attempt, attempts=attempts)


__all__ = ("CampaignRun", "DialOutcome", "Dialer", "DialerState", "schedule_retry")
