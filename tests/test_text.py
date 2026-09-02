"""Numeric normalisation, text_for_speech and the ASR lexicon (§5.3, §5.5).

§19 asks for property-based tests on the numeric parser specifically, and the
reason is worth restating: this code sits between what a farmer says and what
gets ordered or sprayed. A rounding slip here is a wrong quantity of
agrochemical, not a display bug.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from uaagro_db.seeds import data as seed
from voice_worker.text.lexicon import (
    Lexicon,
    LexiconEntry,
    levenshtein,
    normalise,
    similarity,
)
from voice_worker.text.numbers import (
    digits_to_words,
    extract_numbers,
    npk_grade_to_words,
    parse_number,
    parse_phone_number,
    rupees_to_words,
    to_hindi_price_words,
    to_hindi_words,
)
from voice_worker.text.speech import (
    over_long_sentences,
    split_sentences,
    text_for_speech,
    unspeakable_fragments,
)
from voice_worker.text.translit import transliterate_word

# --------------------------------------------------------------------------- #
# Rendering numbers
# --------------------------------------------------------------------------- #


def test_specification_price_example() -> None:
    """§5.3 gives this exact conversion."""
    assert rupees_to_words(1250) == "बारह सौ पचास रुपये"


def test_specification_npk_example() -> None:
    """§5.3: read as three nutrient figures, never as one number."""
    assert npk_grade_to_words("12-32-16") == "बारह बत्तीस सोलह"


def test_prices_use_the_colloquial_hundreds_form() -> None:
    """A shopkeeper says "तेरह सौ पचास", not "एक हज़ार तीन सौ पचास"."""
    assert to_hindi_price_words(1350) == "तेरह सौ पचास"
    assert to_hindi_price_words(9900) == "निन्यानवे सौ"


def test_large_amounts_revert_to_the_thousand_form() -> None:
    """Past ten thousand the hundreds form stops sounding natural."""
    assert "हज़ार" in to_hindi_price_words(24500)


def test_indian_grouping_uses_lakh_and_crore() -> None:
    assert to_hindi_words(150_000) == "एक लाख पचास हज़ार"
    assert to_hindi_words(10_000_000).startswith("एक करोड़")


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "शून्य"), (16, "सोलह"), (69, "उनहत्तर"), (99, "निन्यानवे"), (100, "एक सौ")],
)
def test_irregular_hindi_cardinals(value: int, expected: str) -> None:
    """Hindi has no rule for these; the table is the implementation."""
    assert to_hindi_words(value) == expected


def test_phone_numbers_are_read_digit_by_digit() -> None:
    """§11.3 read-back is for verification. Pairs are faster but छिहत्तर (76)
    and छियासठ (66) differ by a syllable, which a farmer beside a tractor will
    confirm as correct when it is not."""
    spoken = digits_to_words("9876543210")
    assert spoken.split() == [
        "नौ",
        "आठ",
        "सात",
        "छह",
        "पाँच",
        "चार",
        "तीन",
        "दो",
        "एक",
        "शून्य",
    ]


def test_paired_reading_is_available_but_not_the_default() -> None:
    assert digits_to_words("9876543210", paired=True) != digits_to_words("9876543210")


# --------------------------------------------------------------------------- #
# Parsing numbers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("205", 205),
        ("२०५", 205),
        ("दो सौ पाँच", 205),
        ("do sau paanch", 205),
        ("1,250", 1250),
        ("दस", 10),
        ("ten", 10),
        ("दो लाख पचास हज़ार", 250_000),
        ("12.5", Decimal("12.5")),
    ],
)
def test_every_written_and_spoken_form_parses_to_one_value(spoken: str, expected: object) -> None:
    assert parse_number(spoken) == Decimal(str(expected))


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("आधा", "0.5"),
        ("डेढ़", "1.5"),
        ("ढाई", "2.5"),
        ("साढ़े तीन", "3.5"),
        ("सवा दो", "2.25"),
        ("पौने चार", "3.75"),
    ],
)
def test_fractional_quantity_words(spoken: str, expected: str) -> None:
    """ "डेढ़ बीघा" is a real plot size. Missing these mis-reads it by half."""
    assert parse_number(spoken) == Decimal(expected)


@pytest.mark.parametrize("text", ["डीएपी चाहिए", "hello", "", "   ", "दो बोरी", "नमस्ते जी"])
def test_prose_returns_none_rather_than_a_guess(text: str) -> None:
    """§1 N1: no number is better than a wrong one. The agent asks again."""
    assert parse_number(text) is None


@given(st.integers(min_value=0, max_value=9_999_999))
def test_rendered_numbers_parse_back_to_themselves(value: int) -> None:
    """The round trip is the property that matters: whatever the agent speaks,
    the parser must recover if the farmer repeats it."""
    assert parse_number(to_hindi_words(value)) == Decimal(value)


@given(st.integers(min_value=1, max_value=99_999))
def test_price_rendering_also_round_trips(value: int) -> None:
    assert parse_number(to_hindi_price_words(value)) == Decimal(value)


@given(st.integers(min_value=0, max_value=10**9))
def test_rendering_never_produces_digits(value: int) -> None:
    """A digit reaching the synthesiser is read in English."""
    assert not any(character.isdigit() for character in to_hindi_words(value))


def test_multiple_quantities_in_one_breath() -> None:
    found = extract_numbers("दस बोरी डीएपी और पाँच बोरी यूरिया")
    assert found == [Decimal(10), Decimal(5)]


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("9876543210", "9876543210"),
        ("+91 98765 43210", "9876543210"),
        ("09876543210", "9876543210"),
        ("नौ आठ सात छह पाँच चार तीन दो एक शून्य", "9876543210"),
    ],
)
def test_phone_capture(spoken: str, expected: str) -> None:
    assert parse_phone_number(spoken) == expected


@pytest.mark.parametrize("spoken", ["98765", "12345678901234", "कुछ नहीं"])
def test_incomplete_phone_numbers_are_rejected(spoken: str) -> None:
    """A nine-digit guess wastes the read-back that §11.3 requires."""
    assert parse_phone_number(spoken) is None


# --------------------------------------------------------------------------- #
# text_for_speech
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [
        "NPK 12-32-16",
        "DAP 50 kg बोरी ₹1,350",
        "Rs 267 की यूरिया 45kg",
        "Imidacloprid 17.8% SL 250 ml",
        "Cymoxanil 8% + Mancozeb 64% WP",
        "PHI 15 days",
        "MRP ₹1,470 + GST 5%",
        "2.5 acre में डालें",
        "आपका नंबर 9876543210 है",
        "सल्फर 90% WDG 1000 gm",
    ],
)
def test_nothing_unspeakable_survives_normalisation(raw: str) -> None:
    """§5.3: never hand a raw catalogue string to the TTS. Latin letters,
    digits and symbols all survive a missing rule *silently* -- the synthesiser
    produces something, just not Hindi."""
    assert unspeakable_fragments(text_for_speech(raw)) == []


def test_currency_is_consumed_before_the_bare_number_rule() -> None:
    """Rule order: otherwise the digits are spoken and the rupee is orphaned."""
    spoken = text_for_speech("₹1,350")
    assert spoken == "तेरह सौ पचास रुपये"


def test_units_attach_to_their_number() -> None:
    assert text_for_speech("50kg") == "पचास किलो"
    assert text_for_speech("250 ml") == "दो सौ पचास मिलीलीटर"


def test_abbreviations_are_spelled_in_devanagari() -> None:
    """Latin letters make Sarvam switch to English mid-sentence."""
    assert text_for_speech("NPK") == "एन पी के"
    assert text_for_speech("DAP") == "डी ए पी"


def test_formulation_plus_reads_as_and() -> None:
    assert "और" in text_for_speech("Cymoxanil 8% + Mancozeb 64%")


def test_empty_input_is_empty_output() -> None:
    assert text_for_speech("") == ""
    assert text_for_speech("   ") == ""


def test_sentences_split_on_the_danda() -> None:
    """§5.3 chunks on sentence boundaries so barge-in cuts at a natural point."""
    parts = split_sentences("डीएपी उपलब्ध है। तेरह सौ पचास की बोरी। कितनी चाहिए?")
    assert len(parts) == 3
    assert parts[0].endswith("।")


def test_over_long_sentences_are_reported_not_truncated() -> None:
    """§5.3 caps a sentence at about fifteen words. The fix is a shorter
    prompt, so the validator reports rather than clipping mid-thought."""
    long_sentence = " ".join(["शब्द"] * 30) + "।"
    assert over_long_sentences(long_sentence)
    assert not over_long_sentences("डीएपी उपलब्ध है।")


# --------------------------------------------------------------------------- #
# Transliteration fallback
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("latin", "devanagari"),
    [
        ("Imidacloprid", "इमिडाक्लोप्रिड"),
        ("Mancozeb", "मैंकोज़ेब"),
        ("Tebuconazole", "टेबुकोनाज़ोल"),
    ],
)
def test_known_ingredients_transliterate_exactly(latin: str, devanagari: str) -> None:
    assert transliterate_word(latin) == devanagari


@pytest.mark.parametrize(
    "latin", ["Pyraclostrobin", "Azoxystrobin", "Hexaconazole", "Difenoconazole"]
)
def test_unknown_ingredients_produce_valid_devanagari(latin: str) -> None:
    """The fallback approximates, but it must never emit an unpronounceable
    cluster -- a matra after a halant is not valid Devanagari."""
    import re

    out = transliterate_word(latin)
    assert out
    assert not re.search(r"्[ािीुूेैोौ]", out), f"invalid cluster in {out!r}"
    assert not re.search(r"[A-Za-z]", out)


# --------------------------------------------------------------------------- #
# ASR lexicon
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def catalogue_lexicon() -> Lexicon:
    return Lexicon.from_entries(
        [LexiconEntry(p.sku, p.name_hi, p.name_en, p.lexicon) for p in seed.PRODUCTS]
    )


@pytest.mark.parametrize("spoken", ["यूरिया", "urea", "यूरीया", "uria"])
def test_every_spoken_form_of_urea_reaches_one_sku(catalogue_lexicon: Lexicon, spoken: str) -> None:
    """§5.5 names this case exactly."""
    match = catalogue_lexicon.match(spoken)
    assert match is not None
    assert match.sku == "FRT-URE-45"


@pytest.mark.parametrize("spoken", ["डीएपी", "dap", "डी ए पी", "काली खाद", "डीएपि"])
def test_dap_survives_asr_variation(catalogue_lexicon: Lexicon, spoken: str) -> None:
    match = catalogue_lexicon.match(spoken)
    assert match is not None
    assert match.sku == "FRT-DAP-50"


@pytest.mark.parametrize("spoken", ["नमस्ते", "मेरा नाम रमेश है", "xyz", ""])
def test_unrelated_speech_does_not_match_a_product(catalogue_lexicon: Lexicon, spoken: str) -> None:
    """A wrong SKU is a wrong agrochemical, so the floor refuses rather than
    returns the nearest thing."""
    assert catalogue_lexicon.match(spoken) is None


def test_the_catalogue_has_no_ambiguous_variants(catalogue_lexicon: Lexicon) -> None:
    """Two products answering to one spoken name means the agent cannot know
    which the farmer meant. Surfaced as a catalogue error, not resolved by
    guessing -- this test is what caught the two sulphur products."""
    assert catalogue_lexicon.conflicts() == {}


def test_a_genuinely_ambiguous_word_resolves_to_nothing(
    catalogue_lexicon: Lexicon,
) -> None:
    """ "सल्फर" is both a soil amendment and a fungicide in this catalogue.
    Neither claims the bare word, so the agent has to ask."""
    assert catalogue_lexicon.match("सल्फर") is None
    assert catalogue_lexicon.match("गंधक") is not None
    assert catalogue_lexicon.match("गंधक की दवा") is not None
    assert catalogue_lexicon.match("गंधक").sku != catalogue_lexicon.match("गंधक की दवा").sku


def test_multi_token_products_match_as_one_phrase(catalogue_lexicon: Lexicon) -> None:
    matches = catalogue_lexicon.find_all("सिंगल सुपर फॉस्फेट का रेट क्या है")
    assert [m.sku for m in matches] == ["FRT-SSP-50"]


def test_postpositions_do_not_block_a_match(catalogue_lexicon: Lexicon) -> None:
    """A farmer says "गेहूँ का बीज", not the catalogue phrase "गेहूँ बीज"."""
    matches = catalogue_lexicon.find_all("मुझे गेहूँ का बीज चाहिए")
    assert matches
    assert matches[0].sku.startswith("SED-WHT")


def test_several_products_in_one_utterance(catalogue_lexicon: Lexicon) -> None:
    matches = catalogue_lexicon.find_all("दस बोरी डीएपी और पाँच बोरी यूरिया चाहिए")
    assert {m.sku for m in matches} == {"FRT-DAP-50", "FRT-URE-45"}


def test_keyterms_cover_the_whole_catalogue(catalogue_lexicon: Lexicon) -> None:
    """§5.5 injects these into the recogniser where it supports boosting --
    biasing the decode beats correcting it afterwards."""
    terms = catalogue_lexicon.keyterms()
    assert len(terms) >= len(seed.PRODUCTS)
    assert "यूरिया" in terms


def test_nukta_and_vowel_length_never_decide_a_match() -> None:
    """ASR output is inconsistent about both, so neither may be load-bearing."""
    assert normalise("ज़िंक") == normalise("जिंक")
    assert normalise("यूरीया") == normalise("यूरिया")


def test_edit_distance_basics() -> None:
    assert levenshtein("urea", "urea") == 0
    assert levenshtein("urea", "uria") == 1
    assert similarity("urea", "urea") == 1.0
    assert 0.0 <= similarity("urea", "dap") < 1.0


@given(st.text(min_size=0, max_size=30), st.text(min_size=0, max_size=30))
def test_similarity_is_bounded_and_symmetric(left: str, right: str) -> None:
    forward = similarity(left, right)
    assert 0.0 <= forward <= 1.0
    assert forward == pytest.approx(similarity(right, left))


def test_sentence_punctuation_does_not_break_a_match(catalogue_lexicon: Lexicon) -> None:
    """The Devanagari danda lives inside the letter block, so a naive character
    class treats "डीएपी।" as a different word from "डीएपी" -- and the match
    silently misses. Regression guard for that."""
    assert catalogue_lexicon.match("डीएपी।") is not None
    assert catalogue_lexicon.match("डीएपी।").sku == catalogue_lexicon.match("डीएपी").sku
    assert normalise("चाहिए।") == normalise("चाहिए")
