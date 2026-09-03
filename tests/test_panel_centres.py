"""Centres: the head office, the map, and what the seed promises (§12.3, §15.1).

Exactly one centre is primary. The helpline answers stock and price for it
when the caller's own centre is unknown, so the flag has to be somewhere at
all times: moving it is one request, dropping it is refused, and the seed
puts it back on the head office only when nobody has chosen one.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from tests.panel_support import act_as
from uaagro_domain.enums import Role

pytestmark = pytest.mark.integration

HEAD_OFFICE = "NKSK-LKO-01"


async def _rows(panel: AsyncClient) -> dict[str, dict[str, Any]]:
    return {row["code"]: row for row in (await panel.get("/admin/centres")).json()}


async def test_rows_carry_the_head_office_and_the_new_fields(panel: AsyncClient) -> None:
    act_as(panel, Role.OPS_MANAGER)
    rows = await _rows(panel)
    primary = [row for row in rows.values() if row["isPrimary"]]
    assert [row["code"] for row in primary] == [HEAD_OFFICE]
    head = rows[HEAD_OFFICE]
    assert head["addressSpoken"]
    assert "soil_testing" in head["services"]
    assert isinstance(head["stockOuts"], int)

    stock = (await panel.get(f"/admin/centres/{head['id']}/stock")).json()
    assert head["stockOuts"] == sum(1 for item in stock if not item["isAvailable"])


async def test_the_head_office_moves_and_is_never_dropped(panel: AsyncClient) -> None:
    act_as(panel, Role.OPS_MANAGER)
    rows = await _rows(panel)
    other = next(row for code, row in rows.items() if code != HEAD_OFFICE and row["isActive"])

    moved = await panel.patch(f"/admin/centres/{other['id']}", json={"isPrimary": True})
    assert moved.status_code == 200, moved.text
    assert moved.json()["isPrimary"] is True
    after = await _rows(panel)
    assert [row["code"] for row in after.values() if row["isPrimary"]] == [other["code"]]

    # There is always one: it can be moved, not switched off or un-flagged.
    dropped = await panel.patch(f"/admin/centres/{other['id']}", json={"isPrimary": False})
    assert dropped.status_code == 422
    closed = await panel.patch(f"/admin/centres/{other['id']}", json={"isActive": False})
    assert closed.status_code == 422

    restored = await panel.patch(
        f"/admin/centres/{rows[HEAD_OFFICE]['id']}", json={"isPrimary": True}
    )
    assert restored.status_code == 200
    final = await _rows(panel)
    assert [row["code"] for row in final.values() if row["isPrimary"]] == [HEAD_OFFICE]
    assert final[other["code"]]["isPrimary"] is False

    act_as(panel, Role.CENTRE_MANAGER)
    assert (
        await panel.patch(f"/admin/centres/{other['id']}", json={"isPrimary": True})
    ).status_code == 403


async def test_the_nearest_centre_is_found_by_distance(panel: AsyncClient) -> None:
    act_as(panel, Role.CENTRE_MANAGER)
    # Mohanlalganj, where the head office is seeded.
    response = await panel.get("/admin/centres/nearest?lat=26.68&lng=80.98")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["centre"]["code"] == HEAD_OFFICE
    assert body["distanceKm"] < 1.0
    assert "stockOuts" in body["centre"]

    far = (await panel.get("/admin/centres/nearest?lat=27.57&lng=80.68")).json()
    assert far["centre"]["code"] != HEAD_OFFICE
    assert far["distanceKm"] >= 0

    assert (await panel.get("/admin/centres/nearest?lat=26.68")).status_code == 422
    assert (await panel.get("/admin/centres/nearest?lat=95&lng=80")).status_code == 422
    act_as(panel, Role.READ_ONLY)
    assert (await panel.get("/admin/centres/nearest?lat=26.68&lng=80.98")).status_code == 403


def test_the_haversine_knows_lucknow_from_sitapur() -> None:
    from api.routers.panel_centres import haversine_km

    assert haversine_km(26.85, 80.95, 26.85, 80.95) == 0.0
    assert 75 < haversine_km(26.85, 80.95, 27.57, 80.68) < 90


async def test_a_centre_is_created_with_its_services_and_spoken_address(
    panel: AsyncClient,
) -> None:
    act_as(panel, Role.OPS_MANAGER)
    created = await panel.post(
        "/admin/centres",
        json={
            "name": "Naveen Khushhali Kisan Sewa Kendra — Gonda",
            "district": "Gonda",
            "openTime": "08:00",
            "closeTime": "19:00",
            "addressSpoken": "  गोंडा में, बस स्टैंड के पास  ",
            "services": ["soil_testing", " soil_testing ", "", "drone_spray"],
        },
    )
    assert created.status_code == 201, created.text
    centre = created.json()
    assert centre["isPrimary"] is False
    assert centre["addressSpoken"] == "गोंडा में, बस स्टैंड के पास"
    assert centre["services"] == ["soil_testing", "drone_spray"]
    assert centre["stockOuts"] == 0

    cleared = await panel.patch(f"/admin/centres/{centre['id']}", json={"services": []})
    assert cleared.json()["services"] == []
    too_long = await panel.patch(f"/admin/centres/{centre['id']}", json={"services": ["x" * 41]})
    assert too_long.status_code == 422


async def test_the_seed_keeps_one_head_office_across_runs(
    embedded_pg: Any, migrator_engine: Any
) -> None:
    """A re-seed on every boot must not create a second primary or move it.

    Row counts are the seed suite's concern (``test_database``), which runs
    on a fresh database; by now other modules have added farmers and centres
    of their own, and what must survive them is the single head office.
    """
    from tests import pgfixture

    output = pgfixture.seed(migrator_dsn=embedded_pg.migrator_dsn, app_dsn=embedded_pg.app_dsn)
    assert "Seed complete." in output
    assert "centres                  created=0" in output

    async with migrator_engine.connect() as connection:
        codes = (
            (
                await connection.execute(
                    text("SELECT code FROM centres WHERE is_primary AND deleted_at IS NULL")
                )
            )
            .scalars()
            .all()
        )
    assert codes == [HEAD_OFFICE]
