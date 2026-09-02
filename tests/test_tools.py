"""The §6.3 tool contract and the Tier-1 tools (§9, §16.2).

Run against real Postgres, because these tools exist to be the *only* route to
a fact. §1 N1 makes an unsourced price or dose the worst thing the agent can
say, so testing them against a stand-in would test the wrong thing entirely.

The assertions that carry the most weight are the refusals: a malformed
argument, an unapproved dose, a restricted product. Each is a case where
returning something plausible would be worse than returning nothing.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, ClassVar

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from uaagro_db.seeds import data as seed
from voice_worker.text.lexicon import Lexicon, LexiconEntry
from voice_worker.tools.advisory import CalculateDose, RecommendForCrop
from voice_worker.tools.base import (
    P95_BUDGET_MS,
    Tool,
    ToolContext,
    ToolRegistry,
    validate_against_schema,
)
from voice_worker.tools.catalogue import (
    CheckAvailability,
    FindNearestCentre,
    GetProductDetails,
    SearchProducts,
)
from voice_worker.tools.session import reset_session_factory, set_session_factory

pytestmark = pytest.mark.integration

CONTEXT = ToolContext(call_id="test-call", language="hi-IN")


@pytest.fixture
async def tools(app_engine):  # type: ignore[no-untyped-def]
    """Point the tool layer at the embedded database, as the agent's role.

    Bound to ``voice_agent`` rather than to a staff role: that is what the
    media path actually connects as, and RLS treats the two differently.
    """
    maker = async_sessionmaker(app_engine, expire_on_commit=False)

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            await session.execute(
                text(
                    "SELECT set_config('app.role','voice_agent',true),"
                    "       set_config('app.centre_ids','',true),"
                    "       set_config('app.user_id','',true)"
                )
            )
            yield session
            await session.commit()

    set_session_factory(factory)
    registry = ToolRegistry()
    lexicon = Lexicon.from_entries(
        [LexiconEntry(p.sku, p.name_hi, p.name_en, p.lexicon) for p in seed.PRODUCTS]
    )
    for tool in (
        SearchProducts(lexicon),
        CheckAvailability(),
        GetProductDetails(),
        FindNearestCentre(),
        RecommendForCrop(),
        CalculateDose(),
    ):
        registry.register(tool)
    try:
        yield registry
    finally:
        reset_session_factory()


async def _centre_code(app_engine) -> str:  # type: ignore[no-untyped-def]
    async with app_engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.role','voice_agent',true)")
        )
        code = await connection.scalar(text("SELECT code FROM centres ORDER BY code LIMIT 1"))
    return str(code)


# --------------------------------------------------------------------------- #
# The contract
# --------------------------------------------------------------------------- #


def test_schema_validation_reports_every_problem_at_once() -> None:
    """A model correcting one argument per turn burns turns the caller waits
    through."""
    problems = validate_against_schema(
        {"area": "lots", "unit": "furlong"},
        RecommendForCrop.parameters
        | {
            "properties": {
                "area": {"type": "number"},
                "unit": {"type": "string", "enum": ["bigha", "acre"]},
            },
            "required": ["crop"],
        },
    )
    assert len(problems) >= 2


@pytest.mark.parametrize(
    "args",
    [
        {},                                  # missing required
        {"crop": "x"},                       # too short
        {"crop": "wheat", "surprise": 1},    # unknown argument
        {"crop": 42},                        # wrong type
    ],
)
async def test_a_malformed_argument_is_refused_not_guessed(
    tools: ToolRegistry, args: dict[str, Any]
) -> None:
    """§6.3. Coercing "do bori" into 2 here would put an invented quantity
    behind the audit trail §18 relies on."""
    result = await tools.execute("recommend_for_crop", args, CONTEXT)
    assert not result.ok
    assert not result.grounded
    assert result.error


async def test_an_unknown_tool_does_not_raise_into_the_audio_loop(
    tools: ToolRegistry,
) -> None:
    result = await tools.execute("no_such_tool", {}, CONTEXT)
    assert not result.ok
    assert "no_such_tool" in (result.error or "")


async def test_a_slow_tool_times_out_and_reports_it(tools: ToolRegistry) -> None:
    """§6.3 caps a tool at 400 ms. §11.4 turns a timeout into a hold phrase."""

    class Slow(Tool):
        name = "slow"
        description = "sleeps"
        parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

        async def run(self, args, context):  # type: ignore[no-untyped-def]
            await asyncio.sleep(5)
            return {}

    tools.register(Slow())
    result = await tools.execute("slow", {}, CONTEXT, timeout_ms=60, retries=0)
    assert result.timed_out
    assert not result.ok
    assert not result.grounded


async def test_a_failing_tool_never_leaks_a_stack_trace(tools: ToolRegistry) -> None:
    class Broken(Tool):
        name = "broken"
        description = "raises"
        parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

        async def run(self, args, context):  # type: ignore[no-untyped-def]
            raise RuntimeError("internal detail nobody should hear")

    tools.register(Broken())
    result = await tools.execute("broken", {}, CONTEXT)
    assert not result.ok
    assert "internal detail" not in (result.error or "")


async def test_a_turn_is_capped_at_two_tool_calls(tools: ToolRegistry) -> None:
    """§6.3. Three would mean three latency budgets inside one turn."""
    calls = [
        ("find_nearest_centre", {"district": "Barabanki"}),
        ("find_nearest_centre", {"district": "Sitapur"}),
        ("find_nearest_centre", {"district": "Lucknow"}),
    ]
    results = await tools.execute_many(calls, CONTEXT)
    assert len(results) == 2


# --------------------------------------------------------------------------- #
# Catalogue (§9 Tier 1)
# --------------------------------------------------------------------------- #


async def test_a_spoken_product_name_resolves_through_the_lexicon(
    tools: ToolRegistry,
) -> None:
    result = await tools.execute("search_products", {"query": "डीएपी"}, CONTEXT)
    assert result.ok
    assert result.data["matched_by"] == "lexicon"
    assert result.data["products"][0]["sku"] == "FRT-DAP-50"


async def test_a_product_named_inside_a_question_still_resolves(
    tools: ToolRegistry,
) -> None:
    """The agent hands the tool the farmer's whole sentence. Matching that
    against product names found nothing, and a price question was answered
    with no price -- so the lexicon looks *inside* the utterance."""
    result = await tools.execute("search_products", {"query": "डीएपी का रेट क्या है?"}, CONTEXT)
    assert result.ok
    assert result.data["matched_by"] == "lexicon"
    assert [p["sku"] for p in result.data["products"]] == ["FRT-DAP-50"]

    both = await tools.execute(
        "search_products", {"query": "यूरिया और डीएपी दोनों का रेट बताइए"}, CONTEXT
    )
    assert both.ok
    assert {p["sku"] for p in both.data["products"]} == {"FRT-URE-45", "FRT-DAP-50"}


async def test_an_ambiguous_word_returns_candidates_rather_than_a_choice(
    tools: ToolRegistry,
) -> None:
    """§5.5: "सल्फर" is a soil amendment and a fungicide. Picking one would
    dispense the wrong product."""
    result = await tools.execute("search_products", {"query": "सल्फर"}, CONTEXT)
    assert result.ok
    assert result.data["products"] == [] or result.data.get("ambiguous")


async def test_price_and_stock_come_from_the_database(
    tools: ToolRegistry, app_engine
) -> None:  # type: ignore[no-untyped-def]
    code = await _centre_code(app_engine)
    result = await tools.execute(
        "check_availability", {"sku": "FRT-DAP-50", "centre_code": code}, CONTEXT
    )
    assert result.ok
    assert result.data["sku"] == "FRT-DAP-50"
    assert "available" in result.data
    if result.data["available"]:
        assert "price" in result.data


async def test_a_stock_out_carries_what_the_script_needs(
    tools: ToolRegistry, app_engine
) -> None:  # type: ignore[no-untyped-def]
    """KB §3.4 forbids answering with only "नहीं है": acknowledge, offer an
    alternative, give a restock date, offer a callback. The tool supplies the
    facts for all four so none is improvised."""
    async with app_engine.begin() as connection:
        await connection.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        centre_id = await connection.scalar(text("SELECT id FROM centres ORDER BY code LIMIT 1"))
        await connection.execute(
            text(
                "UPDATE inventory SET is_available = false, qty_on_hand = 0,"
                " restock_eta = now() + interval '5 days'"
                " WHERE centre_id = :c AND variant_id ="
                " (SELECT v.id FROM product_variants v JOIN products p ON p.id = v.product_id"
                "  WHERE p.sku = 'CP-IMD-250')"
            ),
            {"c": centre_id},
        )

    code = await _centre_code(app_engine)
    result = await tools.execute(
        "check_availability", {"sku": "CP-IMD-250", "centre_code": code}, CONTEXT
    )

    assert result.ok
    assert result.data["available"] is False
    assert result.data["restock_eta"]
    assert "alternatives" in result.data
    assert "centre_phone" in result.data

    async with app_engine.begin() as connection:
        await connection.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        await connection.execute(
            text(
                "UPDATE inventory SET is_available = true, qty_on_hand = 100 "
                "WHERE qty_on_hand = 0"
            )
        )


async def test_a_restricted_product_is_flagged_not_hidden(
    tools: ToolRegistry, app_engine
) -> None:  # type: ignore[no-untyped-def]
    """§16.2 routes it to a human. Omitting the row would make the agent say
    the product does not exist, which is false."""
    code = await _centre_code(app_engine)
    result = await tools.execute(
        "check_availability", {"sku": "CP-GLY-1000", "centre_code": code}, CONTEXT
    )
    assert result.ok
    assert result.data.get("restricted") is True


async def test_composition_is_returned_as_named_percentages(
    tools: ToolRegistry,
) -> None:
    """KB §3.3: read each figure as a percentage of the named nutrient, never
    as bare digits."""
    result = await tools.execute("get_product_details", {"sku": "FRT-DAP-50"}, CONTEXT)
    assert result.ok
    composition = result.data["composition"]
    assert composition
    assert {"ingredient", "percent"} <= set(composition[0])


async def test_a_centre_lookup_returns_something_actionable(
    tools: ToolRegistry,
) -> None:
    result = await tools.execute("find_nearest_centre", {"district": "Barabanki"}, CONTEXT)
    assert result.ok
    centre = result.data["centres"][0]
    assert centre["code"]
    assert centre["open"] and centre["close"]


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("search_products", {"query": "डीएपी"}),
        ("get_product_details", {"sku": "FRT-DAP-50"}),
        ("find_nearest_centre", {"district": "Barabanki"}),
        ("recommend_for_crop", {"crop": "wheat"}),
    ],
)
async def test_every_tool_meets_its_p95_budget(
    tools: ToolRegistry, tool: str, args: dict[str, Any]
) -> None:
    """§6.3 gives every tool 150 ms at p95; §21 makes it a Phase 3 gate.

    Warmed first, and deliberately so. The first call of a worker's life pays
    for connection setup and plan compilation -- measured at 300 ms here, which
    is real but is a cold-start problem for §7.5's warm-up to solve, not a
    property of the query. Measuring it as steady-state latency would blame the
    tool for something a pre-warmed pool removes entirely.
    """
    for _ in range(5):
        await tools.execute(tool, args, CONTEXT)

    samples = []
    for _ in range(20):
        result = await tools.execute(tool, args, CONTEXT)
        samples.append(result.latency_ms)
    samples.sort()
    p95 = samples[int(len(samples) * 0.95) - 1]

    assert p95 <= P95_BUDGET_MS, (
        f"{tool} p95 {p95:.1f}ms exceeds the {P95_BUDGET_MS}ms budget in §6.3 "
        f"(p50 {samples[len(samples) // 2]:.1f}ms, max {samples[-1]:.1f}ms)"
    )


# --------------------------------------------------------------------------- #
# Advisory -- the safety control (§9, §16.2)
# --------------------------------------------------------------------------- #


async def test_unapproved_advice_is_unservable_through_the_tool(
    tools: ToolRegistry,
) -> None:
    """The §21 Phase 3 gate item, and the most important assertion here.

    Every seeded recommendation is a draft, so the tool must refuse. Not rank
    them lower, not fall back to a similar crop, not defer to the model --
    refuse, and hand back a message the agent can relay before escalating.
    """
    result = await tools.execute("recommend_for_crop", {"crop": "wheat"}, CONTEXT)
    assert not result.ok
    assert not result.grounded
    assert result.data.get("code") == "unapproved_advisory"
    assert "approv" in (result.error or "").lower()


async def test_an_approved_row_is_served_with_its_precaution(
    tools: ToolRegistry, app_engine
) -> None:  # type: ignore[no-untyped-def]
    """KB §5: a crop-protection recommendation is spoken with its pre-harvest
    interval and at least one precaution, so both must be in the payload."""
    async with app_engine.begin() as connection:
        await connection.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        approver = await connection.scalar(text("SELECT id FROM users LIMIT 1"))
        row_id = await connection.scalar(
            text(
                "SELECT r.id FROM crop_recommendations r JOIN crops c ON c.id = r.crop_id"
                " WHERE c.name_en = 'wheat' AND r.is_crop_protection"
                " AND r.phi_days IS NOT NULL AND r.precaution_note_hi IS NOT NULL LIMIT 1"
            )
        )
        assert row_id is not None, "no seeded wheat spray row carries a PHI"
        await connection.execute(
            text(
                "UPDATE crop_recommendations SET approval_state='approved',"
                " approved_by_user_id=:u, approved_at=now() WHERE id=:i"
            ),
            {"u": approver, "i": row_id},
        )

    try:
        result = await tools.execute("recommend_for_crop", {"crop": "wheat"}, CONTEXT)
        assert result.ok
        recommendation = result.data["recommendations"][0]
        assert recommendation["phi_days"] is not None
        assert recommendation["precaution_hi"]
        assert result.data["must_speak_precaution"]

        # And the dose scales to the farmer's own unit.
        dose = await tools.execute(
            "calculate_dose",
            {"recommendation_id": recommendation["id"], "area": 1, "unit": "bigha"},
            CONTEXT,
        )
        assert dose.ok
        assert dose.data["total_dose"]
        # KB §7: the bigha is district-dependent, so an unconfirmed factor must
        # prompt the agent to ask rather than silently dose against a default.
        assert dose.data.get("confirm_bigha") is True
    finally:
        async with app_engine.begin() as connection:
            await connection.execute(text("SELECT set_config('app.role','ops_manager',true)"))
            await connection.execute(
                text(
                    "UPDATE crop_recommendations SET approval_state='draft',"
                    " approved_by_user_id=NULL, approved_at=NULL WHERE id=:i"
                ),
                {"i": row_id},
            )


async def test_dosing_refuses_an_unknown_recommendation(tools: ToolRegistry) -> None:
    """A fabricated id must not produce a dose."""
    result = await tools.execute(
        "calculate_dose",
        {"recommendation_id": str(uuid.uuid4()), "area": 2, "unit": "acre"},
        CONTEXT,
    )
    assert not result.ok
    assert result.data.get("code") == "unapproved_advisory"


async def test_an_ambiguous_symptom_asks_before_advising(
    tools: ToolRegistry,
) -> None:
    """KB §6: yellowing leaves are nitrogen, water or disease. Guessing and
    then prescribing for the guess is the failure this prevents."""
    result = await tools.execute(
        "recommend_for_crop", {"crop": "wheat", "problem": "पत्ती पीली"}, CONTEXT
    )
    assert result.ok
    assert result.data["needs_clarification"] is True
    assert result.data["recommendations"] == []
    assert result.data["ask"]


async def test_an_unknown_crop_is_reported_not_substituted(
    tools: ToolRegistry,
) -> None:
    result = await tools.execute("recommend_for_crop", {"crop": "dragonfruit"}, CONTEXT)
    assert not result.ok
    assert result.data.get("code") == "not_found"
