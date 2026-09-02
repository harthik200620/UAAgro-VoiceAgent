"""Warm transfer and WhatsApp (§6.3, §12.3, §14).

Both tools reach outside the database, so both are split the same way: the
**decision** is made here against stored data, and the **act** -- dialling a
manager, posting to Meta -- happens through a port that Phases 5 and 6 fill in.
That split is not ceremony. It means the rules that matter (a reason from a
fixed enum, the transfer chain, the daily cap, template-only messaging, consent
still valid) are testable now, and they hold whatever adapter is plugged in
later.

Two rules from §12.3 are enforced rather than requested:

**The reason is an enum.** §12.3 rejects a free-text reason outright, so an
unrecognised one is a validation failure at the schema, before any target is
resolved. A model that could write its own reason could route a safety
emergency into a routine queue.

**Nothing dead-ends.** When the chain is exhausted or the centre is shut, §12.3
requires an apology, a specific callback commitment, a P1 ticket and a WhatsApp
with the reference. The tool returns exactly that instruction rather than an
empty result the agent has to improvise around.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, ClassVar

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.models import Centre, Farmer, User
from uaagro_domain.enums import TransferReason, TransferUrgency
from uaagro_domain.errors import InvalidArgumentError, NotFoundError
from uaagro_domain.phone import redact
from uaagro_domain.timezone import now_ist

from .base import Tool, ToolContext
from .session import tool_session

log = structlog.get_logger(__name__)

#: §12.3: transfers per caller per day. A caller who has hit the cap still gets
#: a ticket and a callback -- the cap limits dialling, not help.
DAILY_TRANSFER_CAP = 3

#: §12.3 step 6.
TRANSFER_RING_SECONDS = 20

#: Reasons that bypass hours, the cap and the chain entirely. §16.1 makes the
#: safety path the highest-severity route in the system: a closed centre is not
#: a reason to keep someone who has swallowed pesticide on the line with a bot.
ALWAYS_TRANSFER = (TransferReason.SAFETY_EMERGENCY,)

_DAY_CODES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True, slots=True)
class TransferTarget:
    """One rung of the §12.3 chain."""

    kind: str
    name: str | None
    number: str
    rank: int


class TransferPort(ABC):
    """What the telephony adapter must provide (Phase 5)."""

    @abstractmethod
    async def is_busy(self, number: str) -> bool:
        """True when this number is already on a transferred call (§12.3-3)."""

    @abstractmethod
    async def transfers_today(self, call_id: str, farmer_id: str | None) -> int:
        """How many times this caller has already been transferred today."""


class NullTransferPort(TransferPort):
    """Stand-in until the Phase 5 adapter lands.

    Reports every target free and no transfers used. It is deliberately
    permissive rather than restrictive: a stub that refused transfers would make
    the safety path look tested when it was only blocked.
    """

    async def is_busy(self, number: str) -> bool:
        return False

    async def transfers_today(self, call_id: str, farmer_id: str | None) -> int:
        return 0


class WhatsAppPort(ABC):
    """What the messaging adapter must provide (Phase 6)."""

    @abstractmethod
    async def send_template(
        self, *, to_phone_hash: bytes, template: str, params: Mapping[str, str], language: str
    ) -> str:
        """Queue one template message and return its provider id."""


class QueueingWhatsAppPort(WhatsAppPort):
    """Records the intent to send without sending.

    Phase 6 replaces this with the Cloud API adapter. Until then the tool is
    honest with the agent -- the farmer is told a message is on its way and the
    row exists to prove whether it was -- rather than silently succeeding.
    """

    def __init__(self) -> None:
        self.queued: list[dict[str, Any]] = []

    async def send_template(
        self, *, to_phone_hash: bytes, template: str, params: Mapping[str, str], language: str
    ) -> str:
        message_id = f"queued-{uuid.uuid4().hex[:12]}"
        self.queued.append(
            {"template": template, "language": language, "params": dict(params)}
        )
        log.info("whatsapp.queued", template=template, language=language)
        return message_id


class TransferToHuman(Tool):
    """Decide and prepare a warm transfer (§12.3)."""

    name = "transfer_to_human"
    description = (
        "Hand the caller to a person. Say the transfer line to the caller first, "
        "then wait. The reason must be one of the listed values -- do not invent "
        "one. If no one is reachable the result says so and tells you what to "
        "commit to instead; never end a call with 'please call back later'."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            # §12.3: a free-text reason is rejected. The enum is the guard.
            "reason": {"type": "string", "enum": [r.value for r in TransferReason]},
            "urgency": {"type": "string", "enum": [u.value for u in TransferUrgency]},
            "context_note": {
                "type": "string",
                "maxLength": 200,
                "description": (
                    "One line for the manager whisper: who, where, what they want. "
                    "Spoken to the manager only, never to the caller."
                ),
            },
        },
        "required": ["reason"],
    }
    read_only = False

    def __init__(
        self,
        port: TransferPort | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """
        Args:
            clock: Current time in IST. Injectable because "is the centre open"
                is a real branch with a real consequence -- outside hours a
                transfer becomes a callback commitment -- and a test that reads
                the wall clock exercises whichever branch the time of day
                happens to select. This one passed all afternoon and started
                failing at 19:00 when the seeded centres closed.
        """
        self._port = port or NullTransferPort()
        self._clock = clock or now_ist

    async def run(self, args: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
        reason = TransferReason(str(args["reason"]))
        urgency = TransferUrgency(
            str(args.get("urgency") or _default_urgency(reason).value)
        )
        override = reason in ALWAYS_TRANSFER or urgency is TransferUrgency.CRITICAL

        async with tool_session() as session:
            centre = await _centre(session, context)
            if centre is None:
                raise NotFoundError(resource="centre", identifier="assigned")

            used = await self._port.transfers_today(context.call_id, context.farmer_id)
            if used >= DAILY_TRANSFER_CAP and not override:
                log.info("transfer.capped", reason=reason.value, used=used)
                return _fallback(
                    centre,
                    reason,
                    why="daily_cap_reached",
                    urgency=urgency,
                )

            now = self._clock()
            if not _is_open(centre, now) and not override:
                return _fallback(centre, reason, why="centre_closed", urgency=urgency)

            for target in await _chain(session, centre):
                if await self._port.is_busy(target.number):
                    continue
                note = str(args.get("context_note") or "")
                whisper = await _whisper(session, context, reason, note)
                log.info(
                    "transfer.target_selected",
                    reason=reason.value,
                    urgency=urgency.value,
                    target_kind=target.kind,
                    # §23-6: never the number itself, in any log line.
                    target=redact(target.number),
                )
                return {
                    "action": "transfer",
                    "reason": reason.value,
                    "urgency": urgency.value,
                    "target_kind": target.kind,
                    "target_name": target.name,
                    "target_number": target.number,
                    "ring_seconds": TRANSFER_RING_SECONDS,
                    # §12.3-4: agent-to-manager only, ~6 seconds.
                    "whisper": whisper,
                    # §12.3-1: the caller hears this before anything happens.
                    "say_first": _transfer_line(target),
                }

            return _fallback(centre, reason, why="chain_exhausted", urgency=urgency)


class SendWhatsApp(Tool):
    """Send an approved template to the caller (§14)."""

    name = "send_whatsapp"
    description = (
        "Send the caller a WhatsApp with an offer, price list, dosage card, "
        "centre location or ticket reference. Only pre-approved templates can be "
        "sent, and only to a caller whose consent is current -- if the result "
        "says consent is missing, ask for it on the call first."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "template": {"type": "string", "minLength": 2, "maxLength": 80},
            "params": {"type": "object"},
        },
        "required": ["template"],
    }
    read_only = False

    def __init__(self, port: WhatsAppPort | None = None) -> None:
        self._port = port or QueueingWhatsAppPort()

    async def run(self, args: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
        template = str(args["template"]).strip()
        params = args.get("params") or {}
        if not isinstance(params, dict):
            raise InvalidArgumentError(field="params", reason="must be an object")

        if context.farmer_id is None:
            # Nothing to send to, and no number the model could supply instead:
            # a phone number never travels through a tool argument (§17).
            raise InvalidArgumentError(
                field="farmer_id", reason="is not set -- no identified caller on this call"
            )

        async with tool_session() as session:
            farmer: Farmer | None = await session.get(Farmer, uuid.UUID(context.farmer_id))
            if farmer is None:
                raise NotFoundError(resource="farmer", identifier="caller")

            # Consent is checked here rather than trusted from the context
            # block: the block is built once per call, and a farmer may have
            # opted out *during* it (§13.2 makes that synchronous for exactly
            # this reason).
            from .farmer import _may_send_whatsapp

            if not await _may_send_whatsapp(session, farmer, farmer.phone_hash):
                return {
                    "sent": False,
                    "reason": "no_valid_consent",
                    "ask": "क्या मैं आपको WhatsApp पर जानकारी भेज दूँ?",
                }

            message_id = await self._port.send_template(
                to_phone_hash=farmer.phone_hash,
                template=template,
                params={str(k): str(v) for k, v in params.items()},
                language=farmer.preferred_language,
            )
            return {
                "sent": True,
                "template": template,
                "message_id": message_id,
                "to_last4": farmer.phone_last4,
            }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _default_urgency(reason: TransferReason) -> TransferUrgency:
    if reason is TransferReason.SAFETY_EMERGENCY:
        return TransferUrgency.CRITICAL
    if reason in (TransferReason.ABUSE_OR_ANGER, TransferReason.LEGAL_OR_DISPUTE):
        return TransferUrgency.HIGH
    return TransferUrgency.NORMAL


def _is_open(centre: Centre, now: datetime) -> bool:
    if _DAY_CODES[now.weekday()] not in centre.working_days:
        return False
    current: time = now.time()
    return centre.open_time <= current <= centre.close_time


async def _centre(session: AsyncSession, context: ToolContext) -> Centre | None:
    if context.centre_id:
        return await session.get(Centre, uuid.UUID(context.centre_id))
    if context.farmer_id:
        farmer: Farmer | None = await session.get(Farmer, uuid.UUID(context.farmer_id))
        if farmer is not None and farmer.assigned_centre_id is not None:
            return await session.get(Centre, farmer.assigned_centre_id)
    centre: Centre | None = await session.scalar(
        select(Centre).where(Centre.is_active).order_by(Centre.transfer_priority).limit(1)
    )
    return centre


async def _chain(session: AsyncSession, centre: Centre) -> list[TransferTarget]:
    """§12.3-2: manager, centre desk, district regional manager, helpline desk.

    Ordered, not scored. When a farmer is angry about this centre's stock, the
    person who can fix it is this centre's manager -- reaching a different
    centre first would be faster and less useful.

    All four rungs matter, and the last one especially: the first three all
    depend on data that may be missing (a manager with no number on file, a
    single-centre district). Without a final org-wide desk the chain can be one
    rung deep, and a single busy line then sends every caller to a callback
    ticket -- which looks like the fallback working when it is really the chain
    being too short to walk.
    """
    chain: list[TransferTarget] = []

    if centre.manager_user_id is not None:
        manager: User | None = await session.get(User, centre.manager_user_id)
        if manager is not None and manager.phone:
            chain.append(TransferTarget("centre_manager", manager.full_name, manager.phone, 1))

    if centre.transfer_number:
        chain.append(TransferTarget("centre_desk", centre.name, centre.transfer_number, 2))

    regional = (
        await session.scalars(
            select(Centre)
            .where(
                Centre.district_id == centre.district_id,
                Centre.id != centre.id,
                Centre.is_active,
                Centre.transfer_number.is_not(None),
            )
            .order_by(Centre.transfer_priority)
            .limit(1)
        )
    ).first()
    if regional is not None and regional.transfer_number:
        chain.append(
            TransferTarget("district_desk", regional.name, regional.transfer_number, 3)
        )

    helpline = await _helpline_number(session, centre)
    if helpline is not None:
        chain.append(TransferTarget("helpline_desk", "हेल्पलाइन", helpline, 4))

    return chain


async def _helpline_number(session: AsyncSession, centre: Centre) -> str | None:
    """The central desk, from ``organizations.settings``.

    Configuration rather than a column: it is one string per tenant, it changes
    without a schema migration, and §15 puts transfer chains in the admin panel
    where an operator edits exactly this.
    """
    from uaagro_db.models import Organization

    organization: Organization | None = await session.get(Organization, centre.organization_id)
    if organization is None:
        return None
    number = organization.settings.get("helpline_transfer_number")
    return str(number) if number else None


async def _whisper(
    session: AsyncSession, context: ToolContext, reason: TransferReason, note: str
) -> str:
    """The six seconds the manager hears before the bridge (§12.3-4).

    Terse on purpose. A manager who has to listen to a paragraph before the
    farmer is connected is a manager who stops listening.
    """
    parts: list[str] = []
    if context.farmer_id:
        farmer: Farmer | None = await session.get(Farmer, uuid.UUID(context.farmer_id))
        if farmer is not None:
            parts.extend(p for p in (farmer.full_name, farmer.village) if p)
    parts.append(reason.value.replace("_", " "))
    if note:
        parts.append(note)
    return ", ".join(parts)


def _transfer_line(target: TransferTarget) -> str:
    who = f"{target.name} जी" if target.name else "हमारे साथी"
    return f"जी, मैं आपको {who} से जोड़ रहा हूँ। एक क्षण रुकिए।"


def _fallback(
    centre: Centre, reason: TransferReason, *, why: str, urgency: TransferUrgency
) -> dict[str, Any]:
    """§12.3-6. Never "please call back later" and nothing else."""
    return {
        "action": "commit_callback",
        "reason": reason.value,
        "urgency": urgency.value,
        "unavailable_because": why,
        # The agent is told what to do next rather than left to decide: these
        # three steps together are what stops the call dead-ending (§11.4).
        "next_steps": ["create_ticket", "send_whatsapp"],
        "ticket_priority": "p1",
        "callback_within_hours": 24,
        "centre_phone": centre.phone,
        "centre_hours": f"{centre.open_time:%H:%M}-{centre.close_time:%H:%M}",
    }


__all__: Sequence[str] = (
    "DAILY_TRANSFER_CAP",
    "NullTransferPort",
    "QueueingWhatsAppPort",
    "SendWhatsApp",
    "TransferPort",
    "TransferToHuman",
    "WhatsAppPort",
)
