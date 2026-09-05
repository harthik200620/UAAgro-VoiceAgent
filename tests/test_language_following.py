"""§11.1: follow a caller who switches language.

The recogniser labels every turn with the language it heard. Before this,
nothing downstream read the label: an English or Marathi turn was answered
in Hindi, by the Hindi templates, in the Hindi voice. Now the label picks a
route -- the reply is composed in English by the direct layer, or put into
the caller's language by the model with the direct layer's facts alongside
-- and the synthesiser switches voice when this deployment has one for it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from datetime import time
from typing import Any

from uaagro_domain.settings import Settings, get_defaults
from voice_worker.adapters.factory import SpeechStack, build_speech_stack
from voice_worker.adapters.stt.base import SttConfig, SttEvent, SttEventType, STTService
from voice_worker.adapters.tts.base import TtsChunk, TtsConfig, TTSService
from voice_worker.flow.address import AddressBudget
from voice_worker.flow.agent import Agent
from voice_worker.flow.context import CallerContext, ContextBuilder
from voice_worker.flow.direct import CentreFacts, DirectAnswers
from voice_worker.flow.focus import ConversationFocus
from voice_worker.flow.validator import OutputValidator
from voice_worker.pipelines.conversation import ConversationPipeline
from voice_worker.runtime import audio as audio_utils
from voice_worker.runtime.playback import PacedSender
from voice_worker.text.lexicon import Lexicon, LexiconEntry
from voice_worker.text.speech import text_for_speech
from voice_worker.tools.base import ToolContext, ToolResult

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

ENTRIES = [
    LexiconEntry(
        "SED-PDY-PUSA1509",
        "धान पूसा 1509",
        "Paddy Pusa Basmati 1509",
        ("पूसा 1509",),
        "seeds",
        ("paddy",),
    ),
    LexiconEntry("SED-PDY-SARJU52", "धान सरजू 52", "Paddy Sarju-52", ("सरजू",), "seeds", ("paddy",)),
    LexiconEntry("FERT-UREA-45", "यूरिया", "Urea", ("urea",), "fertilisers", ("wheat", "paddy")),
]

CENTRE = CentreFacts(
    id="c1",
    code="NKSK-LKO-01",
    name="लखनऊ सेंटर",
    address_spoken="अलीगंज में",
    phone="+919999000001",
    open_time=time(8, 0),
    close_time=time(19, 0),
    working_days=("mon", "tue", "wed", "thu", "fri", "sat"),
    own=False,
    name_en="Lucknow centre",
    address_en="Aliganj, Sector B main road",
)


class StockRegistry:
    def get(self, name: str) -> object | None:
        return None

    async def execute(self, name: str, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        sku = str(args["sku"])
        price = {"SED-PDY-PUSA1509": "1500", "SED-PDY-SARJU52": "900"}.get(sku, "1150")
        return ToolResult(
            tool=name,
            ok=True,
            data={
                "sku": sku,
                "available": True,
                "price": price,
                "pack": "50 kg",
                "alternatives": [],
            },
            latency_ms=1.0,
        )


def layer() -> DirectAnswers:
    return DirectAnswers(
        registry=StockRegistry(),  # type: ignore[arg-type]
        context=ToolContext(call_id="t"),
        lexicon=Lexicon.from_entries(list(ENTRIES)),
        centre=CENTRE,
    )


class RecordingGateway:
    """Answers with a fixed line and keeps what it was asked."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.requests: list[dict[str, Any]] = []

    async def stream(self, **kwargs: Any) -> AsyncIterator[tuple[str, str]]:
        self.requests.append(kwargs)
        yield self.answer, "primary"


def agent(gateway: RecordingGateway | None = None) -> Agent:
    return Agent(
        registry=StockRegistry(),  # type: ignore[arg-type]
        context_builder=ContextBuilder(persona="आप सहायक हैं।"),
        gateway=gateway,
        validator=OutputValidator(),
        caller=CallerContext(name=None),
        address=AddressBudget(),
        direct=layer(),
    )


# --------------------------------------------------------------------------- #
# The direct layer speaks English when the caller does
# --------------------------------------------------------------------------- #


async def test_the_direct_layer_composes_english_for_an_english_caller() -> None:
    direct = layer()
    focus = ConversationFocus()
    focus.note_user_turn("I want rice seeds")
    first = await direct.answer(
        "I want rice seeds", _intent("I want rice seeds"), focus, language="en-IN"
    )
    assert first is not None
    assert first.language == "en-IN"
    assert first.text == (
        "For paddy we have Paddy Pusa Basmati 1509 and Paddy Sarju-52. Which one would you like?"
    )

    focus.note_user_turn("the second one")
    second = await direct.answer(
        "the second one", _intent("the second one"), focus, language="en-IN"
    )
    assert second is not None and second.resolved
    assert second.text == (
        "Paddy Sarju-52 is in stock, 50 kg bag at 900 rupees. That is the Lucknow centre price."
    )


async def test_the_same_call_switches_back_to_hindi_with_the_caller() -> None:
    direct = layer()
    focus = ConversationFocus()
    focus.note_user_turn("urea price")
    english = await direct.answer("urea price", _intent("urea price"), focus, language="en-IN")
    assert english is not None and english.text.startswith("Urea, 50 kg bag: 1150 rupees.")

    focus.note_user_turn("यूरिया का रेट")
    hindi = await direct.answer("यूरिया का रेट", _intent("यूरिया का रेट"), focus, language="hi-IN")
    assert hindi is not None and hindi.language == "hi-IN"
    assert hindi.text.startswith("यूरिया, 50 किलो का बैग: 1150 रुपये।")


async def test_the_centre_is_described_in_english() -> None:
    direct = layer()
    reply = await direct.answer(
        "where is the shop", _intent("where is the shop"), ConversationFocus(), language="en-IN"
    )
    assert reply is not None
    assert reply.text == (
        "Lucknow centre is at Aliganj, Sector B main road. Open 8 am to 7 pm, Monday to Saturday. "
        "Phone number 9 9 9 9 0 0 0 0 0 1."
    )


# --------------------------------------------------------------------------- #
# The agent: English direct, other languages through the model
# --------------------------------------------------------------------------- #


async def test_the_agent_answers_an_english_caller_without_the_model() -> None:
    gateway = RecordingGateway("unused")
    bot = agent(gateway)
    pieces = [p async for p in bot.respond("I want rice seeds", language="en-IN")]
    assert " ".join(pieces).startswith(
        "For paddy we have Paddy Pusa Basmati 1509 and Paddy Sarju-52."
    )
    assert gateway.requests == []
    assert bot.language == "en-IN"


async def test_a_marathi_caller_gets_the_direct_facts_rendered_by_the_model() -> None:
    marathi = "आमच्याकडे धान पूसा 1509 आणि धान सरजू 52 आहेत. कोणते हवे?"
    gateway = RecordingGateway(marathi)
    bot = agent(gateway)
    pieces = [p async for p in bot.respond("धान का बीज चाहिए", language="mr-IN")]
    assert " ".join(pieces) == marathi
    assert len(gateway.requests) == 1
    prompt = " ".join(gateway.requests[0]["system_blocks"]) + gateway.requests[0]["user_message"]
    assert "मराठी" in prompt, "the model is told which language the farmer is speaking"
    assert "धान पूसा 1509" in gateway.requests[0]["user_message"]


async def test_a_rendering_with_a_number_the_facts_do_not_carry_falls_back_to_hindi() -> None:
    gateway = RecordingGateway("धान पूसा 1509 फक्त 999 रुपये.")
    bot = agent(gateway)
    pieces = [p async for p in bot.respond("धान का बीज चाहिए", language="mr-IN")]
    assert " ".join(pieces) == "धान के लिए हमारे पास धान पूसा 1509 और धान सरजू 52 हैं। कौन सा चाहिए?"


async def test_without_a_model_a_marathi_caller_still_gets_the_hindi_answer() -> None:
    bot = agent(None)
    pieces = [p async for p in bot.respond("यूरिया का रेट", language="mr-IN")]
    assert pieces[0].startswith("यूरिया, 50 किलो का बैग: 1150 रुपये।")


# --------------------------------------------------------------------------- #
# The pipeline follows the language it hears
# --------------------------------------------------------------------------- #


class ScriptedSTT(STTService):
    provider = "scripted"
    emits_turn_events = True

    def __init__(self, script: list[SttEvent]) -> None:
        self.script = script

    async def start(self, config: SttConfig) -> None:
        return None

    async def send_audio(self, pcm: bytes) -> None:
        return None

    async def events(self) -> AsyncIterator[SttEvent]:
        for event in self.script:
            yield event
            # Long enough for the answer to be spoken to the end: the next
            # turn is a new question, not an interruption of this one.
            await asyncio.sleep(0.05)

    async def finalise(self) -> None:
        return None

    async def close(self) -> None:
        return None


class RecordingTTS(TTSService):
    provider = "scripted"

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []

    async def synthesise(self, text: str, config: TtsConfig) -> AsyncIterator[TtsChunk]:
        self.requests.append((text, config.language))
        yield TtsChunk(audio=audio_utils.tone(440, 120), is_first=True)
        yield TtsChunk(audio=b"", is_final=True)

    async def close(self) -> None:
        return None


class LanguageRecorder:
    def __init__(self) -> None:
        self.languages: list[str] = []

    async def respond(self, transcript: str, *, language: str) -> AsyncIterator[str]:
        self.languages.append(language)
        yield "Urea is in stock, 50 kg bag at 1150 rupees." if language == "en-IN" else "जी।"


def _pipeline(
    script: list[SttEvent], *, voices: dict[str, TtsConfig]
) -> tuple[ConversationPipeline, RecordingTTS, LanguageRecorder]:
    defaults = get_defaults()
    built = build_speech_stack("hi-IN", Settings(), defaults)
    tts = RecordingTTS()
    responder = LanguageRecorder()
    stack = SpeechStack(
        language=built.language,
        served_by=built.served_by,
        stt=ScriptedSTT(script),
        stt_config=built.stt_config,
        tts=tts,
        tts_config=built.tts_config,
        turn_detector=built.turn_detector,
        quality_tier=built.quality_tier,
        tier_note=built.tier_note,
        voices=voices,
    )

    async def send(frame: bytes) -> None:
        return None

    pipeline = ConversationPipeline(
        stack=stack,
        sender=PacedSender(send, realtime=False),
        responder=responder,
        defaults=defaults,
    )
    return pipeline, tts, responder


def _turn(text: str, language: str) -> SttEvent:
    return SttEvent(type=SttEventType.END_OF_TURN, text=text, language=language)


async def test_an_english_turn_is_answered_and_voiced_in_english_when_a_voice_exists() -> None:
    english_voice = TtsConfig(language="en-IN", speaker="voice-en")
    pipeline, tts, responder = _pipeline(
        [_turn("I want urea", "en"), _turn("यूरिया का रेट", "hi")],
        voices={"en-IN": english_voice},
    )
    await pipeline.run()
    assert responder.languages == ["en-IN", "hi-IN"]
    # The English answer streams as a first clause and a remainder -- two
    # requests, both in the English voice; the Hindi turn follows in Hindi.
    languages = [language for _, language in tts.requests]
    assert languages[-1] == "hi-IN" and set(languages[:-1]) == {"en-IN"}
    assert " ".join(text for text, language in tts.requests if language == "en-IN") == (
        "Urea is in stock, 50 kg bag at 1150 rupees."
    )


async def test_a_language_with_no_voice_is_answered_in_the_language_being_served() -> None:
    pipeline, tts, responder = _pipeline([_turn("எனக்கு யூரியா வேண்டும்", "ta")], voices={})
    await pipeline.run()
    assert responder.languages == ["hi-IN"]
    assert [language for _, language in tts.requests] == ["hi-IN"]


# --------------------------------------------------------------------------- #
# Speech text outside Hindi keeps its numerals
# --------------------------------------------------------------------------- #


def test_english_speech_text_keeps_digits_and_names_the_currency() -> None:
    assert (
        text_for_speech("Urea, 50 kg bag: ₹1,350. 18% N + 46% P", language="en-IN")
        == "Urea, 50 kg bag: 1350 rupees. 18 percent N and 46 percent P"
    )


def test_marathi_speech_text_uses_the_marathi_words_for_the_symbols() -> None:
    assert text_for_speech("₹1350", language="mr-IN") == "1350 रुपये"
    assert text_for_speech("50% + 46%", language="mr-IN") == "50 टक्के आणि 46 टक्के"


def test_hindi_speech_text_is_unchanged_by_the_new_path() -> None:
    assert "रुपये" in text_for_speech("₹1350", language="hi-IN")
    assert "1350" not in text_for_speech("₹1350", language="hi-IN")


# --------------------------------------------------------------------------- #


def _intent(text: str) -> Any:
    from voice_worker.flow.intents import classify_by_rule

    return classify_by_rule(text).intent
