"""The real MySQL client, against a MySQL-protocol server in this process.

``mysql-mimic`` speaks the wire protocol and answers queries from Python; the
rows come from lists and the schema from a dict, so the client's queries --
the handshake, ``information_schema``, the streaming ``SELECT`` -- are the
ones a real server would see, with no MySQL installed. The identity provider
checks a password, so a wrong one is refused the way a real server refuses.

The last test needs a real server: set ``MYSQL_TEST_URL`` to
``mysql://user:password@host:3306/database`` and it connects, lists the
tables and reads one; without the variable it is skipped.
"""

from __future__ import annotations

import os
import socket
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
from mysql_mimic import IdentityProvider, MysqlServer, NativePasswordAuthPlugin, Session, User
from sqlglot.executor import execute

from api.services.sources import mysql
from uaagro_domain.errors import ValidationError

USER = "reader"
PASSWORD = "s3cret-pw"  # not-a-secret: a test fixture
DATABASE = "shop"

ROWS: dict[str, list[dict[str, Any]]] = {
    "stores": [
        {"id": 1, "code": "S1", "name": "Kendra one", "district": "Sitapur"},
        {"id": 2, "code": "S2", "name": "Kendra two", "district": "Hardoi"},
        {"id": 3, "code": "S3", "name": "Kendra three", "district": "Unnao"},
    ],
    "items": [{"code": "U-45", "title": "Urea 45 kg", "price": "266.50"}],
}
SCHEMA = {
    DATABASE: {
        "stores": {"id": "INT", "code": "VARCHAR(32)", "name": "VARCHAR(200)", "district": "TEXT"},
        "items": {"code": "VARCHAR(48)", "title": "VARCHAR(240)", "price": "DECIMAL(12,2)"},
    }
}


class ListSession(Session):
    """Answers every SELECT from ``ROWS``; the info-schema queries come from ``SCHEMA``."""

    async def query(self, expression: Any, sql: str, attrs: dict[str, str]) -> Any:
        result = execute(expression, tables=ROWS, dialect="mysql")
        return result.rows, result.columns

    async def schema(self) -> dict[str, Any]:
        return SCHEMA


class OneReader(IdentityProvider):
    async def get_user(self, username: str) -> User | None:
        if username != USER:
            return None
        return User(
            name=username,
            auth_string=NativePasswordAuthPlugin.create_auth_string(PASSWORD),
            auth_plugin=NativePasswordAuthPlugin.name,
        )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@pytest.fixture
async def server() -> AsyncIterator[mysql.ConnectionSpec]:
    port = _free_port()
    mimic = MysqlServer(session_factory=ListSession, identity_provider=OneReader())
    await mimic.start_server(host="127.0.0.1", port=port)
    try:
        yield mysql.ConnectionSpec(
            host="127.0.0.1", port=port, database=DATABASE, user=USER, password=PASSWORD
        )
    finally:
        mimic.close()
        await mimic.wait_closed()


async def test_the_client_reads_the_schema_and_streams_rows(server: mysql.ConnectionSpec) -> None:
    async with mysql.connect(server) as client:
        assert await client.ping()

        tables = await client.tables()
        assert [table.name for table in tables] == ["items", "stores"]

        columns, sample = await client.columns("stores")
        assert [column.name for column in columns] == ["id", "code", "name", "district"]
        assert columns[1].type == "VARCHAR(32)"
        assert len(sample) == 3 and sample[0]["code"] == "S1"

        # Our field names come back as keys; their column may be typed in
        # any case; batches smaller than the table still yield every row.
        rows = [
            row
            async for row in client.fetch(
                "stores", {"code": "CODE", "name": "name", "district": "District"}, batch=2
            )
        ]
        assert rows == [
            {"code": "S1", "name": "Kendra one", "district": "Sitapur"},
            {"code": "S2", "name": "Kendra two", "district": "Hardoi"},
            {"code": "S3", "name": "Kendra three", "district": "Unnao"},
        ]


async def test_a_table_or_column_the_database_lacks_is_refused(
    server: mysql.ConnectionSpec,
) -> None:
    async with mysql.connect(server) as client:
        with pytest.raises(mysql.UnknownTableError) as missing_table:
            await client.columns("nope")
        assert missing_table.value.code == "not_found"
        assert f"{DATABASE}.nope" in missing_table.value.remedy

        with pytest.raises(ValidationError) as missing_column:
            async for _ in client.fetch("stores", {"code": "code", "name": "colour"}):
                pass
        assert missing_column.value.context == {"table": "stores", "unknown_columns": ["colour"]}


async def test_refusals_name_the_server_and_never_the_password(
    server: mysql.ConnectionSpec,
) -> None:
    wrong = mysql.ConnectionSpec(
        host=server.host, port=server.port, database=DATABASE, user=USER, password="wrong-pw"
    )
    with pytest.raises(mysql.SourceUnreachableError) as denied:
        async with mysql.connect(wrong):
            pass
    assert denied.value.code == "source_unreachable" and denied.value.http_status == 502
    assert "wrong-pw" not in str(denied.value) and "wrong-pw" not in repr(wrong)
    assert f"{server.host}:{server.port}" in denied.value.remedy
    assert f"{USER!r}" in denied.value.remedy

    closed = mysql.ConnectionSpec(
        host="127.0.0.1", port=1, database=DATABASE, user=USER, password=PASSWORD
    )
    with pytest.raises(mysql.SourceUnreachableError) as refused:
        async with mysql.connect(closed):
            pass
    assert "did not answer" in refused.value.message
    assert PASSWORD not in str(refused.value)


async def test_a_real_mysql_server_when_one_is_configured() -> None:
    url = os.environ.get("MYSQL_TEST_URL")
    if not url:
        pytest.skip("set MYSQL_TEST_URL=mysql://user:password@host:3306/database to run this")
    parts = urlsplit(url)
    spec = mysql.ConnectionSpec(
        host=parts.hostname or "127.0.0.1",
        port=parts.port or 3306,
        database=parts.path.lstrip("/"),
        user=unquote(parts.username or ""),
        password=unquote(parts.password or "") or None,
        tls="tls=1" in (parts.query or ""),
    )
    async with mysql.connect(spec) as client:
        version = await client.ping()
        assert version
        tables = await client.tables()
        assert isinstance(tables, list)
        if tables:
            columns, sample = await client.columns(tables[0].name)
            assert columns
            assert len(sample) <= mysql.SAMPLE_ROWS
