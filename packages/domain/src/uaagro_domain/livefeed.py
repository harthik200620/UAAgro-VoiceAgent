"""Live events out of the media path, for the panel (§15.1 Live Calls).

The panel wants to watch a call as it happens: the transcript growing turn by
turn, the "speaking / listening" state, a contact card turning green the moment
its call ends. The media path has all of that in memory and, until now, nowhere
to put it -- the database is written as the call goes, but polling Postgres
every second from every open browser tab is not a live view, it is a load test.

So the media path and the dialer *publish* and the API *subscribes*, through a
Redis pub/sub channel. Three properties, in order of importance:

**Publishing never blocks the audio loop.** ``publish`` is synchronous and
puts the event on a bounded in-process queue; a background task drains the
queue to Redis. If Redis is slow or down the queue fills and events are
dropped and counted -- the live view degrades, the call does not.

**Nothing in an event identifies a phone number.** Payloads carry a farmer's
name and the last four digits, the same as every API response (§17, §23-6).
The channel is internal, but "internal" is how recording URLs end up in
Slack; the rule is enforced at the source.

**An event is a delta, never the state.** Subscribers load a snapshot from
the database and apply events on top. That keeps the feed small and means a
subscriber that connects late, or reconnects, is never wrong -- it is at most
one snapshot behind.

Redis is the transport because every service already has it and the fan-out is
small: a handful of panel tabs, not a public feed. Redis pub/sub is fire and
forget by design, which is exactly the contract above.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from . import fastjson

log = structlog.get_logger(__name__)

#: One channel for everything. Subscribers filter by the scope fields on each
#: event; a channel per campaign would leave the live-calls view subscribing to
#: an unbounded set.
CHANNEL = "uaagro:live"

#: Events the media path emits for one call, in the order they can happen.
CALL_STARTED = "call.started"
CALL_IDENTIFIED = "call.identified"
CALL_ACTIVITY = "call.activity"
CALL_TURN = "call.turn"
CALL_DTMF = "call.dtmf"
CALL_TRANSFER = "call.transfer"
CALL_ENDED = "call.ended"
#: Events the dialer and the post-call pipeline emit for a campaign.
CONTACT_UPDATED = "contact.updated"
CAMPAIGN_UPDATED = "campaign.updated"

#: How many events may wait for Redis before the oldest are dropped. At one
#: event per turn plus a few per call, a thousand is minutes of backlog.
QUEUE_SIZE = 1000

#: Between reconnect attempts when Redis is unreachable. Short enough that a
#: restart of Redis is invisible, long enough not to spin.
RECONNECT_DELAY_S = 1.0


@dataclass(frozen=True, slots=True)
class LiveEvent:
    """One thing that happened, with enough scope to filter on."""

    type: str
    payload: dict[str, Any]
    call_id: str | None = None
    centre_id: str | None = None
    campaign_id: str | None = None
    at: float = field(default_factory=time.time)

    def encode(self) -> str:
        return fastjson.dumps(
            {
                "type": self.type,
                "payload": self.payload,
                "call_id": self.call_id,
                "centre_id": self.centre_id,
                "campaign_id": self.campaign_id,
                "at": self.at,
            }
        )

    @classmethod
    def decode(cls, raw: str | bytes) -> LiveEvent:
        data = fastjson.loads(raw)
        return cls(
            type=str(data["type"]),
            payload=dict(data.get("payload") or {}),
            call_id=data.get("call_id"),
            centre_id=data.get("centre_id"),
            campaign_id=data.get("campaign_id"),
            at=float(data.get("at") or 0.0),
        )


class LiveFeed(Protocol):
    """What a publisher looks like from the media path's side."""

    def publish(self, event: LiveEvent) -> None: ...

    async def close(self) -> None: ...


class NullLiveFeed:
    """No feed configured. Every publish is a no-op, which the tests rely on."""

    def publish(self, event: LiveEvent) -> None:
        return None

    async def close(self) -> None:
        return None


class RedisLiveFeed:
    """Publishes through a bounded queue and one Redis connection."""

    def __init__(self, url: str, *, queue_size: int = QUEUE_SIZE) -> None:
        self._url = url
        self._queue: asyncio.Queue[LiveEvent | None] = asyncio.Queue(maxsize=queue_size)
        self._task: asyncio.Task[None] | None = None
        self._client: Any = None
        self.dropped = 0
        self.published = 0

    def publish(self, event: LiveEvent) -> None:
        """Queue an event. Never blocks, never raises."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped in (1, 100, 1000) or self.dropped % 10_000 == 0:
                log.warning("livefeed.dropped", dropped=self.dropped, event_type=event.type)
            return
        if self._task is None:
            # Started on the first publish rather than in the constructor, so
            # the feed can be built before an event loop exists -- at import
            # time in the worker, before uvicorn has started one.
            self._task = asyncio.get_running_loop().create_task(self._drain())

    async def _drain(self) -> None:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            try:
                client = await self._connect()
                await client.publish(CHANNEL, event.encode())
                self.published += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The event is lost; the feed is best-effort by contract. What
                # matters is that a Redis outage costs a live view, not a call.
                self.dropped += 1
                log.warning("livefeed.publish_failed", error=type(exc).__name__)
                await self._disconnect()
                await asyncio.sleep(RECONNECT_DELAY_S)

    async def _connect(self) -> Any:
        if self._client is None:
            import redis.asyncio as redis

            self._client = redis.from_url(  # type: ignore[no-untyped-call]
                self._url, socket_connect_timeout=2.0, socket_timeout=2.0
            )
        return self._client

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()

    async def close(self) -> None:
        """Flush what is queued, then stop. Bounded by the queue's own size."""
        if self._task is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(None)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._task, timeout=5.0)
            self._task = None
        await self._disconnect()


async def subscribe(url: str) -> AsyncIterator[LiveEvent | None]:
    """Yield events as they arrive, and ``None`` once a second when nothing has.

    The idle ``None`` is deliberate: a server-sent-events consumer has to
    send heartbeats and notice a client that went away, and it can only do
    either when it gets control back. Blocking on the next event would keep
    an abandoned stream open for as long as the feed stayed quiet.

    One subscription per API stream rather than a shared fan-out: the count
    of open panel tabs is small, and a shared subscription would need its own
    lifecycle, backpressure and error handling for a saving nobody would
    measure.
    """
    import redis.asyncio as redis

    client = redis.from_url(url)  # type: ignore[no-untyped-call]
    pubsub = client.pubsub()
    try:
        await pubsub.subscribe(CHANNEL)
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message is None:
                yield None
                continue
            try:
                yield LiveEvent.decode(message["data"])
            except (KeyError, ValueError, TypeError) as exc:
                # A malformed message is somebody else's bug; skipping it is
                # the only reading that keeps every other tab live.
                log.warning("livefeed.malformed", error=type(exc).__name__)
    finally:
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(CHANNEL)
        with contextlib.suppress(Exception):
            await pubsub.aclose()
        with contextlib.suppress(Exception):
            await client.aclose()


__all__ = (
    "CALL_ACTIVITY",
    "CALL_DTMF",
    "CALL_ENDED",
    "CALL_IDENTIFIED",
    "CALL_STARTED",
    "CALL_TRANSFER",
    "CALL_TURN",
    "CAMPAIGN_UPDATED",
    "CHANNEL",
    "CONTACT_UPDATED",
    "LiveEvent",
    "LiveFeed",
    "NullLiveFeed",
    "RedisLiveFeed",
    "subscribe",
)
