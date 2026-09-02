"""Server-sent events for the panel's live views (§15.1).

One relay shape serves both the live-calls page and a campaign page: a
snapshot first, then the deltas the media path and the dialer publish, then a
heartbeat whenever fifteen seconds pass without one. The router supplies the
snapshot and an ``accept`` function that decides what each Redis event means
for this stream -- which is where row-level scope is enforced, because the
feed itself is organisation-wide.

Two things this deliberately does not do:

- **Hold a database connection.** A stream lives for as long as a tab is
  open. The snapshot and any lookup an event needs open their own short
  session; the pool is for requests, not for tabs.
- **Buffer.** Each event is written as it arrives. The response headers ask
  every proxy in between to do the same.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import Request
from fastapi.responses import StreamingResponse

from uaagro_domain import fastjson, livefeed
from uaagro_domain.livefeed import LiveEvent
from uaagro_domain.settings import get_settings

log = structlog.get_logger(__name__)

HEARTBEAT_S = 15.0

#: What the router hands back per Redis event: zero or more (event name,
#: payload) pairs to write to this stream.
Accept = Callable[[LiveEvent], Awaitable[list[tuple[str, Any]]]]

SSE_HEADERS = {
    "cache-control": "no-cache, no-transform",
    "connection": "keep-alive",
    "x-accel-buffering": "no",
}


def encode(event: str, data: Any) -> bytes:
    return f"event: {event}\ndata: {fastjson.dumps(data)}\n\n".encode()


async def relay(
    request: Request,
    *,
    snapshot_event: str,
    snapshot: Callable[[], Awaitable[Any]],
    accept: Accept,
) -> AsyncIterator[bytes]:
    """Snapshot, then deltas, then heartbeats, until the client goes away."""
    yield encode(snapshot_event, await snapshot())
    last_write = time.monotonic()

    try:
        async for event in livefeed.subscribe(get_settings().redis_url):
            if await request.is_disconnected():
                return
            if event is not None:
                try:
                    for name, payload in await accept(event):
                        yield encode(name, payload)
                        last_write = time.monotonic()
                except Exception as exc:
                    # One event the router could not handle -- a lookup that
                    # failed -- must not end every other event's stream.
                    log.warning(
                        "events.accept_failed", event_type=event.type, error=type(exc).__name__
                    )
            if time.monotonic() - last_write >= HEARTBEAT_S:
                yield encode("heartbeat", {"at": datetime.now(UTC).isoformat()})
                last_write = time.monotonic()
    except Exception as exc:
        # Redis gone, most likely. Ending the stream makes the panel
        # reconnect with backoff, which is the right recovery.
        log.warning("events.stream_ended", error=type(exc).__name__)


def streaming_response(body: AsyncIterator[bytes]) -> StreamingResponse:
    return StreamingResponse(body, media_type="text/event-stream", headers=SSE_HEADERS)


__all__ = ("HEARTBEAT_S", "Accept", "encode", "relay", "streaming_response")
