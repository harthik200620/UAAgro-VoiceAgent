"""The Data page: what is stored, where, and how the panel reaches it (§15.1).

The operator asked to see where things live without being handed a database
client. So this reads the catalogue Postgres keeps about itself -- row
estimates, sizes, indexes, partitions -- and presents it by the names an
operator uses ("Calls", "Transcripts") rather than by table.

The connection is described, never exposed: host, port, database and user,
and whether TLS is on. The password is not in the response because it is not
in this process's memory as a separate thing to show -- it is inside the DSN
the engine was built from, and the DSN is not returned either. Changing the
connection is a deployment change; the panel can test one before it is made.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import urlsplit

import structlog
from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from uaagro_db.engine import get_app_engine
from uaagro_db.storage import ObjectStore
from uaagro_domain.enums import Role
from uaagro_domain.errors import ValidationError
from uaagro_domain.settings import get_defaults, get_settings

from ..security.deps import DbDep, Principal, require_role
from ..security.ratelimit import get_redis

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin/data", tags=["panel"])


class TableRow(BaseModel):
    label: str
    table: str
    rows: int
    bytes: int
    note: str
    indexedBy: str
    partitioned: bool


class Recordings(BaseModel):
    bucket: str
    region: str
    retentionDays: int


class Backups(BaseModel):
    schedule: str
    lastAt: str | None
    lastBytes: int | None


class Storage(BaseModel):
    countedAt: str
    tables: list[TableRow]
    recordings: Recordings
    backups: Backups


class ServiceHealth(BaseModel):
    ok: bool
    latencyMs: int | None = None
    endpoint: str | None = None


class Connection(BaseModel):
    host: str
    port: int
    database: str
    user: str
    tls: bool
    poolSize: int
    poolBusy: int
    latencyMs: int
    serverVersion: str
    rowLevelSecurity: bool


class Health(BaseModel):
    """Whether the two services beside the database are answering.

    Separate from :class:`Connection` because it is the only part of the Data
    page that waits on something outside this process. A store that is down
    answers by timing out, and the page should not: it renders, and this
    arrives after it.
    """

    redis: ServiceHealth
    storage: ServiceHealth


class ConnectionTest(BaseModel):
    dsn: str


class ConnectionTestResult(BaseModel):
    ok: bool
    latencyMs: int | None
    serverVersion: str | None
    error: str | None
    note: str


#: The tables the operator cares about, in the order the page lists them.
#: Everything else in the schema is an implementation detail of these.
_LABELLED: list[tuple[str, str, str]] = [
    ("Calls", "calls", "one partition per month"),
    ("Transcripts", "call_turns", "every turn of every call, partitioned with calls"),
    ("Call events", "call_events", "keypresses, hand-overs, hang-ups"),
    ("Farmers", "farmers", "phone number encrypted at rest, looked up by hash"),
    ("Consent", "consent_records", "who agreed to promotional calls, and when"),
    ("Do-not-call", "dnd_status", "the registry scrub and the farmers who asked"),
    ("Knowledge pieces", "kb_chunks", "text plus a 768-number vector each"),
    ("Documents", "kb_documents", ""),
    ("Centres", "centres", ""),
    ("Products", "products", ""),
    ("Stock and prices", "inventory", "per centre"),
    ("Scripts", "agent_configs", "one live version per direction"),
    ("Campaigns", "campaigns", ""),
    ("Campaign contacts", "campaign_contacts", ""),
    ("Follow-ups", "tickets", ""),
    ("WhatsApp messages", "whatsapp_messages", ""),
    ("Staff", "users", "passwords hashed with Argon2id"),
    ("Audit trail", "audit_log", "every change, hash-chained"),
]


#: How long a storage report stands before it is measured again.
#:
#: On-disk sizes move at the pace of a day's calls, and the page prints the
#: moment it was counted, so a report a minute old is honest and a reload is
#: instant. It is one report for the whole deployment -- the catalogue does
#: not vary by who is looking -- so there is nothing per-user to leak here.
STORAGE_TTL_S = 60.0

_cached_storage: tuple[float, Storage] | None = None


@router.get("/storage", response_model=Storage)
async def storage(db: DbDep, _: Annotated[Principal, require_role(Role.OPS_MANAGER)]) -> Storage:
    global _cached_storage
    now = time.monotonic()
    if _cached_storage is not None and now - _cached_storage[0] < STORAGE_TTL_S:
        return _cached_storage[1]
    report = await _measure(db)
    _cached_storage = (now, report)
    return report


async def _measure(db: Any) -> Storage:
    sizes = await _sizes(db)
    indexes = await _indexes(db)
    settings = get_settings()
    defaults = get_defaults()
    tables = []
    for label, table, note in _LABELLED:
        rows, size, partitioned = sizes.get(table, (0, 0, False))
        tables.append(
            TableRow(
                label=label,
                table=table,
                rows=rows,
                bytes=size,
                note=note,
                indexedBy=indexes.get(table, ""),
                partitioned=partitioned,
            )
        )
    return Storage(
        countedAt=datetime.now(UTC).isoformat(),
        tables=tables,
        recordings=Recordings(
            bucket=settings.s3_bucket,
            region=settings.s3_region,
            retentionDays=defaults.compliance.retention_days_recordings,
        ),
        # Backups are the platform's job (RDS snapshots, or the compose
        # stack's nightly dump); nothing here can vouch for one it did not
        # see, so the schedule is named and the last run is left honest.
        backups=Backups(
            schedule="Nightly at 02:00 (see docs/RUNBOOK.md)", lastAt=None, lastBytes=None
        ),
    )


async def _sizes(db: Any) -> dict[str, tuple[int, int, bool]]:
    """Live row estimates and on-disk bytes per table, partitions folded in.

    Restricted to the tables the page lists. ``pg_total_relation_size`` is not
    a lookup: it stats every file of the relation, its indexes and its TOAST,
    so asking it about every relation in the schema -- which here means the
    labelled tables, every monthly partition of three of them, and everything
    else besides -- was most of the half-second this page took, and on a cold
    file cache it was enough to hit the statement timeout.
    """
    names = [table for _, table, _ in _LABELLED]
    plain = (
        await db.execute(
            text(
                """
                SELECT c.relname, COALESCE(s.n_live_tup, 0),
                       pg_total_relation_size(c.oid), c.relkind::text
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
                WHERE n.nspname = 'public'
                  AND c.relkind IN ('r', 'p')
                  AND c.relname = ANY(:names)
                """
            ),
            {"names": names},
        )
    ).all()
    children = (
        await db.execute(
            text(
                """
                SELECT parent.relname, COALESCE(SUM(s.n_live_tup), 0),
                       COALESCE(SUM(pg_total_relation_size(i.inhrelid)), 0)
                FROM pg_inherits i
                JOIN pg_class parent ON parent.oid = i.inhparent
                LEFT JOIN pg_stat_user_tables s ON s.relid = i.inhrelid
                WHERE parent.relname = ANY(:names)
                GROUP BY parent.relname
                """
            ),
            {"names": names},
        )
    ).all()
    folded = {name: (int(rows), int(size)) for name, rows, size in children}
    out: dict[str, tuple[int, int, bool]] = {}
    for name, rows, size, kind in plain:
        partitioned = kind == "p"
        if partitioned and name in folded:
            rows, size = folded[name]
        out[str(name)] = (int(rows), int(size), partitioned)

    # The statistics collector's live-row estimate is what a big table gets:
    # exact counts over millions of call turns would make this the slowest
    # page in the panel. Everything else is counted exactly -- an estimate of
    # zero on a freshly seeded table reads as "the data is missing", which is
    # a worse answer than a query that takes a millisecond.
    #
    # All of them in one statement, not one round trip each. Eighteen counts
    # of a few hundred rows are microseconds of work and were half a second of
    # waiting, because each one paid for its own round trip through the pool.
    wanted = [
        name
        for _, name, _ in _LABELLED
        if name in out and not (name in _ESTIMATED and out[name][0] > 0)
    ]
    if wanted:
        counts = await _count_all(db, wanted)
        for name, exact in counts.items():
            _, size, partitioned = out[name]
            out[name] = (exact, size, partitioned)
    return out


async def _count_all(db: Any, tables: list[str]) -> dict[str, int]:
    """Exact row counts for several tables in a single round trip.

    The table names are ours -- they come from ``_LABELLED`` by way of the
    catalogue, never from a request -- but they are still checked against that
    list before being written into SQL, so that the day someone makes the list
    configurable this does not quietly become an injection point.
    """
    known = {name for _, name, _ in _LABELLED}
    safe = [name for name in tables if name in known]
    if not safe:
        return {}
    # The suppression is on the interpolation: `safe` is the intersection with
    # _LABELLED above, so every name here is one of ours.
    unions = " UNION ALL ".join(
        f"SELECT '{name}' AS relation, count(*) AS rows FROM \"{name}\""  # noqa: S608
        for name in safe
    )
    rows = (await db.execute(text(unions))).all()
    return {str(name): int(count) for name, count in rows}


#: Tables large enough that an estimate beats a count. Counted exactly only
#: when the estimate says empty.
_ESTIMATED = {"calls", "call_turns", "call_events", "audit_log", "kb_chunks"}


async def _indexes(db: Any) -> dict[str, str]:
    """A readable "indexed by" per table: the columns of its indexes."""
    rows = (
        await db.execute(
            text(
                "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'"
            )
        )
    ).all()
    by_table: dict[str, list[str]] = {}
    for table, name, definition in rows:
        columns = re.search(r"\((.*)\)", definition or "")
        if columns is None:
            continue
        spec = columns.group(1)
        kind = ""
        if " USING hnsw" in (definition or ""):
            kind = "vector "
        elif " USING gin" in (definition or ""):
            kind = "full-text "
        entry = f"{kind}{spec}"
        if "UNIQUE" in (definition or "") and "pkey" not in str(name):
            entry += " unique"
        if "pkey" in str(name):
            continue
        by_table.setdefault(str(table), []).append(entry)
    return {table: " · ".join(dict.fromkeys(entries)) for table, entries in by_table.items()}


@router.get("/connection", response_model=Connection)
async def connection(
    db: DbDep, _: Annotated[Principal, require_role(Role.SUPER_ADMIN)]
) -> Connection:
    settings = get_settings()
    parts = urlsplit(settings.database_url)
    started = time.perf_counter()
    version = str(await db.scalar(text("SHOW server_version")) or "")
    latency_ms = round((time.perf_counter() - started) * 1000)
    pool = get_app_engine().pool
    tls = any(key in (parts.query or "") for key in ("ssl=", "sslmode="))

    return Connection(
        host=parts.hostname or "",
        port=parts.port or 5432,
        database=(parts.path or "/").lstrip("/"),
        user=parts.username or "",
        tls=tls,
        poolSize=int(getattr(pool, "size", lambda: 0)()),
        poolBusy=int(getattr(pool, "checkedout", lambda: 0)()),
        latencyMs=latency_ms,
        serverVersion=version,
        rowLevelSecurity=True,
    )


@router.get("/health", response_model=Health)
async def health(_: Annotated[Principal, require_role(Role.OPS_MANAGER)]) -> Health:
    """Redis and object storage, probed at the same time and given a deadline.

    Both at once rather than one after the other, because two dead services
    should cost one wait rather than two, and neither gets longer than
    ``PROBE_BUDGET_S``: past that the answer an operator needs is "it is not
    answering", which is what a longer wait would eventually say anyway.
    """
    settings = get_settings()
    redis_probe, storage_probe = await asyncio.gather(
        _probe(_ping_redis()),
        _probe(ObjectStore(settings).ping()),
    )
    return Health(
        redis=redis_probe,
        storage=ServiceHealth(
            ok=storage_probe.ok, latencyMs=storage_probe.latencyMs, endpoint=settings.s3_endpoint
        ),
    )


#: A service that has not answered by now is reported as not answering.
PROBE_BUDGET_S = 2.0


async def _ping_redis() -> bool:
    await get_redis().ping()
    return True


async def _probe(check: Coroutine[Any, Any, bool]) -> ServiceHealth:
    """Run one health check, and never let it hold the page."""
    started = time.perf_counter()
    try:
        ok = await asyncio.wait_for(check, PROBE_BUDGET_S)
    except Exception as exc:
        log.warning("data.service_unreachable", error=type(exc).__name__)
        return ServiceHealth(ok=False)
    return ServiceHealth(ok=ok, latencyMs=round((time.perf_counter() - started) * 1000))


@router.post("/connection/test", response_model=ConnectionTestResult)
async def connection_test(
    body: ConnectionTest, _: Annotated[Principal, require_role(Role.SUPER_ADMIN)]
) -> ConnectionTestResult:
    """Try a DSN once. It is not stored, not logged and not applied.

    The running connection changes through the deployment's environment and a
    restart; a database address that could be swapped from a web form would be
    the easiest way to point the helpline at somebody else's data.
    """
    dsn = body.dsn.strip()
    if not dsn.startswith(("postgresql://", "postgres://", "postgresql+asyncpg://")):
        raise ValidationError(
            "That is not a Postgres address.",
            remedy="It starts with postgresql://user:password@host:5432/database",
        )
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
    note = "To switch to it, set DATABASE_URL in the deployment's environment and restart."
    try:
        import asyncpg  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover - declared dependency of uaagro-db
        return ConnectionTestResult(
            ok=False, latencyMs=None, serverVersion=None, error="asyncpg is missing", note=note
        )
    started = time.perf_counter()
    try:
        conn = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:
        return ConnectionTestResult(
            ok=False,
            latencyMs=None,
            serverVersion=None,
            error=type(exc).__name__.replace("Error", " error").strip().capitalize(),
            note=note,
        )
    try:
        version = str(await conn.fetchval("SHOW server_version"))
    finally:
        await conn.close()
    return ConnectionTestResult(
        ok=True,
        latencyMs=round((time.perf_counter() - started) * 1000),
        serverVersion=version,
        error=None,
        note=note,
    )


__all__ = ("router",)
