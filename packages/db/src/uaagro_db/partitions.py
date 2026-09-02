"""Monthly range partitions for the call tables (§10).

``calls``, ``call_turns``, ``call_events`` and ``dtmf_events`` are the largest
tables in the system by two orders of magnitude at 80 centres with seasonal
peaks (§3). They are range-partitioned on ``started_at``, one partition per
month.

An insert into a range-partitioned table with no matching partition **fails**.
That failure would land in the audio path, so partitions are created ahead of
time: the initial migration creates a window around today, and
``make db-partitions`` (wired to a scheduled job from Phase 6) rolls it forward.
A default partition is deliberately *not* used -- it silently absorbs
out-of-range rows and then blocks the creation of the partition that should
have held them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from .models.call import PARTITIONED_TABLES

#: Months of runway kept ahead of today. A quarter is enough that a failed
#: maintenance job is noticed long before an insert can fail.
MONTHS_AHEAD = 3
#: Months kept behind, so a backfill or a late post-call write still lands.
MONTHS_BEHIND = 3


@dataclass(frozen=True, slots=True)
class Partition:
    """One month's partition of one table."""

    parent: str
    year: int
    month: int

    @property
    def name(self) -> str:
        return f"{self.parent}_p{self.year:04d}_{self.month:02d}"

    @property
    def start(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def end(self) -> date:
        return date(self.year + 1, 1, 1) if self.month == 12 else date(self.year, self.month + 1, 1)

    def create_sql(self) -> str:
        return (
            f"CREATE TABLE IF NOT EXISTS {self.name} "
            f"PARTITION OF {self.parent} "
            f"FOR VALUES FROM ('{self.start.isoformat()}') TO ('{self.end.isoformat()}')"
        )


def month_offsets(anchor: date, *, behind: int, ahead: int) -> list[tuple[int, int]]:
    """``(year, month)`` pairs spanning ``anchor - behind`` to ``anchor + ahead``."""
    months: list[tuple[int, int]] = []
    total = anchor.year * 12 + (anchor.month - 1)
    for offset in range(-behind, ahead + 1):
        value = total + offset
        months.append((value // 12, value % 12 + 1))
    return months


def plan_partitions(
    anchor: date,
    *,
    behind: int = MONTHS_BEHIND,
    ahead: int = MONTHS_AHEAD,
    tables: tuple[str, ...] = PARTITIONED_TABLES,
) -> list[Partition]:
    """Every partition that should exist for the window around ``anchor``."""
    return [
        Partition(parent=table, year=year, month=month)
        for table in tables
        for year, month in month_offsets(anchor, behind=behind, ahead=ahead)
    ]


def partition_ddl(
    anchor: date,
    *,
    behind: int = MONTHS_BEHIND,
    ahead: int = MONTHS_AHEAD,
    tables: tuple[str, ...] = PARTITIONED_TABLES,
) -> list[str]:
    """``CREATE TABLE ... PARTITION OF`` statements, all idempotent."""
    plan = plan_partitions(anchor, behind=behind, ahead=ahead, tables=tables)
    return [p.create_sql() for p in plan]


async def ensure_partitions(
    connection: AsyncConnection,
    *,
    anchor: date,
    behind: int = MONTHS_BEHIND,
    ahead: int = MONTHS_AHEAD,
) -> list[str]:
    """Create any missing partitions. Returns the names that were created.

    Idempotent, so it is safe to run on every deploy and on a schedule.
    """
    created: list[str] = []
    for partition in plan_partitions(anchor, behind=behind, ahead=ahead):
        result = await connection.execute(
            text(
                "SELECT 1 FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relname = :name AND n.nspname = current_schema()"
            ),
            {"name": partition.name},
        )
        if result.first() is None:
            await connection.execute(text(partition.create_sql()))
            created.append(partition.name)
    return created
