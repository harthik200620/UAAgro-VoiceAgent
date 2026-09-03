"""The data-source routes, on the embedded database with a source made of lists (§15.1).

Built on an app that carries only this router, so the tests hold whatever
state the rest of the API is in. What they assert: who may do what, that
every shape is the contract's, that the password goes in and never comes
out, that a connection can be tried before it is saved, that the mapping
screen gets columns and a sample, and that a sync starts in the background
and shows up in the runs list finished.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.services.sources import mysql
from api.services.sources import sync as source_sync
from tests.test_sources_support import CONNECTION, MAPPING, Connections, cleanup, tables
from uaagro_db.engine import bind_rls_context
from uaagro_db.models import Organization, User
from uaagro_domain.enums import Role
from uaagro_domain.errors import UAAgroError

pytestmark = pytest.mark.integration

BODY: dict[str, Any] = {"name": "Test source (api)", **CONNECTION, "mapping": MAPPING}


@pytest.fixture
async def client(app_engine, migrator_engine, monkeypatch) -> AsyncIterator[AsyncClient]:  # type: ignore[no-untyped-def]
    """This router alone, signed in as a seeded user, syncing on the embedded db."""
    from contextlib import asynccontextmanager

    from api.routers import panel_sources
    from api.security import deps
    from uaagro_db.roles import SYSTEM_ROLE

    app = FastAPI()
    app.include_router(panel_sources.router)

    @app.exception_handler(UAAgroError)
    async def domain_error(_: Request, exc: UAAgroError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": exc.to_dict()})

    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    state: dict[str, Any] = {}
    async with maker() as session:
        state["org_id"] = await session.scalar(select(Organization.id).limit(1))
        users = (await session.execute(select(User.id, User.role))).all()
    state["users"] = {role: user_id for user_id, role in users}

    async def fake_principal() -> deps.Principal:
        return state["principal"]  # type: ignore[no-any-return]

    async def fake_db() -> AsyncIterator[Any]:
        async with maker() as session:
            principal = state["principal"]
            await bind_rls_context(
                session,
                user_id=principal.user_id,
                role=principal.role.value,
                centre_ids=principal.centre_ids,
                org_id=principal.organization_id,
            )
            yield session
            await session.commit()

    @asynccontextmanager
    async def system_sessions():  # type: ignore[no-untyped-def]
        async with maker() as session:
            await bind_rls_context(session, user_id=None, role=SYSTEM_ROLE, centre_ids=())
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr(source_sync, "open_session", system_sessions)
    app.dependency_overrides[deps.current_principal] = fake_principal
    app.dependency_overrides[deps.scoped_db] = fake_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        http.state = state  # type: ignore[attr-defined]
        yield http
    await source_sync.drain()
    await cleanup(migrator_engine)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Connections:
    connections = Connections(tables())
    monkeypatch.setattr(mysql, "connect", connections.connect)
    return connections


def _as(http: AsyncClient, role: Role) -> uuid.UUID:
    from api.security.deps import Principal

    state = http.state  # type: ignore[attr-defined]
    user_id = state["users"][role]
    state["principal"] = Principal(
        user_id=user_id,
        organization_id=state["org_id"],
        role=role,
        centre_ids=(),
        session_id=uuid.uuid4(),
    )
    return user_id  # type: ignore[no-any-return]


async def _create(http: AsyncClient, **overrides: Any) -> dict[str, Any]:
    _as(http, Role.SUPER_ADMIN)
    response = await http.post("/admin/data/sources", json={**BODY, **overrides})
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


async def test_the_owner_writes_sources_and_managers_read_them(
    client: AsyncClient, app_engine: Any
) -> None:
    _as(client, Role.OPS_MANAGER)
    assert (await client.post("/admin/data/sources", json=BODY)).status_code == 403
    _as(client, Role.READ_ONLY)
    assert (await client.get("/admin/data/sources")).status_code == 403
    _as(client, Role.CENTRE_MANAGER)
    assert (await client.get("/admin/data/sources")).status_code == 403

    _as(client, Role.SUPER_ADMIN)
    response = await client.post("/admin/data/sources", json=BODY)
    assert response.status_code == 201, response.text
    source = response.json()
    assert source["kind"] == "mysql"
    assert source["host"] == CONNECTION["host"] and source["port"] == 3306
    assert source["user"] == "reader" and source["database"] == "shop"
    assert source["tls"] is False and source["hasPassword"] is True
    assert source["schedule"] == "manual" and source["lastRun"] is None
    assert set(source["mapping"]) == {"stores", "products", "stock"}
    assert source["mapping"]["products"]["columns"]["sku"] == "code"
    assert source["createdAt"] and source["updatedAt"]
    assert "s3cret" not in response.text and "password" not in source

    _as(client, Role.OPS_MANAGER)
    listed = await client.get("/admin/data/sources")
    assert listed.status_code == 200
    assert any(row["id"] == source["id"] for row in listed.json())
    assert "s3cret" not in listed.text
    for method, path in (
        ("patch", f"/admin/data/sources/{source['id']}"),
        ("delete", f"/admin/data/sources/{source['id']}"),
        ("post", "/admin/data/sources/test"),
        ("post", f"/admin/data/sources/{source['id']}/test"),
        ("get", f"/admin/data/sources/{source['id']}/tables/items/columns"),
    ):
        refused = await client.request(method, path, json={} if method != "get" else None)
        assert refused.status_code == 403, (method, path, refused.text)
    _as(client, Role.CENTRE_MANAGER)
    assert (await client.post(f"/admin/data/sources/{source['id']}/sync")).status_code == 403
    assert (await client.get(f"/admin/data/sources/{source['id']}/runs")).status_code == 403

    # The audit row describes the source and not its secret.
    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role', 'super_admin', true)"))
        after = await session.scalar(
            text(
                "SELECT after FROM audit_log WHERE resource_type = 'data_source' "
                "AND resource_id = :id AND action = 'create'"
            ),
            {"id": source["id"]},
        )
    assert after is not None
    assert after["has_password"] is True and after["mapped"] == ["stores", "products", "stock"]
    assert "s3cret" not in str(after)


async def test_a_mapping_is_checked_field_by_field(client: AsyncClient) -> None:
    _as(client, Role.SUPER_ADMIN)
    response = await client.post(
        "/admin/data/sources",
        json={**BODY, "mapping": {"products": {"table": "items", "columns": {"name": "title"}}}},
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["context"]["table"] == "products"
    assert error["context"]["missing"] == ["sku", "category", "pack_size", "mrp"]

    source = await _create(client)
    unknown = await client.patch(
        f"/admin/data/sources/{source['id']}",
        json={"mapping": {"stock": {"table": "stock", "columns": {"colour": "c"}}}},
    )
    assert unknown.status_code == 422
    assert unknown.json()["error"]["context"] == {"table": "stock", "unknown": ["colour"]}

    schedule = await client.patch(
        f"/admin/data/sources/{source['id']}", json={"schedule": "weekly"}
    )
    assert schedule.status_code == 422
    assert schedule.json()["error"]["code"] == "validation_error"


async def test_a_connection_can_be_tried_before_it_is_saved(
    client: AsyncClient, fake: Connections
) -> None:
    _as(client, Role.SUPER_ADMIN)
    before = len((await client.get("/admin/data/sources")).json())
    response = await client.post("/admin/data/sources/test", json=CONNECTION)
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["ok"] is True and report["error"] is None
    assert report["serverVersion"] == "8.0.36-fake"
    assert isinstance(report["latencyMs"], int)
    assert {row["name"]: row["rows"] for row in report["tables"]} == {
        "items": 6,
        "shops": 2,
        "stock": 5,
    }
    assert fake.specs[-1].password == CONNECTION["password"]
    assert len((await client.get("/admin/data/sources")).json()) == before

    fake.refuse = "(2003, \"Can't connect to MySQL server on 'db.example.test'\")"
    response = await client.post("/admin/data/sources/test", json=CONNECTION)
    assert response.status_code == 200
    report = response.json()
    assert report["ok"] is False and report["tables"] == []
    assert report["latencyMs"] is None and report["serverVersion"] is None
    assert "db.example.test:3306" in report["error"] and "reader" in report["error"]
    assert "s3cret" not in response.text


async def test_the_saved_connection_serves_the_mapping_screen(
    client: AsyncClient, fake: Connections
) -> None:
    source = await _create(client)
    tested = await client.post(f"/admin/data/sources/{source['id']}/test")
    assert tested.status_code == 200 and tested.json()["ok"] is True
    assert fake.specs[-1].password == CONNECTION["password"]

    columns = await client.get(f"/admin/data/sources/{source['id']}/tables/items/columns")
    assert columns.status_code == 200, columns.text
    body = columns.json()
    assert [column["name"] for column in body["columns"]][:3] == ["code", "title", "title_hi"]
    assert all(column["type"] for column in body["columns"])
    assert len(body["sample"]) == 5
    assert body["sample"][0]["code"] == "TST-UREA-45"

    missing = await client.get(f"/admin/data/sources/{source['id']}/tables/nope/columns")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"

    fake.refuse = "(1045, \"Access denied for user 'reader'@'10.0.0.5' (using password: YES)\")"
    down = await client.get(f"/admin/data/sources/{source['id']}/tables/items/columns")
    assert down.status_code == 502
    error = down.json()["error"]
    assert error["code"] == "source_unreachable"
    assert "db.example.test:3306" in error["remedy"] and "'reader'" in error["remedy"]
    assert "s3cret" not in down.text

    unknown = await client.post(f"/admin/data/sources/{uuid.uuid4()}/test")
    assert unknown.status_code == 404


async def test_a_sync_runs_in_the_background_and_its_runs_are_listed(
    client: AsyncClient, fake: Connections
) -> None:
    fake.delay = 0.3  # long enough for the second press to find it running
    source = await _create(client)
    _as(client, Role.OPS_MANAGER)
    started = await client.post(f"/admin/data/sources/{source['id']}/sync")
    assert started.status_code == 202, started.text
    run = started.json()
    assert run["status"] == "running" and run["finishedAt"] is None
    assert (run["stores"], run["products"], run["stock"], run["error"]) == (0, 0, 0, None)

    again = await client.post(f"/admin/data/sources/{source['id']}/sync")
    assert again.status_code == 202 and again.json()["id"] == run["id"]

    await source_sync.drain()
    runs = await client.get(f"/admin/data/sources/{source['id']}/runs")
    assert runs.status_code == 200
    listed = runs.json()
    assert len(listed) == 1 and listed[0]["id"] == run["id"]
    assert listed[0]["status"] == "ok", listed[0]["error"]
    assert (listed[0]["stores"], listed[0]["products"], listed[0]["stock"]) == (2, 3, 3)
    assert listed[0]["finishedAt"] is not None

    rows = (await client.get("/admin/data/sources")).json()
    mine = next(row for row in rows if row["id"] == source["id"])
    assert mine["lastRun"]["id"] == run["id"] and mine["lastRun"]["status"] == "ok"

    _as(client, Role.SUPER_ADMIN)
    second = await client.post(f"/admin/data/sources/{source['id']}/sync")
    assert second.status_code == 202 and second.json()["id"] != run["id"]
    await source_sync.drain()
    newest_first = (await client.get(f"/admin/data/sources/{source['id']}/runs")).json()
    assert [row["id"] for row in newest_first] == [second.json()["id"], run["id"]]

    cleared = await client.patch(
        f"/admin/data/sources/{source['id']}", json={"password": "", "schedule": "hourly"}
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["hasPassword"] is False and cleared.json()["schedule"] == "hourly"
    assert cleared.json()["lastRun"]["id"] == second.json()["id"]

    assert (await client.delete(f"/admin/data/sources/{source['id']}")).status_code == 204
    assert (await client.get(f"/admin/data/sources/{source['id']}/runs")).status_code == 404
    assert (await client.delete(f"/admin/data/sources/{source['id']}")).status_code == 404


async def test_a_source_with_nothing_mapped_cannot_sync(
    client: AsyncClient, fake: Connections
) -> None:
    source = await _create(client, mapping=None)
    assert source["mapping"] == {"stores": None, "products": None, "stock": None}
    _as(client, Role.OPS_MANAGER)
    refused = await client.post(f"/admin/data/sources/{source['id']}/sync")
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "validation_error"
    assert (await client.get(f"/admin/data/sources/{source['id']}/runs")).json() == []
