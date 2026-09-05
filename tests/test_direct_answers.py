"""Answers composed from data, without the model (§9 Tier 1).

A price, a stock check, the centre's hours: one lookup, one fixed sentence in
the farmer's register, no generation. These tests pin the resolution rules --
which product "स्टॉक है क्या?" is about -- and the wording.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import time
from typing import Any

import pytest

from uaagro_domain.enums import Intent, TransferReason
from voice_worker.flow.address import AddressBudget
from voice_worker.flow.agent import TRANSFER_PLACEHOLDER_HI, Agent
from voice_worker.flow.context import CallerContext, ContextBuilder
from voice_worker.flow.direct import (
    ASK_PRODUCT_PRICE_HI,
    HEARING_OK_HI,
    MACHINE_HI,
    OVERVIEW_HI,
    CentreFacts,
    DirectAnswers,
    spoken_centre_name,
)
from voice_worker.flow.focus import ConversationFocus, category_in, crop_in
from voice_worker.flow.validator import OutputValidator
from voice_worker.text.lexicon import Lexicon, LexiconEntry, Place
from voice_worker.text.register import farmers_register
from voice_worker.tools.base import ToolContext, ToolResult

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

ENTRIES = [
    LexiconEntry(
        sku="FERT-DAP-50",
        name_hi="डीएपी",
        name_en="DAP 18:46:0",
        variants=("dap", "डी ए पी", "डाई अमोनियम फ़ॉस्फ़ेट"),
        category="fertilisers",
        crops=("wheat", "potato"),
    ),
    LexiconEntry(
        sku="FERT-UREA-45",
        name_hi="यूरिया",
        name_en="Urea",
        variants=("urea",),
        category="fertilisers",
        crops=("wheat", "rice"),
    ),
    LexiconEntry(
        sku="SED-POT-KUFRI",
        name_hi="आलू कुफ़री बहार",
        name_en="Potato Kufri Bahar Seed",
        variants=("कुफ़री", "kufri"),
        category="seeds",
        crops=("potato",),
    ),
    LexiconEntry(
        sku="SED-WHT-HD",
        name_hi="गेहूँ एचडी 2967",
        name_en="Wheat HD 2967 Seed",
        variants=("एचडी 2967", "hd 2967"),
        category="seeds",
        crops=("wheat",),
    ),
    LexiconEntry(
        sku="SED-WHT-PBW",
        name_hi="गेहूँ पीबीडब्ल्यू 343",
        name_en="Wheat PBW 343 Seed",
        variants=("पीबीडब्ल्यू 343", "pbw 343"),
        category="seeds",
        crops=("wheat",),
    ),
    LexiconEntry(
        sku="CP-MONO-36",
        name_hi="मोनोक्रोटोफ़ॉस",
        name_en="Monocrotophos 36 SL",
        variants=("monocrotophos",),
        category="crop-protection",
        crops=("cotton",),
    ),
]
PLACES = (Place("Barabanki", "बाराबंकी"), Place("Lucknow", "लखनऊ"))

CENTRE = CentreFacts(
    id="c1",
    code="NKSK-LKO-01",
    name="लखनऊ सेंटर",
    address_spoken="अलीगंज में, सेक्टर बी की मेन रोड पर",
    phone="+919999000001",
    open_time=time(8, 0),
    close_time=time(19, 0),
    working_days=("mon", "tue", "wed", "thu", "fri", "sat"),
    own=False,
)


class FakeRegistry:
    """Answers the tools the direct layer calls, from a table."""

    def __init__(self, stock: dict[str, dict[str, Any]] | None = None) -> None:
        self.stock = stock or {}
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    def get(self, name: str) -> object | None:
        return None

    async def execute(self, name: str, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        self.calls.append((name, dict(args)))
        if name == "check_availability":
            data = self.stock.get(str(args["sku"]))
            if data is None:
                return ToolResult(
                    tool=name,
                    ok=True,
                    data={"sku": args["sku"], "stocked": False, "alternatives": []},
                )
            return ToolResult(tool=name, ok=True, data=dict(data))
        if name == "get_product_details":
            return ToolResult(
                tool=name,
                ok=True,
                data={
                    "sku": args["sku"],
                    "name_hi": "डीएपी",
                    "composition": [
                        {"ingredient": "N", "percent": "18"},
                        {"ingredient": "P2O5", "percent": "46"},
                    ],
                },
            )
        if name == "search_products":
            return ToolResult(tool=name, ok=True, data={"matched_by": "search", "products": []})
        if name == "find_nearest_centre":
            return ToolResult(
                tool=name,
                ok=True,
                data={
                    "centres": [
                        {
                            "code": "NKSK-BBK-01",
                            "name_hi": "बाराबंकी सेंटर",
                            "address_spoken_hi": "देवा रोड पर, बस अड्डे के पास",
                            "phone": None,
                            "open": "08:00",
                            "close": "19:00",
                            "services": [],
                        }
                    ]
                },
            )
        return ToolResult(tool=name, ok=False, error="no such tool")

    async def execute_many(
        self, planned: Sequence[tuple[str, Mapping[str, Any]]], context: ToolContext
    ) -> list[ToolResult]:
        return [await self.execute(name, args, context) for name, args in planned]


DAP_IN_STOCK = {
    "sku": "FERT-DAP-50",
    "name_hi": "डीएपी",
    "centre": "NKSK-LKO-01",
    "available": True,
    "pack": "50 kg",
    "price": "1350",
    "mrp": "1350",
}
POTATO_OUT = {
    "sku": "SED-POT-KUFRI",
    "name_hi": "आलू कुफ़री बहार",
    "centre": "NKSK-LKO-01",
    "available": False,
    "pack": "50 kg",
    "restock_eta": "2026-09-10",
    "alternatives": [],
    "centre_phone": "+919999000001",
}


def direct(
    stock: dict[str, dict[str, Any]] | None = None, centre: CentreFacts | None = CENTRE
) -> tuple[DirectAnswers, FakeRegistry]:
    registry = FakeRegistry(stock)
    layer = DirectAnswers(
        registry=registry,  # type: ignore[arg-type]
        context=ToolContext(call_id="t"),
        lexicon=Lexicon.from_entries(list(ENTRIES), places=PLACES),
        centre=centre,
    )
    return layer, registry


# --------------------------------------------------------------------------- #
# Resolving the product
# --------------------------------------------------------------------------- #


async def test_a_price_question_is_answered_from_the_stock_row() -> None:
    layer, registry = direct({"FERT-DAP-50": DAP_IN_STOCK})
    reply = await layer.answer("डीएपी का रेट क्या है?", Intent.PRICE_ENQUIRY, ConversationFocus())
    assert reply is not None
    assert (
        reply.text == "डीएपी, 50 किलो का बैग: 1350 रुपये। लखनऊ सेंटर पर स्टॉक में है। यह लखनऊ सेंटर का रेट है।"
    )
    assert registry.calls == [
        ("check_availability", {"sku": "FERT-DAP-50", "centre_code": "NKSK-LKO-01"})
    ]
    assert reply.results and reply.results[0].tool == "check_availability"


async def test_the_head_office_note_is_said_once_per_call() -> None:
    layer, _ = direct({"FERT-DAP-50": DAP_IN_STOCK})
    focus = ConversationFocus()
    first = await layer.answer("डीएपी का रेट?", Intent.PRICE_ENQUIRY, focus)
    second = await layer.answer("डीएपी मिल जाएगा?", Intent.PRODUCT_AVAILABILITY, focus)
    assert first is not None and "यह लखनऊ सेंटर का रेट है" in first.text
    assert second is not None and "यह लखनऊ सेंटर का रेट है" not in second.text
    assert second.text == "डीएपी स्टॉक में है, 50 किलो का बैग 1350 रुपये में।"


async def test_a_crop_and_a_kind_narrow_the_catalogue_to_one_product() -> None:
    layer, registry = direct({"SED-POT-KUFRI": POTATO_OUT})
    focus = ConversationFocus()
    focus.note_user_turn("आलू का बीज है क्या?")
    reply = await layer.answer("आलू का बीज है क्या?", Intent.PRODUCT_AVAILABILITY, focus)
    assert reply is not None
    assert reply.text == (
        "आलू कुफ़री बहार अभी लखनऊ सेंटर पर स्टॉक में नहीं है। 10 सितंबर तक आ जाएगा। "
        "आने से पहले सेंटर पर फ़ोन करके पक्का कर लीजिए।"
    )
    assert registry.calls[0][1]["sku"] == "SED-POT-KUFRI"
    assert focus.product is not None and focus.product.sku == "SED-POT-KUFRI"


async def test_the_product_from_the_previous_turn_answers_a_bare_stock_question() -> None:
    layer, registry = direct({"SED-POT-KUFRI": {**POTATO_OUT, "available": True, "price": "1200"}})
    focus = ConversationFocus()
    focus.note_user_turn("Potato Cut Seeds के बारे में पूछना था")
    await layer.answer("Potato Cut Seeds के बारे में पूछना था", Intent.PRODUCT_AVAILABILITY, focus)
    focus.note_user_turn("स्टॉक है क्या?")
    reply = await layer.answer("स्टॉक है क्या?", Intent.PRODUCT_AVAILABILITY, focus)
    assert reply is not None
    assert reply.text.startswith("आलू कुफ़री बहार स्टॉक में है, 50 किलो का बैग 1200 रुपये में।")
    assert [c[1]["sku"] for c in registry.calls] == ["SED-POT-KUFRI", "SED-POT-KUFRI"]


async def test_several_matches_become_a_choice_and_none_becomes_honesty() -> None:
    layer, _ = direct()
    focus = ConversationFocus()
    choice = await layer.answer("गेहूँ का बीज चाहिए", Intent.PRODUCT_AVAILABILITY, focus)
    assert choice is not None
    assert choice.text == "गेहूँ के लिए हमारे पास गेहूँ एचडी 2967 और गेहूँ पीबीडब्ल्यू 343 हैं। कौन सा चाहिए?"

    none = await layer.answer("टमाटर का बीज है?", Intent.PRODUCT_AVAILABILITY, ConversationFocus())
    assert none is not None
    assert none.text.startswith("टमाटर का बीज अभी हमारे पास नहीं है। बीज में ")


async def test_no_product_at_all_is_a_question_back() -> None:
    layer, registry = direct()
    reply = await layer.answer("रेट क्या है?", Intent.PRICE_ENQUIRY, ConversationFocus())
    assert reply is not None and reply.text == ASK_PRODUCT_PRICE_HI
    assert registry.calls == []


async def test_a_licensed_product_is_handed_to_a_person() -> None:
    layer, _ = direct(
        {
            "CP-MONO-36": {
                **DAP_IN_STOCK,
                "sku": "CP-MONO-36",
                "name_hi": "मोनोक्रोटोफ़ॉस",
                "restricted": True,
            }
        }
    )
    reply = await layer.answer(
        "मोनोक्रोटोफ़ॉस चाहिए", Intent.PRODUCT_AVAILABILITY, ConversationFocus()
    )
    assert reply is not None
    assert reply.offered_transfer
    assert "लाइसेंस" in reply.text and reply.text.endswith("जोड़ दूँ?")


async def test_composition_is_read_from_the_details_tool() -> None:
    layer, _ = direct()
    reply = await layer.answer(
        "डीएपी में क्या क्या है?", Intent.PRODUCT_COMPOSITION, ConversationFocus()
    )
    assert reply is not None
    assert reply.text == "डीएपी में नाइट्रोजन 18 परसेंट और फ़ॉस्फ़ोरस 46 परसेंट है।"


# --------------------------------------------------------------------------- #
# The centre, and small talk
# --------------------------------------------------------------------------- #


async def test_the_centre_is_described_with_hours_and_a_spoken_phone_number() -> None:
    layer, _ = direct()
    reply = await layer.answer("सेंटर कहाँ है?", Intent.CENTRE_LOCATION, ConversationFocus())
    assert reply is not None
    assert reply.text.startswith("लखनऊ सेंटर अलीगंज में, सेक्टर बी की मेन रोड पर है। ")
    assert "सुबह 8 बजे से शाम 7 बजे तक खुला रहता है, सोमवार से शनिवार।" in reply.text
    assert "फ़ोन नंबर" in reply.text and "9999000001" not in reply.text


async def test_a_named_district_looks_up_that_centre() -> None:
    layer, registry = direct()
    reply = await layer.answer(
        "बाराबंकी वाला सेंटर कहाँ है?", Intent.CENTRE_LOCATION, ConversationFocus()
    )
    assert reply is not None
    assert reply.text.startswith("बाराबंकी सेंटर देवा रोड पर, बस अड्डे के पास है।")
    assert registry.calls == [("find_nearest_centre", {"district": "Barabanki"})]


async def test_what_do_you_have_lists_the_kinds_or_the_products_of_one_kind() -> None:
    layer, _ = direct()
    overview = await layer.answer("आपके पास क्या-क्या मिलता है?", Intent.UNKNOWN, ConversationFocus())
    assert overview is not None and overview.text == OVERVIEW_HI
    seeds = await layer.answer("कौन कौन से बीज हैं?", Intent.UNKNOWN, ConversationFocus())
    assert seeds is not None
    assert (
        seeds.text
        == "बीज में हमारे पास आलू कुफ़री बहार, गेहूँ एचडी 2967 और गेहूँ पीबीडब्ल्यू 343 हैं। कौन सा चाहिए?"
    )


async def test_small_talk_is_answered_without_a_model() -> None:
    layer, _ = direct()
    hearing = await layer.answer("मेरी आवाज़ आ रही है?", Intent.UNKNOWN, ConversationFocus())
    assert hearing is not None and hearing.text == HEARING_OK_HI
    machine = await layer.answer("आप मशीन हो क्या?", Intent.UNKNOWN, ConversationFocus())
    assert machine is not None and machine.text == MACHINE_HI and machine.offered_transfer
    other = await layer.answer("कल बारिश होगी क्या", Intent.UNKNOWN, ConversationFocus())
    assert other is None


async def test_without_a_lexicon_the_model_answers() -> None:
    layer = DirectAnswers(registry=FakeRegistry(), context=ToolContext(call_id="t"))  # type: ignore[arg-type]
    assert await layer.answer("डीएपी का रेट?", Intent.PRICE_ENQUIRY, ConversationFocus()) is None


# --------------------------------------------------------------------------- #
# Focus and register
# --------------------------------------------------------------------------- #


def test_kinds_and_crops_are_read_in_hindi_english_and_hinglish() -> None:
    assert category_in("potato ka seed chahiye") == "seeds"
    assert category_in("कोई अच्छी दवाई बताइए") == "crop-protection"
    assert crop_in("pyaz me kya dalein") == "onion"
    assert crop_in("गेहूं की बुवाई") == "wheat"
    assert crop_in("आम की बात है") == "mango"
    assert crop_in("आम तौर पर") == "mango"  # a known limit: a lone "आम" is a crop


def test_a_new_kind_or_crop_forgets_the_old_product() -> None:
    focus = ConversationFocus()
    focus.note_product("FERT-DAP-50", "डीएपी")
    focus.note_user_turn("इसका रेट?")
    assert focus.product is not None
    focus.note_user_turn("और गेहूँ का बीज?")
    assert focus.product is None and focus.crop == "wheat" and focus.category == "seeds"


def test_the_register_swaps_textbook_words_and_drops_stage_directions() -> None:
    assert farmers_register("उर्वरक की मात्रा उपलब्ध है।") == "खाद की डोज़ स्टॉक में है।"
    assert farmers_register("*(मैनेजर से कनेक्ट करने का प्रयास)* माफ़ कीजिए, अभी उपलब्ध नहीं हैं।") == (
        "माफ़ कीजिए, अभी स्टॉक में नहीं हैं।"
    )
    assert farmers_register("केंद्र प्रबंधक से संपर्क कीजिए।") == "सेंटर मैनेजर से फ़ोन कीजिए।"
    assert farmers_register("डीएपी (50 किलो) 1350 रुपये।") == "डीएपी (50 किलो) 1350 रुपये।"
    assert farmers_register("उपलब्धता देख लेता हूँ") == "स्टॉक देख लेता हूँ"


def test_keyterms_include_the_districts() -> None:
    lexicon = Lexicon.from_entries(list(ENTRIES), places=PLACES)
    assert "बाराबंकी" in lexicon.keyterms() and "Lucknow" in lexicon.keyterms()


# --------------------------------------------------------------------------- #
# Through the agent
# --------------------------------------------------------------------------- #


class CountingGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, **kwargs: Any) -> AsyncIterator[tuple[str, str]]:
        self.calls += 1
        yield "जी, कुछ भी।", "primary"


def make_agent(registry: FakeRegistry, layer: DirectAnswers) -> tuple[Agent, CountingGateway]:
    gateway = CountingGateway()
    agent = Agent(
        registry=registry,  # type: ignore[arg-type]
        context_builder=ContextBuilder(persona="आप सहायक हैं।"),
        gateway=gateway,
        validator=OutputValidator(),
        caller=CallerContext(name=None),
        address=AddressBudget(),
        direct=layer,
    )
    return agent, gateway


async def test_the_agent_answers_a_price_without_the_model_and_remembers_it() -> None:
    layer, registry = direct({"FERT-DAP-50": DAP_IN_STOCK})
    agent, gateway = make_agent(registry, layer)
    pieces = [p async for p in agent.respond("डीएपी का रेट क्या है?")]
    assert pieces == [
        "डीएपी, 50 किलो का बैग: 1350 रुपये।",
        "लखनऊ सेंटर पर स्टॉक में है।",
        "यह लखनऊ सेंटर का रेट है।",
    ]
    assert gateway.calls == 0
    assert agent.last_turn is not None and agent.last_turn.direct
    assert agent.last_turn.intent is Intent.PRICE_ENQUIRY
    assert agent.memory.turns[-1].text == " ".join(pieces)
    assert agent.last_turn.as_dict()["direct"] is True


async def test_yes_after_the_agent_offered_a_person_is_a_transfer() -> None:
    layer, registry = direct()
    agent, gateway = make_agent(registry, layer)
    first = [p async for p in agent.respond("आप मशीन हो?")]
    assert first == [MACHINE_HI.split("। ")[0] + "।", "चाहें तो किसी व्यक्ति से जोड़ दूँ?"]
    second = [p async for p in agent.respond("हाँ जी")]
    assert second == [TRANSFER_PLACEHOLDER_HI]
    assert agent.last_turn is not None and agent.last_turn.intent is Intent.TALK_TO_HUMAN
    assert gateway.calls == 0


async def test_a_question_the_direct_layer_declines_goes_to_the_model() -> None:
    layer, registry = direct()
    agent, gateway = make_agent(registry, layer)
    pieces = [p async for p in agent.respond("कल बारिश होगी क्या")]
    assert gateway.calls == 1
    assert pieces == ["कुछ भी।"]


@pytest.mark.parametrize(
    "line, expected",
    [
        ("डीएपी का रेट?", "डीएपी"),
        ("यूरिया मिलेगा?", "यूरिया"),
    ],
)
async def test_named_products_resolve_by_their_spoken_forms(line: str, expected: str) -> None:
    layer, _ = direct(
        {
            "FERT-DAP-50": DAP_IN_STOCK,
            "FERT-UREA-45": {
                **DAP_IN_STOCK,
                "sku": "FERT-UREA-45",
                "name_hi": "यूरिया",
                "pack": "45 kg",
                "price": "266",
            },
        }
    )
    reply = await layer.answer(line, Intent.PRICE_ENQUIRY, ConversationFocus())
    assert reply is not None and reply.text.startswith(expected)


def test_the_centre_is_named_by_its_place_and_prices_are_whole_rupees() -> None:
    from voice_worker.flow.phrasebook import money as _money

    assert spoken_centre_name("नवीन खुशहाली किसान सेवा केंद्र - लखनऊ") == "लखनऊ सेंटर"
    assert spoken_centre_name("बाराबंकी सेंटर") == "बाराबंकी सेंटर"
    assert _money("1307.44") == "1307"
    assert _money("1307.50") == "1308"
    assert _money(1350) == "1350"


async def test_how_many_centres_is_left_to_the_knowledge_base() -> None:
    layer, registry = direct()
    assert (
        await layer.answer("आपके कितने सेंटर हैं?", Intent.CENTRE_LOCATION, ConversationFocus()) is None
    )
    assert (
        await layer.answer("कौन कौन से ज़िले में सेंटर है?", Intent.CENTRE_LOCATION, ConversationFocus())
        is None
    )
    assert registry.calls == []


async def test_advice_without_an_approved_recommendation_is_refused_plainly() -> None:
    from voice_worker.flow.agent import NO_ADVICE_HI

    layer, registry = direct()
    agent, gateway = make_agent(registry, layer)
    pieces = [p async for p in agent.respond("गन्ने में लाल सड़न के लिए कौन सी दवा डालूँ?")]
    assert " ".join(pieces) == NO_ADVICE_HI
    assert gateway.calls == 0, "the model was asked for advice with nothing approved"
    assert ("recommend_for_crop", {"crop": "sugarcane"}) in registry.calls
    turn = agent.last_turn
    assert turn is not None and turn.escalation is not None and turn.escalation.escalate
    assert turn.escalation.reason is TransferReason.MISSING_DATA
    assert turn.ends_agent_turns


def test_the_validator_refuses_application_advice_without_a_recommendation() -> None:
    validator = OutputValidator()
    refused = validator.validate("गन्ने में ट्राइकोडर्मा वाली दवा डालें।", tool_results=())
    assert not refused.ok and "advice" in refused.rules
    grounded = validator.validate(
        "गन्ने में ट्राइकोडर्मा वाली दवा डालें।",
        tool_results=[
            {"tool": "recommend_for_crop", "ok": True, "data": {"recommendations": [{"x": 1}]}}
        ],
    )
    assert grounded.ok
    described = validator.validate("लाल सड़न में गन्ने का गूदा लाल और सड़ा दिखता है।", tool_results=())
    assert described.ok


async def test_advice_with_no_crop_named_asks_for_it() -> None:
    from voice_worker.flow.agent import ASK_CROP_HI

    layer, registry = direct({"FERT-DAP-50": DAP_IN_STOCK})
    agent, gateway = make_agent(registry, layer)
    pieces = [p async for p in agent.respond("दवा को पानी में मिलाकर छिड़काव करें, कितनी मात्रा")]
    assert " ".join(pieces) == ASK_CROP_HI
    assert gateway.calls == 0
    assert agent.last_turn is not None and agent.last_turn.escalation is None


@pytest.mark.parametrize(
    "line, expected",
    [
        ("गन्ने में लाल सड़न रोक की कौन-सी दवा डालूँ?", Intent.CROP_RECOMMENDATION),
        ("गेहूँ में कितना यूरिया डालूँ", Intent.DOSAGE_QUERY),
        ("धान में झुलसा लग गया", Intent.PROBLEM_DIAGNOSIS),
    ],
)
def test_advice_questions_are_classified_the_way_farmers_ask_them(
    line: str, expected: Intent
) -> None:
    from voice_worker.flow.intents import classify_by_rule

    assert classify_by_rule(line).intent is expected
