"""Identity, orders, tickets and escalation (§6.3, §12.3, §17).

The assertions that matter most here are about what does *not* come back. A
phone number never leaves ``lookup_farmer``; a transfer reason the model wrote
itself is refused; a caller with no consent gets no WhatsApp. Each is a control
that fails silently if nobody checks it -- the tool still returns something
plausible either way.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from uaagro_db.seeds.loader import DEMO_PHONE_PREFIX
from uaagro_domain.timezone import ist
from voice_worker.tools import TOOL_NAMES, build_registry
from voice_worker.tools.base import ToolContext, ToolRegistry
from voice_worker.tools.escalation import (
    DAILY_TRANSFER_CAP,
    QueueingWhatsAppPort,
    SendWhatsApp,
    TransferPort,
    TransferToHuman,
)
from voice_worker.tools.session import reset_session_factory, set_session_factory

pytestmark = pytest.mark.integration

#: The first farmer given an order history by the seeder.
ORDERED_FARMER_PHONE = f"{DEMO_PHONE_PREFIX}000000"


@pytest.fixture
async def registry(app_engine):  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(app_engine, expire_on_commit=False)

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
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
    registry = build_registry()
    # Warmed, because the worker warms before it reports ready (§7): the driver
    # prepares statements per connection, so an unwarmed pool makes the first
    # call on each connection pay compilation and exceed the 400 ms hard
    # timeout. Testing against a cold pool measures a state production does not
    # serve calls in.
    await registry.warm()
    try:
        yield registry
    finally:
        reset_session_factory()


async def _known_farmer(registry: ToolRegistry) -> dict[str, object]:
    result = await registry.execute(
        "lookup_farmer", {"phone": ORDERED_FARMER_PHONE}, ToolContext(call_id="c1")
    )
    assert result.ok, result.error
    assert result.data["known"] is True
    return dict(result.data)


# --------------------------------------------------------------------------- #
# Registry completeness
# --------------------------------------------------------------------------- #


def test_every_tool_in_the_spec_exists() -> None:
    """§6.3 lists thirteen. A name the model is offered but that has no
    implementation fails on first use, mid-call."""
    built = set(build_registry().tools)
    assert [name for name in TOOL_NAMES if name not in built] == []
    assert len(TOOL_NAMES) == 13


# --------------------------------------------------------------------------- #
# lookup_farmer (§17)
# --------------------------------------------------------------------------- #


async def test_a_known_caller_comes_back_with_their_context(
    registry: ToolRegistry,
) -> None:
    data = await _known_farmer(registry)
    assert data["name"]
    assert data["village"]
    assert data["language"]
    assert isinstance(data["recent_orders"], list)


async def test_an_unknown_caller_is_not_an_error(registry: ToolRegistry) -> None:
    """A first-time caller is the normal case on a helpline. Raising here would
    push the agent into escalation instead of a greeting."""
    result = await registry.execute(
        "lookup_farmer", {"phone": "9998123456"}, ToolContext(call_id="c1")
    )
    assert result.ok
    assert result.data["known"] is False


async def test_no_phone_number_ever_comes_back_out(registry: ToolRegistry) -> None:
    """§17 and §23-6. The tool result goes into the LLM context and is persisted
    to ``call_turns.tool_results``, so a number here would be copied into two
    places it must never reach."""
    data = await _known_farmer(registry)
    serialised = json.dumps(data, default=str)
    assert ORDERED_FARMER_PHONE not in serialised
    assert ORDERED_FARMER_PHONE[-6:] not in serialised
    assert "phone" not in serialised or "phone_enc" not in serialised


async def test_a_malformed_number_is_refused(registry: ToolRegistry) -> None:
    result = await registry.execute(
        "lookup_farmer", {"phone": "12345678901234"}, ToolContext(call_id="c1")
    )
    assert not result.ok
    assert not result.grounded


# --------------------------------------------------------------------------- #
# get_order_status
# --------------------------------------------------------------------------- #


async def test_orders_come_back_with_what_the_farmer_asks_about(
    registry: ToolRegistry,
) -> None:
    data = await _known_farmer(registry)
    result = await registry.execute(
        "get_order_status", {"farmer_id": data["farmer_id"]}, ToolContext(call_id="c1")
    )
    assert result.ok, result.error
    assert result.data["count"] >= 1
    order = result.data["orders"][0]
    assert order["order_ref"]
    assert order["status"]
    assert order["items"]


async def test_a_dispatched_order_always_carries_a_date(
    registry: ToolRegistry, app_engine
) -> None:  # type: ignore[no-untyped-def]
    """§11.4: "it has been sent" with no answer to "when?" is a dead end. The
    schema constraint makes that state unrepresentable; this proves the tool
    surfaces the date rather than dropping it."""
    async with app_engine.connect() as connection:
        await connection.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        rows = (
            await connection.execute(
                text(
                    "SELECT order_ref, farmer_id::text FROM orders "
                    "WHERE status = 'dispatched' LIMIT 1"
                )
            )
        ).first()
    assert rows is not None, "the seed should contain a dispatched order"
    order_ref, farmer_id = rows

    result = await registry.execute(
        "get_order_status",
        {"farmer_id": farmer_id, "order_ref": order_ref},
        ToolContext(call_id="c1"),
    )
    assert result.ok, result.error
    assert result.data["orders"][0]["promised_date"]


async def test_a_reference_that_matches_nothing_is_said_plainly(
    registry: ToolRegistry,
) -> None:
    data = await _known_farmer(registry)
    result = await registry.execute(
        "get_order_status",
        {"farmer_id": data["farmer_id"], "order_ref": "UA-9999-9"},
        ToolContext(call_id="c1"),
    )
    assert not result.ok
    assert result.data.get("code") == "not_found"


async def test_one_farmer_cannot_read_another_farmers_orders(
    registry: ToolRegistry, app_engine
) -> None:  # type: ignore[no-untyped-def]
    """The tool filters by farmer_id, but that is an argument the model
    supplies. This checks the filter is real rather than decorative."""
    data = await _known_farmer(registry)
    async with app_engine.connect() as connection:
        await connection.execute(text("SELECT set_config('app.role','voice_agent',true)"))
        other = await connection.scalar(
            text("SELECT farmer_id::text FROM orders WHERE farmer_id <> :f LIMIT 1"),
            {"f": data["farmer_id"]},
        )
    assert other is not None

    mine = await registry.execute(
        "get_order_status", {"farmer_id": data["farmer_id"]}, ToolContext(call_id="c1")
    )
    theirs = await registry.execute(
        "get_order_status", {"farmer_id": other}, ToolContext(call_id="c1")
    )
    my_refs = {o["order_ref"] for o in mine.data["orders"]}
    their_refs = {o["order_ref"] for o in theirs.data["orders"]}
    assert my_refs.isdisjoint(their_refs)


# --------------------------------------------------------------------------- #
# create_ticket (§11.4)
# --------------------------------------------------------------------------- #


async def test_a_callback_ticket_commits_to_a_time(registry: ToolRegistry) -> None:
    """§11.4: the commitment is spoken out loud, so the tool has to return a
    number the agent can actually say."""
    result = await registry.execute(
        "create_ticket",
        {"type": "callback", "summary": "डीएपी के दाम पर बात करनी है"},
        ToolContext(call_id="c1"),
    )
    assert result.ok, result.error
    assert result.data["ticket_ref"].startswith("TKT-")
    assert result.data["callback_within_hours"] == 24


async def test_a_safety_incident_cannot_be_filed_as_routine(
    registry: ToolRegistry,
) -> None:
    """§16.1. The model asked for p3; the ingestion of a pesticide is not a p3,
    and letting the caller's phrasing set the priority is not safe."""
    result = await registry.execute(
        "create_ticket",
        {
            "type": "safety_incident",
            "summary": "caller reports swallowing spray",
            "priority": "p3",
        },
        ToolContext(call_id="c1"),
    )
    assert result.ok, result.error
    assert result.data["priority"] == "p0"
    assert result.data["callback_within_hours"] == 1


async def test_an_invented_ticket_type_is_refused(registry: ToolRegistry) -> None:
    result = await registry.execute(
        "create_ticket",
        {"type": "refund_request", "summary": "wants money back"},
        ToolContext(call_id="c1"),
    )
    assert not result.ok
    assert not result.grounded


# --------------------------------------------------------------------------- #
# transfer_to_human (§12.3)
# --------------------------------------------------------------------------- #


class _Port(TransferPort):
    """A telephony stand-in whose answers the test controls."""

    def __init__(self, *, busy: set[str] | None = None, used: int = 0) -> None:
        self.busy = busy or set()
        self.used = used

    async def is_busy(self, number: str) -> bool:
        return number in self.busy

    async def transfers_today(self, call_id: str, farmer_id: str | None) -> int:
        return self.used


#: A Tuesday, 11:00 IST -- inside every seeded centre's hours. Pinned because
#: "is the centre open" is a real branch: outside hours a transfer becomes a
#: callback commitment, and a test reading the wall clock silently swaps which
#: branch it covers depending on when the suite runs. These assertions are about
#: the chain, so the clock is held open; the closed branch has its own test.
OPEN_HOURS = datetime(2026, 9, 1, 11, 0, tzinfo=ist())
AFTER_HOURS = datetime(2026, 9, 1, 22, 30, tzinfo=ist())


async def _transfer(
    registry: ToolRegistry,
    port: TransferPort,
    args: dict[str, object],
    *,
    at: datetime = OPEN_HOURS,
) -> dict[str, object]:
    registry.tools["transfer_to_human"] = TransferToHuman(port, clock=lambda: at)
    data = await _known_farmer(registry)
    result = await registry.execute(
        "transfer_to_human",
        args,
        ToolContext(call_id="c1", farmer_id=str(data["farmer_id"])),
    )
    assert result.ok, result.error
    return dict(result.data)


async def test_a_free_text_reason_is_rejected(registry: ToolRegistry) -> None:
    """§12.3 states this outright. A model that can write its own reason can
    route a safety emergency into a routine queue."""
    result = await registry.execute(
        "transfer_to_human",
        {"reason": "the farmer sounds cross about something"},
        ToolContext(call_id="c1"),
    )
    assert not result.ok
    assert not result.grounded


async def test_a_transfer_is_warm_not_blind(registry: ToolRegistry) -> None:
    """§12.3-1 and -4: the caller is told first, and the manager is whispered
    context before the bridge. Both strings come from the tool so neither is
    left to the model to invent."""
    data = await _transfer(
        registry,
        _Port(),
        {"reason": "explicit_request", "context_note": "wants 20 bags of DAP"},
    )
    assert data["action"] == "transfer"
    assert data["say_first"]
    assert "wants 20 bags of DAP" in str(data["whisper"])


async def test_the_chain_walks_past_a_busy_target(registry: ToolRegistry) -> None:
    """§12.3-2. Stopping at the first busy number would strand the caller on a
    system that has three more people it could have reached."""
    first = await _transfer(registry, _Port(), {"reason": "explicit_request"})
    second = await _transfer(
        registry, _Port(busy={str(first["target_number"])}), {"reason": "explicit_request"}
    )
    assert second["action"] == "transfer"
    assert second["target_number"] != first["target_number"]


async def test_a_call_after_closing_gets_a_commitment_not_a_dead_line(
    registry: ToolRegistry,
) -> None:
    """§12.3-6. There is nobody to bridge to at 22:30, so the caller gets a
    specific commitment instead of a ring-out.

    Its own test because the clock is now pinned everywhere else. Before that,
    this branch was covered only when the suite happened to run after 19:00 --
    and when it did, it broke the tests that meant to cover the other one.
    """
    data = await _transfer(
        registry, _Port(), {"reason": "explicit_request"}, at=AFTER_HOURS
    )
    assert data["action"] == "commit_callback"
    assert data["unavailable_because"] == "centre_closed"
    assert data["centre_hours"]
    assert data["callback_within_hours"]


async def test_a_safety_emergency_ignores_closing_time(registry: ToolRegistry) -> None:
    """§16.1. A closed centre is not a reason to keep someone who has swallowed
    pesticide on the line with a bot."""
    data = await _transfer(
        registry, _Port(), {"reason": "safety_emergency"}, at=AFTER_HOURS
    )
    assert data["action"] == "transfer"


async def test_a_capped_caller_still_gets_a_commitment(registry: ToolRegistry) -> None:
    """§12.3 caps transfers per caller per day. The cap limits dialling, not
    help -- an empty result here would dead-end the call, which §11.4 forbids."""
    data = await _transfer(
        registry, _Port(used=DAILY_TRANSFER_CAP), {"reason": "explicit_request"}
    )
    assert data["action"] == "commit_callback"
    assert data["unavailable_because"] == "daily_cap_reached"
    assert data["callback_within_hours"]
    assert "create_ticket" in list(data["next_steps"])


async def test_a_safety_emergency_ignores_the_cap(registry: ToolRegistry) -> None:
    """§16.1. Someone who has swallowed pesticide is not held behind a rate
    limit designed to stop transfer abuse."""
    data = await _transfer(
        registry, _Port(used=DAILY_TRANSFER_CAP * 3), {"reason": "safety_emergency"}
    )
    assert data["action"] == "transfer"
    assert data["urgency"] == "critical"


async def test_an_exhausted_chain_never_dead_ends(registry: ToolRegistry) -> None:
    first = await _transfer(registry, _Port(), {"reason": "product_complaint"})
    assert first["action"] == "transfer"

    # Now with every reachable number busy.
    class _AllBusy(TransferPort):
        async def is_busy(self, number: str) -> bool:
            return True

        async def transfers_today(self, call_id: str, farmer_id: str | None) -> int:
            return 0

    data = await _transfer(registry, _AllBusy(), {"reason": "product_complaint"})
    assert data["action"] == "commit_callback"
    assert data["unavailable_because"] == "chain_exhausted"
    assert data["centre_phone"] or data["centre_hours"]


# --------------------------------------------------------------------------- #
# send_whatsapp (§14, §18)
# --------------------------------------------------------------------------- #


async def test_whatsapp_needs_current_consent(
    registry: ToolRegistry, app_engine
) -> None:  # type: ignore[no-untyped-def]
    """§18: consent is not permanent and a purchase does not imply it. Revoking
    it must stop the send on the very next turn, not at the next campaign."""
    port = QueueingWhatsAppPort()
    registry.tools["send_whatsapp"] = SendWhatsApp(port)
    data = await _known_farmer(registry)
    context = ToolContext(call_id="c1", farmer_id=str(data["farmer_id"]))

    async with app_engine.begin() as connection:
        await connection.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        await connection.execute(
            text(
                "INSERT INTO consent_records (farmer_id, consent_type, channel, granted_at,"
                " expires_at, evidence) VALUES (:f,'promotional_whatsapp','voice', now(),"
                " now() + interval '30 days', '{}'::jsonb)"
            ),
            {"f": data["farmer_id"]},
        )

    allowed = await registry.execute(
        "send_whatsapp", {"template": "price_list", "params": {"sku": "FRT-DAP-50"}}, context
    )
    assert allowed.ok, allowed.error
    assert allowed.data["sent"] is True
    assert port.queued

    async with app_engine.begin() as connection:
        await connection.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        await connection.execute(
            text(
                "UPDATE consent_records SET revoked_at = now() WHERE farmer_id = :f"
                " AND consent_type = 'promotional_whatsapp'"
            ),
            {"f": data["farmer_id"]},
        )

    refused = await registry.execute(
        "send_whatsapp", {"template": "price_list", "params": {}}, context
    )
    assert refused.ok
    assert refused.data["sent"] is False
    assert refused.data["reason"] == "no_valid_consent"
    assert refused.data["ask"]


async def test_whatsapp_cannot_be_sent_to_an_arbitrary_number(
    registry: ToolRegistry,
) -> None:
    """§17: a phone number never travels through a tool argument, so there is no
    way for the model to address a message to someone who is not the caller."""
    assert "phone" not in SendWhatsApp().parameters["properties"]
    result = await registry.execute(
        "send_whatsapp",
        {"template": "offer", "phone": "9999000001"},
        ToolContext(call_id="c1"),
    )
    assert not result.ok
