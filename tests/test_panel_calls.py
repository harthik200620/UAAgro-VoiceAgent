"""Calls as the panel reads them: the list, one call, its recording, its
summary (§15.1, §17).

A call is written by the media path and enriched by the post-call job; the
panel reads the result. What these check is that the list says which calls
were the browser page's, that the numbers under a transcript are the
transcript's, that a recording is streamed from the store and never named,
and that "summarise again" runs the job's summariser and nothing else.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.panel_support import act_as
from uaagro_domain.enums import (
    CallDirection,
    CallOutcome,
    CallStatus,
    Role,
    TelephonyProvider,
    TurnRole,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def calls(app_engine) -> AsyncIterator[dict[str, Any]]:  # type: ignore[no-untyped-def]
    """A finished helpline call with its transcript, and a browser test call."""
    from uaagro_db.models import Call, CallTurn

    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    started = datetime.now(UTC) - timedelta(minutes=5)

    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        org = await session.scalar(text("SELECT id FROM organizations LIMIT 1"))
        centre = await session.scalar(text("SELECT id FROM centres ORDER BY code LIMIT 1"))
        farmer = await session.scalar(text("SELECT id FROM farmers LIMIT 1"))
        real = Call(
            organization_id=org,
            call_ref=f"CALL-{uuid.uuid4().hex[:8].upper()}",
            direction=CallDirection.INBOUND,
            provider=TelephonyProvider.EXOTEL,
            centre_id=centre,
            farmer_id=farmer,
            started_at=started,
            answered_at=started,
            ended_at=started + timedelta(seconds=40),
            duration_seconds=40,
            status=CallStatus.COMPLETED,
            outcome=CallOutcome.RESOLVED,
            language_final="hi-IN",
            latency_stats={"first_reply_ms": 850},
            intents=["price_enquiry"],
            cost_total_inr=Decimal("3.5"),
        )
        test = Call(
            organization_id=org,
            call_ref=f"SIM-{uuid.uuid4().hex[:8].upper()}",
            direction=CallDirection.INBOUND,
            provider=TelephonyProvider.SIMULATOR,
            centre_id=centre,
            started_at=started,
            answered_at=started,
            ended_at=started + timedelta(seconds=10),
            duration_seconds=10,
            status=CallStatus.COMPLETED,
            outcome=CallOutcome.RESOLVED,
            language_final="hi-IN",
        )
        session.add_all([real, test])
        await session.flush()

        def turn(index: int, role: TurnRole, said: str, **fields: Any) -> CallTurn:
            return CallTurn(
                call_id=real.id,
                started_at=started,
                centre_id=centre,
                turn_index=index,
                role=role,
                text_original=said,
                audio_offset_ms=1000 * (index + 1),
                **fields,
            )

        session.add_all(
            [
                turn(0, TurnRole.USER, "डीएपी का रेट क्या है"),
                turn(
                    1,
                    TurnRole.ASSISTANT,
                    "डीएपी 1250 रुपये बोरी है।",
                    latency_ms={"totalMs": 850, "fromCache": False, "llmMs": 400},
                    tool_calls={"calls": [{"name": "check_availability", "ms": 120}]},
                    llm_model="claude-haiku-4-5",
                ),
                turn(2, TurnRole.USER, "धन्यवाद"),
                turn(
                    3,
                    TurnRole.ASSISTANT,
                    "नमस्ते।",
                    latency_ms={"totalMs": 120, "fromCache": True},
                ),
            ]
        )
        await session.commit()
        ids = {"real": real.id, "test": test.id, "ref": real.call_ref}

    yield ids

    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        await session.execute(text("DELETE FROM call_turns WHERE call_id = :c"), {"c": ids["real"]})
        await session.execute(
            text("DELETE FROM calls WHERE id = ANY(:ids)"), {"ids": [ids["real"], ids["test"]]}
        )
        await session.commit()


# --------------------------------------------------------------------------- #
# The list
# --------------------------------------------------------------------------- #


async def test_the_list_marks_test_calls_and_can_leave_them_out(
    panel: AsyncClient, calls: dict[str, Any]
) -> None:
    act_as(panel, Role.OPS_MANAGER)
    body = (await panel.get("/admin/calls?limit=100")).json()
    rows = {row["id"]: row for row in body["rows"]}
    real = rows[str(calls["real"])]
    assert real["isTest"] is False
    assert real["intents"] == ["price_enquiry"]
    assert real["intent"] == "price_enquiry"
    assert real["summaryHi"] is None
    assert real["transferred"] is False
    assert real["recordingAvailable"] is False
    assert real["firstReplyMs"] == 850
    assert real["callerLast4"] is not None and len(real["callerLast4"]) == 4
    assert rows[str(calls["test"])]["isTest"] is True

    only_tests = (await panel.get("/admin/calls?is_test=true&limit=100")).json()
    assert only_tests["rows"], "the browser call should be listed"
    assert all(row["isTest"] for row in only_tests["rows"])
    assert str(calls["test"]) in {row["id"] for row in only_tests["rows"]}

    no_tests = (await panel.get("/admin/calls?is_test=false&limit=100")).json()
    assert not any(row["isTest"] for row in no_tests["rows"])
    assert str(calls["real"]) in {row["id"] for row in no_tests["rows"]}


async def test_the_list_never_carries_a_phone_number_and_pages(
    panel: AsyncClient, calls: dict[str, Any]
) -> None:
    act_as(panel, Role.OPS_MANAGER)
    response = await panel.get("/admin/calls?limit=5&offset=0")
    assert response.status_code == 200
    assert len(response.json()["rows"]) <= 5
    for forbidden in ("phone_enc", "from_number_hash", "phone_hash"):
        assert forbidden not in response.text
    digits = "".join(ch if ch.isdigit() else " " for ch in response.text).split()
    assert not any(len(run) >= 10 and run.isdigit() for run in digits)
    # The contract caps a page at 200; an unbounded page is a denial of service.
    assert (await panel.get("/admin/calls?limit=201")).status_code == 422


async def test_the_list_and_the_detail_need_the_same_role(
    panel: AsyncClient, calls: dict[str, Any]
) -> None:
    act_as(panel, Role.AGRONOMIST)
    assert (await panel.get("/admin/calls")).status_code == 403
    assert (await panel.get(f"/admin/calls/{calls['real']}")).status_code == 403
    act_as(panel, Role.CENTRE_MANAGER)
    assert (await panel.get("/admin/calls")).status_code == 200
    # Scoped to no centre at all: the row-level policy hides every call.
    assert (await panel.get(f"/admin/calls/{calls['real']}")).status_code == 404


# --------------------------------------------------------------------------- #
# One call
# --------------------------------------------------------------------------- #


async def test_the_detail_counts_the_transcript(panel: AsyncClient, calls: dict[str, Any]) -> None:
    act_as(panel, Role.OPS_MANAGER)
    body = (await panel.get(f"/admin/calls/{calls['real']}")).json()
    assert body["isTest"] is False
    assert body["intents"] == ["price_enquiry"]
    assert body["stats"] == {
        "turns": 4,
        "farmerTurns": 2,
        "agentTurns": 2,
        "cachedReplies": 1,
        "toolCalls": 1,
        "llmModel": "claude-haiku-4-5",
    }
    assert body["recording"] == {
        "available": False,
        "durationSeconds": None,
        "bytes": None,
        "retainedUntil": None,
    }
    assert [turn["role"] for turn in body["turns"]] == ["farmer", "agent", "farmer", "agent"]
    assert (await panel.get(f"/admin/calls/{calls['test']}")).json()["isTest"] is True


async def test_the_recording_is_streamed_from_the_local_store(
    panel: AsyncClient,
    calls: dict[str, Any],
    migrator_engine: Any,
    settings_env: Any,
    tmp_path: Any,
) -> None:
    from uaagro_db.storage import object_store

    settings_env(STORAGE_BACKEND="local", STORAGE_LOCAL_DIR=str(tmp_path))
    key = f"recordings/2026/09/03/{calls['ref']}.wav"
    audio = b"RIFF" + bytes(range(64))
    await object_store().put(key, audio, content_type="audio/wav")
    async with migrator_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE calls SET recording_object_key = :k, recording_duration = 40 WHERE id = :id"
            ),
            {"k": key, "id": calls["real"]},
        )

    act_as(panel, Role.OPS_MANAGER)
    detail = (await panel.get(f"/admin/calls/{calls['real']}")).json()
    assert detail["recording"]["available"] is True
    assert detail["recording"]["bytes"] == len(audio)
    assert detail["recording"]["durationSeconds"] == 40
    assert detail["recording"]["retainedUntil"] is not None
    assert key not in detail.get("recording", {}).values()

    streamed = await panel.get(f"/admin/calls/{calls['real']}/recording")
    assert streamed.status_code == 200
    assert streamed.content == audio
    assert streamed.headers["content-type"].startswith("audio/wav")
    assert "no-store" in streamed.headers["cache-control"]
    assert (await panel.get("/admin/calls?limit=100")).json()["rows"]
    listed = {row["id"]: row for row in (await panel.get("/admin/calls?limit=100")).json()["rows"]}
    assert listed[str(calls["real"])]["recordingAvailable"] is True

    # The key is a promise; a missing object is "not available", not a 500.
    await object_store().delete(key)
    detail = (await panel.get(f"/admin/calls/{calls['real']}")).json()
    assert detail["recording"]["available"] is False
    assert (await panel.get(f"/admin/calls/{calls['real']}/recording")).status_code == 404


# --------------------------------------------------------------------------- #
# The summary, again
# --------------------------------------------------------------------------- #


async def test_summarise_reruns_the_post_call_summariser(
    panel: AsyncClient, calls: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from worker import summary

    transcripts: list[str] = []

    async def fake(transcript: str, language: str) -> tuple[str, str]:
        transcripts.append(transcript)
        return "किसान ने डीएपी का रेट पूछा, 1250 रुपये बताया।", "Farmer asked the DAP rate; told 1250."

    monkeypatch.setattr(summary, "build_summariser", lambda settings: fake)

    act_as(panel, Role.OPS_MANAGER)
    response = await panel.post(f"/admin/calls/{calls['real']}/summarise")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summaryHi"].startswith("किसान ने डीएपी")
    assert body["summaryEn"].startswith("Farmer asked")
    assert transcripts and "डीएपी का रेट क्या है" in transcripts[0]
    assert "assistant:" in transcripts[0]

    # A call with no transcript has nothing to summarise.
    empty = await panel.post(f"/admin/calls/{calls['test']}/summarise")
    assert empty.status_code == 422

    act_as(panel, Role.READ_ONLY)
    assert (await panel.post(f"/admin/calls/{calls['real']}/summarise")).status_code == 403
