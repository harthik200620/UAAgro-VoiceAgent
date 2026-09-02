"""The call session's new duties: direction, turns, the live feed, hanging up.

Driven over a fake transport with the Exotel frames a real call produces, and
a fake pipeline that reports turns the way the conversation pipeline does.
No speech vendor, no database, no Redis: what is asserted is the session's
contract with all three -- which rows it asks for, which events it publishes,
and that a scripted closing ends the call from our side.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import uuid
from datetime import datetime
from typing import Any

import pytest

from uaagro_domain import livefeed
from uaagro_domain.enums import CallDirection, CallOutcome, CallStatus, TurnRole
from uaagro_domain.livefeed import LiveEvent
from voice_worker.adapters.telephony.exotel import ExotelSerializer
from voice_worker.pipelines.conversation import TurnOutcome
from voice_worker.runtime import session as session_module
from voice_worker.runtime.assembly import CallLink
from voice_worker.runtime.direction import OurNumbers
from voice_worker.runtime.metrics import TurnMetrics
from voice_worker.runtime.session import CallSession, TransportClosed


class _Transport:
    """Frames in through a queue; frames out into a list."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        message = await self.inbox.get()
        if message is None:
            raise TransportClosed("closed")
        return message

    def frame(self, payload: dict[str, Any]) -> None:
        self.inbox.put_nowait(json.dumps(payload))


class _Repository:
    """Records every write the session asks for."""

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.context: list[dict[str, Any]] = []
        self.turns: list[dict[str, Any]] = []
        self.events: list[str] = []
        self.dtmf: list[str] = []
        self.finalised: list[dict[str, Any]] = []
        self.linked: list[uuid.UUID] = []

    async def create_call(self, record: Any) -> None:
        self.calls.append(record)

    async def attach_call_context(
        self, call_id: uuid.UUID, started_at: datetime, **fields: Any
    ) -> None:
        self.context.append(fields)

    async def record_turn(self, call_id: uuid.UUID, started_at: datetime, **fields: Any) -> None:
        self.turns.append(fields)

    async def record_event(
        self, call_id: uuid.UUID, event_type: str, payload: Any, started_at: Any
    ) -> None:
        self.events.append(event_type)

    async def record_dtmf(self, call_id: uuid.UUID, digit: str, *args: Any) -> None:
        self.dtmf.append(digit)

    async def link_contact(self, contact_id: uuid.UUID, call_id: uuid.UUID) -> None:
        self.linked.append(contact_id)

    async def finish_contact(self, contact_id: uuid.UUID, **fields: Any) -> None:
        return None

    async def finalise_call(self, call_id: uuid.UUID, started_at: datetime, **fields: Any) -> None:
        self.finalised.append(fields)


class _Feed:
    def __init__(self) -> None:
        self.events: list[LiveEvent] = []

    def publish(self, event: LiveEvent) -> None:
        self.events.append(event)

    async def close(self) -> None:
        return None

    def of(self, event_type: str) -> list[LiveEvent]:
        return [e for e in self.events if e.type == event_type]


class _Responder:
    accepts_dtmf = True
    call_over = False
    result = None


class _Pipeline:
    """Enough of the conversation pipeline for the session to drive."""

    def __init__(self) -> None:
        self.on_turn: Any = None
        self.on_activity: Any = None
        self.on_call_over: Any = None
        self.responder = _Responder()
        self.audio: list[bytes] = []
        self.digits: list[str] = []
        self._stop = asyncio.Event()

    async def run(self) -> None:
        await self._stop.wait()

    async def feed_audio(self, pcm: bytes) -> None:
        self.audio.append(pcm)

    async def on_dtmf(self, digit: str) -> None:
        self.digits.append(digit)

    async def close(self) -> None:
        self._stop.set()


class _Built:
    def __init__(self, pipeline: _Pipeline, link: CallLink) -> None:
        self.pipeline = pipeline
        self.link = link
        self.greeting_pcm = b""
        self.agent = None
        self.finish = None


OURS = OurNumbers(inbound=frozenset({"1800123456"}), outbound=frozenset({"1409876543"}))
CONTACT = uuid.uuid4()
CAMPAIGN = uuid.uuid4()


def _session(
    transport: _Transport, repo: _Repository, feed: _Feed, pipeline: _Pipeline
) -> CallSession:
    link = CallLink(
        farmer_id=uuid.uuid4(),
        farmer_name="कमला देवी",
        farmer_last4="9560",
        centre_id=uuid.uuid4(),
        centre_code="NKSK-STP-01",
        centre_name="Sitapur",
        campaign_id=CAMPAIGN,
        contact_id=CONTACT,
        language="hi-IN",
        config_version=3,
    )

    async def factory(_: CallSession) -> _Built:
        return _Built(pipeline, link)

    return CallSession(
        transport=transport,  # type: ignore[arg-type]
        serializer=ExotelSerializer(),
        repository=repo,
        direction=CallDirection.INBOUND,
        pipeline_factory=factory,
        live_feed=feed,
        our_numbers=OURS,
    )


def _start_frame() -> dict[str, Any]:
    return {
        "event": "start",
        "stream_sid": "stream-1",
        "start": {
            "call_sid": "call-1",
            "from": "+911409876543",
            "to": "+919876509560",
            "custom_parameters": {"CustomField": f"contact:{CONTACT}"},
        },
    }


def _turn(
    index: int, farmer: str, agent: str, *, total_ms: float, cached: bool = False
) -> TurnOutcome:
    metrics = TurnMetrics(turn_index=index)
    metrics.mark("speech_ended_at", 100.0)
    metrics.mark("committed_at", 100.1)
    metrics.mark("transcript_at", 100.18)
    metrics.mark("generation_started_at", 100.18)
    metrics.mark("first_token_at", 100.4)
    metrics.mark("synthesis_started_at", 100.4)
    metrics.mark("first_audio_at", 100.0 + total_ms / 1000)
    metrics.mark("audio_sent_at", 100.0 + total_ms / 1000 + 0.02)
    metrics.from_cache = cached
    return TurnOutcome(
        turn_index=index, transcript=farmer, response=agent, spoken=agent, metrics=metrics
    )


async def _settle() -> None:
    """Let the session drain its queue: a frame costs several loop turns."""
    for _ in range(40):
        await asyncio.sleep(0.002)


async def test_an_outbound_call_is_recognised_linked_and_announced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module, "HANGUP_GRACE_S", 0.0)
    transport, repo, feed, pipeline = _Transport(), _Repository(), _Feed(), _Pipeline()
    session = _session(transport, repo, feed, pipeline)
    running = asyncio.create_task(session.run())

    transport.frame({"event": "connected"})
    transport.frame(_start_frame())
    await _settle()

    assert session.direction is CallDirection.OUTBOUND
    assert session.contact_id == CONTACT
    assert repo.calls[0].direction is CallDirection.OUTBOUND
    assert repo.linked == [CONTACT]
    assert repo.context[0]["campaign_id"] == CAMPAIGN
    assert repo.context[0]["agent_config_version"] == 3

    started = feed.of(livefeed.CALL_STARTED)
    assert len(started) == 1 and started[0].payload["direction"] == "outbound"
    identified = feed.of(livefeed.CALL_IDENTIFIED)
    assert identified[0].payload["farmerName"] == "कमला देवी"
    assert identified[0].payload["callerLast4"] == "9560"
    assert identified[0].centre_id == str(session._built.link.centre_id)
    assert identified[0].campaign_id == str(CAMPAIGN)
    # Never a phone number, in any event, in any field.
    for event in feed.events:
        assert "9876509560" not in event.encode()

    transport.frame({"event": "stop"})
    await running


async def test_turns_are_persisted_as_two_rows_and_streamed_as_two_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module, "HANGUP_GRACE_S", 0.0)
    transport, repo, feed, pipeline = _Transport(), _Repository(), _Feed(), _Pipeline()
    session = _session(transport, repo, feed, pipeline)
    running = asyncio.create_task(session.run())
    transport.frame(_start_frame())
    await _settle()

    pipeline.on_activity("thinking")
    pipeline.on_turn(_turn(0, "डीएपी का रेट क्या है", "बारह सौ रुपये", total_ms=612))
    pipeline.on_turn(_turn(1, "ठीक है", "धन्यवाद", total_ms=48, cached=True))
    await _settle()

    turns = feed.of(livefeed.CALL_TURN)
    assert [t.payload["role"] for t in turns] == ["farmer", "agent", "farmer", "agent"]
    assert turns[0].payload["text"] == "डीएपी का रेट क्या है"
    assert turns[1].payload["latency"]["totalMs"] == 612
    assert turns[1].payload["latency"]["llmMs"] == pytest.approx(220, abs=1)
    assert turns[3].payload["latency"]["fromCache"] is True
    assert feed.of(livefeed.CALL_ACTIVITY)[0].payload["activity"] == "thinking"

    transport.frame({"event": "stop"})
    await running

    roles = [t["role"] for t in repo.turns]
    assert roles == [TurnRole.USER, TurnRole.ASSISTANT, TurnRole.USER, TurnRole.ASSISTANT]
    assert [t["turn_index"] for t in repo.turns] == [0, 1, 2, 3]
    assert repo.turns[1]["latency"]["totalMs"] == 612
    stats = repo.finalised[0]["latency_stats"]
    # The cached turn does not count as evidence of the live path's speed.
    assert stats["first_reply_ms"] == 612
    assert stats["replies"] == 1
    assert stats["turns"] == 2
    ended = feed.of(livefeed.CALL_ENDED)[0].payload
    assert ended["firstReplyMs"] == 612


async def test_a_keypress_reaches_the_pipeline_and_the_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module, "HANGUP_GRACE_S", 0.0)
    transport, repo, feed, pipeline = _Transport(), _Repository(), _Feed(), _Pipeline()
    session = _session(transport, repo, feed, pipeline)
    running = asyncio.create_task(session.run())
    transport.frame(_start_frame())
    transport.frame({"event": "dtmf", "dtmf": {"digit": "1"}})
    await _settle()
    assert pipeline.digits == ["1"]
    assert repo.dtmf == ["1"]
    assert feed.of(livefeed.CALL_DTMF)[0].payload["digit"] == "1"
    transport.frame({"event": "stop"})
    await running


async def test_the_script_can_end_the_call_from_our_side(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_module, "HANGUP_GRACE_S", 0.0)
    transport, repo, feed, pipeline = _Transport(), _Repository(), _Feed(), _Pipeline()
    session = _session(transport, repo, feed, pipeline)
    running = asyncio.create_task(session.run())
    transport.frame(_start_frame())
    await _settle()

    class _Result:
        call_outcome = CallOutcome.OFFER_ACCEPTED

    pipeline.responder.result = _Result()  # type: ignore[assignment]
    await pipeline.on_call_over()
    # No stop frame arrives: the session ends the loop itself.
    await asyncio.wait_for(running, timeout=2.0)

    assert session.status is CallStatus.COMPLETED
    assert session.outcome is CallOutcome.OFFER_ACCEPTED
    assert "hangup" in repo.events
    assert feed.of(livefeed.CALL_ENDED)[0].payload["outcome"] == "offer_accepted"


async def test_media_still_flows_to_the_pipeline() -> None:
    transport, repo, feed, pipeline = _Transport(), _Repository(), _Feed(), _Pipeline()
    session = _session(transport, repo, feed, pipeline)
    running = asyncio.create_task(session.run())
    transport.frame(_start_frame())
    pcm = bytes(320)
    transport.frame({"event": "media", "media": {"payload": base64.b64encode(pcm).decode()}})
    await _settle()
    assert pipeline.audio == [pcm]
    transport.inbox.put_nowait(None)
    with contextlib.suppress(Exception):
        await running
    assert session.status is CallStatus.ABANDONED
