"""Speaking before the answer is finished (§16.2, §16.3, §7).

Waiting for the last token before saying the first word costs the whole
generation. Through the configured endpoint that is a first token at ~1.7 s and
the rest of a 35-word Hindi answer several seconds behind it -- all of it
silence on the line, when the opening sentence was ready long before.

The reason it was not done sooner is real and is what these tests pin down:
speech cannot be recalled. Once "डीएपी उपलब्ध है" has been played, no later
validation failure can take it back. So the rules are:

* **A dosage answer never streams.** §16.2 wants dose, pre-harvest interval and
  precaution spoken together; a truncated one is worse than a slow one.
* **The first sentence is spoken only after it validates**, which keeps §16.3's
  retry intact for the common failure -- nothing has been said, so the answer
  is regenerated with the violation as feedback, exactly as before.
* **A failure after speech has started stops**, and hands over. A partial true
  answer plus a handover beats a contradiction.

What makes per-sentence validation sound at all: §16.3's checks are local and
*monotonic* -- register, guarantees, filler and ungrounded numbers all condemn
the sentence they appear in and cannot be redeemed by a later one -- and the
tool results they ground against are complete before generation starts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from uaagro_domain.enums import Intent
from voice_worker.flow.agent import Agent
from voice_worker.flow.context import ContextBuilder
from voice_worker.flow.validator import FALLBACK_SCRIPT_HI, OutputValidator
from voice_worker.tools.base import ToolRegistry


class ScriptedGateway:
    """Yields prepared fragments, and records how many generations were asked for."""

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


def make_agent(gateway: Any, **kwargs: Any) -> Agent:
    return Agent(
        registry=ToolRegistry(),
        context_builder=ContextBuilder(persona="आप सहायक हैं।"),
        gateway=gateway,
        validator=OutputValidator(),
        **kwargs,
    )


async def collect(agent: Agent, transcript: str) -> list[str]:
    return [piece async for piece in agent.respond(transcript)]


# --------------------------------------------------------------------------- #
# The streaming path
# --------------------------------------------------------------------------- #


async def test_each_sentence_is_released_as_it_completes() -> None:
    """The point of the change: three sentences reach the pipeline as three
    pieces, so the first can be synthesised while the third is still being
    generated."""
    gateway = ScriptedGateway(
        ["जी हाँ, ", "डीएपी उपलब्ध है। ", "बैग पचास ", "किलो का है। ", "कुछ और चाहिए?"]
    )
    agent = make_agent(gateway)

    pieces = await collect(agent, "डीएपी है क्या")

    assert len(pieces) >= 2, f"the answer arrived in one piece: {pieces}"
    assert pieces[0].startswith("जी हाँ")
    assert "".join(pieces).replace("  ", " ").strip().endswith("कुछ और चाहिए?")


async def test_a_dosage_answer_is_not_split(monkeypatch) -> None:
    """§16.2: the dose, the pre-harvest interval and the precaution are one
    unit. Speaking the dose and stopping is the outcome worse than being slow.
    """
    gateway = ScriptedGateway(
        ["एक एकड़ में ", "दो सौ ग्राम डालें। ", "कटाई से पहले ", "इक्कीस दिन रुकें।"]
    )
    agent = make_agent(gateway)

    async def dosage(_self: Agent, _t: str) -> Intent:
        return Intent.DOSAGE_QUERY

    monkeypatch.setattr(Agent, "_classify", dosage)

    pieces = await collect(agent, "कितना डालें")

    assert len(pieces) == 1, f"a dosage answer was streamed in pieces: {pieces}"


async def test_a_bad_opening_still_gets_its_retry() -> None:
    """§16.3's retry survives, because nothing has been spoken yet.

    This is the case the previous design handled and the streaming one must not
    lose: the first sentence is checked *before* the caller hears anything, so
    a violation can still be regenerated with feedback.
    """
    gateway = ScriptedGateway(
        # First generation opens with तुम, which §11.3 forbids.
        ["तुम्हें डीएपी चाहिए? ", "मैं देखता हूँ।"],
        # Retry is clean.
        ["जी, आपको डीएपी चाहिए? ", "मैं देखता हूँ।"],
    )
    agent = make_agent(gateway)

    pieces = await collect(agent, "डीएपी")

    assert gateway.calls == 2, "the violation was not retried"
    joined = "".join(pieces)
    assert "तुम्हें" not in joined
    # The retry's "जी," opener is trimmed and its "मैं देखता हूँ" dropped as
    # filler; what survives is the question itself.
    assert "आपको डीएपी चाहिए" in joined


async def test_a_failure_after_speaking_stops_rather_than_contradicting() -> None:
    """Words already played cannot be recalled.

    The second sentence quotes a price no tool produced -- §16.2's core
    violation. The first was clean and has been spoken, so the turn stops
    there and hands over instead of retracting.
    """
    gateway = ScriptedGateway(
        ["जी हाँ, डीएपी उपलब्ध है। ", "कीमत 1350 रुपये है।"]
    )
    agent = make_agent(gateway)

    pieces = await collect(agent, "डीएपी है क्या")

    assert pieces[0].startswith("जी हाँ")
    assert "1350" not in "".join(pieces), "an ungrounded price was spoken"
    assert pieces[-1] == FALLBACK_SCRIPT_HI
    assert gateway.calls == 1, "it retried after already speaking"


async def test_stopping_early_escalates_rather_than_dead_ending() -> None:
    """§11.4 forbids a dead end. A truncated answer leaves the caller with
    half an answer, so it has to reach a person."""
    gateway = ScriptedGateway(["जी हाँ, उपलब्ध है। ", "कीमत 990 रुपये है।"])
    agent = make_agent(gateway)

    await collect(agent, "डीएपी है क्या")

    assert agent.last_turn is not None
    assert agent.last_turn.escalation is not None, "a truncated turn dead-ended"


async def test_an_ungrounded_number_is_still_caught_sentence_by_sentence() -> None:
    """The grounding check is the whole point of §16.3 and must not be weakened
    by being run on smaller pieces."""
    gateway = ScriptedGateway(["कीमत 1200 रुपये है।"], ["कीमत केंद्र पर बताई जाएगी।"])
    agent = make_agent(gateway)

    pieces = await collect(agent, "रेट क्या है")

    assert "1200" not in "".join(pieces)


# --------------------------------------------------------------------------- #
# The paths that must not stream
# --------------------------------------------------------------------------- #


async def test_the_safety_script_bypasses_generation_entirely() -> None:
    """§16.1: no classification, no tools, no model -- and no streaming either.
    The script is fixed and must arrive whole."""
    gateway = ScriptedGateway(["कुछ भी"])
    agent = make_agent(gateway)

    pieces = await collect(agent, "मेरी आँख में दवा चली गई")

    assert gateway.calls == 0, "the model was consulted on a safety turn"
    assert len(pieces) == 1


async def test_no_gateway_says_so_instead_of_inventing() -> None:
    """§1 N1: silence-with-escalation is the correct degraded behaviour."""
    agent = make_agent(None)

    pieces = await collect(agent, "डीएपी का रेट")

    assert pieces == [FALLBACK_SCRIPT_HI]


# --------------------------------------------------------------------------- #
# The two paths agree
# --------------------------------------------------------------------------- #


async def test_the_buffered_and_streaming_paths_share_one_preparation() -> None:
    """`handle` and `respond` run the same safety check, classifier and
    escalation passes. A safety path that fired on one and not the other would
    be the worst bug this file could carry, so they share `_prepare` rather
    than each keeping a copy."""
    import inspect

    from voice_worker.flow import agent as module

    source = inspect.getsource(module.Agent)
    assert source.count("_check_safety(transcript)") == 1, (
        "the safety check appears more than once; the two paths can now drift"
    )
    assert "await self._prepare(" in source


async def test_handle_still_returns_a_whole_turn() -> None:
    """The buffered path is unchanged and still used -- by dosage answers, by
    the safety path, and by every test written against it."""
    gateway = ScriptedGateway(["जी हाँ, ", "उपलब्ध है।"])
    agent = make_agent(gateway)

    result = await agent.handle("डीएपी है क्या")

    assert result.text.strip() == "जी हाँ, उपलब्ध है।"
    assert result.validation.ok


@pytest.mark.parametrize("transcript", ["डीएपी है क्या", "नमस्ते"])
async def test_memory_records_the_caller_once_per_turn(transcript: str) -> None:
    """`_prepare` adds the caller's line and `_finish` adds the agent's. Both
    paths run each exactly once; a duplicate would corrupt §6.2's history."""
    gateway = ScriptedGateway(["जी, ठीक है।"])
    agent = make_agent(gateway)

    await collect(agent, transcript)

    roles = [turn.role for turn in agent.memory.turns]
    assert roles.count("user") == 1
    assert roles.count("assistant") == 1
