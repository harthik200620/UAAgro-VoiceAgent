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

import re
import time
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


@router.get("/storage", response_model=Storage)
async def storage(db: DbDep, _: Annotated[Principal, require_role(Role.OPS_MANAGER)]) -> Storage:
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
    """Live row estimates and on-disk bytes per table, partitions folded in."""
    plain = (
        await db.execute(
            text(
                """
                SELECT c.relname, COALESCE(s.n_live_tup, 0),
                       pg_total_relation_size(c.oid), c.relkind::text
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
                WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
                """
            )
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
                GROUP BY parent.relname
                """
            )
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
    for name, (rows, size, partitioned) in list(out.items()):
        if name in _ESTIMATED and rows > 0:
            continue
        if name not in {table for _, table, _ in _LABELLED}:
            continue
        exact = await db.scalar(text(f'SELECT count(*) FROM "{name}"'))  # noqa: S608
        out[name] = (int(exact or 0), size, partitioned)
    return out


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

    redis_health = ServiceHealth(ok=False)
    try:
        started = time.perf_counter()
        await get_redis().ping()
        redis_health = ServiceHealth(
            ok=True, latencyMs=round((time.perf_counter() - started) * 1000)
        )
    except Exception as exc:
        log.warning("data.redis_unreachable", error=type(exc).__name__)

    storage_health = ServiceHealth(ok=False, endpoint=settings.s3_endpoint)
    try:
        storage_health = ServiceHealth(
            ok=await ObjectStore(settings).ping(), endpoint=settings.s3_endpoint
        )
    except Exception as exc:
        log.warning("data.storage_unreachable", error=type(exc).__name__)

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
        redis=redis_health,
        storage=storage_health,
    )


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
