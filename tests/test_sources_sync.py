"""Pulling a client's database into the catalogue, against a real Postgres (§15.1).

What these prove: a first pull creates centres, districts, brands, products,
pack sizes and stock from rows shaped like a shop's; a second pull updates
in place and adds nothing twice; nothing is ever deleted; rows the catalogue
cannot take are reported by key and reason rather than dropped in silence;
a server that does not answer is a failed run whose error names the host
and never the password; and the scheduled path runs a source when its
interval has passed and not before.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.services.sources import mysql, sync
from tests.test_sources_support import CONNECTION, MAPPING, Connections, cleanup, tables
from uaagro_db.models import (
    AuditLog,
    Centre,
    DataSource,
    DataSourceRun,
    District,
    Inventory,
    Organization,
    Product,
    ProductVariant,
)
from worker.sync import run_scheduled_syncs

pytestmark = pytest.mark.integration


@pytest.fixture
def sessions(app_engine):  # type: ignore[no-untyped-def]
    """A session factory of the shape the sync expects: the system role bound."""
    from contextlib import asynccontextmanager

    from uaagro_db.engine import bind_rls_context
    from uaagro_db.roles import SYSTEM_ROLE

    maker = async_sessionmaker(app_engine, expire_on_commit=False)

    @asynccontextmanager
    async def factory():  # type: ignore[no-untyped-def]
        async with maker() as session:
            await bind_rls_context(session, user_id=None, role=SYSTEM_ROLE, centre_ids=())
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    return factory


@pytest.fixture
async def source_id(sessions, migrator_engine) -> AsyncIterator[uuid.UUID]:  # type: ignore[no-untyped-def]
    """A saved source with the fake's mapping, removed with everything it wrote."""
    async with sessions() as session:
        org_id = await session.scalar(select(Organization.id).limit(1))
        source = DataSource(
            organization_id=org_id,
            name="Test source (sync)",
            kind="mysql",
            host=CONNECTION["host"],
            port=CONNECTION["port"],
            database=CONNECTION["database"],
            user=CONNECTION["user"],
            password_enc=sync.encrypt_password(str(CONNECTION["password"])),
            tls=False,
            mapping=MAPPING,
            schedule="manual",
            is_active=True,
        )
        session.add(source)
        await session.flush()
        created = source.id
    yield created
    await cleanup(migrator_engine)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Connections:
    connections = Connections(tables())
    monkeypatch.setattr(mysql, "connect", connections.connect)
    return connections


async def _run(sessions: Any, run_id: uuid.UUID) -> DataSourceRun:
    async with sessions() as session:
        run = await session.get(DataSourceRun, run_id)
        assert run is not None
        return run


async def _centre(session: Any, code: str) -> tuple[Centre, District]:
    row = (
        await session.execute(
            select(Centre, District)
            .join(District, District.id == Centre.district_id)
            .where(Centre.code == code)
        )
    ).one()
    return row[0], row[1]


async def _variant(session: Any, sku: str) -> ProductVariant:
    return (
        await session.execute(
            select(ProductVariant)
            .join(Product, Product.id == ProductVariant.product_id)
            .where(Product.sku == sku)
        )
    ).scalar_one()


async def test_a_first_pull_builds_the_catalogue_from_the_shops_rows(
    sessions: Any, source_id: uuid.UUID, fake: Connections
) -> None:
    run_id = await sync.run_now(sessions, source_id, started_by=None)
    assert run_id is not None

    # The stored password reached the client decrypted, and nowhere else.
    assert fake.specs and fake.specs[0].password == CONNECTION["password"]
    assert fake.specs[0].host == CONNECTION["host"]
    assert "s3cret" not in repr(fake.specs[0])

    run = await _run(sessions, run_id)
    assert run.status == "ok", run.error
    assert (run.stores_written, run.products_written, run.stock_written) == (2, 3, 3)
    assert run.report["unknown_skus"] == ["NOPE-1"]
    assert run.report["unknown_stores"] == ["TST-S9"]
    skipped = {entry["key"]: entry["reason"] for entry in run.report["skipped"]}
    assert set(skipped) == {"TST-INS-02", "TST-ODD-01", "TST-ODD-02"}
    assert "CIB&RC" in skipped["TST-INS-02"]
    assert "category" in skipped["TST-ODD-01"]
    assert "pack_size" in skipped["TST-ODD-02"]

    async with sessions() as session:
        source = await session.get(DataSource, source_id)
        assert source is not None and source.last_run_id == run_id

        centre, district = await _centre(session, "TST-S1")
        assert centre.name == "Test Kendra Testpur"
        assert district.name == "Testpur" and district.name_hi == "Testpur"
        assert district.bigha_acres == Decimal("0.625") and district.bigha_verified is False
        assert centre.block == "Nanpara" and centre.pincode == "271865"
        assert centre.latitude == Decimal("27.870000")
        assert centre.manager_name == "Anil Verma"
        assert centre.transfer_number == "+919876540099"
        assert centre.open_time.strftime("%H:%M") == "08:00"
        assert centre.close_time.strftime("%H:%M") == "19:00"

        # "sitapur" found the seeded district; the unreadable fields are empty.
        centre, district = await _centre(session, "TST-S2")
        assert district.name == "Sitapur"
        assert centre.pincode is None and centre.transfer_number is None

        urea = (
            await session.execute(select(Product).where(Product.sku == "TST-UREA-45"))
        ).scalar_one()
        assert urea.name_hi == "यूरिया"
        assert urea.product_type == "straight_fertiliser"
        seed = (
            await session.execute(select(Product).where(Product.sku == "TST-SEED-01"))
        ).scalar_one()
        assert seed.name_hi == seed.name_en == "Hybrid Maize Seed"
        assert seed.product_type == "hybrid_seed"
        brand = await session.scalar(
            text("SELECT name FROM brands WHERE id = :b"), {"b": seed.brand_id}
        )
        assert brand == "Test Seeds Co"
        imida = (
            await session.execute(select(Product).where(Product.sku == "TST-INS-01"))
        ).scalar_one()
        assert imida.product_type == "insecticide" and imida.cib_registration_no == "CIR-TEST-1"
        absent = (
            await session.scalars(select(Product.sku).where(Product.sku.like("TST-INS-02%")))
        ).all()
        assert absent == []

        urea_variant = await _variant(session, "TST-UREA-45")
        assert (urea_variant.pack_size_value, urea_variant.pack_size_unit) == (Decimal("45"), "kg")
        assert urea_variant.mrp == Decimal("266.50")

        centre_one, _ = await _centre(session, "TST-S1")
        centre_two, _ = await _centre(session, "TST-S2")
        stock = {
            (row.centre_id, row.variant_id): row
            for row in (await session.scalars(select(Inventory))).all()
        }
        seed_variant = await _variant(session, "TST-SEED-01")
        at_one = stock[(centre_one.id, urea_variant.id)]
        assert (at_one.qty_on_hand, at_one.selling_price, at_one.is_available) == (
            120,
            Decimal("260.00"),
            True,
        )
        seed_at_one = stock[(centre_one.id, seed_variant.id)]
        assert seed_at_one.qty_on_hand == 0 and seed_at_one.is_available is False
        assert seed_at_one.selling_price == Decimal("1250.00")  # no price: the MRP
        at_two = stock[(centre_two.id, urea_variant.id)]
        assert at_two.qty_on_hand == 15 and at_two.selling_price == Decimal("266.50")

        audit = (
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.resource_type == "data_source_run",
                    AuditLog.resource_id == str(run_id),
                )
            )
        ).all()
        assert len(audit) == 1
        assert audit[0].after is not None and audit[0].after["status"] == "ok"


async def test_a_second_pull_updates_in_place_and_deletes_nothing(
    sessions: Any, source_id: uuid.UUID, fake: Connections
) -> None:
    first = await sync.run_now(sessions, source_id, started_by=None)
    assert first is not None

    # The shop sells some urea, reprices it and renames it; a seed line
    # vanishes from the product table but its stock row is still there.
    fake.tables["stock"][0]["on_hand"] = 7
    fake.tables["stock"][0]["rate"] = "₹255"
    fake.tables["items"][0]["title"] = "Urea 45 kg (neem coated)"
    fake.tables["items"] = [row for row in fake.tables["items"] if row["code"] != "TST-SEED-01"]

    second = await sync.run_now(sessions, source_id, started_by=None)
    assert second is not None and second != first
    run = await _run(sessions, second)
    assert run.status == "ok", run.error
    assert (run.stores_written, run.products_written, run.stock_written) == (2, 2, 3)
    # Everything the catalogue has that the source did not list -- on the
    # demo seed, the whole seeded range too -- capped, with the total kept.
    async with sessions() as session:
        listed = {row["code"] for row in fake.tables["items"]}
        expected = sorted(
            sku
            for sku in (
                await session.scalars(select(Product.sku).where(Product.deleted_at.is_(None)))
            ).all()
            if sku not in listed
        )
    assert "TST-SEED-01" in expected
    assert run.report["missing_from_source_total"] == len(expected)
    assert run.report["missing_from_source"] == expected[: sync.REPORT_LIMIT]

    async with sessions() as session:
        skus = (await session.scalars(select(Product.sku).where(Product.sku.like("TST-%")))).all()
        assert sorted(skus) == ["TST-INS-01", "TST-SEED-01", "TST-UREA-45"]
        variants = (
            await session.scalars(
                select(ProductVariant.id)
                .join(Product, Product.id == ProductVariant.product_id)
                .where(Product.sku.like("TST-%"))
            )
        ).all()
        assert len(variants) == 3
        centre_one, _ = await _centre(session, "TST-S1")
        urea = await _variant(session, "TST-UREA-45")
        rows = (
            await session.scalars(
                select(Inventory).where(
                    Inventory.centre_id == centre_one.id, Inventory.variant_id == urea.id
                )
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].qty_on_hand == 7 and rows[0].selling_price == Decimal("255.00")
        renamed = (
            await session.execute(select(Product).where(Product.sku == "TST-UREA-45"))
        ).scalar_one()
        assert renamed.name_en == "Urea 45 kg (neem coated)"
        # The vanished product keeps its last known stock.
        seed = await _variant(session, "TST-SEED-01")
        kept = await session.scalar(
            select(Inventory).where(
                Inventory.centre_id == centre_one.id, Inventory.variant_id == seed.id
            )
        )
        assert kept is not None and kept.qty_on_hand == 0
        codes = (await session.scalars(select(Centre.code).where(Centre.code.like("TST-%")))).all()
        assert sorted(codes) == ["TST-S1", "TST-S2"]


async def test_a_server_that_does_not_answer_is_a_failed_run_without_the_password(
    sessions: Any, source_id: uuid.UUID, fake: Connections
) -> None:
    fake.refuse = "(2003, \"Can't connect to MySQL server on 'db.example.test'\")"
    run_id = await sync.run_now(sessions, source_id, started_by=None)
    assert run_id is not None
    run = await _run(sessions, run_id)
    assert run.status == "failed"
    assert run.finished_at is not None
    assert run.error is not None
    assert "db.example.test" in run.error and "3306" in run.error and "reader" in run.error
    assert "s3cret" not in run.error
    assert (run.stores_written, run.products_written, run.stock_written) == (0, 0, 0)
    async with sessions() as session:
        audit = await session.scalar(
            select(AuditLog).where(
                AuditLog.resource_type == "data_source_run", AuditLog.resource_id == str(run_id)
            )
        )
        assert audit is not None and audit.after is not None
        assert audit.after["status"] == "failed" and "s3cret" not in str(audit.after)
        codes = (await session.scalars(select(Centre.code).where(Centre.code.like("TST-%")))).all()
        assert codes == []


async def test_a_row_the_catalogue_cannot_take_fails_the_run_and_writes_nothing(
    sessions: Any, source_id: uuid.UUID, fake: Connections
) -> None:
    """Halfway failures roll back: the catalogue is whole or untouched."""
    fake.tables["items"].append(
        {
            "code": "TST-UREA-45",  # the same sku, a second pack size, twice over
            "title": "Urea 45 kg",
            "title_hi": None,
            "cat": "Fertilizer",
            "brand": "IFFCO",
            "pack": "45 kg",
            "price": "266.50",
            "cib": None,
        }
    )
    fake.tables["stock"] = [
        {"store": "TST-S1", "item": "TST-UREA-45", "on_hand": 1, "rate": None, "avail": None}
    ]
    run_id = await sync.run_now(sessions, source_id, started_by=None)
    assert run_id is not None
    run = await _run(sessions, run_id)
    # A duplicate product row is an update, not an error; the run is fine.
    assert run.status == "ok", run.error
    assert run.products_written == 4

    # But a mapping with no tables at all is refused before anything runs.
    async with sessions() as session:
        source = await session.get(DataSource, source_id)
        assert source is not None
        source.mapping = {}
    run_id = await sync.run_now(sessions, source_id, started_by=None)
    assert run_id is not None
    run = await _run(sessions, run_id)
    assert run.status == "failed" and run.error is not None
    assert "Nothing is mapped" in run.error


async def test_a_run_left_behind_by_a_dead_process_is_closed_by_the_next(
    sessions: Any, source_id: uuid.UUID, fake: Connections
) -> None:
    async with sessions() as session:
        stale = DataSourceRun(
            source_id=source_id,
            started_at=datetime.now(UTC) - sync.RUN_STALE_AFTER - timedelta(minutes=1),
            status="running",
            report={},
        )
        session.add(stale)
        await session.flush()
        stale_id = stale.id
        fresh = DataSourceRun(
            source_id=source_id, started_at=datetime.now(UTC), status="running", report={}
        )
        session.add(fresh)
        await session.flush()
        fresh_id = fresh.id

    # A run that is genuinely in flight stops another from starting.
    assert await sync.run_now(sessions, source_id, started_by=None) is None
    async with sessions() as session:
        closed = await session.get(DataSourceRun, stale_id)
        assert closed is not None and closed.status == "failed"
        assert closed.error == sync.INTERRUPTED
        live = await session.get(DataSourceRun, fresh_id)
        assert live is not None and live.status == "running"
        live.status = "failed"
        live.finished_at = datetime.now(UTC)

    run_id = await sync.run_now(sessions, source_id, started_by=None)
    assert run_id is not None
    assert (await _run(sessions, run_id)).status == "ok"


async def test_a_stopping_service_records_the_run_it_interrupted(
    sessions: Any, source_id: uuid.UUID, fake: Connections, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API runs syncs as tasks; its shutdown hook cancels them, and the
    run row must say so rather than stay running forever."""
    fake.delay = 5.0
    monkeypatch.setattr(sync, "open_session", sessions)
    async with sessions() as session:
        source = await session.get(DataSource, source_id)
        assert source is not None
        run_id = (await sync.begin_run(session, source, started_by=None)).id
    sync.start(source_id, run_id)
    assert sync.in_flight(source_id)
    await asyncio.sleep(0.2)  # far enough in to have opened its session
    await sync.shutdown()
    assert not sync.in_flight(source_id)
    run = await _run(sessions, run_id)
    assert run.status == "failed" and run.error == sync.INTERRUPTED
    assert run.finished_at is not None
    async with sessions() as session:
        codes = (await session.scalars(select(Centre.code).where(Centre.code.like("TST-%")))).all()
        assert codes == []


async def test_scheduled_sources_run_when_due_and_not_before(
    sessions: Any, source_id: uuid.UUID, fake: Connections
) -> None:
    assert await run_scheduled_syncs({}, session_factory=sessions) == ""

    async with sessions() as session:
        source = await session.get(DataSource, source_id)
        assert source is not None
        source.schedule = "hourly"

    assert await run_scheduled_syncs({}, session_factory=sessions) == "synced 1 of 1 due"
    assert await run_scheduled_syncs({}, session_factory=sessions) == ""

    async with sessions() as session:
        last = (
            await session.scalars(
                select(DataSourceRun)
                .where(DataSourceRun.source_id == source_id)
                .order_by(DataSourceRun.started_at.desc())
            )
        ).first()
        assert last is not None and last.status == "ok" and last.started_by_user_id is None
        last.started_at = datetime.now(UTC) - timedelta(hours=1, minutes=1)

    assert await run_scheduled_syncs({}, session_factory=sessions) == "synced 1 of 1 due"

    # Daily means a day; an hour-old success is not due.
    async with sessions() as session:
        source = await session.get(DataSource, source_id)
        assert source is not None
        source.schedule = "daily"
    assert await run_scheduled_syncs({}, session_factory=sessions) == ""
