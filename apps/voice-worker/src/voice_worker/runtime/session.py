"""Per-call session: transport lifecycle and durable call record.

Phase 1 scope. This handles the media WebSocket, identifies the call, writes the
``calls`` row and its events, records DTMF, and returns audio so the transport
round-trip is demonstrably working. Speech recognition, the LLM and synthesis
arrive in Phase 2 -- the seam they plug into is :meth:`CallSession.on_audio`.

Two invariants that hold from Phase 1 onward:

* **§1 N8 -- every call produces a record.** The ``calls`` row is written at
  INIT and updated incrementally, so a worker crash or a dropped socket still
  leaves a queryable call, marked as interrupted rather than lost.
* **§7.6 -- the audio loop never blocks on I/O.** Database writes are handed to
  a background drain task through a queue. A slow query delays persistence, not
  the caller's audio.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from uaagro_db.crypto import get_cipher
from uaagro_domain import livefeed
from uaagro_domain.enums import (
    CallDirection,
    CallOutcome,
    CallStatus,
    ContactStatus,
    InterestLevel,
    TelephonyProvider,
    TurnRole,
)
from uaagro_domain.livefeed import LiveEvent, LiveFeed, NullLiveFeed
from uaagro_domain.logging import bind_call, clear_call
from uaagro_domain.phone import normalise_msisdn

from ..adapters.telephony.base import (
    CallMetadata,
    InboundEvent,
    InboundEventType,
    TelephonySerializer,
)
from . import audio as audio_utils
from .direction import OurNumbers, classify_direction

if TYPE_CHECKING:
    from ..pipelines.conversation import TurnOutcome

log = structlog.get_logger(__name__)

#: Bound on the persistence queue. If writes fall this far behind, dropping
#: events is better than growing memory without limit during a peak -- and the
#: drop is logged loudly rather than silently.
PERSIST_QUEUE_MAX = 512

#: How long a drain task is given to flush after the call ends.
DRAIN_TIMEOUT_S = 10.0

#: When the script has said its closing line, how long to let the provider
#: play out its buffer before the socket is closed. The sender paces audio in
#: real time, so what remains is the provider's own jitter buffer, not the
#: sentence.
HANGUP_GRACE_S = 0.8
#: §12.3-6, spoken when the hand-over could not be made: a commitment, never
#: "please call back later".
TRANSFER_FAILED_LINE_HI = (
    "माफ़ कीजिए, अभी हमारे साथी से बात नहीं हो पा रही है। "
    "मैंने आपकी बात दर्ज कर ली है, केंद्र से आपको चौबीस घंटे के अंदर फ़ोन आएगा।"
)


class TransportClosed(Exception):
    """The caller's connection went away.

    Deliberately distinct from a genuine error. On a rural GSM line a dropped
    connection is a routine outcome, not a fault (§3), and §11.4 wants such a
    call marked *interrupted* rather than failed. Classifying it as a system
    failure would poison the failed-call rate the §15 dashboard shows and send
    the operator chasing an incident that never happened.
    """


class Transport(Protocol):
    """The subset of a WebSocket the session needs.

    Narrow on purpose, so the simulator and a real provider socket are
    interchangeable without either importing the other. Implementations
    translate their framework's disconnect into :class:`TransportClosed`.
    """

    async def send_text(self, data: str) -> None: ...
    async def receive_text(self) -> str: ...


class CallRepository(Protocol):
    """Persistence the session depends on.

    A protocol rather than a concrete class so the transport tests can run
    without a database, while production uses the SQLAlchemy implementation.
    """

    async def create_call(self, record: CallRecord) -> None: ...
    async def attach_call_context(
        self,
        call_id: uuid.UUID,
        started_at: datetime,
        *,
        farmer_id: uuid.UUID | None,
        centre_id: uuid.UUID | None,
        campaign_id: uuid.UUID | None,
        language: str | None,
        agent_config_version: int | None,
    ) -> None: ...
    async def record_turn(
        self,
        call_id: uuid.UUID,
        started_at: datetime,
        *,
        turn_index: int,
        role: TurnRole,
        text: str,
        language: str | None,
        at_ms: int,
        latency: dict[str, Any] | None,
        tool_calls: list[dict[str, Any]],
        interrupted: bool,
    ) -> None: ...
    async def record_event(
        self, call_id: uuid.UUID, event_type: str, payload: dict[str, Any], started_at: datetime
    ) -> None: ...
    async def record_dtmf(
        self,
        call_id: uuid.UUID,
        digit: str,
        received_at: datetime,
        started_at: datetime,
        context: str | None,
    ) -> None: ...
    async def link_contact(self, contact_id: uuid.UUID, call_id: uuid.UUID) -> None: ...
    async def mark_transferred(
        self, call_id: uuid.UUID, started_at: datetime, *, reason: str, completed: bool
    ) -> None: ...
    async def finish_contact(
        self,
        contact_id: uuid.UUID,
        *,
        status: ContactStatus,
        outcome: str | None,
        dtmf: str | None,
        interest: InterestLevel | None,
    ) -> None: ...
    async def finalise_call(
        self,
        call_id: uuid.UUID,
        started_at: datetime,
        *,
        status: CallStatus,
        outcome: CallOutcome | None,
        ended_at: datetime,
        duration_seconds: int,
        error_code: str | None,
        error_detail: str | None,
        latency_stats: dict[str, Any] | None = None,
    ) -> None: ...


@dataclass(slots=True)
class CallRecord:
    """The identity of a call, as known at INIT."""

    id: uuid.UUID
    started_at: datetime
    call_ref: str
    direction: CallDirection
    provider: TelephonyProvider
    from_number_hash: bytes | None = None
    to_number_hash: bytes | None = None
    organization_id: uuid.UUID | None = None


@dataclass(slots=True)
class CallStats:
    """Counters the session accumulates. Written to the call record at close."""

    inbound_frames: int = 0
    inbound_bytes: int = 0
    outbound_frames: int = 0
    outbound_bytes: int = 0
    dtmf_digits: list[str] = field(default_factory=list)
    unknown_events: int = 0
    persist_drops: int = 0

    @property
    def inbound_audio_ms(self) -> int:
        return audio_utils.duration_ms(b"\x00" * self.inbound_bytes)


def _hash_number(number: str | None) -> bytes | None:
    """HMAC a caller number for lookup, or None if it is unusable.

    Withheld and malformed numbers are both normal on a rural GSM line, and
    neither is an error: the call proceeds with an unidentified caller, which
    is the same path a first-time farmer takes.
    """
    if not number:
        return None
    try:
        return get_cipher().hash(normalise_msisdn(number))
    except Exception:
        # Never logged with the number attached, and never re-raised: a
        # malformed CLI must not cost the call.
        return None


class PipelineFactory(Protocol):
    """Builds the conversational pipeline for one call.

    Takes the session because the pipeline needs its audio sink and its
    buffer-clear -- barge-in is only honest when the thing that generates and
    the thing that plays are the same call (§5.4).
    """

    async def __call__(self, session: CallSession) -> Any: ...


class CallSession:
    """Drives one call from ``connected`` to ``stop``."""

    def __init__(
        self,
        *,
        transport: Transport,
        serializer: TelephonySerializer,
        repository: CallRepository | None = None,
        direction: CallDirection = CallDirection.INBOUND,
        greeting_pcm: bytes | None = None,
        organization_id: uuid.UUID | None = None,
        pipeline_factory: PipelineFactory | None = None,
        on_finished: Callable[[uuid.UUID], Awaitable[None]] | None = None,
        live_feed: LiveFeed | None = None,
        our_numbers: OurNumbers | None = None,
        transfer_adapter: Callable[[frozenset[str]], Any] | None = None,
    ) -> None:
        self._transport = transport
        self._serializer = serializer
        self._repository = repository
        self._direction = direction
        self._greeting_pcm = greeting_pcm
        self._organization_id = organization_id
        # Injected rather than constructed here: this class owns the transport
        # and the call record, and knows nothing about speech. `main` supplies
        # the real factory; the protocol tests supply none and still exercise
        # every frame path.
        self._pipeline_factory = pipeline_factory
        self._on_finished = on_finished
        self._live_feed: LiveFeed = live_feed if live_feed is not None else NullLiveFeed()
        # The numbers this deployment owns, so the start frame can be read
        # as "one we placed" or "one we answered" (see `direction`).
        self._our_numbers = our_numbers
        # Builds the provider's call-control client for one approved
        # destination (§17: a transfer target is never a caller-supplied
        # value). None on a worker with no telephony configured, where a
        # hand-over becomes a callback commitment instead.
        self._transfer_adapter = transfer_adapter
        self._transferred = False
        self._call_sid = ""
        self._pipeline: Any | None = None
        self._pipeline_task: asyncio.Task[None] | None = None
        self._built: Any | None = None

        self.call_id = uuid.uuid4()
        self.started_at = datetime.now(UTC)
        self.metadata: CallMetadata | None = None
        self.from_number_hash: bytes | None = None
        #: The hash of whichever number belongs to the farmer -- the caller
        #: on an inbound call, the person we dialled on an outbound one.
        self.farmer_number_hash: bytes | None = None
        self.contact_id: uuid.UUID | None = None
        #: Set on a panel test call: the draft script version to speak.
        self.config_id: uuid.UUID | None = None
        self.stats = CallStats()
        self.status = CallStatus.IN_PROGRESS
        self.outcome: CallOutcome | None = None

        self._persist_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(
            maxsize=PERSIST_QUEUE_MAX
        )
        self._drain_task: asyncio.Task[None] | None = None
        self._closed = False
        #: Set when the conversation wants the call ended from our side --
        #: the outbound script's closing line has been spoken.
        self._hangup = asyncio.Event()
        #: Wall-clock moment the current turn's thinking began, which is the
        #: closest thing to "the farmer stopped speaking" the session sees.
        self._turn_started_wall: datetime | None = None
        self._first_reply_ms: int | None = None
        self._reply_totals: list[float] = []
        self._turns_seen = 0

    @property
    def direction(self) -> CallDirection:
        return self._direction

    # -- lifecycle -------------------------------------------------------- #

    async def run(self) -> CallStats:
        """Process the call until the socket closes.

        Returns the accumulated stats. Never raises for a transport failure --
        a dropped socket is a normal outcome on a rural GSM line, and §11.4
        requires the partial transcript to be persisted and the call marked
        interrupted rather than lost.
        """
        bind_call(str(self.call_id), direction=self._direction.value)
        self._drain_task = asyncio.create_task(self._drain())
        error_code: str | None = None
        error_detail: str | None = None

        hangup = asyncio.ensure_future(self._hangup.wait())
        try:
            while True:
                # Two things can end the loop: the provider's stop frame, or
                # our own decision to hang up after a scripted closing. The
                # receive is raced against the hang-up event so neither has to
                # poll the other.
                receive = asyncio.ensure_future(self._transport.receive_text())
                done, _ = await asyncio.wait({receive, hangup}, return_when=asyncio.FIRST_COMPLETED)
                if hangup in done:
                    receive.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await receive
                    self.status = CallStatus.COMPLETED
                    self._enqueue("hangup", {"by": "agent"})
                    break
                message = receive.result()
                event = self._serializer.decode(message)
                if await self._handle(event) is False:
                    break
        except asyncio.CancelledError:
            self.status = CallStatus.FAILED
            error_code = "worker_shutdown"
            error_detail = "The worker was draining when this call was in progress."
            raise
        except TransportClosed:
            # The line dropped. Routine on rural GSM, so it is recorded as an
            # interrupted call, not a failure (§11.4). The partial record is
            # already persisted; a mid-resolution call becomes a callback
            # ticket once the post-call pipeline lands in Phase 4.
            self.status = CallStatus.ABANDONED
            self.outcome = CallOutcome.CALLER_HUNG_UP
            error_code = "line_dropped"
            log.info("call.line_dropped", frames_in=self.stats.inbound_frames)
        # Anything else is a genuine fault and must never crash the worker.
        except Exception as exc:
            self.status = CallStatus.FAILED
            self.outcome = CallOutcome.SYSTEM_FAILURE
            error_code = type(exc).__name__
            error_detail = str(exc)[:500]
            log.warning(
                "call.transport_error",
                error=type(exc).__name__,
                frames_in=self.stats.inbound_frames,
            )
        finally:
            hangup.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await hangup
            await self._close(error_code=error_code, error_detail=error_detail)
            clear_call()

        return self.stats

    async def _handle(self, event: InboundEvent) -> bool:
        """Process one event. Returns False when the call should end."""
        match event.type:
            case InboundEventType.CONNECTED:
                self._enqueue("connected", {})
                return True

            case InboundEventType.START:
                if event.metadata is not None:
                    await self._on_start(event.metadata)
                return True

            case InboundEventType.MEDIA:
                if event.audio:
                    self.stats.inbound_frames += 1
                    self.stats.inbound_bytes += len(event.audio)
                    await self.on_audio(event.audio)
                return True

            case InboundEventType.DTMF:
                if event.digit:
                    await self._on_dtmf(event.digit)
                return True

            case InboundEventType.STOP:
                self.status = CallStatus.COMPLETED
                if self.outcome is None:
                    self.outcome = CallOutcome.CALLER_HUNG_UP
                self._enqueue("stop", {})
                return False

            case _:
                self.stats.unknown_events += 1
                self._enqueue("unknown_event", {"raw_event": event.raw.get("event")})
                return True

    async def _on_start(self, metadata: CallMetadata) -> None:
        self._serializer.bind(metadata)
        self.metadata = metadata

        # Which way is this call going? Decided from the start frame, before
        # the record is written, because the direction is on the record and
        # decides which conversation is built (§13.2).
        decided = classify_direction(metadata, self._our_numbers)
        self._direction = decided.direction
        self.contact_id = decided.contact_id
        self.config_id = decided.config_id

        bind_call(
            str(self.call_id),
            direction=self._direction.value,
            stream_sid=metadata.stream_sid,
        )
        log.info(
            "call.started",
            provider=self._serializer.provider.value,
            # Hashed and masked by the logging processor, never the raw number.
            from_number=metadata.from_number or "",
            to_number=metadata.to_number or "",
            placed_by_us=self._direction is CallDirection.OUTBOUND,
        )

        # Hashed once, here, and kept for the pipeline. §17 makes the HMAC the
        # only lookup key; the raw number never reaches a column, a log line or
        # the agent's context.
        self.from_number_hash = _hash_number(metadata.from_number)
        self.farmer_number_hash = _hash_number(decided.farmer_number)
        self._call_sid = metadata.call_sid

        if self._repository is not None:
            record = CallRecord(
                id=self.call_id,
                started_at=self.started_at,
                call_ref=metadata.call_sid,
                direction=self._direction,
                provider=self._serializer.provider,
                from_number_hash=self.from_number_hash,
                to_number_hash=_hash_number(metadata.to_number),
                organization_id=self._organization_id,
            )
            # Written directly rather than queued: everything else references
            # this row, so it must exist before any event is persisted (§11.1).
            await self._repository.create_call(record)
            if self.contact_id is not None:
                with contextlib.suppress(Exception):
                    await self._repository.link_contact(self.contact_id, self.call_id)

        self._enqueue(
            "start",
            {"stream_sid": metadata.stream_sid, "call_sid": metadata.call_sid},
        )
        self._publish(
            livefeed.CALL_STARTED,
            {
                "id": str(self.call_id),
                "callRef": metadata.call_sid,
                "startedAt": self.started_at.isoformat(),
                "direction": self._direction.value,
                "centreCode": None,
                "centreName": None,
                "language": "",
                "farmerName": None,
                "callerLast4": None,
                "elapsedSeconds": 0,
                "turnCount": 0,
                "lastIntent": None,
                "activity": "speaking",
                "lastReplyMs": None,
                "campaignId": None,
            },
        )
        await self._start_pipeline()
        await self._speak_greeting()

    async def _on_dtmf(self, digit: str) -> None:
        self.stats.dtmf_digits.append(digit)
        log.info("call.dtmf", digit=digit)
        received_at = datetime.now(UTC)
        if self._repository is not None:
            self._enqueue("dtmf", {"digit": digit, "received_at": received_at.isoformat()})
            with contextlib.suppress(asyncio.QueueFull):
                await self._repository.record_dtmf(
                    self.call_id, digit, received_at, self.started_at, None
                )
        self._publish(
            livefeed.CALL_DTMF,
            {"callId": str(self.call_id), "digit": digit, "at": self._elapsed(received_at)},
        )
        # A keypress is a turn on an outbound call (§13.2); the pipeline
        # decides whether its responder wants it.
        if self._pipeline is not None:
            handler = getattr(self._pipeline, "on_dtmf", None)
            if handler is not None:
                try:
                    await handler(digit)
                except Exception as exc:
                    log.warning("call.dtmf_handling_failed", error=type(exc).__name__)

    async def _close(self, *, error_code: str | None, error_detail: str | None) -> None:
        if self._closed:
            return
        self._closed = True

        ended_at = datetime.now(UTC)
        duration = max(0, int((ended_at - self.started_at).total_seconds()))

        # Before anything else: stop generating and stop speaking. §5.4 -- a
        # response still in flight when the caller has hung up costs a vendor
        # call for audio nobody will hear.
        await self._stop_pipeline()

        if self.status is CallStatus.IN_PROGRESS:
            # The loop exited without a stop frame and without an exception.
            # §11.4: persist what we have and mark it, rather than leaving a
            # call stuck in progress forever.
            self.status = CallStatus.ABANDONED
            self.outcome = self.outcome or CallOutcome.CALLER_HUNG_UP
            error_code = error_code or "socket_closed"

        log.info(
            "call.ended",
            status=self.status.value,
            outcome=self.outcome.value if self.outcome else None,
            duration_s=duration,
            frames_in=self.stats.inbound_frames,
            frames_out=self.stats.outbound_frames,
            dtmf=len(self.stats.dtmf_digits),
        )

        if self._drain_task is not None:
            await self._persist_queue.put(("__stop__", {}))
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._drain_task, timeout=DRAIN_TIMEOUT_S)

        # What the outbound script decided about the contact, written before
        # the call row is closed so the campaign's card and the call's
        # outcome never disagree.
        finish = getattr(self._built, "finish", None)
        if finish is not None:
            try:
                await finish(self._repository, self.status, self.outcome)
            except Exception as exc:
                log.warning("call.contact_finish_failed", error=type(exc).__name__)

        if self._repository is not None and self.metadata is not None:
            with contextlib.suppress(Exception):
                await self._repository.finalise_call(
                    self.call_id,
                    self.started_at,
                    status=self.status,
                    outcome=self.outcome,
                    ended_at=ended_at,
                    duration_seconds=duration,
                    error_code=error_code,
                    error_detail=error_detail,
                    latency_stats=self._latency_stats(),
                )

        if self.metadata is not None:
            self._publish(
                livefeed.CALL_ENDED,
                {
                    "callId": str(self.call_id),
                    "status": self.status.value,
                    "outcome": self.outcome.value if self.outcome else None,
                    "durationSeconds": duration,
                    "firstReplyMs": self._first_reply_ms,
                },
            )

        # §11.5: the post-call pipeline runs within 30 seconds of the call
        # ending. Enqueued last, after the call row is final, so the job does
        # not race the writer for the row it is about to enrich.
        if self._on_finished is not None:
            try:
                await self._on_finished(self.call_id)
            except Exception as exc:
                # A queue that is down must not turn a completed call into a
                # failed one. The call happened; the enrichment can be replayed.
                log.warning("call.postcall_enqueue_failed", error=type(exc).__name__)

    # -- audio ------------------------------------------------------------ #

    async def _start_pipeline(self) -> None:
        """Build the conversation and start consuming recogniser events.

        Failure here does not drop the call. §11.4 requires a route for every
        failure, and the route for "the agent could not be assembled" is the
        same as for a model outage: the caller hears the greeting and is
        transferred, rather than a dead line. The call is marked so the panel
        shows why.
        """
        if self._pipeline_factory is None:
            return
        try:
            built = await self._pipeline_factory(self)
        except Exception as exc:
            log.error("call.pipeline_unavailable", error=type(exc).__name__)
            self.outcome = CallOutcome.SYSTEM_FAILURE
            self._enqueue("pipeline_unavailable", {"error": type(exc).__name__})
            return

        self._built = built
        self._pipeline = built.pipeline
        # A pre-rendered greeting from the published config replaces the
        # placeholder tone, if the factory produced one.
        if built.greeting_pcm:
            self._greeting_pcm = built.greeting_pcm

        # The pipeline tells the session what happened; the session is the
        # only thing that knows the call record and the live feed. Set here
        # rather than passed into the factory so a factory built for tests
        # needs to know nothing about either.
        pipeline = built.pipeline
        if hasattr(pipeline, "on_turn"):
            pipeline.on_turn = self._on_turn
            pipeline.on_activity = self._on_activity
            pipeline.on_call_over = self._on_call_over
            pipeline.on_transfer = self._on_transfer

        link = getattr(built, "link", None)
        if link is not None:
            await self._identified(link)

        self._pipeline_task = asyncio.create_task(self._run_pipeline())

    async def _identified(self, link: Any) -> None:
        """The pipeline looked the farmer up; record and announce who it is."""
        if self._repository is not None:
            with contextlib.suppress(Exception):
                await self._repository.attach_call_context(
                    self.call_id,
                    self.started_at,
                    farmer_id=link.farmer_id,
                    centre_id=link.centre_id,
                    campaign_id=link.campaign_id,
                    language=link.language,
                    agent_config_version=link.config_version,
                )
        self._publish(
            livefeed.CALL_IDENTIFIED,
            {
                "callId": str(self.call_id),
                "farmerName": link.farmer_name,
                "callerLast4": link.farmer_last4,
                "centreCode": link.centre_code,
                "centreName": link.centre_name,
                "language": link.language or "",
                "campaignId": str(link.campaign_id) if link.campaign_id else None,
            },
        )

    async def _run_pipeline(self) -> None:
        """Consume recogniser events until the stream ends.

        Wrapped because this runs as a detached task: an exception here would
        otherwise surface only when the task is garbage collected, long after
        the call it broke.
        """
        if self._pipeline is None:
            return
        try:
            await self._pipeline.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("call.pipeline_failed", error=type(exc).__name__)
            self.outcome = self.outcome or CallOutcome.SYSTEM_FAILURE
            self._enqueue("pipeline_failed", {"error": type(exc).__name__})

    async def _stop_pipeline(self) -> None:
        """Cancel the turn loop and close the speech services.

        Cancelled, not merely awaited: the pipeline blocks on the recogniser's
        event stream, which does not end just because the socket did.
        """
        if self._pipeline_task is not None:
            self._pipeline_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pipeline_task
            self._pipeline_task = None
        if self._pipeline is not None:
            with contextlib.suppress(Exception):
                await self._pipeline.close()
            self._pipeline = None

    async def on_audio(self, pcm: bytes) -> None:
        """Feed inbound caller audio to the recogniser.

        Never blocks on anything slow: §7.6 makes latency on this path a P1,
        and a media frame arrives every 20 ms whether or not we are ready.
        """
        if self._pipeline is None:
            return
        await self._pipeline.feed_audio(pcm)

    async def send_audio(self, pcm: bytes) -> None:
        """Send PCM to the caller in 20 ms frames."""
        for frame in audio_utils.iter_frames(pcm):
            await self._transport.send_text(self._serializer.encode_audio(frame))
            self.stats.outbound_frames += 1
            self.stats.outbound_bytes += len(frame)

    async def clear_playback(self) -> None:
        """Drop audio the provider has buffered but not played (§5.4).

        Barge-in requires both halves: cancelling generation *and* this. Doing
        only the first leaves the agent talking over the farmer for another
        second, which is the failure everyone ships.
        """
        await self._transport.send_text(self._serializer.encode_clear())
        self._enqueue("playback_cleared", {})

    async def _speak_greeting(self) -> None:
        """Play the opening audio.

        §11.1 wants first audio out within ~50 ms of the start frame, which is
        why the greeting is pre-rendered rather than synthesised on demand. In
        Phase 1 it is a placeholder tone; Phase 2 swaps in the cached Sarvam
        rendering of the §11.1 greeting without touching this call site.
        """
        pcm = self._greeting_pcm
        if pcm is None:
            return
        await self.send_audio(pcm)

    # -- persistence ------------------------------------------------------ #

    def _enqueue(self, event_type: str, payload: dict[str, Any]) -> None:
        """Queue an event for the drain task. Never blocks the audio loop."""
        try:
            self._persist_queue.put_nowait((event_type, payload))
        except asyncio.QueueFull:
            self.stats.persist_drops += 1
            log.warning("call.persist_queue_full", event_type=event_type)

    async def _drain(self) -> None:
        """Write queued events to the database, off the audio path."""
        while True:
            event_type, payload = await self._persist_queue.get()
            if event_type == "__stop__":
                return
            if self._repository is None:
                continue
            try:
                if event_type == "turn":
                    await self._persist_turn(payload)
                else:
                    await self._repository.record_event(
                        self.call_id, event_type, payload, self.started_at
                    )
            # Persistence failing must not kill a live call.
            except Exception as exc:
                log.warning("call.persist_failed", event_type=event_type, error=type(exc).__name__)

    async def _persist_turn(self, payload: dict[str, Any]) -> None:
        """Two ``call_turns`` rows: what the farmer said, what the agent said."""
        if self._repository is None:
            return
        index = int(payload["turn_index"])
        language = payload.get("language")
        if payload.get("farmer_text"):
            await self._repository.record_turn(
                self.call_id,
                self.started_at,
                turn_index=index * 2,
                role=TurnRole.USER,
                text=str(payload["farmer_text"]),
                language=language,
                at_ms=int(payload["farmer_at_ms"]),
                latency=None,
                tool_calls=[],
                interrupted=False,
            )
        if payload.get("agent_text"):
            await self._repository.record_turn(
                self.call_id,
                self.started_at,
                turn_index=index * 2 + 1,
                role=TurnRole.ASSISTANT,
                text=str(payload["agent_text"]),
                language=language,
                at_ms=int(payload["agent_at_ms"]),
                latency=payload.get("latency"),
                tool_calls=list(payload.get("tools") or []),
                interrupted=bool(payload.get("interrupted")),
            )

    # -- what the pipeline reports ----------------------------------------- #

    def _on_activity(self, state: str) -> None:
        if state == "thinking":
            self._turn_started_wall = datetime.now(UTC)
        self._publish(livefeed.CALL_ACTIVITY, {"callId": str(self.call_id), "activity": state})

    def _on_turn(self, outcome: TurnOutcome) -> None:
        """A finished turn: persist it and put it on the live feed.

        Synchronous by contract -- it runs on the audio path -- so the
        database write is queued and the feed publish is a queue put.
        """
        metrics = outcome.metrics
        total = metrics.total_ms if metrics is not None else None
        farmer_at = self._turn_started_wall or datetime.now(UTC)
        farmer_s = self._elapsed(farmer_at)
        agent_s = farmer_s + (total / 1000 if total is not None else 0.0)

        latency: dict[str, Any] | None = None
        if metrics is not None:
            segments = metrics.segments()
            latency = {
                "totalMs": round(total) if total is not None else None,
                "fromCache": metrics.from_cache,
                "turnMs": segments.get("turn_commit"),
                "sttMs": segments.get("stt_final"),
                "toolMs": segments.get("tool_execution"),
                "llmMs": segments.get("llm_ttft"),
                "ttsMs": segments.get("tts_ttfb"),
                "networkMs": segments.get("network_out"),
            }
            if total is not None and not metrics.from_cache:
                self._reply_totals.append(total)
                if self._first_reply_ms is None:
                    self._first_reply_ms = round(total)

        tools = self._tools_of_last_turn()
        language = self._language()
        # A keypress arrives as a marker the recogniser could never produce;
        # the DTMF event already told the panel about it.
        farmer_text = "" if outcome.transcript.startswith("[dtmf ") else outcome.transcript
        agent_text = outcome.spoken or outcome.response
        self._turns_seen += 1

        self._enqueue(
            "turn",
            {
                "turn_index": outcome.turn_index,
                "language": language,
                "farmer_text": farmer_text,
                "farmer_at_ms": int(farmer_s * 1000),
                "agent_text": agent_text,
                "agent_at_ms": int(agent_s * 1000),
                "latency": latency,
                "tools": tools,
                "interrupted": outcome.interrupted,
            },
        )
        if farmer_text:
            self._publish(
                livefeed.CALL_TURN,
                {
                    "callId": str(self.call_id),
                    "turnIndex": outcome.turn_index * 2,
                    "role": "farmer",
                    "text": farmer_text,
                    "at": round(farmer_s, 2),
                    "latency": None,
                    "tools": [],
                },
            )
        if agent_text:
            self._publish(
                livefeed.CALL_TURN,
                {
                    "callId": str(self.call_id),
                    "turnIndex": outcome.turn_index * 2 + 1,
                    "role": "agent",
                    "text": agent_text,
                    "at": round(agent_s, 2),
                    "latency": latency,
                    "tools": tools,
                },
            )

    async def _on_transfer(self, request: Any) -> None:
        """Join the caller to a person (§12.3), the line having been spoken.

        Success is the provider accepting the transfer: from then on the call
        is between the farmer and the manager, and this stream ends when the
        provider closes it. A hand-over that cannot be made is recorded as
        such -- the post-call job opens the follow-up -- and the caller hears
        a commitment rather than silence.
        """
        if self._transferred or self._hangup.is_set():
            return
        target = str(getattr(request, "to", "") or "")
        reason = str(getattr(request, "reason", "") or "")
        target_kind = str(getattr(request, "target_kind", "") or "")
        if self._transfer_adapter is None or not self._call_sid or not target:
            log.warning("call.transfer_unavailable", reason=reason, target_kind=target_kind)
            await self._transfer_failed(reason, "telephony_unconfigured")
            return
        try:
            adapter = self._transfer_adapter(frozenset({target}))
            await adapter.transfer(
                call_sid=self._call_sid,
                to=target,
                whisper_text=getattr(request, "whisper", None),
            )
        except Exception as exc:
            log.warning(
                "call.transfer_failed",
                reason=reason,
                target_kind=target_kind,
                error=type(exc).__name__,
            )
            await self._transfer_failed(reason, type(exc).__name__)
            return
        self._transferred = True
        self.outcome = CallOutcome.TRANSFERRED
        to_name = str(getattr(request, "target_name", "") or "")
        await self._record_transfer(reason, completed=True)
        self._enqueue("transfer", {"to": to_name, "reason": reason, "target_kind": target_kind})
        self._publish(
            livefeed.CALL_TRANSFER, {"callId": str(self.call_id), "to": to_name, "reason": reason}
        )
        log.info("call.transferred", reason=reason, target_kind=target_kind)

    async def _transfer_failed(self, reason: str, why: str) -> None:
        await self._record_transfer(reason, completed=False)
        self._enqueue("transfer_failed", {"reason": reason, "error": why})
        announce = getattr(self._pipeline, "announce", None)
        if announce is None:
            return
        try:
            await announce(TRANSFER_FAILED_LINE_HI)
        except Exception as exc:
            log.warning("call.transfer_fallback_failed", error=type(exc).__name__)

    async def _record_transfer(self, reason: str, *, completed: bool) -> None:
        if self._repository is None:
            return
        try:
            await self._repository.mark_transferred(
                self.call_id, self.started_at, reason=reason, completed=completed
            )
        except Exception as exc:
            log.warning("call.persist_failed", event_type="transfer", error=type(exc).__name__)

    async def _on_call_over(self) -> None:
        """The script has said its last line: end the call from our side."""
        if self._hangup.is_set():
            return
        responder = getattr(self._pipeline, "responder", None)
        result = getattr(responder, "result", None)
        decided = getattr(result, "call_outcome", None)
        if isinstance(decided, CallOutcome):
            self.outcome = decided
        await asyncio.sleep(HANGUP_GRACE_S)
        log.info("call.hangup_by_agent", outcome=self.outcome.value if self.outcome else None)
        self._hangup.set()

    def _tools_of_last_turn(self) -> list[dict[str, Any]]:
        agent = getattr(self._built, "agent", None)
        last = getattr(agent, "last_turn", None)
        results = getattr(last, "tool_results", None) or []
        tools: list[dict[str, Any]] = []
        for result in results:
            tools.append(
                {
                    "name": str(getattr(result, "tool", "tool")),
                    "ms": round(float(getattr(result, "latency_ms", 0.0) or 0.0)),
                    "ok": bool(getattr(result, "ok", True)),
                }
            )
        return tools

    def _language(self) -> str | None:
        link = getattr(self._built, "link", None)
        return getattr(link, "language", None)

    def _latency_stats(self) -> dict[str, Any]:
        """Written to ``calls.latency_stats``; what the call page summarises."""
        totals = sorted(self._reply_totals)

        def nearest_rank(p: float) -> int | None:
            if not totals:
                return None
            index = max(0, min(len(totals) - 1, round(p * len(totals) + 0.5) - 1))
            return round(totals[index])

        return {
            "first_reply_ms": self._first_reply_ms,
            "p50_ms": nearest_rank(0.5),
            "p95_ms": nearest_rank(0.95),
            "replies": len(totals),
            "turns": self._turns_seen,
        }

    def _elapsed(self, at: datetime) -> float:
        return max(0.0, (at - self.started_at).total_seconds())

    def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        link = getattr(self._built, "link", None)
        centre_id = getattr(link, "centre_id", None)
        campaign_id = getattr(link, "campaign_id", None)
        self._live_feed.publish(
            LiveEvent(
                type=event_type,
                payload=payload,
                call_id=str(self.call_id),
                centre_id=str(centre_id) if centre_id else None,
                campaign_id=str(campaign_id) if campaign_id else None,
            )
        )
