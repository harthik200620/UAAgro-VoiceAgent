"""Shared pieces for the data-source tests: a source that answers from lists.

Not a test module despite the name; the name keeps it beside the tests it
serves. :class:`FakeClient` implements the same protocol as the MySQL client,
so the sync and the panel routes are exercised end to end without a MySQL
server -- ``test_sources_mysql.py`` covers the real client separately.

The rows are shaped the way a shop's database is shaped, not the way ours
is: column names that mean nothing to us, prices with a rupee sign, a
quantity written as "15 bags", a district in lower case. That is what the
mapping and the parsers exist for.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from api.services.sources import mysql

SHOPS: list[dict[str, Any]] = [
    {
        "id": 1,
        "store_code": "TST-S1",
        "store_name": "Test Kendra Testpur",
        "dist": "Testpur",
        "blk": "Nanpara",
        "pin": "271865",
        "lat": "27.87",
        "lng": "81.50",
        "mgr": "Anil Verma",
        "mgr_phone": "98765 40099",
        "opens": "08:00",
        "closes": "7 pm",
    },
    {
        "id": 2,
        "store_code": "TST-S2",
        "store_name": "Test Kendra Sitapur",
        "dist": "sitapur",
        "blk": None,
        "pin": "bad",
        "lat": None,
        "lng": None,
        "mgr": None,
        "mgr_phone": "not a number",
        "opens": None,
        "closes": None,
    },
]

ITEMS: list[dict[str, Any]] = [
    {
        "code": "TST-UREA-45",
        "title": "Urea 45 kg",
        "title_hi": "यूरिया",
        "cat": "Fertilizer",
        "brand": "IFFCO",
        "pack": "45 kg bag",
        "price": "₹266.50",
        "cib": None,
    },
    {
        "code": "TST-SEED-01",
        "title": "Hybrid Maize Seed",
        "title_hi": None,
        "cat": "Seeds",
        "brand": "Test Seeds Co",
        "pack": "4 kg",
        "price": "1,250",
        "cib": None,
    },
    {
        "code": "TST-INS-01",
        "title": "Imida 17.8 SL",
        "title_hi": "इमिडा",
        "cat": "Insecticide",
        "brand": "Bayer",
        "pack": "100 ml",
        "price": "310",
        "cib": "CIR-TEST-1",
    },
    # Crop protection without a registration number: reported, not written.
    {
        "code": "TST-INS-02",
        "title": "Chloro 20 EC",
        "title_hi": None,
        "cat": "Pesticide",
        "brand": "Test Seeds Co",
        "pack": "1 L",
        "price": "450",
        "cib": None,
    },
    # A category the catalogue has no place for.
    {
        "code": "TST-ODD-01",
        "title": "Mystery item",
        "title_hi": None,
        "cat": "Gadget",
        "brand": None,
        "pack": "1",
        "price": "10",
        "cib": None,
    },
    # No pack size.
    {
        "code": "TST-ODD-02",
        "title": "No pack",
        "title_hi": None,
        "cat": "Seeds",
        "brand": None,
        "pack": "",
        "price": "10",
        "cib": None,
    },
]

STOCK: list[dict[str, Any]] = [
    {"store": "TST-S1", "item": "TST-UREA-45", "on_hand": 120, "rate": "260", "avail": "yes"},
    {"store": "TST-S1", "item": "TST-SEED-01", "on_hand": 0, "rate": None, "avail": None},
    {"store": "TST-S2", "item": "TST-UREA-45", "on_hand": "15 bags", "rate": None, "avail": None},
    {"store": "TST-S1", "item": "NOPE-1", "on_hand": 3, "rate": None, "avail": None},
    {"store": "TST-S9", "item": "TST-UREA-45", "on_hand": 3, "rate": None, "avail": None},
]

MAPPING: dict[str, Any] = {
    "stores": {
        "table": "shops",
        "columns": {
            "code": "store_code",
            "name": "store_name",
            "district": "dist",
            "block": "blk",
            "pincode": "pin",
            "latitude": "lat",
            "longitude": "lng",
            "manager_name": "mgr",
            "manager_phone": "mgr_phone",
            "open_time": "opens",
            "close_time": "closes",
        },
    },
    "products": {
        "table": "items",
        "columns": {
            "sku": "code",
            "name": "title",
            "name_hi": "title_hi",
            "category": "cat",
            "brand": "brand",
            "pack_size": "pack",
            "mrp": "price",
            "cib_registration_no": "cib",
        },
    },
    "stock": {
        "table": "stock",
        "columns": {
            "store_code": "store",
            "sku": "item",
            "qty": "on_hand",
            "price": "rate",
            "is_available": "avail",
        },
    },
}

#: What the fake ``connect`` sees when a test supplies these details.
CONNECTION = {
    "host": "db.example.test",
    "port": 3306,
    "database": "shop",
    "user": "reader",
    "password": "s3cret-pw",  # not-a-secret: a test fixture
}


def tables() -> dict[str, list[dict[str, Any]]]:
    """A fresh copy, so a test may edit rows without touching the next test."""
    return {
        "shops": [dict(row) for row in SHOPS],
        "items": [dict(row) for row in ITEMS],
        "stock": [dict(row) for row in STOCK],
    }


class FakeClient:
    """The source client protocol, answered from lists."""

    def __init__(
        self,
        tables: dict[str, list[dict[str, Any]]],
        *,
        version: str = "8.0.36-fake",
        delay: float = 0.0,
    ) -> None:
        self._tables = tables
        self._version = version
        self._delay = delay

    async def ping(self) -> str:
        return self._version

    async def tables(self) -> list[mysql.TableInfo]:
        return [
            mysql.TableInfo(name=name, rows=len(rows))
            for name, rows in sorted(self._tables.items())
        ]

    async def columns(self, table: str) -> tuple[list[mysql.ColumnInfo], list[dict[str, Any]]]:
        rows = self._table(table)
        names = list(dict.fromkeys(key for row in rows for key in row))
        columns = [mysql.ColumnInfo(name=name, type="varchar(255)") for name in names]
        sample = [{name: mysql.jsonable(row.get(name)) for name in names} for row in rows[:5]]
        return columns, sample

    async def fetch(
        self, table: str, columns: Mapping[str, str], *, batch: int = 500
    ) -> AsyncIterator[dict[str, Any]]:
        rows = self._table(table)
        if self._delay:
            await asyncio.sleep(self._delay)
        for row in rows:
            yield {alias: row.get(source) for alias, source in columns.items()}

    def _table(self, table: str) -> list[dict[str, Any]]:
        try:
            return self._tables[table]
        except KeyError:
            raise mysql.UnknownTableError(table, "shop") from None


@dataclass
class Connections:
    """A stand-in for ``mysql.connect`` that remembers what it was asked.

    ``specs`` lets a test check that the password stored encrypted reached
    the client decrypted; ``refuse`` makes every connection fail the way a
    closed port does.
    """

    tables: dict[str, list[dict[str, Any]]]
    specs: list[mysql.ConnectionSpec] = field(default_factory=list)
    refuse: str | None = None
    delay: float = 0.0

    @asynccontextmanager
    async def connect(self, spec: mysql.ConnectionSpec) -> AsyncIterator[FakeClient]:
        self.specs.append(spec)
        if self.refuse is not None:
            raise mysql.SourceUnreachableError(spec, self.refuse)
        yield FakeClient(self.tables, delay=self.delay)


#: Everything the sync writes for these rows, so a test can remove it.
CLEANUP_SQL = (
    "DELETE FROM inventory WHERE variant_id IN (SELECT v.id FROM product_variants v "
    "JOIN products p ON p.id = v.product_id WHERE p.sku LIKE 'TST-%')",
    "DELETE FROM product_variants WHERE product_id IN "
    "(SELECT id FROM products WHERE sku LIKE 'TST-%')",
    "DELETE FROM products WHERE sku LIKE 'TST-%'",
    "DELETE FROM centres WHERE code LIKE 'TST-%'",
    "DELETE FROM districts WHERE name = 'Testpur'",
    "DELETE FROM brands WHERE name = 'Test Seeds Co'",
    "DELETE FROM data_source_runs WHERE source_id IN "
    "(SELECT id FROM data_sources WHERE name LIKE 'Test source%')",
    "DELETE FROM data_sources WHERE name LIKE 'Test source%'",
)


async def cleanup(migrator_engine: Any) -> None:
    from sqlalchemy import text

    async with migrator_engine.begin() as connection:
        for statement in CLEANUP_SQL:
            await connection.execute(text(statement))
