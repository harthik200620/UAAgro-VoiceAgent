"""Editing what the agent says (§13.2, §15.1 Flows & Prompts).

A published version is immutable: the words a farmer heard on Tuesday are the
words on the row that was live on Tuesday, and the audit trail can prove it.
Editing therefore always produces a new draft, cloned from whatever the
operator started on, and publishing is the separate, audited step it already
was.

The operator edits a *script* -- the message, the follow-up question, the
lines behind each key -- not a prompt. The system prompt stays what it is and
is shown read-only; the legal lines are shown locked. What reaches the voice
is exactly what the panel showed, which is why the preview goes through the
same normaliser the call does.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.audit import append_audit
from uaagro_db.models import AgentConfig
from uaagro_domain.enums import AuditAction, FlowType, Role
from uaagro_domain.errors import NotFoundError, SafetyError, ValidationError
from uaagro_domain.script import PANEL_KEYS, OutboundScript
from uaagro_domain.settings import get_defaults

from ..security.deps import DbDep, Principal, require_role
from ..services.worker_client import WorkerClient
from .admin import FlowDetail, build_flow_detail

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin/flows", tags=["panel"])


class PublishedIsImmutable(SafetyError):
    code = "published_immutable"


class ScriptBody(BaseModel):
    script: dict[str, Any]
    name: str | None = Field(default=None, min_length=2, max_length=160)
    changelog: str | None = Field(default=None, max_length=500)


class Text(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class Preview(BaseModel):
    spoken: str
    words: int
    seconds: float
    substitutions: list[dict[str, str]]


class TestCall(BaseModel):
    phone: str = Field(min_length=10, max_length=16)


class TestCallResult(BaseModel):
    callSid: str


async def _flow(db: AsyncSession, config_id: uuid.UUID) -> AgentConfig:
    row = await db.scalar(
        select(AgentConfig).where(AgentConfig.id == config_id, AgentConfig.deleted_at.is_(None))
    )
    if row is None:
        raise NotFoundError(resource="agent_config", identifier=str(config_id))
    return row


def _apply(row: AgentConfig, script: dict[str, Any]) -> None:
    """Merge the panel's edits into the row, checking what the voice needs."""
    if row.flow_type is FlowType.OUTBOUND:
        for panel_key in PANEL_KEYS:
            value = script.get(panel_key)
            if value is not None and not isinstance(value, str):
                raise ValidationError(f"{panel_key} must be text.", remedy="Type the line as text.")
        merged = dict(row.script or {})
        merged.update(
            {
                stored: " ".join(script[panel].split())
                for panel, stored in PANEL_KEYS.items()
                if isinstance(script.get(panel), str)
            }
        )
        problems = OutboundScript.from_config(merged).problems()
        if problems:
            raise ValidationError(problems[0], remedy="Fix the script and save again.")
        row.script = merged
        return

    if isinstance(script.get("greetingKnown"), str) and script["greetingKnown"].strip():
        row.greeting_template = " ".join(script["greetingKnown"].split())
    if isinstance(script.get("closing"), str) and script["closing"].strip():
        row.closing_template = " ".join(script["closing"].split())
    if isinstance(script.get("greetingUnknown"), str):
        merged = dict(row.script or {})
        merged["greeting_unknown"] = " ".join(script["greetingUnknown"].split())
        row.script = merged


async def _detail(db: AsyncSession, row: AgentConfig) -> FlowDetail:
    return await build_flow_detail(db, row)


@router.post("/{config_id}/versions", response_model=FlowDetail)
async def new_version(
    config_id: uuid.UUID,
    body: ScriptBody,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> FlowDetail:
    """A new draft, cloned from ``config_id`` with the edits applied."""
    base = await _flow(db, config_id)
    next_version = (
        int(
            await db.scalar(
                select(func.max(AgentConfig.version)).where(
                    AgentConfig.organization_id == base.organization_id,
                    AgentConfig.flow_type == base.flow_type,
                )
            )
            or 0
        )
        + 1
    )
    draft = AgentConfig(
        organization_id=base.organization_id,
        name=(body.name or base.name).strip(),
        flow_type=base.flow_type,
        version=next_version,
        is_published=False,
        system_prompt=base.system_prompt,
        greeting_template=base.greeting_template,
        closing_template=base.closing_template,
        tool_allowlist=list(base.tool_allowlist),
        escalation_rules=dict(base.escalation_rules),
        language_routes=dict(base.language_routes),
        llm_settings=dict(base.llm_settings),
        tts_settings=dict(base.tts_settings),
        guardrails=dict(base.guardrails),
        script=dict(base.script or {}),
        changelog=(body.changelog or f"Edited from v{base.version}").strip(),
        created_by=principal.user_id,
    )
    _apply(draft, body.script)
    db.add(draft)
    await db.flush()
    await append_audit(
        db,
        action=AuditAction.CREATE,
        resource_type="agent_config",
        resource_id=str(draft.id),
        actor_user_id=principal.user_id,
        before={"cloned_from": str(base.id), "version": base.version},
        after={"version": draft.version, "flow_type": draft.flow_type.value},
    )
    return await _detail(db, draft)


@router.patch("/{config_id}", response_model=FlowDetail)
async def patch_flow(
    config_id: uuid.UUID,
    body: ScriptBody,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> FlowDetail:
    """Edit a draft in place. A published version is immutable (409)."""
    row = await _flow(db, config_id)
    if row.is_published:
        raise PublishedIsImmutable(
            f"v{row.version} is live and cannot be edited.",
            remedy="Create a new version from it; publishing that version is the change.",
        )
    before = {"name": row.name, "script": dict(row.script or {}), "greeting": row.greeting_template}
    if body.name:
        row.name = body.name.strip()
    if body.changelog is not None:
        row.changelog = body.changelog.strip()
    _apply(row, body.script)
    row.updated_at = datetime.now(UTC)
    await append_audit(
        db,
        action=AuditAction.UPDATE,
        resource_type="agent_config",
        resource_id=str(row.id),
        actor_user_id=principal.user_id,
        before=before,
        after={
            "name": row.name,
            "script": dict(row.script or {}),
            "greeting": row.greeting_template,
        },
    )
    return await _detail(db, row)


@router.post("/{config_id}/preview", response_model=Preview)
async def preview(
    config_id: uuid.UUID,
    body: Text,
    db: DbDep,
    _: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> Preview:
    row = await _flow(db, config_id)
    language = _language_of(row)
    result = await WorkerClient().preview(body.text, language=language)
    return Preview(
        spoken=str(result.get("spoken") or ""),
        words=int(result.get("words") or 0),
        seconds=float(result.get("seconds") or 0.0),
        substitutions=[
            {"from": str(s.get("from", "")), "to": str(s.get("to", ""))}
            for s in result.get("substitutions") or []
            if isinstance(s, dict)
        ],
    )


@router.post("/{config_id}/audio")
async def audio(
    config_id: uuid.UUID,
    body: Text,
    db: DbDep,
    _: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> Response:
    """The line, rendered by the configured voice. A vendor call, on purpose."""
    row = await _flow(db, config_id)
    wav = await WorkerClient().speech(body.text, language=_language_of(row))
    return Response(
        content=wav, media_type="audio/wav", headers={"cache-control": "private, no-store"}
    )


@router.post("/{config_id}/test-call", response_model=TestCallResult)
async def test_call(
    config_id: uuid.UUID,
    body: TestCall,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> TestCallResult:
    row = await _flow(db, config_id)
    if row.flow_type is not FlowType.OUTBOUND:
        raise ValidationError(
            "Test calls play the outbound script.",
            remedy="Call the helpline number to hear the inbound greeting.",
        )
    sid = await WorkerClient().test_call(phone=body.phone, config_id=str(row.id))
    await append_audit(
        db,
        action=AuditAction.CREATE,
        resource_type="test_call",
        resource_id=sid or None,
        actor_user_id=principal.user_id,
        # The number is the operator's own and is still not written down.
        after={"config_id": str(row.id), "version": row.version},
    )
    return TestCallResult(callSid=sid)


def _language_of(row: AgentConfig) -> str:
    voice = row.tts_settings.get("language") if isinstance(row.tts_settings, dict) else None
    return str(voice) if voice else get_defaults().default_language


__all__ = ("router",)
