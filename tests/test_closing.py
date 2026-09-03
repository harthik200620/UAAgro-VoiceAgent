"""Ending a call properly (§11.1 CLOSE, §11.4).

Three ways a helpline call ends, and each must end in the goodbye being
*heard* and the line then dropping from our side: the farmer says goodbye;
the agent asks whether there is anything else and the farmer says no; the
farmer goes quiet and stays quiet.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

from tests.test_interruption import Harness, Responder, end
from tests.test_voice_gate import quiet, voice
from uaagro_domain.enums import CallOutcome
from voice_worker.flow.address import AddressBudget
from voice_worker.flow.agent import Agent
from voice_worker.flow.closing import (
    CLOSING_LINE_HI,
    SILENCE_PROMPT_HI,
    SILENCE_WARN_HI,
    asked_for_more,
    declines_more,
    farmer_is_done,
    is_farewell,
)
from voice_worker.flow.context import CallerContext, ContextBuilder
from voice_worker.flow.validator import OutputValidator
from voice_worker.pipelines.conversation import SilenceLadder
from voice_worker.runtime.vad import VoiceGate
from voice_worker.text.speech import text_for_speech
from voice_worker.tools.base import ToolRegistry

# --------------------------------------------------------------------------- #
# The words
# --------------------------------------------------------------------------- #


def test_a_goodbye_is_recognised_in_the_ways_farmers_say_it() -> None:
    for line in (
        "बस, धन्यवाद",
        "ठीक है धन्यवाद जी",
        "बहुत बहुत धन्यवाद",
        "थैंक यू सर",
        "ok thank you",
        "बस इतना ही",
        "और कुछ नहीं, बस",
        "अच्छा ठीक है, रखता हूँ",
        "चलो ठीक है, नमस्ते",
        "ओके बाय",
        "हो गया जी, धन्यवाद",
        "नमस्ते",
    ):
        assert is_farewell(line), line


def test_a_question_with_a_thank_you_in_it_is_still_a_question() -> None:
    for line in (
        "धन्यवाद, और यूरिया का रेट क्या है?",
        "ठीक है, डीएपी मिलेगा क्या",
        "अच्छा",
        "ठीक है",
        "बताइए",
        "हाँ जी",
        "नमस्ते, मुझे गेहूँ के बीज चाहिए",
        "थैंक्स, और एक बात बताइए",
    ):
        assert not is_farewell(line), line


def test_a_plain_no_counts_only_after_the_agent_asked_for_more() -> None:
    assert declines_more("नहीं")
    assert declines_more("नहीं जी, बस")
    assert declines_more("no")
    assert not declines_more("नहीं, कल आऊँगा")
    assert asked_for_more("बोरी तेरह सौ पचास की है। और कुछ पूछना है?")
    assert asked_for_more("कुछ और चाहिए?")
    assert not asked_for_more("क्या आप कल सेंटर आ सकते हैं?")

    assert farmer_is_done("नहीं", last_agent_line="और कुछ पूछना है?")
    assert not farmer_is_done("नहीं", last_agent_line="क्या आप कल आ सकते हैं?")
    assert farmer_is_done("बस, धन्यवाद", last_agent_line=None)


# --------------------------------------------------------------------------- #
# The agent
# --------------------------------------------------------------------------- #


class ScriptedGateway:
    def __init__(self, *generations: Sequence[str]) -> None:
        self.generations = list(generations)
        self.calls = 0

    async def stream(
        self,
        *,
        system_blocks: Sequence[str],
        user_message: str,
        cacheable_prefix: str = "",
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[tuple[str, str]]:
        index = min(self.calls, len(self.generations) - 1)
        self.calls += 1
        for fragment in self.generations[index]:
            yield fragment, "primary"


def make_agent(gateway: ScriptedGateway, **kwargs: object) -> Agent:
    return Agent(
        registry=ToolRegistry(),
        context_builder=ContextBuilder(persona="आप सहायक हैं।"),
        gateway=gateway,
        validator=OutputValidator(),
        caller=CallerContext(name=None),
        address=AddressBudget(),
        **kwargs,  # type: ignore[arg-type]
    )


async def collect(agent: Agent, transcript: str) -> list[str]:
    return [piece async for piece in agent.respond(transcript)]


async def test_a_goodbye_is_answered_from_the_config_not_the_model() -> None:
    gateway = ScriptedGateway(["कुछ भी"])
    agent = make_agent(gateway, closing_line="धन्यवाद! नमस्ते।")

    pieces = await collect(agent, "बस, धन्यवाद")

    assert pieces == ["धन्यवाद! नमस्ते।"]
    assert gateway.calls == 0, "the model was asked how to say goodbye"
    assert agent.call_over
    assert agent.result is not None and agent.result.call_outcome is CallOutcome.RESOLVED
    assert agent.memory.turns[-1].text == "धन्यवाद! नमस्ते।"


async def test_no_after_anything_else_ends_the_call_and_no_otherwise_does_not() -> None:
    gateway = ScriptedGateway(["बोरी तेरह सौ पचास की है। ", "और कुछ पूछना है?"], ["ठीक है, कल मिलते हैं।"])
    agent = make_agent(gateway)

    await collect(agent, "डीएपी का रेट")
    pieces = await collect(agent, "नहीं")

    assert pieces == [CLOSING_LINE_HI]
    assert agent.call_over

    other = make_agent(ScriptedGateway(["क्या आप कल सेंटर आ सकते हैं?"], ["ठीक है।"]))
    await collect(other, "मुझे बीज चाहिए")
    pieces = await collect(other, "नहीं")
    assert pieces == ["ठीक है।"]
    assert not other.call_over


async def test_the_outbound_questions_agent_leaves_ending_to_the_script() -> None:
    gateway = ScriptedGateway(["जी, कल तक मिल जाएगा।"])
    agent = make_agent(gateway, closes_calls=False)

    pieces = await collect(agent, "ठीक है धन्यवाद")

    assert not agent.call_over
    # The "जी," opener is the address trimmer's business, not this test's.
    assert pieces == ["कल तक मिल जाएगा।"]


# --------------------------------------------------------------------------- #
# The pipeline
# --------------------------------------------------------------------------- #


class ClosingResponder(Responder):
    """Says its sentences; on a goodbye says the closing line and is done."""

    closing_line = CLOSING_LINE_HI

    def __init__(self, sentences: list[str] | None = None) -> None:
        super().__init__(sentences)
        self.call_over = False
        self.result = None

    async def respond(self, transcript: str, *, language: str) -> AsyncIterator[str]:
        if is_farewell(transcript):
            self.calls.append(transcript)
            self.call_over = True
            yield self.closing_line
            return
        async for piece in super().respond(transcript, language=language):
            yield piece


async def test_the_line_drops_only_after_the_goodbye_has_been_heard() -> None:
    responder = ClosingResponder()
    hung_up_after_frames: list[int] = []
    async with Harness([(0.0, end("बस, धन्यवाद"))], responder, tts_ms=600) as h:

        async def over() -> None:
            hung_up_after_frames.append(len(h.sent))

        h.pipeline.on_call_over = over

    goodbye_frames = len(h.sent)
    assert hung_up_after_frames == [goodbye_frames], "hung up before the goodbye finished"
    assert goodbye_frames >= 30, "the goodbye was not played"
    assert h.played() == [
        text_for_speech(s) for s in ("धन्यवाद!", "और कुछ पूछना हो तो कभी भी फ़ोन कीजिए।", "नमस्ते।")
    ]


async def test_silence_is_prompted_twice_and_then_closed() -> None:
    hung_up = asyncio.Event()
    answer = Responder(["डीएपी की बोरी उपलब्ध है।"])
    async with Harness([(0.0, end("डीएपी का रेट बताइए"))], answer, tts_ms=200) as h:
        h.pipeline.silence = SilenceLadder(prompt_s=0.4, warn_s=0.8, close_s=1.2)

        async def over() -> None:
            hung_up.set()

        h.pipeline.on_call_over = over
        h.pipeline.responder.closing_line = CLOSING_LINE_HI  # type: ignore[attr-defined]
        await asyncio.wait_for(hung_up.wait(), timeout=6.0)

    said = [t.response for t in h.pipeline.turns]
    assert said[0] == "डीएपी की बोरी उपलब्ध है।"
    assert SILENCE_PROMPT_HI in said and SILENCE_WARN_HI in said
    assert said[-1] == CLOSING_LINE_HI
    assert said.index(SILENCE_PROMPT_HI) < said.index(SILENCE_WARN_HI) < said.index(CLOSING_LINE_HI)
    assert h.pipeline.call_outcome is CallOutcome.ABANDONED_SILENCE


async def test_a_voice_resets_the_silence_ladder() -> None:
    """The farmer made a sound at 0.6 s: the clock starts over from there."""
    hung_up = asyncio.Event()
    answer = Responder(["डीएपी की बोरी उपलब्ध है।"])
    script = [(0.0, end("डीएपी का रेट बताइए"))]
    async with Harness(script, answer, gate=VoiceGate(), tts_ms=200) as h:
        h.pipeline.silence = SilenceLadder(prompt_s=0.5, warn_s=1.0, close_s=1.5)

        async def over() -> None:
            hung_up.set()

        h.pipeline.on_call_over = over
        await asyncio.sleep(0.6)  # the answer has played; the ladder is running
        await h.feed(voice(0.4))  # the farmer speaks ...
        await h.feed(quiet(0.5))  # ... and the line goes quiet again
        await asyncio.sleep(0.4)
        assert not hung_up.is_set()
        prompts_by_then = [t.response for t in h.pipeline.turns].count(SILENCE_PROMPT_HI)
        await asyncio.sleep(2.6)

    assert prompts_by_then == 0, "the ladder did not start over when the farmer spoke"
    assert hung_up.is_set(), "the restarted ladder never closed the call"
    assert [t.response for t in h.pipeline.turns].count(SILENCE_PROMPT_HI) == 1
