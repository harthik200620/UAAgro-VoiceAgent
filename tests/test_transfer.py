"""The hand-over, end to end in pieces (§12.3).

The escalation engine decides a person is needed, the transfer tool decides
who, the pipeline waits until the caller has heard the line, and the session
tells the provider to join them -- or, when that fails, promises a call back
and records a transfer that never completed so the follow-up gets opened.
Each seam is exercised on its own with the neighbours faked.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from typing import Any, ClassVar

import pytest

from uaagro_domain import livefeed
from uaagro_domain.enums import CallDirection, CallOutcome, Intent, TransferReason
from uaagro_domain.livefeed import LiveEvent
from voice_worker.adapters.telephony.exotel import ExotelSerializer
from voice_worker.flow.agent import CALLBACK_COMMITMENT_HI, Agent
from voice_worker.flow.context import ContextBuilder
from voice_worker.flow.escalation import EscalationDecision, EscalationEngine, TransferRequest
from voice_worker.flow.validator import OutputValidator
from voice_worker.runtime import session as session_module
from voice_worker.runtime.assembly import CallLink
from voice_worker.runtime.direction import OurNumbers
from voice_worker.runtime.session import TRANSFER_FAILED_LINE_HI, CallSession, TransportClosed
from voice_worker.tools.base import Tool, ToolContext, ToolRegistry, ToolResult

TARGET = "+919876500011"

PREPARED = {
    "action": "transfer",
    "reason": "explicit_request",
    "urgency": "normal",
    "target_kind": "centre_manager",
    "target_name": "Sitapur centre manager",
    "target_number": TARGET,
    "whisper": "कमला देवी, सीतापुर, ऑर्डर के बारे में",
    "say_first": "जी, मैं आपको सीतापुर केंद्र के प्रबंधक से जोड़ रहा हूँ। एक क्षण रुकिए।",
}

FALLBACK = {
    "action": "commit_callback",
    "reason": "explicit_request",
    "urgency": "normal",
    "unavailable_because": "centre_closed",
    "next_steps": ["create_ticket", "send_whatsapp"],
}


# --------------------------------------------------------------------------- #
# The request the tool hands back
# --------------------------------------------------------------------------- #


def test_a_prepared_transfer_becomes_a_request_and_a_fallback_does_not() -> None:
    request = TransferRequest.from_tool_result(PREPARED)
    assert request is not None
    assert request.to == TARGET
    assert request.target_name == "Sitapur centre manager"
    assert request.say_first.startswith("जी, मैं आपको")
    assert TransferRequest.from_tool_result(FALLBACK) is None


def test_the_destination_number_stays_out_of_the_record() -> None:
    """§17, §23-6: the number is for the phone line, not the model or the row."""
    result = ToolResult(tool="transfer_to_human", ok=True, data=dict(PREPARED))
    recorded = result.to_dict()
    assert "target_number" not in json.dumps(recorded)
    assert recorded["data"]["target_name"] == "Sitapur centre manager"
    # The loop still sees it.
    assert TransferRequest.from_tool_result(result.data) is not None


# --------------------------------------------------------------------------- #
# The agent asks the tool who takes the call
# --------------------------------------------------------------------------- #


class _TransferTool(Tool):
    name = "transfer_to_human"
    description = "test double"
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"reason": {"type": "string"}, "urgency": {"type": "string"}},
        "required": ["reason"],
    }
    read_only = False

    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.asked: list[Mapping[str, Any]] = []

    async def run(self, args: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
        self.asked.append(dict(args))
        return dict(self.answer)


class _AlwaysEscalate(EscalationEngine):
    def evaluate(self, signals: Any, *, repeated_intent: bool = False) -> EscalationDecision:
        return EscalationDecision(
            escalate=True, reason=TransferReason.ABUSE_OR_ANGER, immediate=True
        )


def _agent(tool: Tool | None) -> Agent:
    registry = ToolRegistry()
    if tool is not None:
        registry.register(tool)
    return Agent(
        registry=registry,
        context_builder=ContextBuilder(persona="आप सहायक हैं।"),
        gateway=None,
        validator=OutputValidator(),
        escalation=_AlwaysEscalate(),
    )


async def test_an_immediate_escalation_speaks_the_tools_line_and_pends_the_transfer() -> None:
    tool = _TransferTool(PREPARED)
    agent = _agent(tool)
    spoken = [piece async for piece in agent.respond("मुझे अभी मैनेजर से बात करनी है")]

    assert spoken == [PREPARED["say_first"]]
    assert tool.asked == [{"reason": "abuse_or_anger", "urgency": "normal"}]
    pending = agent.pending_transfer
    assert pending is not None and pending.to == TARGET
    assert agent.last_turn is not None and agent.last_turn.ends_agent_turns


async def test_nobody_reachable_becomes_a_callback_commitment() -> None:
    agent = _agent(_TransferTool(FALLBACK))
    spoken = [piece async for piece in agent.respond("मुझे अभी मैनेजर से बात करनी है")]

    assert spoken == [CALLBACK_COMMITMENT_HI]
    assert agent.pending_transfer is None


async def test_without_a_transfer_tool_the_placeholder_is_spoken() -> None:
    agent = _agent(None)
    spoken = [piece async for piece in agent.respond("मुझे अभी मैनेजर से बात करनी है")]

    assert spoken and "जोड़ रहा हूँ" in spoken[0]
    assert agent.pending_transfer is None
    assert agent.last_turn is not None and agent.last_turn.intent is not Intent.SAFETY_EMERGENCY


# --------------------------------------------------------------------------- #
# The session joins the caller to the person
# --------------------------------------------------------------------------- #


class _Transport:
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
    def __init__(self) -> None:
        self.transfers: list[tuple[str, bool]] = []
        self.events: list[str] = []
        self.finalised: list[dict[str, Any]] = []

    async def create_call(self, record: Any) -> None:
        return None

    async def attach_call_context(self, call_id: uuid.UUID, started_at: datetime, **f: Any) -> None:
        return None

    async def record_turn(self, call_id: uuid.UUID, started_at: datetime, **fields: Any) -> None:
        return None

    async def record_event(
        self, call_id: uuid.UUID, event_type: str, payload: Any, at: Any
    ) -> None:
        self.events.append(event_type)

    async def record_dtmf(self, call_id: uuid.UUID, digit: str, *args: Any) -> None:
        return None

    async def link_contact(self, contact_id: uuid.UUID, call_id: uuid.UUID) -> None:
        return None

    async def mark_transferred(
        self, call_id: uuid.UUID, started_at: datetime, *, reason: str, completed: bool
    ) -> None:
        self.transfers.append((reason, completed))

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
    accepts_dtmf = False
    call_over = False
    result = None


class _Pipeline:
    def __init__(self) -> None:
        self.on_turn: Any = None
        self.on_activity: Any = None
        self.on_call_over: Any = None
        self.on_transfer: Any = None
        self.responder = _Responder()
        self.announced: list[str] = []
        self._stop = asyncio.Event()

    async def run(self) -> None:
        await self._stop.wait()

    async def feed_audio(self, pcm: bytes) -> None:
        return None

    async def announce(self, text: str) -> None:
        self.announced.append(text)

    async def close(self) -> None:
        self._stop.set()


class _Built:
    def __init__(self, pipeline: _Pipeline) -> None:
        self.pipeline = pipeline
        self.link = CallLink(
            farmer_id=uuid.uuid4(),
            farmer_name="कमला देवी",
            farmer_last4="9560",
            centre_id=uuid.uuid4(),
            centre_code="NKSK-STP-01",
            centre_name="Sitapur",
            campaign_id=None,
            contact_id=None,
            language="hi-IN",
            config_version=1,
        )
        self.greeting_pcm = b""
        self.agent = None
        self.finish = None


class _Adapter:
    """Call control that records the hand-over it was asked for, or refuses."""

    def __init__(self, approved: frozenset[str], *, fail: bool) -> None:
        self.approved = approved
        self.fail = fail
        self.transfers: list[dict[str, Any]] = []

    async def transfer(self, *, call_sid: str, to: str, whisper_text: str | None = None) -> None:
        if self.fail:
            raise RuntimeError("provider said no")
        self.transfers.append(
            {"call_sid": call_sid, "to": to, "whispered": whisper_text is not None}
        )


def _session(
    transport: _Transport,
    repo: _Repository,
    feed: _Feed,
    pipeline: _Pipeline,
    adapters: list[_Adapter],
    *,
    fail: bool,
) -> CallSession:
    async def factory(_: CallSession) -> _Built:
        return _Built(pipeline)

    def adapter_for(approved: frozenset[str]) -> _Adapter:
        adapter = _Adapter(approved, fail=fail)
        adapters.append(adapter)
        return adapter

    return CallSession(
        transport=transport,  # type: ignore[arg-type]
        serializer=ExotelSerializer(),
        repository=repo,
        direction=CallDirection.INBOUND,
        pipeline_factory=factory,
        live_feed=feed,
        our_numbers=OurNumbers(inbound=frozenset({"1800123456"}), outbound=frozenset()),
        transfer_adapter=adapter_for,
    )


def _start_frame() -> dict[str, Any]:
    return {
        "event": "start",
        "stream_sid": "stream-1",
        "start": {"call_sid": "call-77", "from": "+919876509560", "to": "+911800123456"},
    }


async def _settle() -> None:
    for _ in range(40):
        await asyncio.sleep(0.002)


async def _run_call(
    *, fail: bool
) -> tuple[CallSession, _Repository, _Feed, _Pipeline, list[_Adapter]]:
    transport, repo, feed, pipeline = _Transport(), _Repository(), _Feed(), _Pipeline()
    adapters: list[_Adapter] = []
    session = _session(transport, repo, feed, pipeline, adapters, fail=fail)
    running = asyncio.create_task(session.run())
    transport.frame({"event": "connected"})
    transport.frame(_start_frame())
    await _settle()
    assert pipeline.on_transfer is not None, "the session did not wire the hook"

    request = TransferRequest.from_tool_result(PREPARED)
    await pipeline.on_transfer(request)
    await _settle()
    transport.frame({"event": "stop"})
    await running
    return session, repo, feed, pipeline, adapters


async def test_a_transfer_joins_the_caller_and_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_module, "HANGUP_GRACE_S", 0.0)
    session, repo, feed, pipeline, adapters = await _run_call(fail=False)

    assert len(adapters) == 1 and adapters[0].approved == frozenset({TARGET})
    assert adapters[0].transfers == [{"call_sid": "call-77", "to": TARGET, "whispered": True}]
    assert repo.transfers == [("explicit_request", True)]
    assert "transfer" in repo.events
    assert session.outcome is CallOutcome.TRANSFERRED
    assert repo.finalised[0]["outcome"] is CallOutcome.TRANSFERRED
    transfers = feed.of(livefeed.CALL_TRANSFER)
    assert len(transfers) == 1
    assert transfers[0].payload == {
        "callId": str(session.call_id),
        "to": "Sitapur centre manager",
        "reason": "explicit_request",
    }
    # The number itself never leaves the loop.
    for event in feed.events:
        assert TARGET not in event.encode()
    assert pipeline.announced == []


async def test_a_refused_transfer_promises_a_call_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_module, "HANGUP_GRACE_S", 0.0)
    session, repo, feed, pipeline, _adapters = await _run_call(fail=True)

    assert repo.transfers == [("explicit_request", False)]
    assert "transfer_failed" in repo.events
    assert pipeline.announced == [TRANSFER_FAILED_LINE_HI]
    assert session.outcome is not CallOutcome.TRANSFERRED
    assert feed.of(livefeed.CALL_TRANSFER) == []


async def test_a_second_request_on_the_same_call_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module, "HANGUP_GRACE_S", 0.0)
    transport, repo, feed, pipeline = _Transport(), _Repository(), _Feed(), _Pipeline()
    adapters: list[_Adapter] = []
    session = _session(transport, repo, feed, pipeline, adapters, fail=False)
    running = asyncio.create_task(session.run())
    transport.frame(_start_frame())
    await _settle()
    request = TransferRequest.from_tool_result(PREPARED)
    await pipeline.on_transfer(request)
    await pipeline.on_transfer(request)
    transport.frame({"event": "stop"})
    await running

    assert len(adapters) == 1
    assert repo.transfers == [("explicit_request", True)]


# --------------------------------------------------------------------------- #
# The pipeline waits for the line to be spoken
# --------------------------------------------------------------------------- #


class _HandingOverResponder:
    """Says the transfer line and has a hand-over pending."""

    accepts_dtmf = False
    call_over = False
    pending_transfer = TransferRequest.from_tool_result(PREPARED)

    async def respond(self, transcript: str, *, language: str) -> AsyncIterator[str]:
        yield "जी, मैं आपको प्रबंधक से जोड़ रहा हूँ।"


async def test_the_pipeline_hands_over_only_after_speaking() -> None:
    from tests.test_pipeline import _pipeline
    from voice_worker.adapters.stt.base import SttEvent, SttEventType

    responder = _HandingOverResponder()
    pipeline, tts, _sent = _pipeline(
        [SttEvent(type=SttEventType.END_OF_TURN, text="मैनेजर से बात कराइए")], responder
    )
    order: list[str] = []

    async def on_transfer(request: Any) -> None:
        order.append(f"transfer:{request.to}")

    pipeline.on_transfer = on_transfer
    await pipeline.run()

    assert tts.requests, "the line was not spoken"
    assert order == [f"transfer:{TARGET}"]
    assert len(pipeline.turns) == 1


async def test_an_announcement_is_spoken_and_recorded_as_a_turn() -> None:
    from tests.test_pipeline import CannedResponder, _pipeline

    pipeline, tts, _sent = _pipeline([], CannedResponder())
    await pipeline.announce(TRANSFER_FAILED_LINE_HI)

    assert tts.requests and "चौबीस घंटे" in " ".join(tts.requests)
    assert pipeline.turns[-1].response == TRANSFER_FAILED_LINE_HI
    assert pipeline.turns[-1].transcript == ""
