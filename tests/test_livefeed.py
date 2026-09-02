"""The live feed's contract (§15.1 Live Calls).

Not a test of Redis. What matters is that an event survives the trip as data
the panel can use, that publishing can never hurt a call, and that the
consumer side turns a quiet feed into heartbeats rather than silence.
"""

from __future__ import annotations

import asyncio

from uaagro_domain import livefeed
from uaagro_domain.livefeed import LiveEvent, NullLiveFeed, RedisLiveFeed


def test_an_event_round_trips_with_its_scope_intact() -> None:
    event = LiveEvent(
        type=livefeed.CALL_TURN,
        payload={"callId": "c1", "text": "डीएपी का रेट क्या है", "at": 6.2},
        call_id="c1",
        centre_id="centre-1",
        campaign_id=None,
    )
    restored = LiveEvent.decode(event.encode())
    assert restored.type == livefeed.CALL_TURN
    assert restored.payload == event.payload
    assert restored.centre_id == "centre-1"
    assert restored.campaign_id is None
    assert restored.at == event.at


def test_the_null_feed_accepts_everything_and_does_nothing() -> None:
    feed = NullLiveFeed()
    feed.publish(LiveEvent(type=livefeed.CALL_STARTED, payload={}))


async def test_a_full_queue_drops_rather_than_blocks() -> None:
    """Publishing runs on the audio loop; it must return immediately."""
    feed = RedisLiveFeed("redis://127.0.0.1:1/0", queue_size=2)

    # No connection is ever made: the drain task is replaced before it runs.
    async def never_drain() -> None:
        await asyncio.Event().wait()

    feed._drain = never_drain  # type: ignore[method-assign]
    for i in range(5):
        feed.publish(LiveEvent(type=livefeed.CALL_ACTIVITY, payload={"n": i}))
    assert feed.dropped == 3
    await feed.close()


async def test_close_is_bounded_when_redis_never_answers() -> None:
    feed = RedisLiveFeed("redis://127.0.0.1:1/0", queue_size=4)

    async def never_drain() -> None:
        await asyncio.Event().wait()

    feed._drain = never_drain  # type: ignore[method-assign]
    feed.publish(LiveEvent(type=livefeed.CALL_STARTED, payload={}))
    await asyncio.wait_for(feed.close(), timeout=6.0)
