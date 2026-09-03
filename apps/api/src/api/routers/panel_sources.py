"""The client's MySQL database as a data source (§15.1).

The platform's database stays PostgreSQL and the client's stays theirs. A
data source is the address of theirs plus which of their columns carry our
fields; a sync pulls it into the catalogue, and the agent answers from the
copy. This module is the panel's side of that: the source rows, a connection
test for a connection that is or is not saved yet, a column preview for the
mapping screen, the button that starts a pull, and the runs it produced.

The password is encrypted under the platform data key on the way in and is
never in a response, a log line or an error on the way out: a row read back
says only that one is set. A pull runs in the background of this process --
the route answers with the running row and the panel polls the runs list --
and a connection that does not answer is reported with its host, port and
user, which is what the operator needs to fix it.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.audit import append_audit
from uaagro_db.models import DataSource, DataSourceRun
from uaagro_db.models.sources import SYNC_SCHEDULES
from uaagro_domain.enums import AuditAction, Role
from uaagro_domain.errors import NotFoundError, ValidationError

from ..security.deps import DbDep, Principal, require_role
from ..services.sources import mysql, sync
from ..services.sources.mapping import parse_mapping
from ._panel import iso

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin/data/sources", tags=["panel"])

#: Runs shown per source, newest first.
RUNS_SHOWN = 20


# --------------------------------------------------------------------------- #
# Shapes
# --------------------------------------------------------------------------- #


class SyncRun(BaseModel):
    id: str
    startedAt: str
    finishedAt: str | None
    status: str
    stores: int
    products: int
    stock: int
    error: str | None


class DataSourceRow(BaseModel):
    id: str
    name: str
    kind: str
    host: str
    port: int
    database: str
    user: str
    tls: bool
    hasPassword: bool
    mapping: dict[str, Any]
    schedule: str
    lastRun: SyncRun | None
    createdAt: str
    updatedAt: str


class TableReport(BaseModel):
    name: str
    rows: int | None


class ConnectionReport(BaseModel):
    ok: bool
    latencyMs: int | None
    serverVersion: str | None
    tables: list[TableReport]
    error: str | None


class ColumnRow(BaseModel):
    name: str
    type: str


class ColumnsReport(BaseModel):
    columns: list[ColumnRow]
    sample: list[dict[str, Any]]


class ConnectionBody(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=3306, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=128)
    user: str = Field(min_length=1, max_length=128)
    password: str = Field(default="", max_length=512)
    tls: bool = False


class CreateSource(ConnectionBody):
    name: str = Field(min_length=2, max_length=120)
    mapping: dict[str, Any] | None = None
    schedule: str = "manual"


class PatchSource(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str | None = Field(default=None, min_length=1, max_length=128)
    user: str | None = Field(default=None, min_length=1, max_length=128)
    #: Replaces the stored password; an empty string removes it.
    password: str | None = Field(default=None, max_length=512)
    tls: bool | None = None
    mapping: dict[str, Any] | None = None
    schedule: str | None = None


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def _run_row(run: DataSourceRun) -> SyncRun:
    return SyncRun(
        id=str(run.id),
        startedAt=run.started_at.isoformat(),
        finishedAt=iso(run.finished_at),
        status=run.status,
        stores=run.stores_written,
        products=run.products_written,
        stock=run.stock_written,
        error=run.error,
    )


def _row(source: DataSource, last_run: DataSourceRun | None) -> DataSourceRow:
    return DataSourceRow(
        id=str(source.id),
        name=source.name,
        kind=source.kind,
        host=source.host,
        port=source.port,
        database=source.database,
        user=source.user,
        tls=bool(source.tls),
        hasPassword=source.password_enc is not None,
        mapping=parse_mapping(source.mapping).as_json(),
        schedule=source.schedule,
        lastRun=_run_row(last_run) if last_run is not None else None,
        createdAt=source.created_at.isoformat(),
        updatedAt=source.updated_at.isoformat(),
    )


async def _source(db: AsyncSession, principal: Principal, source_id: uuid.UUID) -> DataSource:
    source = await db.scalar(
        select(DataSource).where(
            DataSource.id == source_id,
            DataSource.organization_id == principal.organization_id,
        )
    )
    if source is None:
        raise NotFoundError(resource="data source", identifier=str(source_id))
    return source


async def _last_run(db: AsyncSession, source: DataSource) -> DataSourceRun | None:
    if source.last_run_id is None:
        return None
    return await db.get(DataSourceRun, source.last_run_id)


def _schedule(value: str) -> str:
    cleaned = value.strip().lower()
    if cleaned not in SYNC_SCHEDULES:
        raise ValidationError(
            f"{value!r} is not a schedule.",
            remedy="Use manual, hourly or daily.",
            context={"schedule": value},
        )
    return cleaned


def _described(source: DataSource) -> dict[str, Any]:
    """What the audit log records about a source: everything but the secret."""
    mapping = parse_mapping(source.mapping)
    return {
        "name": source.name,
        "host": source.host,
        "port": source.port,
        "database": source.database,
        "user": source.user,
        "tls": source.tls,
        "schedule": source.schedule,
        "has_password": source.password_enc is not None,
        "mapped": [name for name, table_map in mapping.as_json().items() if table_map is not None],
    }


@router.get("", response_model=list[DataSourceRow])
async def list_sources(
    db: DbDep, principal: Annotated[Principal, require_role(Role.OPS_MANAGER)]
) -> list[DataSourceRow]:
    sources = (
        await db.scalars(
            select(DataSource)
            .where(DataSource.organization_id == principal.organization_id)
            .order_by(DataSource.created_at, DataSource.name)
        )
    ).all()
    wanted = [source.last_run_id for source in sources if source.last_run_id is not None]
    runs: dict[uuid.UUID, DataSourceRun] = {}
    if wanted:
        runs = {
            run.id: run
            for run in (
                await db.scalars(select(DataSourceRun).where(DataSourceRun.id.in_(wanted)))
            ).all()
        }
    return [
        _row(source, runs.get(source.last_run_id) if source.last_run_id else None)
        for source in sources
    ]


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


@router.post("", response_model=DataSourceRow, status_code=201)
async def create_source(
    body: CreateSource,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.SUPER_ADMIN)],
) -> DataSourceRow:
    mapping = parse_mapping(body.mapping)
    source = DataSource(
        organization_id=principal.organization_id,
        name=body.name.strip(),
        kind="mysql",
        host=body.host.strip(),
        port=body.port,
        database=body.database.strip(),
        user=body.user.strip(),
        password_enc=sync.encrypt_password(body.password),
        tls=body.tls,
        mapping=mapping.as_json(),
        schedule=_schedule(body.schedule),
        is_active=True,
        created_by=principal.user_id,
    )
    db.add(source)
    await db.flush()
    await db.refresh(source)
    await append_audit(
        db,
        action=AuditAction.CREATE,
        resource_type="data_source",
        resource_id=str(source.id),
        actor_user_id=principal.user_id,
        after=_described(source),
    )
    log.info("source.created", source_id=str(source.id), schedule=source.schedule)
    return _row(source, None)


@router.patch("/{source_id}", response_model=DataSourceRow)
async def update_source(
    source_id: uuid.UUID,
    body: PatchSource,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.SUPER_ADMIN)],
) -> DataSourceRow:
    source = await _source(db, principal, source_id)
    before = _described(source)
    if body.name is not None:
        source.name = body.name.strip()
    if body.host is not None:
        source.host = body.host.strip()
    if body.port is not None:
        source.port = body.port
    if body.database is not None:
        source.database = body.database.strip()
    if body.user is not None:
        source.user = body.user.strip()
    if body.password is not None:
        source.password_enc = sync.encrypt_password(body.password)
    if body.tls is not None:
        source.tls = body.tls
    if body.mapping is not None:
        source.mapping = parse_mapping(body.mapping).as_json()
    if body.schedule is not None:
        source.schedule = _schedule(body.schedule)
    source.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(source)
    await append_audit(
        db,
        action=AuditAction.UPDATE,
        resource_type="data_source",
        resource_id=str(source.id),
        actor_user_id=principal.user_id,
        before=before,
        after={**_described(source), "password_changed": body.password is not None},
    )
    return _row(source, await _last_run(db, source))


@router.delete("/{source_id}", status_code=204)
async def delete_source(
    source_id: uuid.UUID,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.SUPER_ADMIN)],
) -> None:
    source = await _source(db, principal, source_id)
    sync.cancel(source.id)
    await append_audit(
        db,
        action=AuditAction.DELETE,
        resource_type="data_source",
        resource_id=str(source.id),
        actor_user_id=principal.user_id,
        before=_described(source),
    )
    # A real delete, not a flag: the row is the only copy of the credentials,
    # and the runs go with it (the foreign key cascades).
    await db.delete(source)
    await db.flush()
    log.info("source.deleted", source_id=str(source_id))


# --------------------------------------------------------------------------- #
# Talking to the source
# --------------------------------------------------------------------------- #


async def _probe(spec: mysql.ConnectionSpec) -> ConnectionReport:
    """Connect, ask the version, list the tables. A refusal is the report."""
    started = time.perf_counter()
    try:
        async with mysql.connect(spec) as client:
            version = await client.ping()
            latency_ms = round((time.perf_counter() - started) * 1000)
            tables = await client.tables()
    except mysql.SourceUnreachableError as exc:
        log.info("source.unreachable", host=spec.host, port=spec.port)
        return ConnectionReport(
            ok=False, latencyMs=None, serverVersion=None, tables=[], error=str(exc)
        )
    return ConnectionReport(
        ok=True,
        latencyMs=latency_ms,
        serverVersion=version,
        tables=[TableReport(name=table.name, rows=table.rows) for table in tables],
        error=None,
    )


@router.post("/test", response_model=ConnectionReport)
async def test_connection(
    body: ConnectionBody, _: Annotated[Principal, require_role(Role.SUPER_ADMIN)]
) -> ConnectionReport:
    """Try a connection that is not saved. Nothing is stored or logged."""
    return await _probe(
        mysql.ConnectionSpec(
            host=body.host.strip(),
            port=body.port,
            database=body.database.strip(),
            user=body.user.strip(),
            password=body.password or None,
            tls=body.tls,
        )
    )


@router.post("/{source_id}/test", response_model=ConnectionReport)
async def test_source(
    source_id: uuid.UUID,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.SUPER_ADMIN)],
) -> ConnectionReport:
    source = await _source(db, principal, source_id)
    return await _probe(sync.connection_spec(source))


@router.get("/{source_id}/tables/{table}/columns", response_model=ColumnsReport)
async def table_columns(
    source_id: uuid.UUID,
    table: str,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.SUPER_ADMIN)],
) -> ColumnsReport:
    """A table's columns and first rows, so the mapping screen can show them."""
    source = await _source(db, principal, source_id)
    async with mysql.connect(sync.connection_spec(source)) as client:
        columns, sample = await client.columns(table)
    return ColumnsReport(
        columns=[ColumnRow(name=column.name, type=column.type) for column in columns],
        sample=sample,
    )


@router.post("/{source_id}/sync", response_model=SyncRun, status_code=202)
async def start_sync(
    source_id: uuid.UUID,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> SyncRun:
    """Start a pull and answer with its running row; the panel polls the runs.

    A pull already in flight is answered with that run rather than refused:
    a second press of the button while the first is working is a request to
    see it, not to run it twice.
    """
    source = await _source(db, principal, source_id)
    if parse_mapping(source.mapping).is_empty:
        raise ValidationError(
            "Nothing is mapped yet.",
            remedy="Map stores, products or stock on the source, then sync.",
        )
    active = await sync.active_run(db, source)
    if active is not None:
        return _run_row(active)
    run = await sync.begin_run(db, source, started_by=principal.user_id)
    # Committed before the task starts: it opens its own session and must
    # find the row.
    await db.commit()
    sync.start(source.id, run.id)
    log.info("source.sync_started", source_id=str(source.id), run_id=str(run.id))
    return _run_row(run)


@router.get("/{source_id}/runs", response_model=list[SyncRun])
async def list_runs(
    source_id: uuid.UUID,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> list[SyncRun]:
    source = await _source(db, principal, source_id)
    # The panel polls this while a run is in flight, so it is where a run
    # abandoned by a dead process gets its final state.
    await sync.active_run(db, source)
    runs = (
        await db.scalars(
            select(DataSourceRun)
            .where(DataSourceRun.source_id == source.id)
            .order_by(DataSourceRun.started_at.desc())
            .limit(RUNS_SHOWN)
        )
    ).all()
    return [_run_row(run) for run in runs]


__all__ = ("router",)
