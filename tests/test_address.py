"""Addressing the farmer in proportion (§11.3), and knowing a nod from a turn."""

from __future__ import annotations

from voice_worker.flow.address import (
    AddressBudget,
    is_backchannel,
    is_filler,
    name_forms,
    trim_address,
)
from voice_worker.text.speech import SentenceBuffer
from voice_worker.text.translit import has_latin


def _budget(name: str | None = "Harthik") -> AddressBudget:
    return AddressBudget(name_forms=name_forms(name))


def test_the_name_is_known_in_both_scripts() -> None:
    forms = name_forms("Harthik")
    assert "Harthik" in forms
    assert any(not has_latin(f) for f in forms), forms


def test_the_name_is_not_said_again_in_a_reply() -> None:
    budget = _budget()
    hindi = next(f for f in budget.name_forms if not has_latin(f))
    assert trim_address(f"{hindi} जी, डीएपी उपलब्ध है।", budget) == "डीएपी उपलब्ध है।"
    assert trim_address(f"जी {hindi} जी, डीएपी उपलब्ध है।", budget) == "डीएपी उपलब्ध है।"
    assert trim_address("Harthik जी, डीएपी उपलब्ध है।", budget) == "डीएपी उपलब्ध है।"
    assert trim_address(f"डीएपी उपलब्ध है, {hindi} जी।", budget) == "डीएपी उपलब्ध है।"


def test_a_bare_ji_opener_is_dropped_but_an_answer_is_kept() -> None:
    budget = _budget(None)
    assert trim_address("जी, डीएपी उपलब्ध है।", budget) == "डीएपी उपलब्ध है।"
    assert trim_address("जी हाँ, डीएपी उपलब्ध है।", budget) == "जी हाँ, डीएपी उपलब्ध है।"
    assert trim_address("जी नहीं, अभी नहीं है।", budget) == "जी नहीं, अभी नहीं है।"


def test_sir_is_a_budget_of_two_per_call() -> None:
    budget = _budget(None)
    assert trim_address("सर, डीएपी उपलब्ध है।", budget) == "सर, डीएपी उपलब्ध है।"
    assert trim_address("यूरिया भी है सर।", budget) == "यूरिया भी है सर।"
    assert trim_address("सर, बोरी पचास किलो की है।", budget) == "बोरी पचास किलो की है।"
    assert budget.sir_used == 2


def test_filler_sentences_are_not_spoken() -> None:
    for filler in (
        "एक क्षण रुकिए, मैं देख रहा हूँ।",
        "जी, ज़रा देख लेता हूँ।",
        "एक मिनट रुकिए।",
        "मैं अभी चेक करता हूँ।",
        "जी, एक सेकंड।",
    ):
        assert is_filler(filler), filler
        assert trim_address(filler, _budget()) == ""
    assert not is_filler("आप कल केंद्र पर आकर देख सकते हैं।")
    assert trim_address("आप कल केंद्र पर आकर देख सकते हैं।", _budget()) == (
        "आप कल केंद्र पर आकर देख सकते हैं।"
    )


def test_a_listener_is_not_a_turn() -> None:
    for nod in ("हाँ", "जी हाँ", "अच्छा ठीक है", "ok", "हम्म", "हैलो"):
        assert is_backchannel(nod), nod
    for turn in ("हाँ डीएपी चाहिए", "", "रेट बताइए", "हाँ जी यूरिया"):
        assert not is_backchannel(turn), turn


def test_the_opening_clause_is_released_before_the_full_stop() -> None:
    """Four words and a comma are enough to start speaking."""
    buffer = SentenceBuffer(first_clause_words=4)
    assert buffer.add("जी हाँ, ") == []
    assert buffer.add("डीएपी उपलब्ध है, ") == ["जी हाँ, डीएपी उपलब्ध है,"]
    # Only the first release; the rest waits for a sentence boundary.
    assert buffer.add("बोरी पचास किलो की है, ") == []
    assert buffer.add("रेट बारह सौ। ") == ["बोरी पचास किलो की है, रेट बारह सौ।"]


def test_a_plain_buffer_still_waits_for_the_sentence() -> None:
    buffer = SentenceBuffer()
    assert buffer.add("जी हाँ, डीएपी उपलब्ध है, ") == []
    assert buffer.add("बोरी पचास किलो की है। ") == ["जी हाँ, डीएपी उपलब्ध है, बोरी पचास किलो की है।"]
