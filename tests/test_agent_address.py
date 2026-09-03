"""What the agent does with the model's words before anyone hears them."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from voice_worker.flow.address import AddressBudget, name_forms
from voice_worker.flow.agent import Agent
from voice_worker.flow.context import CallerContext, ContextBuilder
from voice_worker.flow.validator import FALLBACK_SCRIPT_HI, OutputValidator
from voice_worker.text.translit import has_latin
from voice_worker.tools.base import ToolRegistry


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


def make_agent(gateway: ScriptedGateway, *, name: str | None = "Harthik") -> Agent:
    return Agent(
        registry=ToolRegistry(),
        context_builder=ContextBuilder(persona="आप सहायक हैं।"),
        gateway=gateway,
        validator=OutputValidator(),
        caller=CallerContext(name=name),
        address=AddressBudget(name_forms=name_forms(name)),
    )


async def collect(agent: Agent, transcript: str) -> list[str]:
    return [piece async for piece in agent.respond(transcript)]


async def test_the_name_and_the_filler_never_reach_the_synthesiser() -> None:
    hindi = next(f for f in name_forms("Harthik") if not has_latin(f))
    gateway = ScriptedGateway(
        [
            f"{hindi} जी, डीएपी उपलब्ध है। ",
            "एक क्षण रुकिए, मैं देख रहा हूँ। ",
            f"जी {hindi} जी, बोरी पचास किलो की है।",
        ]
    )
    agent = make_agent(gateway)

    pieces = await collect(agent, "डीएपी है क्या")

    # "उपलब्ध" leaves as the spoken "स्टॉक में" (text/register.py).
    assert pieces == ["डीएपी स्टॉक में है।", "बोरी पचास किलो की है।"], pieces
    # And the memory holds the words that were spoken, not the model's draft.
    assert agent.memory.turns[-1].text == "डीएपी स्टॉक में है। बोरी पचास किलो की है।"


async def test_running_long_stops_quietly_instead_of_handing_over() -> None:
    long = " ".join(["शब्द"] * 12) + "। "
    gateway = ScriptedGateway([long, long, long, long])
    agent = make_agent(gateway, name=None)

    pieces = await collect(agent, "बताइए")

    assert 1 <= len(pieces) <= 3
    assert FALLBACK_SCRIPT_HI not in pieces
    assert agent.last_turn is not None
    assert agent.last_turn.escalation is None
    assert agent.last_turn.validation is not None and agent.last_turn.validation.ok


async def test_sir_survives_twice_and_is_then_trimmed() -> None:
    gateway = ScriptedGateway(
        ["सर, डीएपी उपलब्ध है। ", "यूरिया भी है सर। ", "सर, बोरी पचास किलो की है।"]
    )
    agent = make_agent(gateway, name=None)

    pieces = await collect(agent, "डीएपी है क्या")

    assert pieces == ["सर, डीएपी स्टॉक में है।", "यूरिया भी है सर।", "बोरी पचास किलो की है।"]


async def test_the_memory_records_an_interruption_and_a_resumption() -> None:
    gateway = ScriptedGateway(["डीएपी स्टॉक में है। ", "बोरी पचास किलो की है।"])
    agent = make_agent(gateway, name=None)
    await collect(agent, "डीएपी है क्या")

    agent.note_interruption("डीएपी स्टॉक में है")
    last = agent.memory.turns[-1]
    assert last.role == "assistant" and last.interrupted
    assert "बीच में टोका" in last.render()
    assert last.text == "डीएपी स्टॉक में है"

    agent.note_resumed("बोरी पचास किलो की है।")
    assert not agent.memory.turns[-1].interrupted
    assert agent.memory.turns[-1].text == "डीएपी स्टॉक में है बोरी पचास किलो की है।"


async def test_a_withdrawn_speculation_leaves_no_orphan_question() -> None:
    """The eager transcript was only part of what the farmer said."""

    class Stalling(ScriptedGateway):
        async def stream(self, **kwargs: object) -> AsyncIterator[tuple[str, str]]:  # type: ignore[override]
            import asyncio

            self.calls += 1
            await asyncio.sleep(10)
            yield "", "primary"

    agent = make_agent(Stalling([]), name=None)
    import asyncio

    task = asyncio.create_task(collect(agent, "डीएपी"))
    await asyncio.sleep(0.05)
    task.cancel()
    import contextlib

    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert agent.memory.turns and agent.memory.turns[-1].role == "user"
    agent.discard_pending()
    assert agent.memory.turns == []
