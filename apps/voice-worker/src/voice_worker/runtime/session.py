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
from typing import Any, Protocol

import structlog

from uaagro_db.crypto import get_cipher
from uaagro_domain.enums import (
    CallDirection,
    CallOutcome,
    CallStatus,
    TelephonyProvider,
)
from uaagro_domain.logging import bind_call, clear_call
from uaagro_domain.phone import normalise_msisdn

from ..adapters.telephony.base import (
    CallMetadata,
    InboundEvent,
    InboundEventType,
    TelephonySerializer,
)
from . import audio as audio_utils

log = structlog.get_logger(__name__)

#: Bound on the persistence queue. If writes fall this far behind, dropping
#: events is better than growing memory without limit during a peak -- and the
#: drop is logged loudly rather than silently.
PERSIST_QUEUE_MAX = 512

#: How long a drain task is given to flush after the call ends.
DRAIN_TIMEOUT_S = 10.0


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
        self._pipeline: Any | None = None
        self._pipeline_task: asyncio.Task[None] | None = None

        self.call_id = uuid.uuid4()
        self.started_at = datetime.now(UTC)
        self.metadata: CallMetadata | None = None
        self.from_number_hash: bytes | None = None
        self.stats = CallStats()
        self.status = CallStatus.IN_PROGRESS
        self.outcome: CallOutcome | None = None

        self._persist_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(
            maxsize=PERSIST_QUEUE_MAX
        )
        self._drain_task: asyncio.Task[None] | None = None
        self._closed = False

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

        try:
            while True:
                message = await self._transport.receive_text()
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
        )

        # Hashed once, here, and kept for the pipeline. §17 makes the HMAC the
        # only lookup key; the raw number never reaches a column, a log line or
        # the agent's context.
        self.from_number_hash = _hash_number(metadata.from_number)

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

        self._enqueue(
            "start",
            {"stream_sid": metadata.stream_sid, "call_sid": metadata.call_sid},
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

        self._pipeline = built.pipeline
        # A pre-rendered greeting from the published config replaces the
        # placeholder tone, if the factory produced one.
        if built.greeting_pcm:
            self._greeting_pcm = built.greeting_pcm
        self._pipeline_task = asyncio.create_task(self._run_pipeline())

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
                await self._repository.record_event(
                    self.call_id, event_type, payload, self.started_at
                )
            # Persistence failing must not kill a live call.
            except Exception as exc:
                log.warning("call.persist_failed", event_type=event_type, error=type(exc).__name__)
