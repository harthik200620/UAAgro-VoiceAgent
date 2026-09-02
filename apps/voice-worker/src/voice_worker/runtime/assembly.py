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
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.models import AgentConfig, Farmer
from uaagro_domain.enums import FlowType
from uaagro_domain.errors import ConfigurationError
from uaagro_domain.settings import Defaults, Settings

from ..adapters.factory import SpeechStack, build_speech_stack
from ..adapters.llm.gateway import LlmGateway, build_gateway
from ..flow.agent import Agent
from ..flow.context import CallerContext, ContextBuilder, DynamicHint
from ..flow.escalation import EscalationEngine
from ..flow.state import CallFlow
from ..flow.validator import OutputValidator
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


async def load_agent_settings(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    flow_type: FlowType = FlowType.INBOUND,
) -> AgentSettings:
    """The published config for this flow (§15 Flows & Prompts).

    A partial unique index makes "exactly one published version" a database
    guarantee, so this is a single row by construction rather than by
    ``ORDER BY version DESC LIMIT 1`` and hope.
    """
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
    )


@dataclass(frozen=True, slots=True)
class Caller:
    """What the lookup found, reduced to what the greeting and prompt need."""

    farmer_id: uuid.UUID | None = None
    name: str | None = None
    village: str | None = None
    language: str = "hi-IN"
    known: bool = False


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
    )


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
    agent: Agent
    stack: SpeechStack
    settings: AgentSettings
    caller: Caller
    greeting_pcm: bytes


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
) -> CallPipeline:
    """Construct everything one call needs, in dependency order.

    The order is not arbitrary: the caller lookup decides the language, the
    language decides the speech stack (§5.1 routes Hindi to Flux and Marathi to
    Sarvam, and they are different services with different turn detectors), and
    the stack decides how the greeting is synthesised.
    """
    agent_settings = await load_agent_settings(
        session, organization_id=organization_id, flow_type=flow_type
    )
    caller = await identify_caller(
        session, phone_hash, default_language=defaults.default_language
    )

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
    tool_context = ToolContext(call_id=str(call_id))

    agent = Agent(
        registry=registry,
        context_builder=ContextBuilder(persona=agent_settings.system_prompt),
        gateway=gateway if gateway is not None else build_gateway(settings),
        validator=OutputValidator(),
        escalation=EscalationEngine(),
        caller=CallerContext(
            name=caller.name,
            village=caller.village,
            language=caller.language,
        ),
        hint=DynamicHint(),
        flow=CallFlow(),
        tool_context=tool_context,
    )

    sender = PacedSender(send_frame, realtime=realtime)
    pipeline = ConversationPipeline(
        stack=stack,
        sender=sender,
        responder=agent,
        defaults=defaults,
        cache=audio_cache,
        barge_in=BargeInPolicy(),
        clear_playback=clear_playback,
    )

    greeting_pcm = await _greeting_audio(
        agent_settings.greeting_template, caller, stack, audio_cache
    )

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
    )


async def _greeting_audio(
    template: str, caller: Caller, stack: SpeechStack, cache: AudioCache
) -> bytes:
    """Synthesise the greeting, or take the cached rendering.

    A named greeting is cached per name, which is a smaller win than the
    generic one but still a hit for a farmer who calls twice in a week. A
    failure returns empty rather than raising: §11.4 would rather open with
    silence and recover than drop the call at hello.
    """
    spoken = text_for_speech(render_greeting(template, caller))
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
    "CallPipeline",
    "Caller",
    "ConfigurationMissing",
    "build_call_pipeline",
    "identify_caller",
    "load_agent_settings",
    "render_greeting",
)
