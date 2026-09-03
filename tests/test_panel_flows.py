"""Flows as the panel edits them: versions, publishing, and the inbound
persona (§13.2, §15.1, §1 N6).

Publishing is a real state transition with a real consequence: the worker
refuses to assemble an agent without a published config, so the publish
button is the moment a freshly installed system starts answering calls. The
inbound prompt is the newest thing the panel may change, and the tests here
are about its limits -- never empty, never longer than a turn can carry, and
never touched on an outbound flow, whose words are its script.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from tests.panel_support import act_as
from uaagro_db.seeds import prompts as seed_prompts
from uaagro_domain.enums import Role

pytestmark = pytest.mark.integration


async def _latest(panel: AsyncClient, flow_type: str) -> dict[str, Any]:
    rows = (await panel.get(f"/admin/flows?flow_type={flow_type}")).json()
    assert rows, f"the seed ships an {flow_type} configuration"
    return max(rows, key=lambda r: r["version"])  # type: ignore[no-any-return]


# --------------------------------------------------------------------------- #
# Listing and reading
# --------------------------------------------------------------------------- #


async def test_the_list_carries_no_prompt_text(panel: AsyncClient) -> None:
    """§1 N6: prompts stay server-side. The list draws a table of versions."""
    act_as(panel, Role.OPS_MANAGER)
    rows = (await panel.get("/admin/flows")).json()
    assert rows
    for row in rows:
        assert "systemPrompt" not in row
        assert "usedByCampaigns" in row


async def test_a_read_only_user_cannot_read_a_prompt(panel: AsyncClient) -> None:
    act_as(panel, Role.READ_ONLY)
    assert (await panel.get("/admin/flows")).status_code == 403
    act_as(panel, Role.CENTRE_MANAGER)
    assert (await panel.get("/admin/flows")).status_code == 403


async def test_the_detail_carries_the_prompt_its_length_and_the_seed_defaults(
    panel: AsyncClient,
) -> None:
    act_as(panel, Role.OPS_MANAGER)
    inbound = (await panel.get(f"/admin/flows/{(await _latest(panel, 'inbound'))['id']}")).json()
    assert inbound["systemPrompt"]
    assert inbound["promptWordCount"] == len(inbound["systemPrompt"].split())
    assert inbound["defaults"] == {
        "systemPrompt": seed_prompts.INBOUND_SYSTEM_PROMPT,
        "greeting": seed_prompts.INBOUND_GREETING,
        "closing": seed_prompts.INBOUND_CLOSING,
    }
    assert set(inbound["script"]) == {"greetingKnown", "greetingUnknown", "closing"}

    outbound = (await panel.get(f"/admin/flows/{(await _latest(panel, 'outbound'))['id']}")).json()
    assert outbound["defaults"]["greeting"] == seed_prompts.OUTBOUND_DISCLOSURE
    assert outbound["defaults"]["closing"] == seed_prompts.OUTBOUND_CLOSING
    assert outbound["script"]["onPress2"] == "knowledge_base"


# --------------------------------------------------------------------------- #
# The inbound persona
# --------------------------------------------------------------------------- #


async def test_the_inbound_prompt_is_edited_through_a_new_version(panel: AsyncClient) -> None:
    act_as(panel, Role.OPS_MANAGER)
    base = await _latest(panel, "inbound")
    persona = "  आप यूए एग्रो के फ़ोन सहायक हैं। छोटे जवाब दें, हमेशा आप कहें।  "

    created = await panel.post(
        f"/admin/flows/{base['id']}/versions", json={"script": {}, "systemPrompt": persona}
    )
    assert created.status_code == 200, created.text
    draft = created.json()
    assert draft["version"] == base["version"] + 1
    assert draft["isPublished"] is False
    assert draft["systemPrompt"] == persona.strip()
    assert draft["promptWordCount"] == len(persona.split())
    assert draft["defaults"]["systemPrompt"] == seed_prompts.INBOUND_SYSTEM_PROMPT

    patched = await panel.patch(
        f"/admin/flows/{draft['id']}",
        json={"systemPrompt": persona.strip() + " रेट सिर्फ़ टूल से बताएँ।"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["systemPrompt"].endswith("रेट सिर्फ़ टूल से बताएँ।")
    # The base is untouched: editing always produces a new version.
    assert (await panel.get(f"/admin/flows/{base['id']}")).json()["systemPrompt"] != persona.strip()


async def test_an_empty_or_oversized_prompt_is_refused(panel: AsyncClient) -> None:
    from api.routers.panel_flows import MAX_PROMPT_CHARS

    act_as(panel, Role.OPS_MANAGER)
    base = await _latest(panel, "inbound")
    empty = await panel.post(
        f"/admin/flows/{base['id']}/versions", json={"script": {}, "systemPrompt": "   "}
    )
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "validation_error"

    too_long = await panel.post(
        f"/admin/flows/{base['id']}/versions",
        json={"script": {}, "systemPrompt": "क" * (MAX_PROMPT_CHARS + 1)},
    )
    assert too_long.status_code == 422
    assert "12,000" in too_long.json()["error"]["message"]


async def test_an_outbound_flow_ignores_the_prompt_field(panel: AsyncClient) -> None:
    act_as(panel, Role.OPS_MANAGER)
    base = await _latest(panel, "outbound")
    before = (await panel.get(f"/admin/flows/{base['id']}")).json()["systemPrompt"]
    created = await panel.post(
        f"/admin/flows/{base['id']}/versions",
        json={"script": {"message": "बैठक चौदह अक्टूबर को है"}, "systemPrompt": "ignored"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["systemPrompt"] == before
    assert created.json()["script"]["message"] == "बैठक चौदह अक्टूबर को है"


# --------------------------------------------------------------------------- #
# Publishing
# --------------------------------------------------------------------------- #


async def test_publishing_makes_exactly_one_version_live_and_is_audited(
    panel: AsyncClient, migrator_engine: Any
) -> None:
    act_as(panel, Role.OPS_MANAGER)
    target = await _latest(panel, "inbound")

    response = await panel.post(f"/admin/flows/{target['id']}/publish")
    assert response.status_code == 200, response.text
    assert response.json()["isPublished"] is True

    after = (await panel.get("/admin/flows?flow_type=inbound")).json()
    published = [row for row in after if row["isPublished"]]
    assert len(published) == 1
    assert published[0]["id"] == target["id"]

    # Idempotent: a double-click on the publish button is not a state change.
    again = await panel.post(f"/admin/flows/{target['id']}/publish")
    assert again.status_code == 200
    assert again.json()["isPublished"] is True

    async with migrator_engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT action, resource_type, after FROM audit_log "
                    "WHERE resource_id = :id ORDER BY chain_index DESC LIMIT 1"
                ),
                {"id": target["id"]},
            )
        ).first()
    assert row is not None
    assert row.action == "publish"
    assert row.resource_type == "agent_config"
    recorded = row.after if isinstance(row.after, dict) else json.loads(row.after)
    assert recorded["published_version"] == target["version"]
    assert recorded["flow_type"] == "inbound"
    # Enough to say the prompt changed, never the prompt itself.
    assert "prompt_sha256" in recorded
    assert "system_prompt" not in recorded


async def test_a_centre_manager_cannot_publish_a_flow(panel: AsyncClient) -> None:
    act_as(panel, Role.OPS_MANAGER)
    target = (await panel.get("/admin/flows")).json()[0]
    act_as(panel, Role.CENTRE_MANAGER)
    assert (await panel.post(f"/admin/flows/{target['id']}/publish")).status_code == 403
