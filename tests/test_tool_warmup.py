"""Statement-cache warm-up (§6.3, §7).

A measurement, not a theory. The first execution of ``lookup_farmer`` in a
fresh process takes 671 ms against 13 ms for the second; opening the connection
is 47 ms of that and the rest is SQLAlchemy compiling the ORM statement and
Postgres planning it. ``HARD_TIMEOUT_MS`` is 400, so before this warm-up
existed the *first farmer to call after a worker restart* heard "that lookup is
taking too long" -- and every caller after them was served in single digits.

It is the kind of defect that never shows up in a suite run in file order,
because test number two pays for test number one.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from uaagro_db.seeds.loader import DEMO_PHONE_PREFIX
from voice_worker.tools import build_registry
from voice_worker.tools.base import HARD_TIMEOUT_MS, P95_BUDGET_MS, ToolContext, ToolRegistry
from voice_worker.tools.session import reset_session_factory, set_session_factory

pytestmark = pytest.mark.integration

ORDERED_FARMER_PHONE = f"{DEMO_PHONE_PREFIX}000000"


@pytest.fixture
async def registry(app_engine) -> AsyncIterator[ToolRegistry]:  # type: ignore[no-untyped-def]
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
    try:
        yield build_registry()
    finally:
        reset_session_factory()


async def test_the_first_lookup_after_a_warm_up_is_within_budget(
    registry: ToolRegistry,
) -> None:
    """The property that matters: the first real caller is served in budget.

    Warming is what makes this true. Without it the same call is a timeout --
    which is what `test_a_cold_lookup_is_the_slow_one` demonstrates.
    """
    await registry.warm()

    started = time.perf_counter()
    result = await registry.execute(
        "lookup_farmer", {"phone": ORDERED_FARMER_PHONE}, ToolContext(call_id="first")
    )
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert result.ok, result.error
    assert result.data["known"] is True
    # Generous against the 150 ms budget because this runs on whatever CI box
    # is going; the failure being guarded against is 671 ms, not 160.
    assert elapsed_ms < HARD_TIMEOUT_MS, (
        f"first lookup took {elapsed_ms:.0f} ms after warming; "
        f"the tool times out at {HARD_TIMEOUT_MS} ms"
    )


async def test_a_warmed_lookup_meets_the_section_6_3_budget(
    registry: ToolRegistry,
) -> None:
    """§6.3 budgets every tool at 150 ms p95.

    Asserted as a median, and separately as "no sample times out". That is a
    deliberate weakening, and the reason is that the obvious version of this
    test does not measure what it claims to.

    §6.3's number is a p95 over production traffic. Twenty samples on whatever
    machine the suite is running cannot estimate one: "p95 of twenty" is the
    nineteenth value, which is the worst sample but one, and on a laptop
    running eight hundred other tests against an embedded Postgres that value
    is a scheduler hiccup. A test built on it passes alone and fails in a full
    run -- which is exactly what happened, and a flaky latency gate gets muted
    rather than investigated.

    So this asserts the two things twenty samples *can* support: the typical
    call is far inside budget, and none of them hit the hard timeout -- which
    is the property a caller experiences, because a tool that times out fails
    their turn. The real p95 comes from the §19 load test and from production
    telemetry, both of which have the sample count for it.
    """
    await registry.warm()
    for _ in range(3):
        await registry.execute(
            "lookup_farmer", {"phone": ORDERED_FARMER_PHONE}, ToolContext(call_id="warm")
        )

    samples = []
    for i in range(20):
        result = await registry.execute(
            "lookup_farmer", {"phone": ORDERED_FARMER_PHONE}, ToolContext(call_id=f"m{i}")
        )
        assert result.ok
        samples.append(result.latency_ms)

    ordered = sorted(samples)
    median = ordered[len(ordered) // 2]
    assert median <= P95_BUDGET_MS, (
        f"median {median:.0f} ms over the {P95_BUDGET_MS} ms budget "
        f"(samples {ordered[0]:.0f}-{ordered[-1]:.0f} ms)"
    )
    assert ordered[-1] < HARD_TIMEOUT_MS, (
        f"a warmed lookup took {ordered[-1]:.0f} ms and would have timed out"
    )


async def test_warming_never_runs_a_write(registry: ToolRegistry) -> None:
    """A warm-up that raised a ticket would be a ticket nobody asked for."""
    warmed = await registry.warm()
    for name in warmed:
        tool = registry.get(name)
        assert tool is not None
        assert tool.read_only, f"{name} writes and must never be warmed"


async def test_the_warm_up_reaches_the_expensive_statement(
    registry: ToolRegistry,
) -> None:
    """``lookup_farmer`` returns early for an unknown caller, so warming it
    through the front door would compile the cheap statement and leave the
    four-subquery history for the first real call to pay for."""
    warmed = await registry.warm()
    assert "lookup_farmer" in warmed

    # A known farmer exercises the history path. If warming had missed it, this
    # first call would carry the compilation cost.
    started = time.perf_counter()
    result = await registry.execute(
        "lookup_farmer", {"phone": ORDERED_FARMER_PHONE}, ToolContext(call_id="hist")
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert result.ok
    assert "previous_calls" in result.data
    assert elapsed_ms < HARD_TIMEOUT_MS, f"history path was still cold: {elapsed_ms:.0f} ms"
