"""Editing what the agent says (§13.2, §15.1 Flows & Prompts).

A published version is immutable: the words a farmer heard on Tuesday are the
words on the row that was live on Tuesday, and the audit trail can prove it.
Editing therefore always produces a new draft, cloned from whatever the
operator started on, and publishing is the separate, audited step it already
was. The voice worker reads the published row on every call, so a publish is
live on the next call and on no call before it.

The operator edits a *script* -- the message, the follow-up question, the
lines behind each key -- and, for the helpline, the persona itself: the
inbound system prompt is theirs to change, within a length the §7 budget can
carry. The outbound prompt stays what it is; an offer call's words are its
script, and its legal lines are shown locked. What reaches the voice is
exactly what the panel showed, which is why the preview goes through the
same normaliser the call does.

§1 N6 keeps prompts server-side. The list carries no prompt text, a body is
fetched only when somebody opens a version, and both need an ops-manager
role: the panel renders them from a Server Component, so the browser never
holds a credential that could ask for one.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.audit import append_audit
from uaagro_db.models import AgentConfig, Campaign, User
from uaagro_db.seeds import prompts as seed_prompts
from uaagro_domain.enums import AuditAction, FlowType, Role
from uaagro_domain.errors import NotFoundError, SafetyError, ValidationError
from uaagro_domain.script import PANEL_KEYS, OutboundScript
from uaagro_domain.settings import get_defaults

from ..security.deps import DbDep, Principal, require_role
from ..services.worker_client import WorkerClient

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin/flows", tags=["panel"])

#: The longest inbound persona the panel accepts. Every turn carries the
#: whole prompt, and §7's budget has no room for an essay in front of it.
MAX_PROMPT_CHARS = 12_000


class PublishedIsImmutable(SafetyError):
    code = "published_immutable"


# --------------------------------------------------------------------------- #
# Shapes
# --------------------------------------------------------------------------- #


class FlowVersion(BaseModel):
    id: str
    name: str
    flowType: str
    version: int
    isPublished: bool
    publishedAt: str | None
    publishedByName: str | None
    changelog: str | None
    toolAllowlist: list[str]
    updatedAt: str
    #: Campaigns pinned to this exact version. Shown so an operator knows a
    #: retired version is still what a running campaign speaks.
    usedByCampaigns: int = 0


class FlowDefaults(BaseModel):
    """The seed wording, so an operator can put it back."""

    systemPrompt: str
    greeting: str
    closing: str


class FlowDetail(FlowVersion):
    """One version, body included.

    What the panel edits: the script's steps, or the inbound greeting and
    closing, and for an inbound flow the persona itself.
    """

    script: dict[str, Any]
    systemPrompt: str
    promptWordCount: int
    defaults: FlowDefaults


class ScriptBody(BaseModel):
    script: dict[str, Any] = Field(default_factory=dict)
    name: str | None = Field(default=None, min_length=2, max_length=160)
    changelog: str | None = Field(default=None, max_length=500)
    #: The inbound persona. Ignored for an outbound flow, whose words are its
    #: script; refused when empty or over the length the voice can carry.
    systemPrompt: str | None = None


class PublishResult(BaseModel):
    id: str
    version: int
    isPublished: bool
    previousVersion: int | None


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


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def _flow_version(row: AgentConfig, publisher: str | None, used_by: int = 0) -> FlowVersion:
    return FlowVersion(
        id=str(row.id),
        name=row.name,
        flowType=row.flow_type.value,
        version=row.version,
        isPublished=row.is_published,
        publishedAt=row.published_at.isoformat() if row.published_at else None,
        publishedByName=publisher,
        changelog=row.changelog,
        toolAllowlist=list(row.tool_allowlist),
        updatedAt=row.updated_at.isoformat(),
        usedByCampaigns=used_by,
    )


def _script_of(row: AgentConfig) -> dict[str, Any]:
    """The editable script in the panel's shape (see docs/ADMIN_API.md)."""
    if row.flow_type is FlowType.OUTBOUND:
        return OutboundScript.from_config(row.script).to_panel()
    stored = row.script if isinstance(row.script, dict) else {}
    return {
        "greetingKnown": row.greeting_template,
        "greetingUnknown": str(stored.get("greeting_unknown") or row.greeting_template),
        "closing": row.closing_template,
    }


def _defaults_of(flow_type: FlowType) -> FlowDefaults:
    if flow_type is FlowType.OUTBOUND:
        return FlowDefaults(
            systemPrompt=seed_prompts.OUTBOUND_SYSTEM_PROMPT,
            greeting=seed_prompts.OUTBOUND_DISCLOSURE,
            closing=seed_prompts.OUTBOUND_CLOSING,
        )
    return FlowDefaults(
        systemPrompt=seed_prompts.INBOUND_SYSTEM_PROMPT,
        greeting=seed_prompts.INBOUND_GREETING,
        closing=seed_prompts.INBOUND_CLOSING,
    )


async def _campaign_usage(db: AsyncSession, config_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not config_ids:
        return {}
    rows = (
        await db.execute(
            select(Campaign.agent_config_id, func.count())
            .where(Campaign.agent_config_id.in_(config_ids), Campaign.deleted_at.is_(None))
            .group_by(Campaign.agent_config_id)
        )
    ).all()
    return {config_id: int(count) for config_id, count in rows}


async def build_flow_detail(db: AsyncSession, row: AgentConfig) -> FlowDetail:
    """One version, body included."""
    publisher = (
        await db.scalar(select(User.full_name).where(User.id == row.published_by_user_id))
        if row.published_by_user_id
        else None
    )
    usage = await _campaign_usage(db, [row.id])
    base = _flow_version(row, publisher, usage.get(row.id, 0))
    return FlowDetail(
        **base.model_dump(),
        script=_script_of(row),
        systemPrompt=row.system_prompt,
        promptWordCount=len(row.system_prompt.split()),
        defaults=_defaults_of(row.flow_type),
    )


async def _flow(db: AsyncSession, config_id: uuid.UUID) -> AgentConfig:
    row = await db.scalar(
        select(AgentConfig).where(AgentConfig.id == config_id, AgentConfig.deleted_at.is_(None))
    )
    if row is None:
        raise NotFoundError(resource="agent_config", identifier=str(config_id))
    return row


@router.get("", response_model=list[FlowVersion])
async def flows(
    db: DbDep,
    _: Annotated[Principal, require_role(Role.OPS_MANAGER)],
    flow_type: str | None = None,
) -> list[FlowVersion]:
    """Every version, newest first, with the published one marked."""
    statement: Select[Any] = (
        select(AgentConfig, User.full_name)
        .outerjoin(User, AgentConfig.published_by_user_id == User.id)
        .where(AgentConfig.deleted_at.is_(None))
        .order_by(AgentConfig.flow_type, AgentConfig.version.desc())
    )
    if flow_type:
        statement = statement.where(AgentConfig.flow_type == flow_type)
    rows = (await db.execute(statement)).all()
    usage = await _campaign_usage(db, [row.id for row, _ in rows])
    return [_flow_version(row, publisher, usage.get(row.id, 0)) for row, publisher in rows]


@router.get("/{config_id}", response_model=FlowDetail)
async def flow_detail(
    config_id: uuid.UUID,
    db: DbDep,
    _: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> FlowDetail:
    return await build_flow_detail(db, await _flow(db, config_id))


# --------------------------------------------------------------------------- #
# Editing
# --------------------------------------------------------------------------- #


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


def _apply_prompt(row: AgentConfig, prompt: str | None) -> bool:
    """The inbound persona, as far as the voice can carry it.

    Returns whether anything changed. An outbound flow ignores the field:
    its words are the script, and a prompt sent for it is a panel sending
    the whole form rather than an instruction.
    """
    if prompt is None or row.flow_type is not FlowType.INBOUND:
        return False
    cleaned = prompt.strip()
    if not cleaned:
        raise ValidationError(
            "The system prompt is empty.",
            remedy="Write the persona, or restore the default wording from the panel.",
        )
    if len(cleaned) > MAX_PROMPT_CHARS:
        raise ValidationError(
            f"The system prompt is {len(cleaned):,} characters; the limit is {MAX_PROMPT_CHARS:,}.",
            remedy="Shorten it. Every turn carries the whole prompt, and a long one "
            "spends the latency budget before the farmer's question is read.",
        )
    if cleaned == row.system_prompt:
        return False
    row.system_prompt = cleaned
    return True


def _prompt_fingerprint(row: AgentConfig) -> dict[str, Any]:
    """Enough for the audit log to say the prompt changed, without the prompt."""
    digest = hashlib.sha256(row.system_prompt.encode("utf-8")).hexdigest()
    return {"prompt_chars": len(row.system_prompt), "prompt_sha256": digest[:12]}


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
    prompt_changed = _apply_prompt(draft, body.systemPrompt)
    db.add(draft)
    await db.flush()
    await append_audit(
        db,
        action=AuditAction.CREATE,
        resource_type="agent_config",
        resource_id=str(draft.id),
        actor_user_id=principal.user_id,
        before={"cloned_from": str(base.id), "version": base.version},
        after={
            "version": draft.version,
            "flow_type": draft.flow_type.value,
            "prompt_changed": prompt_changed,
            **_prompt_fingerprint(draft),
        },
    )
    return await build_flow_detail(db, draft)


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
    before = {
        "name": row.name,
        "script": dict(row.script or {}),
        "greeting": row.greeting_template,
        **_prompt_fingerprint(row),
    }
    if body.name:
        row.name = body.name.strip()
    if body.changelog is not None:
        row.changelog = body.changelog.strip()
    _apply(row, body.script)
    prompt_changed = _apply_prompt(row, body.systemPrompt)
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
            "prompt_changed": prompt_changed,
            **_prompt_fingerprint(row),
        },
    )
    return await build_flow_detail(db, row)


@router.post("/{config_id}/publish", response_model=PublishResult)
async def publish_flow(
    config_id: uuid.UUID,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> PublishResult:
    """§15.1 makes publishing a distinct, audited action.

    It is also the moment the system goes live: ``build_call_pipeline`` refuses
    to assemble an agent without a published config, because an agent speaking
    words nobody approved is worse than one that hands the caller to a person.
    A freshly seeded install therefore takes no calls until a human does this.

    A partial unique index enforces one published version per flow. Rather than
    let the update fail on it, the previous version is unpublished in the same
    transaction: the constraint is the guarantee, this is the intent.
    """
    row = await _flow(db, config_id)
    if row.is_published:
        return PublishResult(
            id=str(row.id), version=row.version, isPublished=True, previousVersion=None
        )

    current = await db.scalar(
        select(AgentConfig).where(
            AgentConfig.organization_id == row.organization_id,
            AgentConfig.flow_type == row.flow_type,
            AgentConfig.is_published.is_(True),
            AgentConfig.deleted_at.is_(None),
        )
    )
    previous = current.version if current else None
    if current is not None:
        current.is_published = False
        current.published_at = None
        current.published_by_user_id = None
        # Flushed before the new one is set, so the partial unique index never
        # sees two published rows even momentarily.
        await db.flush()

    row.is_published = True
    row.published_at = datetime.now(UTC)
    row.published_by_user_id = principal.user_id

    await append_audit(
        db,
        action=AuditAction.PUBLISH,
        resource_type="agent_config",
        resource_id=str(row.id),
        actor_user_id=principal.user_id,
        before={"published_version": previous},
        after={
            "published_version": row.version,
            "flow_type": row.flow_type.value,
            **_prompt_fingerprint(row),
        },
    )
    log.info(
        "flows.published",
        flow_type=row.flow_type.value,
        version=row.version,
        previous=previous,
    )
    return PublishResult(
        id=str(row.id),
        version=row.version,
        isPublished=True,
        previousVersion=previous,
    )


# --------------------------------------------------------------------------- #
# Hearing it
# --------------------------------------------------------------------------- #


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


__all__ = ("MAX_PROMPT_CHARS", "FlowDetail", "FlowVersion", "build_flow_detail", "router")
