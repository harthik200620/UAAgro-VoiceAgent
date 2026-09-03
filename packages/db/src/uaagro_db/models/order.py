"""Orders and their lines.

**These tables are not in §10.** They are added because §6.3 requires
``get_order_status(farmer_id, order_ref?)`` and §6.2 puts "last 3 orders" in
the context block of every turn -- neither is implementable against the schema
as written, and §1 N1 forbids the agent answering "where is my order?" from
anything but stored data. The gap is recorded in ``docs/ARCHITECTURE.md``.

Kept deliberately thin: this is not a commerce system. It holds what a farmer
asks about on the phone -- what was ordered, what it cost, where it is, and when
it arrives -- and nothing about payments beyond whether money is still owed.
Anything richer belongs in whatever ERP UA Agro actually bills from.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from uaagro_domain.enums import FulfilmentMode, OrderStatus

from ..base import Base, UserFacingMixin, enum_column, uuid_fk, uuid_pk

#: States in which an order is still coming. Used to decide whether the agent
#: quotes an ETA or speaks in the past tense.
OPEN_STATES = (
    OrderStatus.PLACED,
    OrderStatus.CONFIRMED,
    OrderStatus.PACKED,
    OrderStatus.DISPATCHED,
    OrderStatus.READY_FOR_PICKUP,
)


class Order(Base, UserFacingMixin):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_farmer_time", "farmer_id", "placed_at"),
        Index("ix_orders_centre_status", "centre_id", "status"),
        CheckConstraint("total_amount >= 0", name="order_total_non_negative"),
        CheckConstraint(
            "amount_due >= 0 AND amount_due <= total_amount", name="order_due_within_total"
        ),
        # A farmer told "आपका ऑर्डर भेज दिया गया है" must be able to hear when it
        # will arrive. Dispatched with no promised date is a call that ends in
        # "I don't know", which §11.4 does not allow.
        CheckConstraint(
            "status <> 'dispatched' OR promised_date IS NOT NULL",
            name="dispatched_needs_promised_date",
        ),
        CheckConstraint(
            "status <> 'delivered' OR delivered_at IS NOT NULL",
            name="delivered_needs_timestamp",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = uuid_fk("organizations.id")
    #: Spoken back to the farmer, so it must survive a phone line: short, no
    #: characters that sound alike when read out in Hindi.
    order_ref: Mapped[str] = mapped_column(String(24), nullable=False, unique=True)
    farmer_id: Mapped[uuid.UUID] = uuid_fk("farmers.id", ondelete="CASCADE")
    centre_id: Mapped[uuid.UUID] = uuid_fk("centres.id")

    status: Mapped[OrderStatus] = enum_column(
        OrderStatus, constraint_name="order_status_valid", default=OrderStatus.PLACED, index=True
    )
    mode: Mapped[FulfilmentMode] = enum_column(
        FulfilmentMode, constraint_name="fulfilment_mode_valid", default=FulfilmentMode.PICKUP
    )

    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: What the farmer was told. Read out, so it is a date and not a timestamp.
    promised_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    amount_due: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, server_default="0")

    #: Free text from the centre, e.g. a courier name. Never invented by the
    #: agent -- if it is empty the agent says it does not have that detail.
    delivery_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    placed_via_call_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATES


class OrderItem(Base, UserFacingMixin):
    __tablename__ = "order_items"
    __table_args__ = (
        Index("ix_order_items_order", "order_id"),
        CheckConstraint("quantity > 0", name="order_item_quantity_positive"),
        CheckConstraint("unit_price >= 0", name="order_item_price_non_negative"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    order_id: Mapped[uuid.UUID] = uuid_fk("orders.id", ondelete="CASCADE")
    variant_id: Mapped[uuid.UUID] = uuid_fk("product_variants.id")
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")
