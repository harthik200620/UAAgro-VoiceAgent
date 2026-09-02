"""Assembly: the point where the pieces become a call (§4.1, §11.1).

Everything the earlier phases built -- the speech stack, the tools, the flow
state machine, the safety path, the validator, the escalation engine -- meets
here, in the one function that turns a ringing phone into a conversation.

The pieces were deliberately built without knowing about each other. That is
what made them testable in isolation, and it is also how a codebase ends up
with a fully tested pipeline that nothing in production ever calls. This module
is the answer to "who constructs all of that, and in what order", and it is the
only place in the worker that knows.

Three things it insists on:

**The configuration comes from the database.** §1 N6 keeps prompts server-side
and versioned; §15 has staff editing them in the admin panel and publishing a
new version. Reading a prompt from a Python constant would make the panel a
decoration. A missing published config is a hard failure rather than a
fallback: an agent running an unpublished prompt is an agent nobody approved.

**The caller is looked up before the greeting, not after.** §11.1 wants the
first audio out within ~50 ms, and §11.5 persists the farmer's language for
exactly this moment -- so a returning caller is greeted by name in the language
they last used, instead of being asked again.

**The greeting is synthesised once per (text, voice) and cached.** It is the
same sentence on every call; paying Sarvam and 400 ms for it each time would
spend the whole §7 first-audio budget on a string we already have.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.engine import incall_session
from uaagro_db.models import AgentConfig, Campaign, CampaignContact, Centre, DndStatus, Farmer
from uaagro_domain.enums import (
    CallDirection,
    CallOutcome,
    CallStatus,
    CampaignStatus,
    ContactStatus,
    FlowType,
)
from uaagro_domain.errors import ConfigurationError
from uaagro_domain.livefeed import CONTACT_UPDATED, LiveEvent, LiveFeed
from uaagro_domain.settings import Defaults, Settings

from ..adapters.factory import SpeechStack, build_speech_stack
from ..adapters.llm.gateway import LlmGateway, build_gateway
from ..flow.agent import Agent
from ..flow.context import CallerContext, ContextBuilder, DynamicHint
from ..flow.escalation import EscalationEngine
from ..flow.state import CallFlow
from ..flow.validator import OutputValidator
from ..outbound.responder import OutboundResponder
from ..outbound.script import OutboundScript
from ..pipelines.conversation import ConversationPipeline
from ..runtime.audio_cache import AudioCache
from ..runtime.playback import BargeInPolicy, PacedSender
from ..text.lexicon import Lexicon
from ..text.speech import text_for_speech
from ..tools.base import ToolContext, ToolRegistry

log = structlog.get_logger(__name__)


class ConfigurationMissing(ConfigurationError):
    """No published agent config for this flow.

    Deliberately fatal for the call rather than falling back to a default
    prompt. An agent speaking words nobody published is worse than an agent
    that hands the caller to a person -- §11.4 has a route for the second and
    none for the first.
    """


@dataclass(frozen=True, slots=True)
class AgentSettings:
    """The published configuration, copied out of the ORM row.

    Frozen and detached on purpose: it is read on the audio path across many
    turns, and a lazy attribute on a live ORM object would put a database round
    trip inside the §7 budget the first time something touched it.
    """

    id: uuid.UUID
    version: int
    system_prompt: str
    greeting_template: str
    closing_template: str
    tool_allowlist: tuple[str, ...]
    llm_settings: dict[str, Any] = field(default_factory=dict)
    tts_settings: dict[str, Any] = field(default_factory=dict)
    guardrails: dict[str, Any] = field(default_factory=dict)
    #: The panel-edited script (§13.2). Empty for inbound flows.
    script: dict[str, Any] = field(default_factory=dict)


async def load_agent_settings(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    flow_type: FlowType = FlowType.INBOUND,
    config_id: uuid.UUID | None = None,
) -> AgentSettings:
    """The published config for this flow (§15 Flows & Prompts).

    A partial unique index makes "exactly one published version" a database
    guarantee, so this is a single row by construction rather than by
    ``ORDER BY version DESC LIMIT 1`` and hope.

    ``config_id`` pins a specific version -- a campaign keeps speaking the
    version it was approved with, even after a newer one is published. A
    pinned version that has since been deleted falls back to the published
    one rather than to silence.
    """
    row: AgentConfig | None = None
    if config_id is not None:
        row = await session.scalar(
            select(AgentConfig).where(
                AgentConfig.id == config_id,
                AgentConfig.organization_id == organization_id,
                AgentConfig.flow_type == flow_type,
                AgentConfig.deleted_at.is_(None),
            )
        )
    if row is None:
        row = await session.scalar(
            select(AgentConfig).where(
                AgentConfig.organization_id == organization_id,
                AgentConfig.flow_type == flow_type,
                AgentConfig.is_published.is_(True),
                AgentConfig.deleted_at.is_(None),
            )
        )
    if row is None:
        raise ConfigurationMissing(
            f"No published {flow_type.value} agent config for this organization.",
            remedy=(
                "Publish a version in the admin panel under Flows & Prompts. "
                "The worker will not speak an unapproved prompt."
            ),
            context={"flow_type": flow_type.value},
        )
    return AgentSettings(
        id=row.id,
        version=row.version,
        system_prompt=row.system_prompt,
        greeting_template=row.greeting_template,
        closing_template=row.closing_template,
        tool_allowlist=tuple(row.tool_allowlist),
        llm_settings=dict(row.llm_settings),
        tts_settings=dict(row.tts_settings),
        guardrails=dict(row.guardrails),
        script=dict(row.script or {}),
    )


@dataclass(frozen=True, slots=True)
class Caller:
    """What the lookup found, reduced to what the greeting and prompt need."""

    farmer_id: uuid.UUID | None = None
    name: str | None = None
    village: str | None = None
    language: str = "hi-IN"
    known: bool = False
    #: For the panel, which shows a farmer as a name and four digits.
    last4: str | None = None
    centre_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class CallLink:
    """Who and what this call is about, for the call record and the live view."""

    farmer_id: uuid.UUID | None = None
    farmer_name: str | None = None
    farmer_last4: str | None = None
    centre_id: uuid.UUID | None = None
    centre_code: str | None = None
    centre_name: str | None = None
    campaign_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    language: str | None = None
    config_version: int | None = None


@dataclass(frozen=True, slots=True)
class OutboundContact:
    """The campaign contact an outbound call is serving (§13.2)."""

    contact_id: uuid.UUID
    campaign_id: uuid.UUID
    farmer_id: uuid.UUID | None
    #: The version the campaign was approved with, or None for "the published one".
    agent_config_id: uuid.UUID | None


async def find_outbound_contact(
    session: AsyncSession,
    *,
    contact_id: uuid.UUID | None,
    phone_hash: bytes | None,
) -> OutboundContact | None:
    """Which contact an outbound call belongs to.

    By the id the dialer put in the originate request when there is one; by
    the phone hash otherwise, taking the contact most recently marked as
    being dialled in a running campaign. The second path exists for a
    provider that drops custom fields, and is why the dialer marks the
    contact *before* it dials.
    """
    statement = (
        select(CampaignContact, Campaign.agent_config_id)
        .join(Campaign, Campaign.id == CampaignContact.campaign_id)
        .where(Campaign.deleted_at.is_(None))
    )
    if contact_id is not None:
        statement = statement.where(CampaignContact.id == contact_id)
    elif phone_hash is not None:
        statement = (
            statement.where(
                CampaignContact.phone_hash == phone_hash,
                CampaignContact.status == ContactStatus.DIALING,
                Campaign.status == CampaignStatus.RUNNING,
            )
            .order_by(CampaignContact.last_attempt_at.desc().nulls_last())
            .limit(1)
        )
    else:
        return None
    row = (await session.execute(statement)).first()
    if row is None:
        return None
    contact, config_id = row
    return OutboundContact(
        contact_id=contact.id,
        campaign_id=contact.campaign_id,
        farmer_id=contact.farmer_id,
        agent_config_id=config_id,
    )


async def identify_caller(
    session: AsyncSession, phone_hash: bytes | None, *, default_language: str
) -> Caller:
    """Who is calling, from the number the provider gave us.

    By hash, never by the number: §17 makes the hash the only lookup key, and
    this runs before the greeting on every single call.

    A failure here is not fatal. An unrecognised caller is the normal case for
    a helpline, and a database that is briefly unavailable should still get a
    greeting -- in the default language, which is the honest degradation.
    """
    if phone_hash is None:
        return Caller(language=default_language)
    try:
        farmer = await session.scalar(
            select(Farmer).where(
                Farmer.phone_hash == phone_hash, Farmer.deleted_at.is_(None)
            )
        )
    except Exception as exc:
        log.warning("assembly.caller_lookup_failed", error=type(exc).__name__)
        return Caller(language=default_language)

    if farmer is None:
        return Caller(language=default_language)
    return Caller(
        farmer_id=farmer.id,
        name=farmer.full_name,
        village=farmer.village,
        # §11.5 persisted this so the next call starts in the right language
        # rather than interrogating the farmer again.
        language=farmer.preferred_language or default_language,
        known=True,
        last4=farmer.phone_last4,
        centre_id=farmer.assigned_centre_id,
    )


async def _centre_named(session: AsyncSession, centre_id: uuid.UUID | None) -> Centre | None:
    """The farmer's centre, for the greeting's "{केंद्र}" and the live view."""
    if centre_id is None:
        return None
    try:
        centre: Centre | None = await session.scalar(
            select(Centre).where(Centre.id == centre_id, Centre.deleted_at.is_(None))
        )
    except Exception as exc:
        log.warning("assembly.centre_lookup_failed", error=type(exc).__name__)
        return None
    return centre


def _suppressor(call_id: uuid.UUID, phone_hash: bytes | None) -> Callable[[], Awaitable[None]]:
    """§13.2's opt-out write: ``internal_dnc`` set before the call goes on.

    Its own session, opened when the farmer asks and closed before the
    confirmation is spoken. The session that built the pipeline is long gone
    by then, and the write must not wait for the call to end.
    """

    async def suppress() -> None:
        if phone_hash is None:
            return
        now = datetime.now(UTC)
        async with incall_session() as db:
            existing = await db.scalar(select(DndStatus).where(DndStatus.phone_hash == phone_hash))
            if existing is None:
                db.add(
                    DndStatus(
                        phone_hash=phone_hash,
                        internal_dnc=True,
                        internal_dnc_at=now,
                        internal_dnc_source_call_id=call_id,
                        source="call_opt_out",
                    )
                )
            else:
                await db.execute(
                    update(DndStatus)
                    .where(DndStatus.phone_hash == phone_hash)
                    .values(
                        internal_dnc=True,
                        internal_dnc_at=now,
                        internal_dnc_source_call_id=call_id,
                    )
                )
            await db.commit()
        log.info("outbound.suppressed")

    return suppress


def _contact_finisher(
    contact: OutboundContact,
    responder: OutboundResponder,
    live_feed: LiveFeed | None,
    *,
    farmer_name: str | None,
    farmer_last4: str | None,
) -> Callable[[Any, CallStatus, CallOutcome | None], Awaitable[None]]:
    """Write what the script decided to the contact, and tell the panel."""

    async def finish(repository: Any, status: CallStatus, outcome: CallOutcome | None) -> None:
        result = responder.result
        contact_status = result.status
        panel_outcome = result.outcome
        if panel_outcome is None:
            # The line dropped before the farmer said anything: not reached,
            # and the retry policy decides whether to try again.
            contact_status = ContactStatus.NO_ANSWER
            panel_outcome = "no_answer"
        if repository is not None:
            await repository.finish_contact(
                contact.contact_id,
                status=contact_status,
                outcome=panel_outcome,
                dtmf=result.dtmf,
                interest=result.interest,
            )
        if live_feed is not None:
            live_feed.publish(
                LiveEvent(
                    type=CONTACT_UPDATED,
                    payload={
                        "id": str(contact.contact_id),
                        "farmerName": farmer_name,
                        "last4": farmer_last4 or "",
                        "status": "done"
                        if contact_status is not ContactStatus.NO_ANSWER
                        else "no_answer",
                        "outcome": panel_outcome,
                        "dtmf": result.dtmf,
                        "callId": None,
                    },
                    campaign_id=str(contact.campaign_id),
                )
            )

    return finish


def render_greeting(template: str, caller: Caller) -> str:
    """Fill the published greeting.

    Substitution is positional-free and total: an unknown caller gets a
    greeting with no dangling placeholder, because "नमस्ते  जी" with a hole in
    it is worse than a generic hello.
    """
    name = caller.name or ""
    rendered = (
        template.replace("{name}", name)
        .replace("{village}", caller.village or "")
        .replace("{name_ji}", f"{name} जी" if name else "जी")
    )
    return " ".join(rendered.split())


@dataclass(slots=True)
class CallPipeline:
    """A built pipeline plus what the session needs to run and close it."""

    pipeline: ConversationPipeline
    #: The knowledge agent. On an outbound call it answers the farmer's
    #: questions behind the script; None when no inbound config is published.
    agent: Agent | None
    stack: SpeechStack
    settings: AgentSettings
    caller: Caller
    greeting_pcm: bytes
    link: CallLink = field(default_factory=CallLink)
    #: Outbound only: writes the contact's result when the call ends.
    finish: Callable[[Any, CallStatus, CallOutcome | None], Awaitable[None]] | None = None


async def build_call_pipeline(
    *,
    call_id: uuid.UUID,
    organization_id: uuid.UUID,
    phone_hash: bytes | None,
    session: AsyncSession,
    registry: ToolRegistry,
    settings: Settings,
    defaults: Defaults,
    send_frame: Callable[[bytes], Awaitable[None]],
    clear_playback: Callable[[], Awaitable[None]] | None = None,
    cache: AudioCache | None = None,
    gateway: LlmGateway | None = None,
    stack: SpeechStack | None = None,
    lexicon: Lexicon | None = None,
    flow_type: FlowType = FlowType.INBOUND,
    realtime: bool = True,
    direction: CallDirection = CallDirection.INBOUND,
    contact: OutboundContact | None = None,
    live_feed: LiveFeed | None = None,
    config_id: uuid.UUID | None = None,
) -> CallPipeline:
    """Construct everything one call needs, in dependency order.

    The order is not arbitrary: the caller lookup decides the language, the
    language decides the speech stack (§5.1 routes Hindi to Flux and Marathi to
    Sarvam, and they are different services with different turn detectors), and
    the stack decides how the greeting is synthesised.

    An outbound call (§13.2) is built the same way with one substitution: the
    script responder stands where the LLM agent stands, and the agent moves
    behind it to answer the farmer's questions. Everything else -- the stack,
    the cache, barge-in, latency accounting -- is shared, which is the point.
    """
    if direction is CallDirection.OUTBOUND:
        flow_type = FlowType.OUTBOUND
    # A campaign speaks the version it was approved with; a panel test call
    # speaks the draft it was placed for; everything else speaks what is
    # published.
    pinned = contact.agent_config_id if contact is not None else config_id
    agent_settings = await load_agent_settings(
        session, organization_id=organization_id, flow_type=flow_type, config_id=pinned
    )
    caller = await identify_caller(
        session, phone_hash, default_language=defaults.default_language
    )
    centre = await _centre_named(session, caller.centre_id)

    # §5.1's declarative routing. Chosen from the caller's language, which is
    # why the lookup comes first.
    #
    # An injected stack is not a test hook: §15's sandbox test call runs a real
    # conversation against a stack the panel supplies, without dialling anybody.
    if stack is None:
        # §5.5's vocabulary boosting. Without keyterms the recogniser has no
        # idea "इमिडाक्लोप्रिड" is a word, and every product question starts
        # from a transcript the matcher then has to repair.
        keyterms = tuple(lexicon.keyterms()) if lexicon is not None else ()
        stack = build_speech_stack(
            caller.language, settings, defaults, keyterms=keyterms
        )
    # Opened concurrently with everything below, and awaited just before the
    # greeting is handed back.
    #
    # The handshake measures ~1,150 ms against Soniox, and it used to sit
    # squarely on the critical path: the caller heard 1.1 s of silence, then
    # "नमस्ते". Nothing needs the recogniser until the caller speaks, and the
    # caller does not speak until the greeting has played -- so the whole
    # handshake fits inside time the call was going to spend anyway.
    #
    # A task rather than a bare coroutine so a connect failure still raises
    # here, at construction, rather than surfacing as a mute call later.
    connecting = asyncio.create_task(stack.stt.start(stack.stt_config))

    audio_cache = cache if cache is not None else AudioCache()
    resolved_gateway = gateway if gateway is not None else build_gateway(settings)

    agent: Agent | None
    responder: Any
    finish: Callable[[Any, CallStatus, CallOutcome | None], Awaitable[None]] | None = None
    if flow_type is FlowType.OUTBOUND:
        # The knowledge agent answers questions behind the script. It speaks
        # with the *inbound* persona because that is the one grounded in the
        # catalogue and the knowledge base; a deployment that has not published
        # an inbound config gets a script that politely declines questions.
        agent = None
        try:
            inbound = await load_agent_settings(
                session, organization_id=organization_id, flow_type=FlowType.INBOUND
            )
        except ConfigurationMissing:
            inbound = None
        if inbound is not None:
            agent = build_agent(
                registry=registry,
                persona=inbound.system_prompt,
                gateway=resolved_gateway,
                caller=caller,
                call_id=call_id,
            )
        responder = OutboundResponder(
            script=OutboundScript.from_config(agent_settings.script),
            farmer_name=caller.name,
            centre_name=(centre.name_hi or centre.name) if centre is not None else None,
            questions=agent,
            suppress=_suppressor(call_id, phone_hash),
        )
        opening = responder.opening()
        if contact is not None:
            finish = _contact_finisher(
                contact,
                responder,
                live_feed,
                farmer_name=caller.name,
                farmer_last4=caller.last4,
            )
    else:
        agent = build_agent(
            registry=registry,
            persona=agent_settings.system_prompt,
            gateway=resolved_gateway,
            caller=caller,
            call_id=call_id,
        )
        responder = agent
        opening = render_greeting(agent_settings.greeting_template, caller)

    sender = PacedSender(send_frame, realtime=realtime)
    pipeline = ConversationPipeline(
        stack=stack,
        sender=sender,
        responder=responder,
        defaults=defaults,
        cache=audio_cache,
        barge_in=BargeInPolicy(),
        clear_playback=clear_playback,
    )

    greeting_pcm = await _greeting_audio(opening, stack, audio_cache)

    # Whatever the handshake cost, it was paid while the greeting was being
    # prepared. Awaited rather than left running so that a recogniser that
    # could not connect fails the call now, loudly, instead of at the caller's
    # first word.
    try:
        await connecting
    except BaseException:
        connecting.cancel()
        raise

    log.info(
        "assembly.pipeline_built",
        language=caller.language,
        served_by=stack.served_by,
        tier=stack.quality_tier.value,
        config_version=agent_settings.version,
        # Whether we recognised the caller, never who they are.
        caller_known=caller.known,
        tools=len(agent_settings.tool_allowlist),
    )
    return CallPipeline(
        pipeline=pipeline,
        agent=agent,
        stack=stack,
        settings=agent_settings,
        caller=caller,
        greeting_pcm=greeting_pcm,
        link=CallLink(
            farmer_id=caller.farmer_id,
            farmer_name=caller.name,
            farmer_last4=caller.last4,
            centre_id=centre.id if centre is not None else None,
            centre_code=centre.code if centre is not None else None,
            centre_name=centre.name if centre is not None else None,
            campaign_id=contact.campaign_id if contact is not None else None,
            contact_id=contact.contact_id if contact is not None else None,
            language=stack.served_by,
            config_version=agent_settings.version,
        ),
        finish=finish,
    )


def build_agent(
    *,
    registry: ToolRegistry,
    persona: str,
    gateway: LlmGateway,
    caller: Caller,
    call_id: uuid.UUID,
) -> Agent:
    """The DISCOVER ⇄ RESOLVE agent for one call, or for one panel question.

    Shared by the phone path and the control plane's "try a question" so the
    panel gets exactly the answer a caller would -- same persona, same tools,
    same validator, same refusal to invent a dose.
    """
    return Agent(
        registry=registry,
        context_builder=ContextBuilder(persona=persona),
        gateway=gateway,
        validator=OutputValidator(),
        escalation=EscalationEngine(),
        caller=CallerContext(
            name=caller.name,
            village=caller.village,
            language=caller.language,
        ),
        hint=DynamicHint(),
        flow=CallFlow(),
        tool_context=ToolContext(call_id=str(call_id)),
    )


async def _greeting_audio(opening: str, stack: SpeechStack, cache: AudioCache) -> bytes:
    """Synthesise the opening line, or take the cached rendering.

    A named greeting is cached per name, which is a smaller win than the
    generic one but still a hit for a farmer who calls twice in a week. A
    failure returns empty rather than raising: §11.4 would rather open with
    silence and recover than drop the call at hello.
    """
    spoken = text_for_speech(opening)
    if not spoken:
        return b""
    try:
        cached = await cache.get(spoken, stack.tts_config, provider=stack.served_by)
        if cached is not None:
            return cached
        pcm = await _synthesise(stack, spoken)
        if pcm:
            await cache.put(spoken, stack.tts_config, pcm, provider=stack.served_by)
        return pcm
    except Exception as exc:
        log.warning("assembly.greeting_failed", error=type(exc).__name__)
        return b""


async def _synthesise(stack: SpeechStack, spoken: str) -> bytes:
    chunks: list[bytes] = []
    async for chunk in stack.tts.synthesise(spoken, stack.tts_config):
        chunks.append(chunk.audio)
    return b"".join(chunks)


__all__ = (
    "AgentSettings",
    "CallLink",
    "CallPipeline",
    "Caller",
    "ConfigurationMissing",
    "OutboundContact",
    "build_agent",
    "build_call_pipeline",
    "find_outbound_contact",
    "identify_caller",
    "load_agent_settings",
    "render_greeting",
)
