"""A read-only asyncio client for the client's MySQL server.

The catalogue is pulled, never pushed, and this is the whole of the
platform's contact with the client's database. Three rules hold in every
method:

- **Only SELECT.** Nothing here can change a row on their side, and the
  MySQL account it uses needs SELECT on the mapped tables and on
  ``information_schema``, nothing more.
- **Identifiers are checked, values are bound.** A table or column name
  reaches a query only after ``information_schema`` has confirmed it exists,
  and in the spelling the server reports. Everything else travels as a
  bound parameter.
- **Nothing waits forever.** Five seconds to connect, handshake included; a
  server that does not answer is reported with its host, port and user --
  never the password, which is kept out of the spec's ``repr`` for the same
  reason.
"""

from __future__ import annotations

import asyncio
import base64
import ssl
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Protocol

import aiomysql  # type: ignore[import-untyped]

from uaagro_domain.errors import NotFoundError, UAAgroError, ValidationError
from uaagro_domain.netsafety import ensure_reachable_target
from uaagro_domain.settings import get_settings

#: How long a connection attempt may take, handshake included.
CONNECT_TIMEOUT_S = 5.0
#: Rows shown on the mapping screen per table.
SAMPLE_ROWS = 5
#: Rows pulled per round trip during a sync.
FETCH_BATCH = 500


@dataclass(frozen=True, slots=True)
class ConnectionSpec:
    """Where to connect. ``password`` is excluded from ``repr`` so the object
    can appear in a log line or a traceback without the secret."""

    host: str
    port: int
    database: str
    user: str
    password: str | None = field(default=None, repr=False)
    tls: bool = False


class SourceUnreachableError(UAAgroError):
    """The client's server refused, timed out or dropped the connection."""

    code = "source_unreachable"
    http_status = 502

    def __init__(self, spec: ConnectionSpec, detail: str) -> None:
        super().__init__(
            f"The source database did not answer: {detail}",
            remedy=(
                f"Check that {spec.host}:{spec.port} accepts connections from this server, "
                f"that user {spec.user!r} may SELECT from {spec.database!r}, and that the "
                "password is current."
            ),
            context={
                "host": spec.host,
                "port": spec.port,
                "database": spec.database,
                "user": spec.user,
            },
        )


class UnknownTableError(NotFoundError):
    """A table the mapping names that the database does not show."""

    def __init__(self, table: str, database: str) -> None:
        super().__init__(resource="table", identifier=table)
        self.remedy = (
            "Pick a table from the list the connection test shows. If it exists, grant "
            f"SELECT on {database}.{table} to the source user."
        )


@dataclass(frozen=True, slots=True)
class TableInfo:
    name: str
    #: The storage engine's estimate; None for a view.
    rows: int | None


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    name: str
    type: str


class SourceClient(Protocol):
    """What the routes and the sync need from a source.

    :class:`MySQLClient` is the one production uses; the tests inject one
    that answers from lists, which is why this is a protocol and not a class.
    """

    async def ping(self) -> str:
        """The server version."""

    async def tables(self) -> list[TableInfo]:
        """Every table and view in the database, with approximate row counts."""

    async def columns(self, table: str) -> tuple[list[ColumnInfo], list[dict[str, Any]]]:
        """A table's columns and its first few rows, for the mapping screen."""

    def fetch(
        self, table: str, columns: Mapping[str, str], *, batch: int = FETCH_BATCH
    ) -> AsyncIterator[dict[str, Any]]:
        """Every row of ``table``, keyed by our field names (``columns`` maps
        our field to their column)."""


def describe(exc: BaseException, spec: ConnectionSpec | None = None) -> str:
    """One line about a driver failure, fit for a run row and a panel."""
    if isinstance(exc, TimeoutError):
        text = f"no answer within {CONNECT_TIMEOUT_S:g} s"
    else:
        text = " ".join(str(exc).split()) or type(exc).__name__
    if spec is not None and spec.password:
        text = text.replace(spec.password, "***")
    return text[:200]


def jsonable(value: Any) -> Any:
    """A sample cell as JSON can carry it."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else str(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, bytes | bytearray):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return base64.b64encode(bytes(value)).decode("ascii")
    return str(value)


def _quote(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


_DRIVER_ERRORS: tuple[type[BaseException], ...] = (aiomysql.Error, OSError, TimeoutError)


class MySQLClient:
    """The production client. Built by :func:`connect`, one per task."""

    def __init__(self, connection: Any, spec: ConnectionSpec) -> None:
        self._connection = connection
        self._spec = spec

    async def ping(self) -> str:
        rows = await self._rows("SELECT VERSION()")
        return str(rows[0][0]) if rows and rows[0] and rows[0][0] is not None else ""

    async def tables(self) -> list[TableInfo]:
        rows = await self._rows(
            "SELECT table_name, table_type, table_rows FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type IN ('BASE TABLE', 'VIEW') "
            "ORDER BY table_name",
            (self._spec.database,),
        )
        return [
            TableInfo(
                name=str(name),
                rows=int(count) if count is not None and kind == "BASE TABLE" else None,
            )
            for name, kind, count in rows
        ]

    async def columns(self, table: str) -> tuple[list[ColumnInfo], list[dict[str, Any]]]:
        columns = await self._columns(table)
        select = ", ".join(_quote(column.name) for column in columns)
        # S608: every identifier here came back from information_schema a
        # moment ago, in the server's own spelling; nothing is request data.
        sql = f"SELECT {select} FROM {_quote(table)} LIMIT {SAMPLE_ROWS}"  # noqa: S608
        rows = await self._rows(sql, dict_rows=True)
        return columns, [{str(key): jsonable(value) for key, value in row.items()} for row in rows]

    async def fetch(
        self, table: str, columns: Mapping[str, str], *, batch: int = FETCH_BATCH
    ) -> AsyncIterator[dict[str, Any]]:
        known = {column.name.lower(): column.name for column in await self._columns(table)}
        missing = sorted({wanted for wanted in columns.values() if wanted.lower() not in known})
        if missing:
            raise ValidationError(
                f"Table {table!r} has no column {missing[0]!r}.",
                remedy="Pick the column from the table's column list and save the mapping again.",
                context={"table": table, "unknown_columns": missing},
            )
        for alias in columns:
            if not alias.isidentifier():
                raise ValidationError(
                    f"{alias!r} is not a field.", remedy="Map only the fields the screen offers."
                )
        select = ", ".join(
            f"{_quote(known[source.lower()])} AS {_quote(alias)}"
            for alias, source in columns.items()
        )
        # S608: the table and every source column were resolved against
        # information_schema above and are used in the server's spelling; the
        # aliases are our own field names.
        sql = f"SELECT {select} FROM {_quote(table)}"  # noqa: S608
        try:
            async with self._connection.cursor(aiomysql.SSDictCursor) as cursor:
                await cursor.execute(sql)
                while True:
                    rows = await cursor.fetchmany(max(1, batch))
                    if not rows:
                        return
                    for row in rows:
                        yield {str(key): value for key, value in row.items()}
        except _DRIVER_ERRORS as exc:
            raise SourceUnreachableError(self._spec, describe(exc, self._spec)) from exc

    async def _columns(self, table: str) -> list[ColumnInfo]:
        rows = await self._rows(
            "SELECT column_name, column_type FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (self._spec.database, table),
        )
        if not rows:
            raise UnknownTableError(table, self._spec.database)
        return [ColumnInfo(name=str(name), type=str(kind)) for name, kind in rows]

    async def _rows(
        self, sql: str, params: Sequence[Any] = (), *, dict_rows: bool = False
    ) -> list[Any]:
        cursor_class = aiomysql.DictCursor if dict_rows else aiomysql.Cursor
        try:
            async with self._connection.cursor(cursor_class) as cursor:
                await cursor.execute(sql, tuple(params) or None)
                fetched = await cursor.fetchall()
        except _DRIVER_ERRORS as exc:
            raise SourceUnreachableError(self._spec, describe(exc, self._spec)) from exc
        return list(fetched or [])


@asynccontextmanager
async def connect(spec: ConnectionSpec) -> AsyncIterator[MySQLClient]:
    """Open a connection for one task, and close it however the task ends.

    ``connect_timeout`` in the driver bounds only the TCP open; the
    ``wait_for`` around it bounds the handshake too, so a server that accepts
    the socket and then says nothing is reported rather than waited on.
    """
    # §17: the address came from the Data page. It may be the client's
    # server on the internet or, if the deployment says so, on a private
    # network -- never this host, another service, or the metadata endpoint.
    # A developer's laptop (and the test suite's embedded server) is the one
    # place a database on 127.0.0.1 is the client's database, so the check
    # applies to deployments only.
    settings = get_settings()
    if settings.is_production:
        ensure_reachable_target(
            spec.host,
            purpose="the data source connection",
            allow_private_networks=settings.sources_allow_private_networks,
        )
    try:
        connection = await asyncio.wait_for(
            aiomysql.connect(
                host=spec.host,
                port=spec.port,
                user=spec.user,
                password=spec.password or "",
                db=spec.database,
                connect_timeout=CONNECT_TIMEOUT_S,
                charset="utf8mb4",
                autocommit=True,
                ssl=ssl.create_default_context() if spec.tls else None,
            ),
            CONNECT_TIMEOUT_S,
        )
    except _DRIVER_ERRORS as exc:
        raise SourceUnreachableError(spec, describe(exc, spec)) from exc
    try:
        yield MySQLClient(connection, spec)
    finally:
        try:
            await asyncio.wait_for(connection.ensure_closed(), 2.0)
        except Exception:
            connection.close()


__all__ = (
    "CONNECT_TIMEOUT_S",
    "FETCH_BATCH",
    "SAMPLE_ROWS",
    "ColumnInfo",
    "ConnectionSpec",
    "MySQLClient",
    "SourceClient",
    "SourceUnreachableError",
    "TableInfo",
    "UnknownTableError",
    "connect",
    "describe",
    "jsonable",
)
