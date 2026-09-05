"""Pulling a source into the catalogue (§15.1).

Stores first, then products, then stock, because a stock row needs both a
centre and a pack size to land on. Each table is an upsert on its natural
key -- ``centres.code``, ``products.sku``, ``(product, pack size)`` for a
variant, ``(centre, variant)`` for stock -- so a run is idempotent, and
nothing is ever deleted: a product the source has stopped listing keeps its
last known stock and is named in the run's report instead.

A run is a row in ``data_source_runs`` from the moment it starts, which is
what the panel polls. The catalogue writes, the finished run and its audit
row commit together; a run that fails rolls its writes back and records why,
in words that never include the connection's password.

Two guards keep runs from overlapping. In this process a source has at most
one task in flight. Across processes -- the API runs the panel's syncs, the
background worker the scheduled ones -- a run still marked ``running`` and
younger than :data:`RUN_STALE_AFTER` stops another from starting; one older
than that was left by a process that died, and is closed as failed when the
next run begins.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.audit import append_audit
from uaagro_db.crypto import get_cipher
from uaagro_db.engine import system_session
from uaagro_db.models import (
    Brand,
    Category,
    Centre,
    DataSource,
    DataSourceRun,
    District,
    Inventory,
    Product,
    ProductVariant,
)
from uaagro_domain.enums import AuditAction, ProductCategory
from uaagro_domain.errors import ConflictError, NotFoundError, UAAgroError, ValidationError

from . import mapping as m
from . import mysql

log = structlog.get_logger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
"""Opens a session with an RLS role bound -- ``uaagro_db.engine.system_session``.

``centres`` and ``inventory`` are written here, and ``inventory`` is
row-level secured: the system role sees and writes org-wide, a session with
no role bound sees nothing and writes nothing.
"""

#: Where background runs open their sessions. The API and the worker leave
#: this as the system session; the tests point it at the embedded database.
open_session: SessionFactory = system_session

#: A run still "running" after this long was abandoned by a dead process.
RUN_STALE_AFTER = timedelta(hours=2)
#: How often the unit of work is flushed while rows stream in.
FLUSH_EVERY = 500
#: How many keys a run's report lists per problem; the total is kept beside.
REPORT_LIMIT = 50
ERROR_LIMIT = 500
#: A schedule means "at least this long since the last successful run".
INTERVALS: dict[str, timedelta] = {"hourly": timedelta(hours=1), "daily": timedelta(days=1)}
#: So an hourly check at :07 does not miss a run that started at :07:30.
SCHEDULE_SLACK = timedelta(minutes=5)
#: The state's default bigha, as the seed and the centres page use it.
DEFAULT_BIGHA_ACRES = Decimal("0.625")
DEFAULT_STATE = "Uttar Pradesh"

INTERRUPTED = "Interrupted: the process stopped before the run finished."


@dataclass(slots=True)
class SyncReport:
    """What one run wrote, and what it could not."""

    stores: int = 0
    products: int = 0
    stock: int = 0
    #: Stock rows naming a sku the catalogue does not have.
    unknown_skus: list[str] = field(default_factory=list)
    #: Stock rows naming a store code the catalogue does not have.
    unknown_stores: list[str] = field(default_factory=list)
    #: Catalogue products the source's product table no longer lists.
    missing_from_source: list[str] = field(default_factory=list)
    #: Rows refused, each with the table, its key and why.
    skipped: list[dict[str, str]] = field(default_factory=list)

    def skip(self, table: str, key: str, reason: str) -> None:
        self.skipped.append({"table": table, "key": key, "reason": reason})

    def as_json(self) -> dict[str, Any]:
        return {
            "unknown_skus": self.unknown_skus[:REPORT_LIMIT],
            "unknown_skus_total": len(self.unknown_skus),
            "unknown_stores": self.unknown_stores[:REPORT_LIMIT],
            "unknown_stores_total": len(self.unknown_stores),
            "missing_from_source": self.missing_from_source[:REPORT_LIMIT],
            "missing_from_source_total": len(self.missing_from_source),
            "skipped": self.skipped[:REPORT_LIMIT],
            "skipped_total": len(self.skipped),
        }


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


def encrypt_password(password: str | None) -> bytes | None:
    """Ciphertext under the platform data key, or None for no password."""
    if not password:
        return None
    return get_cipher().encrypt_field(password.encode("utf-8"))


def connection_spec(source: DataSource) -> mysql.ConnectionSpec:
    """The row as a connection, password decrypted for this task only."""
    password = None
    if source.password_enc is not None:
        password = get_cipher().decrypt_field(bytes(source.password_enc)).decode("utf-8")
    return mysql.ConnectionSpec(
        host=source.host,
        port=source.port,
        database=source.database,
        user=source.user,
        password=password,
        tls=source.tls,
    )


# --------------------------------------------------------------------------- #
# The pull
# --------------------------------------------------------------------------- #


async def sync_source(
    session: AsyncSession,
    source: DataSource,
    client: mysql.SourceClient,
    *,
    started_by: uuid.UUID | None,
) -> SyncReport:
    """Pull every mapped table on ``session``. The caller commits or rolls back."""
    mapping = m.parse_mapping(source.mapping)
    if mapping.is_empty:
        raise ValidationError(
            "Nothing is mapped yet.",
            remedy="Map stores, products or stock on the source, then sync.",
        )
    report = SyncReport()
    if mapping.stores is not None:
        await _sync_stores(session, source, client, mapping.stores, report, started_by)
    if mapping.products is not None:
        await _sync_products(session, source, client, mapping.products, report, started_by)
    if mapping.stock is not None:
        await _sync_stock(session, source, client, mapping.stock, report, started_by)
    await session.flush()
    return report


async def _sync_stores(
    session: AsyncSession,
    source: DataSource,
    client: mysql.SourceClient,
    table_map: m.TableMap,
    report: SyncReport,
    started_by: uuid.UUID | None,
) -> None:
    centres = {
        centre.code: centre
        for centre in (
            await session.scalars(
                select(Centre).where(Centre.organization_id == source.organization_id)
            )
        ).all()
    }
    districts = {
        district.name.lower(): district
        for district in (await session.scalars(select(District))).all()
    }
    seen = 0
    async for row in client.fetch(table_map.table, table_map.columns):
        code = m.key_text(row.get("code"), limit=32)
        name = m.clean_text(row.get("name"), limit=200)
        district_name = m.clean_text(row.get("district"), limit=120)
        if code is None or name is None or district_name is None:
            report.skip("stores", code or "?", "code, name and district are required")
            continue
        district = await _district(session, districts, district_name)
        centre = centres.get(code)
        if centre is None:
            centre = Centre(
                organization_id=source.organization_id,
                code=code,
                name=name,
                district_id=district.id,
                created_by=started_by,
            )
            session.add(centre)
            centres[code] = centre
        else:
            centre.name = name
            centre.district_id = district.id
        _apply_store_fields(centre, row)
        report.stores += 1
        seen += 1
        if seen % FLUSH_EVERY == 0:
            await session.flush()
    await session.flush()


def _apply_store_fields(centre: Centre, row: dict[str, Any]) -> None:
    """The optional store fields, written only when the mapping carries them."""
    if "name_hi" in row:
        centre.name_hi = m.clean_text(row["name_hi"], limit=200)
    if "block" in row:
        centre.block = m.clean_text(row["block"], limit=120)
    if "address" in row:
        centre.address = m.clean_text(row["address"], limit=1000)
    if "pincode" in row:
        centre.pincode = m.parse_pincode(row["pincode"])
    if "latitude" in row:
        centre.latitude = m.parse_coordinate(row["latitude"], limit=90)
    if "longitude" in row:
        centre.longitude = m.parse_coordinate(row["longitude"], limit=180)
    if "phone" in row:
        centre.phone = m.parse_staff_number(row["phone"])
    if "manager_name" in row:
        centre.manager_name = m.clean_text(row["manager_name"], limit=200)
    if "manager_phone" in row:
        centre.transfer_number = m.parse_staff_number(row["manager_phone"])
    opens = m.parse_time_of_day(row.get("open_time"))
    closes = m.parse_time_of_day(row.get("close_time"))
    if opens is not None:
        centre.open_time = opens
    if closes is not None:
        centre.close_time = closes


async def _district(session: AsyncSession, districts: dict[str, District], name: str) -> District:
    """The district by name, created with the state's default bigha when new.

    ``bigha_verified`` stays false, which the dose calculator reads as
    "confirm with the farmer" -- the same rule the centres page applies.
    """
    found = districts.get(name.lower())
    if found is not None:
        return found
    district = District(
        name=name,
        name_hi=name,
        state=DEFAULT_STATE,
        bigha_acres=DEFAULT_BIGHA_ACRES,
        bigha_verified=False,
    )
    session.add(district)
    await session.flush()
    districts[name.lower()] = district
    return district


async def _sync_products(
    session: AsyncSession,
    source: DataSource,
    client: mysql.SourceClient,
    table_map: m.TableMap,
    report: SyncReport,
    started_by: uuid.UUID | None,
) -> None:
    org_id = source.organization_id
    products = {
        product.sku: product
        for product in (
            await session.scalars(select(Product).where(Product.organization_id == org_id))
        ).all()
    }
    brands = {brand.name.lower(): brand for brand in (await session.scalars(select(Brand))).all()}
    categories = {
        category.name: category for category in (await session.scalars(select(Category))).all()
    }
    variants = {
        (variant.product_id, variant.pack_size_value, variant.pack_size_unit): variant
        for variant in (
            await session.scalars(
                select(ProductVariant)
                .join(Product, Product.id == ProductVariant.product_id)
                .where(Product.organization_id == org_id)
            )
        ).all()
    }
    seen: set[str] = set()
    async for row in client.fetch(table_map.table, table_map.columns):
        sku = m.key_text(row.get("sku"), limit=48)
        name = m.clean_text(row.get("name"), limit=240)
        category_text = m.clean_text(row.get("category"), limit=200)
        pack = m.parse_pack_size(row.get("pack_size"))
        mrp = m.parse_money(row.get("mrp"))
        missing = [
            label
            for label, value in (
                ("sku", sku),
                ("name", name),
                ("category", category_text),
                ("pack_size", pack),
                ("mrp", mrp),
            )
            if value is None
        ]
        if missing or sku is None or name is None or category_text is None:
            report.skip("products", sku or "?", f"missing or unreadable: {', '.join(missing)}")
            continue
        assert pack is not None and mrp is not None  # narrowed by ``missing``
        category = m.map_category(category_text)
        if category is None:
            report.skip(
                "products", sku, f"category {category_text!r} is not one the catalogue knows"
            )
            continue
        product_type = m.product_type_for(category, category_text, name)
        registration = m.clean_text(row.get("cib_registration_no"), limit=60)
        product = products.get(sku)
        if (
            m.needs_registration(product_type)
            and registration is None
            and (product is None or product.cib_registration_no is None)
        ):
            report.skip(
                "products",
                sku,
                "crop protection needs a CIB&RC registration number; map cib_registration_no",
            )
            continue
        brand = await _brand(session, brands, row.get("brand")) if "brand" in row else None
        name_hi = m.clean_text(row.get("name_hi"), limit=240) or name
        category_row = await _category(session, categories, category)
        if product is None:
            product = Product(
                organization_id=org_id,
                sku=sku,
                name_en=name,
                name_hi=name_hi,
                brand_id=brand.id if brand is not None else None,
                category_id=category_row.id,
                product_type=product_type,
                cib_registration_no=registration,
                created_by=started_by,
            )
            session.add(product)
            await session.flush()  # the variant key needs the id
            products[sku] = product
        else:
            product.name_en = name
            product.name_hi = name_hi
            product.category_id = category_row.id
            product.product_type = product_type
            if "brand" in row:
                product.brand_id = brand.id if brand is not None else None
            if registration is not None:
                product.cib_registration_no = registration
        if "description" in row:
            product.description_en = m.clean_text(row["description"], limit=4000)

        value, unit = pack
        key = (product.id, value, unit)
        variant = variants.get(key)
        if variant is None:
            variant = ProductVariant(
                product_id=product.id,
                pack_size_value=value,
                pack_size_unit=unit,
                mrp=mrp,
                created_by=started_by,
            )
            session.add(variant)
            variants[key] = variant
        else:
            variant.mrp = mrp
        seen.add(sku)
        report.products += 1
        if report.products % FLUSH_EVERY == 0:
            await session.flush()
    await session.flush()
    report.missing_from_source = sorted(
        sku for sku, product in products.items() if sku not in seen and product.deleted_at is None
    )


async def _brand(session: AsyncSession, brands: dict[str, Brand], value: Any) -> Brand | None:
    name = m.clean_text(value, limit=160)
    if name is None:
        return None
    found = brands.get(name.lower())
    if found is not None:
        return found
    brand = Brand(name=name)
    session.add(brand)
    await session.flush()
    brands[name.lower()] = brand
    return brand


async def _category(
    session: AsyncSession, categories: dict[ProductCategory, Category], category: ProductCategory
) -> Category:
    found = categories.get(category)
    if found is not None:
        return found
    row = Category(name=category, slug=category.value.replace("_", "-"))
    session.add(row)
    await session.flush()
    categories[category] = row
    return row


async def _sync_stock(
    session: AsyncSession,
    source: DataSource,
    client: mysql.SourceClient,
    table_map: m.TableMap,
    report: SyncReport,
    started_by: uuid.UUID | None,
) -> None:
    org_id = source.organization_id
    centres = {
        centre.code: centre
        for centre in (
            await session.scalars(select(Centre).where(Centre.organization_id == org_id))
        ).all()
    }
    by_sku: dict[str, list[ProductVariant]] = {}
    rows = await session.execute(
        select(Product.sku, ProductVariant)
        .join(ProductVariant, ProductVariant.product_id == Product.id)
        .where(Product.organization_id == org_id, Product.deleted_at.is_(None))
    )
    for sku, variant in rows.all():
        by_sku.setdefault(str(sku), []).append(variant)
    inventory = {
        (stock.centre_id, stock.variant_id): stock
        for stock in (await session.scalars(select(Inventory))).all()
    }
    unknown_skus: set[str] = set()
    unknown_stores: set[str] = set()
    async for row in client.fetch(table_map.table, table_map.columns):
        store_code = m.key_text(row.get("store_code"), limit=32)
        sku = m.key_text(row.get("sku"), limit=48)
        qty = m.parse_quantity(row.get("qty"))
        if store_code is None or sku is None or qty is None:
            report.skip(
                "stock", f"{store_code or '?'}/{sku or '?'}", "store_code, sku and qty are required"
            )
            continue
        centre = centres.get(store_code)
        if centre is None:
            unknown_stores.add(store_code)
            continue
        candidates = by_sku.get(sku)
        if not candidates:
            unknown_skus.add(sku)
            continue
        if len(candidates) > 1:
            report.skip(
                "stock",
                f"{store_code}/{sku}",
                "several pack sizes share this sku; the stock row cannot tell them apart",
            )
            continue
        variant = candidates[0]
        price = m.parse_money(row.get("price")) if "price" in row else None
        flag = m.parse_flag(row.get("is_available")) if "is_available" in row else None
        available = flag if flag is not None else qty > 0
        stock = inventory.get((centre.id, variant.id))
        if stock is None:
            stock = Inventory(
                centre_id=centre.id,
                variant_id=variant.id,
                qty_on_hand=qty,
                qty_reserved=0,
                selling_price=price if price is not None else variant.mrp,
                is_available=available,
                updated_by_user_id=started_by,
                created_by=started_by,
            )
            session.add(stock)
            inventory[(centre.id, variant.id)] = stock
        else:
            stock.qty_on_hand = qty
            if price is not None:
                stock.selling_price = price
            stock.is_available = available
            stock.updated_by_user_id = started_by
        # The catalogue refuses a discount above the selling price; a price
        # that dropped below an old offer ends the offer.
        if stock.discount_price is not None and stock.discount_price > stock.selling_price:
            stock.discount_price = None
        report.stock += 1
        if report.stock % FLUSH_EVERY == 0:
            await session.flush()
    await session.flush()
    report.unknown_skus = sorted(unknown_skus)
    report.unknown_stores = sorted(unknown_stores)


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #


async def active_run(session: AsyncSession, source: DataSource) -> DataSourceRun | None:
    """The run in flight for this source, if any, after closing abandoned ones."""
    now = datetime.now(UTC)
    running = (
        await session.scalars(
            select(DataSourceRun).where(
                DataSourceRun.source_id == source.id, DataSourceRun.status == "running"
            )
        )
    ).all()
    live: DataSourceRun | None = None
    for run in running:
        if run.started_at < now - RUN_STALE_AFTER:
            await _finish(session, run, report=SyncReport(), error=INTERRUPTED)
        elif live is None or run.started_at > live.started_at:
            live = run
    return live


async def begin_run(
    session: AsyncSession, source: DataSource, *, started_by: uuid.UUID | None
) -> DataSourceRun:
    """Write the running row and point the source at it. The caller commits."""
    run = DataSourceRun(
        source_id=source.id,
        started_at=datetime.now(UTC),
        status="running",
        stores_written=0,
        products_written=0,
        stock_written=0,
        report={},
        started_by_user_id=started_by,
    )
    session.add(run)
    await session.flush()
    await session.refresh(run)
    source.last_run_id = run.id
    return run


async def execute_run(session_factory: SessionFactory, run_id: uuid.UUID) -> None:
    """Do the pull for a run already on the books, and finish the row.

    Success commits the catalogue, the finished run and its audit row as one
    transaction. Failure rolls the catalogue back and records the reason in
    a transaction of its own. A cancellation -- the service stopping -- is
    recorded the same way, quickly, before it is allowed to propagate.
    """
    spec: mysql.ConnectionSpec | None = None
    try:
        async with session_factory() as session:
            run = await session.get(DataSourceRun, run_id)
            if run is None:
                log.warning("source.run_missing", run_id=str(run_id))
                return
            source = await session.get(DataSource, run.source_id)
            if source is None:
                raise NotFoundError(resource="data source", identifier=str(run.source_id))
            spec = connection_spec(source)
            async with mysql.connect(spec) as client:
                report = await sync_source(
                    session, source, client, started_by=run.started_by_user_id
                )
            await _finish(session, run, report=report, error=None)
        log.info(
            "source.synced",
            run_id=str(run_id),
            stores=report.stores,
            products=report.products,
            stock=report.stock,
            skipped=len(report.skipped),
        )
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(_finish_detached(session_factory, run_id, INTERRUPTED), 5.0)
        raise
    except Exception as exc:
        log.warning("source.sync_failed", run_id=str(run_id), error=type(exc).__name__)
        await _finish_detached(session_factory, run_id, _error_text(exc, spec))


def _error_text(exc: BaseException, spec: mysql.ConnectionSpec | None) -> str:
    text = str(exc) if isinstance(exc, UAAgroError) else f"{type(exc).__name__}: {exc}"
    text = " ".join(text.split())
    if spec is not None and spec.password:
        text = text.replace(spec.password, "***")
    return text[:ERROR_LIMIT]


async def _finish(
    session: AsyncSession, run: DataSourceRun, *, report: SyncReport, error: str | None
) -> None:
    run.finished_at = datetime.now(UTC)
    run.status = "ok" if error is None else "failed"
    run.stores_written = report.stores
    run.products_written = report.products
    run.stock_written = report.stock
    run.report = report.as_json()
    run.error = error
    await append_audit(
        session,
        action=AuditAction.CREATE,
        resource_type="data_source_run",
        resource_id=str(run.id),
        actor_user_id=run.started_by_user_id,
        after={
            "source_id": str(run.source_id),
            "status": run.status,
            "stores": report.stores,
            "products": report.products,
            "stock": report.stock,
            "skipped": len(report.skipped),
            "error": error,
        },
    )


async def _finish_detached(session_factory: SessionFactory, run_id: uuid.UUID, error: str) -> None:
    async with session_factory() as session:
        run = await session.get(DataSourceRun, run_id)
        if run is not None and run.status == "running":
            await _finish(session, run, report=SyncReport(), error=error)


async def run_now(
    session_factory: SessionFactory, source_id: uuid.UUID, *, started_by: uuid.UUID | None
) -> uuid.UUID | None:
    """Start and complete one run in the calling task -- the worker's path.

    Returns the run's id, or None when a run was already in flight.
    """
    async with session_factory() as session:
        source = await session.get(DataSource, source_id)
        if source is None:
            raise NotFoundError(resource="data source", identifier=str(source_id))
        if await active_run(session, source) is not None:
            return None
        run = await begin_run(session, source, started_by=started_by)
        run_id = run.id
    await execute_run(session_factory, run_id)
    return run_id


async def due_sources(
    session_factory: SessionFactory, *, now: datetime | None = None
) -> list[uuid.UUID]:
    """Scheduled sources whose interval has passed since their last good run."""
    moment = now or datetime.now(UTC)
    due: list[uuid.UUID] = []
    async with session_factory() as session:
        sources = (
            await session.scalars(
                select(DataSource)
                .where(DataSource.is_active.is_(True), DataSource.schedule.in_(list(INTERVALS)))
                .order_by(DataSource.created_at)
            )
        ).all()
        if not sources:
            return due
        # The latest good run of every source at once. One query per source
        # was fine for three sources and is the wrong shape for thirty.
        rows = await session.execute(
            select(DataSourceRun.source_id, func.max(DataSourceRun.started_at))
            .where(
                DataSourceRun.source_id.in_([s.id for s in sources]),
                DataSourceRun.status == "ok",
            )
            .group_by(DataSourceRun.source_id)
        )
        latest: dict[uuid.UUID, datetime] = {source_id: last for source_id, last in rows.all()}
        for source in sources:
            last = latest.get(source.id)
            if last is None or moment - last >= INTERVALS[source.schedule] - SCHEDULE_SLACK:
                due.append(source.id)
    return due


# --------------------------------------------------------------------------- #
# Background runs in this process
# --------------------------------------------------------------------------- #

_tasks: dict[uuid.UUID, asyncio.Task[None]] = {}


def in_flight(source_id: uuid.UUID) -> bool:
    task = _tasks.get(source_id)
    return task is not None and not task.done()


def start(source_id: uuid.UUID, run_id: uuid.UUID) -> asyncio.Task[None]:
    """Run ``run_id`` in the background, tracked so shutdown can cancel it.

    The run row must be committed before this is called: the task opens its
    own session and has to find it.
    """
    if in_flight(source_id):
        raise ConflictError(
            message="A sync is already running for this source.",
            remedy="Wait for it to finish; the runs list shows it.",
        )
    task = asyncio.create_task(execute_run(open_session, run_id), name=f"source-sync:{source_id}")
    _tasks[source_id] = task
    task.add_done_callback(lambda finished: _forget(source_id, finished))
    return task


def _forget(source_id: uuid.UUID, task: asyncio.Task[None]) -> None:
    if _tasks.get(source_id) is task:
        del _tasks[source_id]


def cancel(source_id: uuid.UUID) -> None:
    task = _tasks.get(source_id)
    if task is not None:
        task.cancel()


async def drain() -> None:
    """Wait for every sync in flight to finish."""
    pending = [task for task in _tasks.values() if not task.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def shutdown() -> None:
    """Cancel every sync in flight and wait for each to record that it was."""
    pending = [task for task in _tasks.values() if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


__all__ = (
    "INTERVALS",
    "RUN_STALE_AFTER",
    "SessionFactory",
    "SyncReport",
    "active_run",
    "begin_run",
    "cancel",
    "connection_spec",
    "drain",
    "due_sources",
    "encrypt_password",
    "execute_run",
    "in_flight",
    "open_session",
    "run_now",
    "shutdown",
    "start",
    "sync_source",
)
