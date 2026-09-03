"""The knowledge page's status, notes and re-indexing (§9, §15.1).

A document the operator adds is a promise the agent will know something.
These tests are about that promise being kept or visibly broken: a typed
note goes through the same queue as a file, a re-index starts the document
over, and when the queue is unreachable the API indexes the document itself
rather than leaving it pending with nobody coming.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from tests.panel_support import act_as
from uaagro_domain.enums import Role
from uaagro_domain.errors import VendorError

pytestmark = pytest.mark.integration

NOTE = "डीएपी का रेट 1250 रुपये प्रति बोरी है। स्टॉक लखनऊ सेंटर पर है।"


@pytest.fixture(autouse=True)
def recorded_queue(quiet_queue: dict[str, list[Any]]) -> dict[str, list[Any]]:
    return quiet_queue


@pytest.fixture(autouse=True)
def local_store(settings_env: Any, tmp_path: Any) -> None:
    settings_env(STORAGE_BACKEND="local", STORAGE_LOCAL_DIR=str(tmp_path))


@pytest.fixture
async def note(panel: AsyncClient) -> AsyncIterator[dict[str, Any]]:
    act_as(panel, Role.OPS_MANAGER)
    response = await panel.post(
        "/admin/knowledge/documents",
        json={"text": NOTE, "title": "DAP price note", "scope": "inbound"},
    )
    assert response.status_code == 202, response.text
    document = response.json()
    yield document
    act_as(panel, Role.OPS_MANAGER)
    await panel.delete(f"/admin/knowledge/documents/{document['id']}")


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #


async def test_status_reports_the_worker_the_documents_and_the_model(
    panel: AsyncClient,
) -> None:
    act_as(panel, Role.AGRONOMIST)
    response = await panel.get("/admin/knowledge/status")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body["worker"]["alive"], bool)
    assert body["worker"]["lastSeenAt"] is None or isinstance(body["worker"]["lastSeenAt"], str)
    assert set(body["documents"]) == {"pending", "indexing", "indexed", "failed"}
    assert body["chunks"] >= body["embedded"] >= 0
    assert body["embeddingModel"]
    assert body["retrievalMs"] is None or isinstance(body["retrievalMs"], int)

    act_as(panel, Role.READ_ONLY)
    assert (await panel.get("/admin/knowledge/status")).status_code == 403
    assert (await panel.get("/admin/knowledge/documents")).status_code == 403


def test_the_heartbeat_key_is_the_workers() -> None:
    """The API reads what the worker writes; two spellings would read nothing."""
    from api.services import jobs
    from worker import tasks

    assert jobs.HEARTBEAT_KEY == tasks.HEARTBEAT_KEY
    assert jobs.HEARTBEAT_TTL_S == tasks.HEARTBEAT_TTL_S == 180


# --------------------------------------------------------------------------- #
# Notes
# --------------------------------------------------------------------------- #


async def test_a_typed_note_is_stored_and_queued_like_a_file(
    panel: AsyncClient, note: dict[str, Any], recorded_queue: dict[str, list[Any]]
) -> None:
    from uaagro_db.storage import knowledge_key, object_store

    assert note["docType"] == "text"
    assert note["ingestStatus"] == "pending"
    assert note["ingestError"] is None
    assert note["scope"] == "inbound"
    assert note["source"] is None
    assert note["sizeBytes"] == len(NOTE.encode("utf-8"))
    assert note["wordCount"] == len(NOTE.split())
    assert note["indexedAt"] is None
    assert ("ingest_document", (note["id"],)) in recorded_queue["enqueued"]

    stored = await object_store().head(knowledge_key(note["id"], "note.txt"))
    assert stored is not None and stored.size == note["sizeBytes"]

    act_as(panel, Role.AGRONOMIST)
    listed = {row["id"]: row for row in (await panel.get("/admin/knowledge/documents")).json()}
    assert listed[note["id"]]["wordCount"] == note["wordCount"]
    for field in ("sizeBytes", "indexedAt", "wordCount"):
        assert field in listed[note["id"]]


async def test_an_empty_note_or_one_without_a_title_is_refused(panel: AsyncClient) -> None:
    act_as(panel, Role.OPS_MANAGER)
    blank = await panel.post("/admin/knowledge/documents", json={"text": "   ", "title": "x"})
    assert blank.status_code == 422
    untitled = await panel.post("/admin/knowledge/documents", json={"text": NOTE})
    assert untitled.status_code == 422
    assert untitled.json()["error"]["code"] == "validation_error"


# --------------------------------------------------------------------------- #
# Re-indexing
# --------------------------------------------------------------------------- #


async def test_reindex_starts_the_document_over(
    panel: AsyncClient,
    note: dict[str, Any],
    recorded_queue: dict[str, list[Any]],
    migrator_engine: Any,
) -> None:
    async with migrator_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE kb_documents SET ingest_status = 'failed', ingest_error = 'scan', "
                "indexed_at = now() WHERE id = :id"
            ),
            {"id": note["id"]},
        )

    act_as(panel, Role.CENTRE_MANAGER)
    assert (await panel.post(f"/admin/knowledge/documents/{note['id']}/reindex")).status_code == 403

    act_as(panel, Role.OPS_MANAGER)
    response = await panel.post(f"/admin/knowledge/documents/{note['id']}/reindex")
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["ingestStatus"] == "pending"
    assert body["ingestError"] is None
    assert body["indexedAt"] is None
    assert recorded_queue["enqueued"].count(("ingest_document", (note["id"],))) == 2

    assert (
        await panel.post(f"/admin/knowledge/documents/{uuid.uuid4()}/reindex")
    ).status_code == 404


# --------------------------------------------------------------------------- #
# The queue is down
# --------------------------------------------------------------------------- #


async def test_when_the_queue_is_unreachable_the_api_indexes_inline(
    panel: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.routers import panel_knowledge
    from api.services import jobs

    async def unreachable(job: str, *args: Any) -> None:
        raise VendorError("The job queue is not reachable.", remedy="Start Redis.")

    indexed: list[uuid.UUID] = []

    async def inline(document_id: uuid.UUID) -> None:
        indexed.append(document_id)

    monkeypatch.setattr(jobs, "enqueue", unreachable)
    monkeypatch.setattr(panel_knowledge, "_ingest_inline", inline)

    act_as(panel, Role.OPS_MANAGER)
    response = await panel.post(
        "/admin/knowledge/documents", json={"text": NOTE, "title": "Offline note"}
    )
    assert response.status_code == 202, response.text
    document = response.json()
    assert document["ingestStatus"] == "pending"
    assert document["ingestError"] == panel_knowledge.INLINE_MARKER
    assert indexed == [uuid.UUID(document["id"])]

    await panel.delete(f"/admin/knowledge/documents/{document['id']}")
