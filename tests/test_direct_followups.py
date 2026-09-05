"""The turns that looped on the first live calls, and must not again (§11.4).

Each test here is a conversation lifted from ``call_turns`` on 4 September
2026, replayed against the direct-answer layer. What they pin down:

* a crop is matched under the catalogue's own spelling (``paddy``, not
  ``rice``), so "राइस का सीड्स" narrows to the paddy seeds;
* a farmer's own inflection of a listed name ("मसूरी" for मसूर) resolves;
* postpositions between words do not break a category ("पशु की आहार");
* an offered choice is answered by ordinal, by name or by "सबका";
* a question back counts as unresolved, the second miss changes tack, and
  the third hands the call to a person -- never the same line a third time.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import time
from typing import Any

import pytest

from uaagro_domain.enums import Intent
from voice_worker.flow.direct import (
    ASK_PRODUCT_STOCK_HI,
    CentreFacts,
    DirectAnswers,
)
from voice_worker.flow.focus import ConversationFocus, category_in, crop_in, same_crop
from voice_worker.flow.intents import classify_by_rule
from voice_worker.text.lexicon import Lexicon, LexiconEntry
from voice_worker.tools.base import ToolContext, ToolResult

# The seeds and feeds the live catalogue carries, with its crop spellings.
ENTRIES = [
    LexiconEntry(
        "SED-ARH-NDA1", "अरहर एनडीए 1", "Arhar NDA-1", ("arhar nda 1",), "seeds", ("pigeon_pea",)
    ),
    LexiconEntry("SED-GRM-JG14", "चना जेजी 14", "Gram JG-14", ("jg 14",), "seeds", ("gram",)),
    LexiconEntry(
        "SED-LEN-K75", "मसूर के 75", "Lentil K-75", ("k 75", "masoor"), "seeds", ("lentil",)
    ),
    LexiconEntry(
        "SED-PDY-PUSA1509",
        "धान पूसा 1509",
        "Paddy Pusa Basmati 1509",
        ("पूसा 1509",),
        "seeds",
        ("paddy",),
    ),
    LexiconEntry("SED-PDY-SARJU52", "धान सरजू 52", "Paddy Sarju-52", ("सरजू",), "seeds", ("paddy",)),
    LexiconEntry(
        "SED-WHT-HD3086", "गेहूँ एचडी 3086", "Wheat HD 3086", ("hd 3086",), "seeds", ("wheat",)
    ),
    LexiconEntry("FEED-CALC", "कैल्शियम", "Calcium supplement", (), "cattle-feed", ()),
    LexiconEntry("FEED-PASHU", "पशु आहार", "Cattle feed 50 kg", (), "cattle-feed", ()),
    LexiconEntry("FERT-UREA-45", "यूरिया", "Urea", ("urea",), "fertilisers", ()),
    LexiconEntry(
        "CP-BIS-100", "बिस्पायरिबैक", "Bispyribac", ("bispyribac",), "crop-protection", ("paddy",)
    ),
    LexiconEntry(
        "CP-CAB-500",
        "कार्बेन्डाज़िम",
        "Carbendazim",
        ("carbendazim",),
        "crop-protection",
        ("wheat", "gram"),
    ),
]

CENTRE = CentreFacts(
    id="c1",
    code="NKSK-LKO-01",
    name="लखनऊ सेंटर",
    address_spoken=None,
    phone=None,
    open_time=time(8, 0),
    close_time=time(19, 0),
    working_days=("mon",),
    own=True,
)


class StockRegistry:
    """Every product is in stock at a price that names it, for readable asserts."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, name: str) -> object | None:
        return None

    async def execute(self, name: str, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        sku = str(args["sku"])
        self.calls.append(sku)
        price = {"SED-PDY-PUSA1509": "1500", "SED-PDY-SARJU52": "900"}.get(sku, "1150")
        return ToolResult(
            tool=name,
            ok=True,
            data={"sku": sku, "available": True, "price": price, "pack": "", "alternatives": []},
            latency_ms=1.0,
        )


class Conversation:
    """Drives the layer the way the agent does: focus, rule intent, answer."""

    def __init__(self) -> None:
        self.registry = StockRegistry()
        self.layer = DirectAnswers(
            registry=self.registry,  # type: ignore[arg-type]
            context=ToolContext(call_id="t"),
            lexicon=Lexicon.from_entries(list(ENTRIES)),
            centre=CENTRE,
        )
        self.focus = ConversationFocus()

    async def say(self, text: str, *, language: str = "hi-IN") -> Any:
        self.focus.note_user_turn(text)
        intent = classify_by_rule(text).intent
        return await self.layer.answer(text, intent, self.focus, language=language)


# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "said, crop",
    [
        ("मेरे को राइस का सीड्स चाहिए", "paddy"),
        ("I want rice seeds", "paddy"),
        ("चावल के बीज हैं क्या", "paddy"),
        ("मसूरी का चाहिए", "lentil"),
        ("मूँग का बीज", "moong"),
        ("अरहर है क्या", "pigeon_pea"),
    ],
)
def test_crops_are_read_under_the_catalogue_spelling(said: str, crop: str) -> None:
    assert crop_in(said) == crop


def test_a_catalogue_that_spells_a_crop_differently_still_meets_the_vocabulary() -> None:
    assert same_crop("rice", "paddy")
    assert same_crop("pigeon pea", "pigeon_pea")
    assert same_crop("mung", "moong")
    assert not same_crop("wheat", "paddy")


@pytest.mark.parametrize(
    "said, category",
    [
        ("पशु की आहार चाहिए", "cattle-feed"),
        ("गाय का खाना मिलेगा", "cattle-feed"),
        ("राइस का सीड्स", "seeds"),
        ("गेहूँ वाला बीज", "seeds"),
        ("पेस्टिसाइड चाहिए", "crop-protection"),
    ],
)
def test_a_kind_is_read_across_particles_and_in_transliterated_english(
    said: str, category: str
) -> None:
    assert category_in(said) == category


@pytest.mark.parametrize(
    "said",
    ["I want rice seeds", "rice seed chahiye", "उसका प्राइसेस बता दीजिए", "ले लूँगा दो बैग"],
)
def test_english_and_transliterated_requests_reach_the_stock_path(said: str) -> None:
    assert classify_by_rule(said).intent in (Intent.PRODUCT_AVAILABILITY, Intent.PRICE_ENQUIRY)


# --------------------------------------------------------------------------- #
# The calls that looped
# --------------------------------------------------------------------------- #


async def test_rice_seeds_then_the_lentil_by_its_spoken_inflection() -> None:
    talk = Conversation()
    first = await talk.say("मेरे को राइस का सीड्स चाहिए, और।")
    assert first is not None and not first.resolved
    assert first.text == "धान के लिए हमारे पास धान पूसा 1509 और धान सरजू 52 हैं। कौन सा चाहिए?"

    second = await talk.say("मसूरी का चाहिए।")
    assert second is not None and second.resolved
    assert second.text.startswith("मसूर के 75 स्टॉक में है")
    assert talk.registry.calls == ["SED-LEN-K75"]
    assert talk.focus.misses == 0


async def test_cattle_feed_across_a_particle_then_noise_ends_with_a_person() -> None:
    talk = Conversation()
    first = await talk.say("पशु की आहार चाहिए।")
    assert first is not None and first.text.startswith("पशु आहार में हमारे पास कैल्शियम और पशु आहार हैं")

    # "हेन" is a hen: the feed is for cattle, and the farmer is told so and
    # offered a person -- not asked "पहला या दूसरा" about feed for cows.
    second = await talk.say("हेन का चाहिए, हेन।")
    assert second is not None and second.offered_transfer
    assert second.text == ("पशु आहार गाय और भैंस के लिए है, मुर्गी के लिए नहीं। मुर्गी के लिए सेंटर मैनेजर से पूछ लूँ?")

    third = await talk.say("हेन का चाहिए, हेन।")
    assert third is not None and third.offered_transfer
    assert third.text == "जी, दोबारा बता देता हूँ। " + second.text


async def test_the_first_one_and_then_its_price() -> None:
    talk = Conversation()
    await talk.say("धान का बीज चाहिए")
    picked = await talk.say("फर्स्ट वाला ले लेता हूँ मैं।")
    assert picked is not None and picked.resolved
    assert picked.text.startswith("धान पूसा 1509 स्टॉक में है")

    price = await talk.say("10 बैग का प्राइस बता दीजिए।")
    assert price is not None and price.text.startswith("धान पूसा 1509: 1500 रुपये।")


async def test_a_price_question_after_a_list_reads_every_price_and_keeps_the_choice() -> None:
    talk = Conversation()
    await talk.say("धान का बीज चाहिए")
    prices = await talk.say("उसका प्राइसेस बता दीजिए।")
    assert prices is not None and prices.resolved
    assert prices.text == "धान पूसा 1509 1500 रुपये और धान सरजू 52 900 रुपये। कौन सा चाहिए?"
    assert talk.focus.choices, "the choice stays open after the prices"

    second = await talk.say("दूसरा वाला")
    assert second is not None and second.text.startswith("धान सरजू 52 स्टॉक में है")


async def test_a_crop_that_narrows_a_choice_is_progress_not_a_miss() -> None:
    talk = Conversation()
    listed = await talk.say("बीज चाहिए")
    assert listed is not None and not listed.resolved and talk.focus.misses == 1

    narrowed = await talk.say("धान")
    assert narrowed is not None and not narrowed.resolved
    assert narrowed.text == "धान के लिए हमारे पास धान पूसा 1509 और धान सरजू 52 हैं। कौन सा चाहिए?"
    assert talk.focus.misses == 1, "naming a crop is the conversation moving"

    everything = await talk.say("सबका रेट बता दो")
    assert everything is not None and everything.resolved
    assert not everything.handover


async def test_a_product_named_elsewhere_beats_a_pending_choice() -> None:
    talk = Conversation()
    await talk.say("I want rice seeds")
    urea = await talk.say("what is the price of urea")
    assert urea is not None and urea.text.startswith("यूरिया: 1150 रुपये।")
    assert talk.registry.calls == ["FERT-UREA-45"]


async def test_the_english_ordinal_picks_from_the_list() -> None:
    talk = Conversation()
    await talk.say("I want rice seeds")
    second = await talk.say("the second one")
    assert second is not None and second.text.startswith("धान सरजू 52 स्टॉक में है")


async def test_small_talk_is_not_mistaken_for_a_tool_request() -> None:
    talk = Conversation()
    machine = await talk.say("आप मशीन हो क्या?")
    assert machine is not None and machine.offered_transfer and "ऑटोमैटिक" in machine.text


async def test_nothing_named_three_times_is_asked_twice_then_handed_over() -> None:
    talk = Conversation()
    one = await talk.say("वो चाहिए")
    assert one is not None and one.text == ASK_PRODUCT_STOCK_HI
    two = await talk.say("वही चाहिए")
    assert two is not None and two.text != ASK_PRODUCT_STOCK_HI and two.offered_transfer
    three = await talk.say("अरे वही")
    assert three is not None and three.handover


# --------------------------------------------------------------------------- #
# "What have you got for cows?" is a question, and the list is its answer
# --------------------------------------------------------------------------- #


async def test_what_all_for_cows_is_answered_with_the_list_not_counted_as_a_miss() -> None:
    """The call of 4 September, 09:57: cow feed, then "what all can you give
    for cows?" twice. The list was read once, the repeat was booked as a miss,
    the agent changed tack, and the third turn went to a person."""
    talk = Conversation()
    first = await talk.say("मेरे को कौ का फीड चाहिए, और।")
    assert first is not None
    assert first.text == "गाय के लिए हमारे पास कैल्शियम और पशु आहार हैं। कौन सा चाहिए?"

    again = await talk.say("कौस के लिए क्या-क्या दे सकते हैं?")
    assert again is not None and again.resolved and not again.handover
    assert again.text == "जी, दोबारा बता देता हूँ। " + first.text, "the list, owned as a repeat"
    assert talk.focus.misses == 0, "asking what there is, is not failing to choose"
    assert talk.focus.choices, "the list stays the offered choice"

    once_more = await talk.say("नहीं, नहीं, कौस के लिए क्या दे सकते हैं?")
    assert once_more is not None and once_more.resolved and not once_more.handover
    assert once_more.text == "जी, दोबारा बता देता हूँ। " + first.text

    picked = await talk.say("पहला वाला")
    assert picked is not None and picked.text.startswith("कैल्शियम स्टॉक में है")


@pytest.mark.parametrize(
    "said",
    [
        "गाय के लिए क्या है आपके पास?",
        "गायों के लिए क्या मिलेगा",
        "भैंस का दाना चाहिए",
        "cow feed kya milega",
        "what do you have for cows",
        "what cattle feed is available",
    ],
)
def test_the_animal_names_the_kind(said: str) -> None:
    assert category_in(said) == "cattle-feed"


async def test_an_english_caller_hears_the_cattle_feed_list_in_english() -> None:
    talk = Conversation()
    reply = await talk.say("What cow feed is available?", language="en-IN")
    assert reply is not None and reply.resolved
    assert reply.text == (
        "For cows we have Calcium supplement and Cattle feed 50 kg. Which one would you like?"
    )


async def test_the_kind_in_the_words_beats_the_kind_in_focus() -> None:
    talk = Conversation()
    await talk.say("धान का बीज चाहिए")
    urea = await talk.say("खाद में क्या-क्या है?")
    assert urea is not None and urea.resolved
    # One product in the kind: the list of one is its stock, looked up.
    assert urea.text.startswith("यूरिया स्टॉक में है")
    assert talk.registry.calls == ["FERT-UREA-45"]


async def test_a_composition_shaped_question_with_no_product_is_the_list() -> None:
    talk = Conversation()
    reply = await talk.say("पशु आहार में क्या क्या है?")
    assert classify_by_rule("पशु आहार में क्या क्या है?").intent is Intent.PRODUCT_COMPOSITION
    assert reply is not None and reply.resolved
    assert reply.text.startswith("पशु आहार में हमारे पास")


async def test_a_second_miss_on_a_long_list_names_every_offer() -> None:
    talk = Conversation()
    await talk.say("बीज चाहिए")
    second = await talk.say("वो वाला चाहिए")
    assert second is not None and not second.resolved
    for name in ("अरहर एनडीए 1", "चना जेजी 14", "मसूर के 75", "धान पूसा 1509"):
        assert name in second.text
    assert "तीसरा" in second.text


async def test_what_else_reads_the_next_ones_not_the_same_ones() -> None:
    talk = Conversation()
    first = await talk.say("बीज चाहिए")
    assert first is not None and first.text.startswith("बीज में हमारे पास अरहर एनडीए 1")
    assert "और भी हैं" in first.text

    rest = await talk.say("और क्या-क्या है?")
    assert rest is not None and rest.resolved
    assert rest.text == "बीज में धान सरजू 52 और गेहूँ एचडी 3086 भी हैं। बस इतने ही हैं। कौन सा चाहिए?"

    nothing = await talk.say("और क्या है")
    assert nothing is not None and nothing.text == "बीज में बस यही हैं। इनमें से कौन सा चाहिए?"

    picked = await talk.say("पहला")
    assert picked is not None and picked.text.startswith("धान सरजू 52 स्टॉक में है")


# --------------------------------------------------------------------------- #
# "Is this for cows?" -- the animal is answered, in the farmer's words
# --------------------------------------------------------------------------- #


async def test_the_10_39_call_cow_feed_then_is_it_for_cows_is_answered_not_listed_again() -> None:
    """The call of 4 September, 10:39: "कौ का फीड है क्या", "कौ का फीड चाहिए",
    "वो कौ का फीड ही है न?", "is it cow feed only?", "cattle feed can be used
    for cow?", "Cow and buffalo?" -- six turns, one list six times, and "can"
    matched the fertiliser CAN."""
    talk = Conversation()
    first = await talk.say("कौका फीड है क्या आपके पास?")
    assert first is not None
    assert first.text == "गाय के लिए हमारे पास कैल्शियम और पशु आहार हैं। कौन सा चाहिए?"

    again = await talk.say("कौका फीड चाहिए।")
    assert again is not None
    assert again.text.startswith("जी, दोबारा बता देता हूँ। गाय के लिए हमारे पास")

    confirm = await talk.say("वो कौका फीड ही है न?")
    assert confirm is not None and confirm.resolved
    assert confirm.text == "हाँ, ये सब गाय के लिए ही हैं — कैल्शियम और पशु आहार। कौन सा देखूँ?"

    english = await talk.say("No, I am asking, is it cow feed only?", language="en-IN")
    assert english is not None and english.resolved
    assert english.text == (
        "Yes, all of these are for cows: Calcium supplement and Cattle feed 50 kg. "
        "Which one shall I check?"
    )

    used = await talk.say("that cattle feed can be used for cow?", language="en-IN")
    assert used is not None and used.resolved
    assert "Calcium Ammonium" not in used.text and "CAN" not in used.text
    assert used.text == "Yes, it is for cows. Which one shall I check?"

    both = await talk.say("Cow and buffalo?", language="en-IN")
    assert both is not None and both.resolved
    assert both.text.startswith("Yes, all of these are for cows and buffaloes")
    assert talk.focus.animals == ("cow", "buffalo")

    picked = await talk.say("दूसरा वाला")
    assert picked is not None and picked.text.startswith("पशु आहार स्टॉक में है")


async def test_a_product_is_confirmed_for_the_animal_named() -> None:
    talk = Conversation()
    await talk.say("पशु आहार चाहिए")
    reply = await talk.say("कैल्शियम गाय और भैंस को दे सकते हैं?")
    assert reply is not None and reply.resolved
    assert reply.text == "हाँ, कैल्शियम गाय और भैंस के लिए ही है। रेट और स्टॉक बताऊँ?"


async def test_a_goat_is_told_the_feed_is_for_cattle_and_offered_a_person() -> None:
    talk = Conversation()
    reply = await talk.say("बकरी का दाना है क्या?")
    assert reply is not None and reply.offered_transfer
    assert reply.text == "दाना गाय और भैंस के लिए है, बकरी के लिए नहीं। बकरी के लिए सेंटर मैनेजर से पूछ लूँ?"


async def test_fertiliser_is_not_feed() -> None:
    talk = Conversation()
    reply = await talk.say("यूरिया गाय को खिला सकते हैं क्या?")
    assert reply is not None
    assert reply.text == "यूरिया खाद है, पशुओं को खिलाने की चीज़ नहीं। गाय के लिए पशु आहार बताऊँ?"


async def test_the_farmers_word_for_the_kind_is_said_back() -> None:
    talk = Conversation()
    reply = await talk.say("फीड में क्या-क्या है?")
    assert reply is not None
    assert reply.text.startswith("फीड में हमारे पास")


async def test_a_second_identical_answer_owns_the_repeat() -> None:
    talk = Conversation()
    one = await talk.say("यूरिया का रेट")
    two = await talk.say("यूरिया का रेट")
    assert one is not None and two is not None
    assert two.text == "जी, दोबारा बता देता हूँ। " + one.text


async def test_yes_after_an_offer_does_what_was_offered() -> None:
    talk = Conversation()
    await talk.say("यूरिया गाय को खिला सकते हैं क्या?")
    listed = await talk.say("हाँ बताओ")
    assert listed is not None
    assert listed.text == "गाय के लिए हमारे पास कैल्शियम और पशु आहार हैं। कौन सा चाहिए?"

    confirmed = await talk.say("कैल्शियम गाय को दे सकते हैं?")
    assert confirmed is not None and confirmed.text.startswith("हाँ, कैल्शियम गाय के लिए ही है")
    price = await talk.say("हाँ")
    assert price is not None and price.text.startswith("कैल्शियम: 1150 रुपये")
    assert talk.registry.calls == ["FEED-CALC"]


async def test_a_product_in_hand_is_confirmed_for_a_second_animal() -> None:
    talk = Conversation()
    await talk.say("What cow feed is available?", language="en-IN")
    await talk.say("the second one", language="en-IN")
    reply = await talk.say("is it ok for buffalo too?", language="en-IN")
    assert reply is not None
    assert (
        reply.text
        == "Yes, Cattle feed 50 kg is for buffaloes. Shall I tell you the price and stock?"
    )


# --------------------------------------------------------------------------- #
# Crops: spoken back, answered from the crop tags, listed by kind
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "said, crop",
    [
        ("बाजरा के लिए क्या है", "pearl_millet"),
        ("मेंथा में क्या डालें", "mint"),
        ("sabzi ke liye dawa", "vegetables"),
        ("टमाटर की दवा", "tomato"),
    ],
)
def test_the_wider_crop_vocabulary(said: str, crop: str) -> None:
    assert crop_in(said) == crop


async def test_a_crop_on_its_own_is_listed_by_kind() -> None:
    talk = Conversation()
    reply = await talk.say("धान के लिए क्या-क्या है?")
    assert reply is not None
    assert reply.text == (
        "धान के लिए हमारे पास बीज में धान पूसा 1509 और धान सरजू 52 और दवा में बिस्पायरिबैक हैं। "
        "बीज या दवा — क्या देखूँ?"
    )
    picked = await talk.say("तीसरा")
    assert picked is not None and picked.text.startswith("बिस्पायरिबैक स्टॉक में है")


async def test_a_kind_without_crop_tags_is_not_narrowed_by_the_crop() -> None:
    talk = Conversation()
    reply = await talk.say("गेहूँ के लिए खाद है क्या?")
    assert reply is not None
    assert reply.text.startswith("यूरिया स्टॉक में है"), "urea is the only fertiliser here"
    assert "नहीं है" not in reply.text


async def test_is_this_for_wheat_is_answered_from_the_crop_tags() -> None:
    talk = Conversation()
    yes = await talk.say("कार्बेन्डाज़िम गेहूँ में डाल सकते हैं?")
    assert yes is not None and yes.resolved
    assert yes.text == "हाँ, कार्बेन्डाज़िम गेहूँ के लिए है। यह गेहूँ और चना के लिए बना है। रेट और स्टॉक बताऊँ?"
    price = await talk.say("हाँ")
    assert price is not None and price.text.startswith("कार्बेन्डाज़िम: 1150 रुपये")

    no = await talk.say("और धान में?")
    assert no is not None
    assert no.text == (
        "कार्बेन्डाज़िम गेहूँ और चना के लिए है, धान के लिए नहीं। धान के लिए दवा में बिस्पायरिबैक हैं। कौन सा देखूँ?"
    )


async def test_english_is_it_for_paddy() -> None:
    talk = Conversation()
    await talk.say("What do you have for paddy?", language="en-IN")
    reply = await talk.say("can I spray bispyribac on wheat?", language="en-IN")
    assert reply is not None
    assert reply.text == (
        "Bispyribac is for paddy, not for wheat. "
        "For wheat, in crop protection, we have Carbendazim. Which one shall I check?"
    )


async def test_untagged_products_are_left_to_the_model_for_a_crop_question() -> None:
    talk = Conversation()
    reply = await talk.say("यूरिया गेहूँ में डाल सकते हैं?")
    assert reply is None, "the catalogue does not say; the model has the knowledge base"


async def test_a_crop_with_nothing_is_said_so() -> None:
    talk = Conversation()
    reply = await talk.say("बाजरा के लिए क्या है?")
    assert reply is not None
    assert reply.text.startswith("बाजरा के लिए अभी कुछ नहीं है")


async def test_a_crop_alone_after_an_untagged_kind_is_a_new_question() -> None:
    talk = Conversation()
    await talk.say("खाद में क्या-क्या है?")
    reply = await talk.say("बाजरा के लिए क्या है")
    assert reply is not None
    assert reply.text.startswith("बाजरा के लिए अभी कुछ नहीं है"), reply.text


def test_a_unique_head_word_of_a_long_english_name_resolves_it() -> None:
    lexicon = Lexicon.from_entries(
        [
            LexiconEntry("CP-BIS", "बिस्पायरिबैक", "Bispyribac Sodium 10% SC", (), "crop-protection"),
            LexiconEntry("FEED-CAL", "कैल्शियम", "Calcium Supplement Liquid", (), "cattle-feed"),
            LexiconEntry("FRT-CAN", "सीएएन", "Calcium Ammonium Nitrate", (), "fertilisers"),
            LexiconEntry("SED-PDY", "धान पूसा", "Paddy Pusa Basmati 1509", (), "seeds"),
        ]
    )
    found = lexicon.match("bispyribac")
    assert found is not None and found.sku == "CP-BIS"
    assert lexicon.match("calcium") is None, "two products start with it"
    assert lexicon.match("paddy") is None, "too short to be a name -- and a crop"


async def test_a_crop_word_that_is_also_a_spoken_form_is_still_the_crop() -> None:
    talk = Conversation()
    talk.layer.lexicon = Lexicon.from_entries(
        [
            *ENTRIES,
            LexiconEntry("SED-POT", "आलू कुफ़री", "Potato Kufri Bahar", ("आलू",), "seeds", ("potato",)),
        ]
    )
    talk.layer.__post_init__()
    reply = await talk.say("आलू के लिए क्या है?")
    assert reply is not None
    assert reply.text.startswith("आलू के लिए हमारे पास बीज में आलू कुफ़री"), reply.text
