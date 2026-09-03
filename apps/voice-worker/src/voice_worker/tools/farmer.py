"""Farmer identity, orders, tickets and intent logging (§6.3).

Four tools that all touch the caller's own record, and therefore all touch the
§17 boundary: a phone number is found by HMAC hash and **never** returned.
``lookup_farmer`` takes a number and gives back a profile; nothing here goes the
other way. Decryption is a privileged, audited operation in the admin panel, not
something a tool the model can call is able to do.

``log_intent`` and ``create_ticket`` write. §6.3 runs writes serially for that
reason -- two concurrent calls could raise the same ticket twice, and a farmer
who is called back twice about one complaint learns the system is not paying
attention.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.crypto import PhoneCipher, build_cipher
from uaagro_db.models import (
    Call,
    ConsentRecord,
    DndStatus,
    Farmer,
    Order,
    OrderItem,
    Product,
    ProductVariant,
    Ticket,
)
from uaagro_db.models.order import OPEN_STATES
from uaagro_domain.enums import (
    ConsentType,
    Intent,
    TicketPriority,
    TicketStatus,
    TicketType,
)
from uaagro_domain.errors import InvalidArgumentError, NotFoundError
from uaagro_domain.phone import normalise_msisdn
from uaagro_domain.settings import Settings

from .base import Tool, ToolContext
from .session import tool_session

log = structlog.get_logger(__name__)

#: §6.2 gives the caller-context block ~150 tokens. Three orders and three open
#: tickets is what fits alongside a name, village and language.
HISTORY_LIMIT = 3

#: How long a callback is promised for. §11.4 requires the commitment to be
#: spoken out loud, so it has to be a number the agent can actually say.
CALLBACK_HOURS = 24

_cipher: PhoneCipher | None = None


def _phone_cipher() -> PhoneCipher:
    """The process-wide cipher.

    Built once: the KMS unwrap behind it is a network call, and doing it per
    lookup would spend the §6.3 budget on key management.
    """
    global _cipher
    if _cipher is None:
        _cipher = build_cipher(Settings())
    return _cipher


#: A well-formed number reserved for warming the statement cache. Never dialled
#: and never stored, so it matches no farmer -- a warm-up that returned a row
#: would pull a real caller's details into memory nothing asked for.
WARMUP_MSISDN = "+919000000000"


class LookupFarmer(Tool):
    """Who is calling, and what the agent already knows about them."""

    name = "lookup_farmer"
    description = (
        "Look up the caller by phone number: name, village, district, assigned "
        "centre, preferred language, recent orders and open tickets. Call this "
        "once at the start of a call. Returns nothing identifying if the number "
        "is not on record -- treat that as a new caller, not an error."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "phone": {
                "type": "string",
                "minLength": 10,
                "maxLength": 20,
                "description": "The caller's number in any common Indian format.",
            }
        },
        "required": ["phone"],
    }

    def warmup_args(self) -> Mapping[str, Any] | None:
        return {"phone": WARMUP_MSISDN}

    async def warm(self) -> None:
        """Compile both statements, not just the first.

        A warm-up on an unknown number returns at the ``farmer is None``
        branch, so ``_history`` -- the four-subquery statement that dominates
        the 671 ms -- would still be compiled on the first real call. This runs
        it too, against an id that no row can carry.
        """
        digest = _phone_cipher().hash(normalise_msisdn(WARMUP_MSISDN))
        async with tool_session() as session:
            await session.scalar(
                select(Farmer).where(Farmer.phone_hash == digest, Farmer.deleted_at.is_(None))
            )
            await _history(session, uuid.uuid4(), digest)

    async def run(self, args: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
        msisdn = normalise_msisdn(str(args["phone"]))
        digest = _phone_cipher().hash(msisdn)

        async with tool_session() as session:
            farmer: Farmer | None = await session.scalar(
                select(Farmer).where(Farmer.phone_hash == digest, Farmer.deleted_at.is_(None))
            )
            if farmer is None:
                # Not an error. A first-time caller is the normal case for a
                # helpline, and raising here would push the agent into an
                # escalation path instead of a greeting.
                return {"known": False, "language": "hi-IN"}

            # One round trip for the history, not four. asyncpg serialises
            # queries on a connection, so five awaits here are five sequential
            # network hops -- enough on its own to miss the 150 ms budget in
            # §6.3, as the first measurement of this tool showed.
            history = await _history(session, farmer.id, digest)

            return {
                "known": True,
                "farmer_id": str(farmer.id),
                "name": farmer.full_name,
                "village": farmer.village,
                "language": farmer.preferred_language,
                "secondary_language": farmer.secondary_language,
                "centre_id": str(farmer.assigned_centre_id) if farmer.assigned_centre_id else None,
                "land": _land(farmer),
                "crops": list(farmer.primary_crops),
                **history,
            }


class GetOrderStatus(Tool):
    """Where a farmer's order has got to."""

    name = "get_order_status"
    description = (
        "Status of a farmer's orders: what was ordered, what it cost, whether it "
        "is ready or dispatched, and the promised date. Give order_ref if the "
        "farmer quoted one, otherwise the most recent orders are returned. Never "
        "state a delivery date that is not in the result."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "farmer_id": {"type": "string", "minLength": 8, "maxLength": 64},
            "order_ref": {"type": "string", "maxLength": 24},
        },
        "required": ["farmer_id"],
    }

    def warmup_args(self) -> Mapping[str, Any] | None:
        return {"farmer_id": "00000000-0000-0000-0000-000000000000"}

    async def run(self, args: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
        farmer_id = _as_uuid(str(args["farmer_id"]), field="farmer_id")
        order_ref = args.get("order_ref")

        async with tool_session() as session:
            query = select(Order).where(Order.farmer_id == farmer_id, Order.deleted_at.is_(None))
            if order_ref:
                query = query.where(Order.order_ref == str(order_ref).strip().upper())
            rows = list(
                (
                    await session.scalars(
                        query.order_by(Order.placed_at.desc()).limit(HISTORY_LIMIT)
                    )
                ).all()
            )

            if not rows and order_ref:
                # A quoted reference that matches nothing is worth saying
                # plainly -- the farmer may have misread it, and a silent empty
                # list would have the agent claim they have no orders at all.
                raise NotFoundError(resource="order", identifier=str(order_ref))

            return {
                "orders": [await _order_payload(session, order) for order in rows],
                "count": len(rows),
            }


class CreateTicket(Tool):
    """Raise work for a human, and commit to a callback."""

    name = "create_ticket"
    description = (
        "Raise a complaint, callback request, sales lead, service booking or "
        "dealership enquiry for the centre team. Use when the answer needs a "
        "human but the caller does not need transferring now. Returns a ticket "
        "reference and a callback time -- say both out loud."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "type": {
                "type": "string",
                "enum": [t.value for t in TicketType],
            },
            "summary": {
                "type": "string",
                "minLength": 4,
                "maxLength": 300,
                "description": "One line, in the caller's own words where possible.",
            },
            "priority": {"type": "string", "enum": [p.value for p in TicketPriority]},
            "detail": {"type": "string", "maxLength": 2000},
        },
        "required": ["type", "summary"],
    }
    read_only = False

    async def run(self, args: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
        ticket_type = TicketType(str(args["type"]))
        priority = TicketPriority(str(args.get("priority") or TicketPriority.P2.value))

        # A safety incident is never filed as routine, whatever the model asked
        # for. §16.1 puts an ingestion or exposure above every other priority in
        # the system, and letting the caller's phrasing set that is not safe.
        if ticket_type is TicketType.SAFETY_INCIDENT:
            priority = TicketPriority.P0

        now = datetime.now(UTC)
        # The constraint in §10 refuses a callback without a commitment, so the
        # due time is set here rather than left for the centre to decide.
        due_at = (
            now + timedelta(hours=CALLBACK_HOURS)
            if ticket_type in (TicketType.CALLBACK, TicketType.SAFETY_INCIDENT)
            else None
        )
        if ticket_type is TicketType.SAFETY_INCIDENT:
            due_at = now + timedelta(hours=1)

        async with tool_session() as session:
            organization_id = await _organization_id(session, context)
            ticket = Ticket(
                organization_id=organization_id,
                ticket_ref=_ticket_ref(now),
                centre_id=_as_uuid(context.centre_id, field="centre_id")
                if context.centre_id
                else None,
                farmer_id=_as_uuid(context.farmer_id, field="farmer_id")
                if context.farmer_id
                else None,
                call_id=_as_uuid(context.call_id, field="call_id", optional=True),
                type=ticket_type,
                priority=priority,
                status=TicketStatus.OPEN,
                subject=str(args["summary"]).strip(),
                description=str(args["detail"]).strip() if args.get("detail") else None,
                due_at=due_at,
            )
            session.add(ticket)
            await session.flush()

            return {
                "ticket_ref": ticket.ticket_ref,
                "type": ticket_type.value,
                "priority": priority.value,
                # Spoken as a commitment, so it is a plain hour count rather
                # than a timestamp the agent would have to render.
                "callback_within_hours": (
                    round((due_at - now).total_seconds() / 3600) if due_at else None
                ),
            }


class LogIntent(Tool):
    """Record what the caller wanted. Called every turn (§6.3)."""

    name = "log_intent"
    description = (
        "Record the caller's intent and the entities mentioned this turn. Call "
        "on every turn. This does not answer anything -- it is for reporting."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "intent": {"type": "string", "enum": [i.value for i in Intent]},
            "entities": {"type": "object"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["intent"],
    }
    read_only = False

    async def run(self, args: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
        intent = Intent(str(args["intent"]))
        entities = args.get("entities") or {}
        if not isinstance(entities, dict):
            raise InvalidArgumentError(field="entities", reason="must be an object")

        # Deliberately not a database write on the turn path. §7 gives the whole
        # turn 1,200 ms and this is analytics: the value is carried back to the
        # conversation loop, which persists it with the turn it belongs to in
        # one statement after the audio has already gone out.
        log.info(
            "turn.intent",
            call_id=context.call_id,
            intent=intent.value,
            entity_keys=sorted(entities),
        )
        return {
            "logged": True,
            "intent": intent.value,
            "entities": entities,
            "confidence": args.get("confidence"),
        }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _as_uuid(value: str | None, *, field: str, optional: bool = False) -> uuid.UUID | None:
    if value is None:
        if optional:
            return None
        raise InvalidArgumentError(field=field, reason="is required")
    try:
        return uuid.UUID(value)
    except ValueError:
        if optional:
            # A synthetic call id from the simulator is not worth failing a
            # ticket over -- the ticket still records the farmer and centre.
            return None
        raise InvalidArgumentError(field=field, reason="is not a valid identifier") from None


def _land(farmer: Farmer) -> str | None:
    if farmer.land_area_value is None or farmer.land_area_unit is None:
        return None
    return f"{farmer.land_area_value.normalize()} {farmer.land_area_unit.value}"


def _ticket_ref(now: datetime) -> str:
    """A reference the agent can read out over a phone line.

    Uppercase hex without the confusable characters would be better still, but
    the reference is also typed into the admin panel, so it stays a plain
    date-plus-suffix that a centre user can search for.
    """
    return f"TKT-{now:%y%m%d}-{uuid.uuid4().hex[:6].upper()}"


async def _organization_id(session: AsyncSession, context: ToolContext) -> uuid.UUID:
    if context.organization_id:
        organization = _as_uuid(context.organization_id, field="organization_id")
        if organization is not None:
            return organization
    # Single-tenant in practice, but the column is not nullable and inventing a
    # value would break the foreign key rather than the call.
    from uaagro_db.models import Organization

    found: uuid.UUID | None = await session.scalar(select(Organization.id).limit(1))
    if found is None:
        raise NotFoundError(resource="organization", identifier="default")
    return found


async def _history(session: AsyncSession, farmer_id: uuid.UUID, digest: bytes) -> dict[str, Any]:
    """Orders, tickets, call count and messaging consent in one statement.

    Four independent lookups that all key on the same farmer. Issued separately
    they cost four round trips; as one query with four scalar subqueries they
    cost one, which is the difference between meeting the §6.3 budget and not.
    """
    # Each history list is aggregated from its own subquery, and the aggregate
    # references that subquery's columns rather than the ORM table -- selecting
    # from one while reading the other joins the table to itself, which
    # Postgres will happily do and which quietly multiplies every row.
    order_rows = (
        select(Order.order_ref, Order.status, Order.placed_at)
        .where(Order.farmer_id == farmer_id, Order.deleted_at.is_(None))
        .order_by(Order.placed_at.desc())
        .limit(HISTORY_LIMIT)
        .subquery()
    )
    orders = (
        select(
            func.coalesce(
                func.json_agg(
                    func.json_build_object(
                        "order_ref",
                        order_rows.c.order_ref,
                        "status",
                        order_rows.c.status,
                        "placed",
                        func.to_char(order_rows.c.placed_at, "YYYY-MM-DD"),
                    )
                ),
                text("'[]'::json"),
            )
        )
        .select_from(order_rows)
        .scalar_subquery()
    )

    ticket_rows = (
        select(Ticket.ticket_ref, Ticket.type, Ticket.subject, Ticket.created_at)
        .where(
            Ticket.farmer_id == farmer_id,
            Ticket.status.in_([TicketStatus.OPEN, TicketStatus.IN_PROGRESS]),
            Ticket.deleted_at.is_(None),
        )
        .order_by(Ticket.created_at.desc())
        .limit(HISTORY_LIMIT)
        .subquery()
    )
    tickets = (
        select(
            func.coalesce(
                func.json_agg(
                    func.json_build_object(
                        "ticket_ref",
                        ticket_rows.c.ticket_ref,
                        "type",
                        ticket_rows.c.type,
                        "subject",
                        ticket_rows.c.subject,
                    )
                ),
                text("'[]'::json"),
            )
        )
        .select_from(ticket_rows)
        .scalar_subquery()
    )

    calls = (
        select(func.count()).select_from(Call).where(Call.farmer_id == farmer_id).scalar_subquery()
    )

    now = datetime.now(UTC)
    consented = (
        select(func.count())
        .select_from(ConsentRecord)
        .where(
            ConsentRecord.farmer_id == farmer_id,
            ConsentRecord.consent_type == ConsentType.PROMOTIONAL_WHATSAPP,
            ConsentRecord.revoked_at.is_(None),
            ConsentRecord.expires_at > now,
        )
        .scalar_subquery()
    )
    blocked = (
        select(func.count())
        .select_from(DndStatus)
        .where(
            DndStatus.phone_hash == digest,
            (DndStatus.is_dnd.is_(True)) | (DndStatus.internal_dnc.is_(True)),
        )
        .scalar_subquery()
    )

    row = (await session.execute(select(orders, tickets, calls, consented, blocked))).one()
    recent_orders, open_tickets, call_count, consent_count, block_count = row

    return {
        "recent_orders": list(recent_orders or []),
        "open_tickets": list(open_tickets or []),
        "previous_calls": int(call_count or 0),
        # §12.2 and §18: whether this caller may be sent a promotional WhatsApp
        # at all. Surfaced here so the agent never offers one it cannot send.
        "may_send_whatsapp": bool(consent_count) and not block_count,
    }


async def _recent_orders(session: AsyncSession, farmer_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await session.scalars(
            select(Order)
            .where(Order.farmer_id == farmer_id, Order.deleted_at.is_(None))
            .order_by(Order.placed_at.desc())
            .limit(HISTORY_LIMIT)
        )
    ).all()
    # Trimmed hard: this goes into the system block of every turn (§6.2), so it
    # carries only what makes the agent sound like it remembers the caller.
    return [
        {
            "order_ref": order.order_ref,
            "status": order.status.value,
            "placed": order.placed_at.date().isoformat(),
        }
        for order in rows
    ]


async def _open_tickets(session: AsyncSession, farmer_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await session.scalars(
            select(Ticket)
            .where(
                Ticket.farmer_id == farmer_id,
                Ticket.status.in_([TicketStatus.OPEN, TicketStatus.IN_PROGRESS]),
                Ticket.deleted_at.is_(None),
            )
            .order_by(Ticket.created_at.desc())
            .limit(HISTORY_LIMIT)
        )
    ).all()
    return [{"ticket_ref": t.ticket_ref, "type": t.type.value, "subject": t.subject} for t in rows]


async def _call_count(session: AsyncSession, farmer_id: uuid.UUID) -> int:
    count: int | None = await session.scalar(
        select(func.count()).select_from(Call).where(Call.farmer_id == farmer_id)
    )
    return int(count or 0)


async def _may_send_whatsapp(session: AsyncSession, farmer: Farmer, digest: bytes) -> bool:
    """§18: promotional consent expires, and DND is not waived by a purchase."""
    dnd: DndStatus | None = await session.scalar(
        select(DndStatus).where(DndStatus.phone_hash == digest)
    )
    if dnd is not None and (dnd.is_dnd or dnd.internal_dnc):
        return False

    now = datetime.now(UTC)
    consent: ConsentRecord | None = await session.scalar(
        select(ConsentRecord)
        .where(
            ConsentRecord.farmer_id == farmer.id,
            ConsentRecord.consent_type == ConsentType.PROMOTIONAL_WHATSAPP,
            ConsentRecord.revoked_at.is_(None),
            ConsentRecord.expires_at > now,
        )
        .order_by(ConsentRecord.granted_at.desc())
    )
    return consent is not None


async def _order_payload(session: AsyncSession, order: Order) -> dict[str, Any]:
    items = (
        await session.execute(
            select(
                OrderItem,
                Product.name_hi,
                ProductVariant.pack_size_value,
                ProductVariant.pack_size_unit,
            )
            .join(ProductVariant, OrderItem.variant_id == ProductVariant.id)
            .join(Product, ProductVariant.product_id == Product.id)
            .where(OrderItem.order_id == order.id)
        )
    ).all()

    payload: dict[str, Any] = {
        "order_ref": order.order_ref,
        "status": order.status.value,
        "mode": order.mode.value,
        "placed": order.placed_at.date().isoformat(),
        "total": _plain(order.total_amount),
        "items": [
            {
                "product_hi": name_hi,
                "pack": f"{pack_value.normalize()} {pack_unit}",
                "quantity": item.quantity,
            }
            for item, name_hi, pack_value, pack_unit in items
        ],
    }
    if order.amount_due > 0:
        payload["amount_due"] = _plain(order.amount_due)
    if order.status in OPEN_STATES and order.promised_date is not None:
        payload["promised_date"] = order.promised_date.isoformat()
    if order.delivered_at is not None:
        payload["delivered"] = order.delivered_at.date().isoformat()
    if order.delivery_note:
        payload["note"] = order.delivery_note
    return payload


def _plain(value: Any) -> str:
    """Decimal without a trailing ``.00``; the agent speaks these."""
    if not isinstance(value, Decimal):
        return str(value)
    if value == value.to_integral_value():
        return str(value.quantize(Decimal(1)))
    return str(value.normalize())


__all__: Sequence[str] = ("CreateTicket", "GetOrderStatus", "LogIntent", "LookupFarmer")
