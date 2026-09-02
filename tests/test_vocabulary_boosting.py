"""§5.5's vocabulary boosting, end to end.

§5.5: "Inject the catalogue lexicon -- every brand, product, active ingredient
and crop name, ~500 terms -- as keyterms where the STT supports it."

Every piece of this existed and none of it was connected: `Lexicon.keyterms()`
produced the terms, `build_speech_stack` accepted them, and the assembly built
its stack without any. The recogniser was decoding "इमिडाक्लोप्रिड" with no hint
that it is a word, and the Deepgram adapter sent no keyterm parameter at all.

The reason this matters more than a percentage point of WER: a mis-heard
product name does not produce a slightly wrong answer, it produces a *lookup
miss*, and the agent then says it cannot find the product the farmer is holding.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import async_sessionmaker

from uaagro_domain.settings import Settings, get_defaults
from voice_worker.adapters.factory import build_speech_stack
from voice_worker.adapters.stt.base import SttConfig
from voice_worker.adapters.stt.deepgram_flux import DeepgramFluxSTT
from voice_worker.text.catalogue_lexicon import load_lexicon

pytestmark = pytest.mark.integration


@pytest.fixture
async def lexicon(app_engine):  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    async with maker() as session:
        await session.execute(sql_text("SELECT set_config('app.role','voice_agent',true)"))
        return await load_lexicon(session)


async def test_the_catalogue_becomes_spoken_vocabulary(lexicon) -> None:  # type: ignore[no-untyped-def]
    """The seeded catalogue is 60 products; §5.5 wants every spoken form."""
    terms = lexicon.keyterms()

    assert len(terms) > 60, f"only {len(terms)} spoken forms from 60 products"
    # Hindi product names, which are what a farmer actually says.
    assert any("यूरिया" in term for term in terms), "urea has no Hindi spoken form"


async def test_active_ingredients_and_brands_are_spoken_forms_too(lexicon) -> None:  # type: ignore[no-untyped-def]
    """A farmer asks for "बायर की दवा" or reads the active ingredient off the
    label as often as they use the catalogue's own product name."""
    terms = {t.lower() for t in lexicon.keyterms()}
    joined = " ".join(terms)

    assert "bayer" in joined or "बायर" in joined, "no brand reached the lexicon"
    # An active ingredient from the seeded crop-protection range.
    assert any("इमिडाक्लोप्रिड" in t for t in terms), "no active ingredient in the lexicon"


async def test_the_recogniser_is_given_the_keyterms(lexicon) -> None:  # type: ignore[no-untyped-def]
    """The wiring that was missing: assembly -> speech stack -> STT config."""
    stack = build_speech_stack(
        "hi-IN", Settings(), get_defaults(), keyterms=tuple(lexicon.keyterms())
    )
    assert stack.stt_config.keyterms, "the stack was built without keyterms"
    assert len(stack.stt_config.keyterms) > 60


def test_deepgram_sends_keyterms_longest_first() -> None:
    """Longest first, and capped.

    A multi-word name is both the hardest to decode and the most damaging to
    lose: "एनपीके बारह बत्तीस सोलह" heard as four unrelated numbers is a failed
    turn, while a single mis-heard "यूरिया" is recoverable by §5.5's fuzzy
    match. The cap exists because these are query parameters and Devanagari is
    expensive once percent-encoded.
    """
    adapter = DeepgramFluxSTT(Settings())
    config = SttConfig(
        language="hi-IN",
        keyterms=("यूरिया", "एनपीके बारह बत्तीस सोलह", "डीएपी"),
    )

    sent = adapter._keyterms(config)

    assert sent[0] == "एनपीके बारह बत्तीस सोलह", "the longest term is not first"
    assert set(sent) == set(config.keyterms), "a term was dropped"


def test_the_keyterm_list_is_capped_and_says_so() -> None:
    """Silently truncating would mean an operator adds eighty products, sees no
    change, and has nothing to look at."""
    adapter = DeepgramFluxSTT(Settings())
    many = tuple(f"उत्पाद-{i:04d}" for i in range(adapter.MAX_KEYTERMS + 50))

    sent = adapter._keyterms(SttConfig(language="hi-IN", keyterms=many))

    assert len(sent) == adapter.MAX_KEYTERMS


def test_duplicate_spoken_forms_are_sent_once() -> None:
    """Two products sharing an active ingredient produce the same term twice,
    and the cap is a budget -- spending it on duplicates buys nothing."""
    adapter = DeepgramFluxSTT(Settings())
    config = SttConfig(language="hi-IN", keyterms=("यूरिया", "यूरिया", "डीएपी"))

    assert len(adapter._keyterms(config)) == 2
