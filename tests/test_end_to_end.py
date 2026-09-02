"""A whole call, through every layer that a real one goes through (§4.1, §11).

This file exists because of a gap that every other test in the suite missed.
Phases 2, 3 and 4 each built their piece and tested it thoroughly in isolation
-- the speech pipeline, the tools, the flow state machine, the safety path --
and every one of those suites passed. Nothing imported them. The worker's media
path still played the Phase 1 placeholder tone, and no test could tell, because
each layer was verified against its own doubles rather than against the layer
above it.

So these tests deliberately assemble the real objects: the real
:class:`CallSession`, the real serializer, the real
:class:`~voice_worker.runtime.assembly.build_call_pipeline`, the real
:class:`~voice_worker.pipelines.conversation.ConversationPipeline`, the real
tool registry against a real database. Only the three things at the edges that
cost money -- the recogniser, the synthesiser and the model -- are doubles.

If the wiring is ever unhooked again, these fail.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from uaagro_db.models import AgentConfig
from uaagro_domain.enums import FlowType, QualityTier
from uaagro_domain.settings import get_defaults, get_settings
from voice_worker.adapters.factory import SpeechStack
from voice_worker.adapters.stt.base import SttConfig, SttEvent, SttEventType, STTService
from voice_worker.adapters.telephony.exotel import ExotelSerializer
from voice_worker.adapters.tts.base import TtsChunk, TtsConfig, TTSService
from voice_worker.runtime import audio as audio_utils
from voice_worker.runtime.assembly import (
    Caller,
    ConfigurationMissing,
    build_call_pipeline,
    identify_caller,
    render_greeting,
)
from voice_worker.runtime.session import CallSession
from voice_worker.tools import build_registry
from voice_worker.tools.session import reset_session_factory, set_session_factory
from voice_worker.turn.base import TurnDetector, TurnResult, TurnState, TurnStrategy

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# The three paid edges, and nothing else
# --------------------------------------------------------------------------- #


class ScriptedSTT(STTService):
    """A recogniser that emits a fixed transcript once audio arrives."""

    provider = "scripted"

    def __init__(self, transcript: str, *, language: str = "hi-IN") -> None:
        self.transcript = transcript
        self.language = language
        self.audio_bytes = 0
        self.closed = False
        self._audio_seen = asyncio.Event()

    async def start(self, config: SttConfig) -> None:
        return None

    async def send_audio(self, pcm: bytes) -> None:
        self.audio_bytes += len(pcm)
        if self.audio_bytes > 0:
            self._audio_seen.set()

    async def events(self) -> AsyncIterator[SttEvent]:
        # Waits for real audio rather than firing immediately: that is what
        # makes this a test of the session actually forwarding frames.
        await self._audio_seen.wait()
        yield SttEvent(type=SttEventType.SPEECH_STARTED)
        yield SttEvent(
            type=SttEventType.FINAL_TRANSCRIPT,
            text=self.transcript,
            confidence=0.94,
            language=self.language,
            language_confidence=0.97,
        )
        yield SttEvent(type=SttEventType.END_OF_TURN, text=self.transcript, confidence=0.94)
        # Then idle, the way a live socket does between turns.
        await asyncio.sleep(3600)

    async def finalise(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class ToneTTS(TTSService):
    """Synthesises a tone and records every string it was asked to speak."""

    provider = "scripted"

    def __init__(self) -> None:
        self.requests: list[str] = []

    async def synthesise(self, text: str, config: TtsConfig) -> AsyncIterator[TtsChunk]:
        self.requests.append(text)
        yield TtsChunk(audio=audio_utils.tone(440, 100), is_first=True)
        yield TtsChunk(audio=b"", is_final=True)

    async def close(self) -> None:
        return None


class ImmediateDetector(TurnDetector):
    strategy = TurnStrategy.FLUX_SEMANTIC

    async def evaluate(self, state: TurnState) -> TurnResult:
        from voice_worker.turn.base import TurnDecision

        return TurnResult(decision=TurnDecision.CONTINUE)


class ScriptedGateway:
    """Stands in for the LLM behind the §6.1 gateway.

    Returns a grounded-sounding Hindi sentence. What it says matters less than
    that the agent, validator and pipeline all accept it and it reaches the
    synthesiser.
    """

    def __init__(self, answer: str = "जी, डीएपी उपलब्ध है।") -> None:
        self.answer = answer
        self.prompts: list[str] = []

    async def complete(self, *args: object, **kwargs: object) -> object:
        from voice_worker.adapters.llm.gateway import Completion, Rung

        prompt = str(kwargs.get("prompt") or (args[0] if args else ""))
        self.prompts.append(prompt)
        return Completion(text=self.answer, rung=Rung.PRIMARY, model="scripted")


def _stack(stt: STTService, tts: TTSService) -> SpeechStack:
    return SpeechStack(
        language="hi-IN",
        served_by="scripted",
        stt=stt,
        stt_config=SttConfig(language="hi-IN"),
        tts=tts,
        tts_config=TtsConfig(language="hi-IN", speaker="anushka"),
        turn_detector=ImmediateDetector(),
        quality_tier=QualityTier.A,
        tier_note="scripted stack for the end-to-end test",
    )


# --------------------------------------------------------------------------- #
# A transport that plays a caller
# --------------------------------------------------------------------------- #


class ScriptedTransport:
    """Feeds a call's frames in and captures everything sent back."""

    def __init__(self, frames: int = 10) -> None:
        self.sent: list[str] = []
        self._inbound = self._script(frames)
        self.closed = False

    def _script(self, frames: int) -> list[str]:
        stream_sid = "stream-e2e"
        messages = [
            json.dumps({"event": "connected"}),
            json.dumps(
                {
                    "event": "start",
                    "stream_sid": stream_sid,
                    "start": {
                        "stream_sid": stream_sid,
                        "call_sid": f"CALL-{uuid.uuid4().hex[:8].upper()}",
                        "account_sid": "acct",
                        "from": "+919000000123",
                        "to": "+911140000000",
                        "media_format": {
                            "encoding": "audio/x-l16",
                            "sample_rate": 8000,
                            "bit_rate": "128kbps",
                        },
                    },
                }
            ),
        ]
        import base64

        frame = base64.b64encode(audio_utils.tone(300, 20)).decode()
        messages += [
            json.dumps(
                {
                    "event": "media",
                    "stream_sid": stream_sid,
                    "media": {"chunk": i + 1, "timestamp": str(i * 20), "payload": frame},
                }
            )
            for i in range(frames)
        ]
        messages.append(json.dumps({"event": "stop", "stream_sid": stream_sid}))
        return messages

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        if not self._inbound:
            # Give the pipeline a moment to answer before the call ends.
            await asyncio.sleep(0.4)
            from voice_worker.runtime.session import TransportClosed

            raise TransportClosed("caller hung up")
        # Real time enough that the pipeline's turn loop interleaves.
        await asyncio.sleep(0.005)
        return self._inbound.pop(0)

    @property
    def audio_frames(self) -> list[dict[str, object]]:
        out = []
        for raw in self.sent:
            message = json.loads(raw)
            if message.get("event") == "media":
                out.append(message)
        return out


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
async def published_config(app_engine):  # type: ignore[no-untyped-def]
    """Publish the seeded inbound config for the duration of one test.

    The seeds leave it unpublished on purpose -- publishing is an audited act,
    not a fixture -- so a test that wants a working agent has to do what a human
    would have to do.
    """
    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        config = await session.scalar(
            select(AgentConfig).where(AgentConfig.flow_type == FlowType.INBOUND)
        )
        assert config is not None, "the seeds should provide an inbound agent config"
        user_id = await session.scalar(text("SELECT id FROM users LIMIT 1"))
        config.is_published = True
        config.published_at = datetime.now(UTC)
        config.published_by_user_id = user_id
        organization_id = config.organization_id
        await session.commit()

    yield organization_id

    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        row = await session.scalar(
            select(AgentConfig).where(AgentConfig.flow_type == FlowType.INBOUND)
        )
        if row is not None:
            row.is_published = False
            row.published_at = None
            row.published_by_user_id = None
        await session.commit()


@pytest.fixture
async def tool_sessions(app_engine):  # type: ignore[no-untyped-def]
    from contextlib import asynccontextmanager

    maker = async_sessionmaker(app_engine, expire_on_commit=False)

    @asynccontextmanager
    async def factory():  # type: ignore[no-untyped-def]
        async with maker() as session:
            await session.execute(
                text(
                    "SELECT set_config('app.role','voice_agent',true),"
                    "       set_config('app.centre_ids','',true)"
                )
            )
            yield session
            await session.commit()

    set_session_factory(factory)
    try:
        yield factory
    finally:
        reset_session_factory()


# --------------------------------------------------------------------------- #
# The tests
# --------------------------------------------------------------------------- #


async def test_a_call_is_heard_answered_and_spoken_back(
    app_engine, published_config, tool_sessions
) -> None:  # type: ignore[no-untyped-def]
    """The whole path: frames in, a grounded answer out, as audio.

    The assertion that matters is the last one. Everything before it could pass
    with the pipeline never constructed -- which is exactly what was happening
    before this file existed.
    """
    stt = ScriptedSTT("डीएपी का रेट क्या है")
    tts = ToneTTS()
    transport = ScriptedTransport(frames=10)
    maker = async_sessionmaker(app_engine, expire_on_commit=False)

    async def factory(session: CallSession):  # type: ignore[no-untyped-def]
        async with maker() as db:
            await db.execute(text("SELECT set_config('app.role','voice_agent',true)"))
            return await build_call_pipeline(
                call_id=session.call_id,
                organization_id=published_config,
                phone_hash=session.from_number_hash,
                session=db,
                registry=build_registry(),
                settings=get_settings(),
                defaults=get_defaults(),
                send_frame=session.send_audio,
                clear_playback=session.clear_playback,
                gateway=ScriptedGateway(),
                stack=_stack(stt, tts),
                realtime=False,
            )

    session = CallSession(
        transport=transport,
        serializer=ExotelSerializer(),
        repository=None,
        pipeline_factory=factory,
    )
    stats = await session.run()

    # The session forwarded caller audio to the recogniser.
    assert stt.audio_bytes > 0, "no caller audio reached the recogniser"
    assert stats.inbound_frames == 10

    # The agent produced something and it was synthesised.
    assert tts.requests, "nothing was ever sent to the synthesiser"

    # And it reached the caller as media frames.
    assert transport.audio_frames, "the caller heard nothing"


async def test_the_greeting_is_the_published_one_not_a_tone(
    app_engine, published_config, tool_sessions
) -> None:  # type: ignore[no-untyped-def]
    """§11.1's greeting comes from the published config.

    The Phase 1 placeholder was a 440 Hz tone. A farmer hearing a beep instead
    of "नमस्ते" is the single most visible symptom of an unwired pipeline.
    """
    tts = ToneTTS()
    maker = async_sessionmaker(app_engine, expire_on_commit=False)

    async with maker() as db:
        await db.execute(text("SELECT set_config('app.role','voice_agent',true)"))
        built = await build_call_pipeline(
            call_id=uuid.uuid4(),
            organization_id=published_config,
            phone_hash=None,
            session=db,
            registry=build_registry(),
            settings=get_settings(),
            defaults=get_defaults(),
            send_frame=_discard,
            gateway=ScriptedGateway(),
            stack=_stack(ScriptedSTT("hello"), tts),
            realtime=False,
        )

    assert built.greeting_pcm, "no greeting audio was produced"
    assert tts.requests, "the greeting was never synthesised"
    # The published Hindi greeting, not a placeholder.
    assert any("ऀ" <= ch <= "ॿ" for ch in tts.requests[0]), (
        f"the greeting was not Devanagari: {tts.requests[0]!r}"
    )


async def test_an_unpublished_config_refuses_to_take_calls(
    app_engine, tool_sessions
) -> None:  # type: ignore[no-untyped-def]
    """§9's posture, applied to prompts.

    The seeds leave the config unpublished, and an agent speaking words nobody
    approved is worse than one that does not answer. The error names the fix.
    """
    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    async with maker() as db:
        await db.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        organization_id = await db.scalar(text("SELECT id FROM organizations LIMIT 1"))

        with pytest.raises(ConfigurationMissing) as raised:
            await build_call_pipeline(
                call_id=uuid.uuid4(),
                organization_id=organization_id,
                phone_hash=None,
                session=db,
                registry=build_registry(),
                settings=get_settings(),
                defaults=get_defaults(),
                send_frame=_discard,
                gateway=ScriptedGateway(),
                stack=_stack(ScriptedSTT("x"), ToneTTS()),
                realtime=False,
            )

    assert "admin panel" in raised.value.remedy


async def test_a_call_survives_an_agent_that_cannot_be_built(app_engine) -> None:  # type: ignore[no-untyped-def]
    """§11.4: every failure has a route, and none of them is a dead line.

    A worker that cannot assemble the agent still answers, still records the
    call, and marks it so the panel says why -- rather than dropping the socket
    on somebody who has been holding for four rings.
    """

    async def failing(session: CallSession):  # type: ignore[no-untyped-def]
        raise RuntimeError("the model gateway is unreachable")

    transport = ScriptedTransport(frames=4)
    session = CallSession(
        transport=transport,
        serializer=ExotelSerializer(),
        repository=None,
        greeting_pcm=audio_utils.tone(440, 100),
        pipeline_factory=failing,
    )
    stats = await session.run()

    from uaagro_domain.enums import CallOutcome

    assert session.outcome is CallOutcome.SYSTEM_FAILURE
    assert stats.inbound_frames == 4
    # The caller still heard the greeting rather than silence.
    assert transport.audio_frames


async def test_the_caller_is_identified_by_hash_never_by_number(
    app_engine, tool_sessions
) -> None:  # type: ignore[no-untyped-def]
    """§17: the HMAC is the only lookup key.

    Also §11.5's payoff -- the language persisted after the last call is what
    the next one opens in.
    """
    from uaagro_db.crypto import get_cipher
    from uaagro_db.seeds.loader import DEMO_PHONE_PREFIX
    from uaagro_domain.phone import normalise_msisdn

    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    digest = get_cipher().hash(normalise_msisdn(f"{DEMO_PHONE_PREFIX}000000"))

    async with maker() as db:
        await db.execute(text("SELECT set_config('app.role','voice_agent',true)"))
        known = await identify_caller(db, digest, default_language="hi-IN")
        unknown = await identify_caller(db, b"\x00" * 32, default_language="hi-IN")
        withheld = await identify_caller(db, None, default_language="hi-IN")

    assert known.known is True
    assert known.language
    assert unknown.known is False
    assert withheld.known is False
    assert withheld.language == "hi-IN"


def test_a_greeting_never_shows_a_hole_where_a_name_should_be() -> None:
    """An unknown caller gets a greeting, not a template artefact."""
    template = "नमस्ते {name_ji}, नवीन खुशहाली किसान सेवा केंद्र में आपका स्वागत है।"

    named = render_greeting(template, Caller(name="संतोष", known=True))
    anonymous = render_greeting(template, Caller())

    assert "संतोष जी" in named
    assert "{" not in anonymous and "}" not in anonymous
    # No double space where the name was removed.
    assert "  " not in anonymous


async def _discard(_frame: bytes) -> None:
    return None
