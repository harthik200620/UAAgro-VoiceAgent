"""Offers, campaigns and outbound messaging (§13, §14).

The governing rule is §1 N2: the agent never chooses whom to call. Targets come
only from an operator-built, operator-approved campaign list, and the compliance
gate (§13.1) is enforced here in the schema as well as in code -- a campaign
cannot reach ``running`` without an approver distinct from its creator.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from uaagro_domain.enums import (
    CampaignStatus,
    ContactStatus,
    ExclusionReason,
    InterestLevel,
    MessageDirection,
    MessageStatus,
)

from ..base import Base, StandardMixin, UserFacingMixin, enum_column, uuid_fk, uuid_pk


class Offer(Base, UserFacingMixin):
    """A promotion. The pitch is rendered from this row, never improvised
    (§13.2) -- an agent that invents an offer invents a price."""

    __tablename__ = "offers"
    __table_args__ = (
        CheckConstraint("valid_to > valid_from", name="offer_validity_ordered"),
        CheckConstraint("discount_value >= 0", name="discount_non_negative"),
        Index("ix_offers_active_window", "is_active", "valid_from", "valid_to"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = uuid_fk("organizations.id")
    code: Mapped[str] = mapped_column(String(48), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description_hi: Mapped[str] = mapped_column(Text, nullable=False)
    description_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    offer_type: Mapped[str] = mapped_column(String(40), nullable=False)

    discount_type: Mapped[str] = mapped_column(String(24), nullable=False)
    discount_value: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    applicable_variant_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False, server_default=text("ARRAY[]::uuid[]")
    )
    min_purchase: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)

    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date] = mapped_column(Date, nullable=False)
    terms_hi: Mapped[str | None] = mapped_column(Text, nullable=True)

    whatsapp_template_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    whatsapp_template_params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    versions: Mapped[list[OfferVersion]] = relationship(
        back_populates="offer", cascade="all, delete-orphan"
    )

    def is_live_on(self, day: date) -> bool:
        return self.is_active and self.valid_from <= day <= self.valid_to


class OfferVersion(Base, StandardMixin):
    """Immutable snapshot of an offer as published, so a call made last week
    can be reconciled against the terms that were live then."""

    __tablename__ = "offer_versions"
    __table_args__ = (UniqueConstraint("offer_id", "version", name="uq_offer_version"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    offer_id: Mapped[uuid.UUID] = uuid_fk("offers.id", ondelete="CASCADE")
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    offer: Mapped[Offer] = relationship(back_populates="versions")


class Campaign(Base, UserFacingMixin):
    """An outbound calling campaign."""

    __tablename__ = "campaigns"
    __table_args__ = (
        Index("ix_campaigns_status_schedule", "status", "scheduled_start"),
        CheckConstraint("max_concurrent_calls > 0", name="concurrency_positive"),
        CheckConstraint("max_attempts_per_contact > 0", name="attempts_positive"),
        # §13.1 four-eyes: the creator cannot approve their own campaign, and a
        # campaign cannot leave the approval states without an approver. Both
        # halves are enforced in the database, not only in the service layer.
        # The one way past the first half is ``self_approved``: a row that says
        # so, written only by the owner's account or by a quick dial where the
        # deployment waives the rule (migration 0011). The waiver is on the row,
        # so an auditor sees it without reading the service's configuration.
        CheckConstraint(
            "approved_by_user_id IS NULL OR approved_by_user_id <> created_by OR self_approved",
            name="approver_differs_from_creator",
        ),
        CheckConstraint(
            "status IN ('draft','pending_approval','cancelled') "
            "OR (approved_by_user_id IS NOT NULL AND approved_at IS NOT NULL)",
            name="running_campaign_requires_approval",
        ),
        # §18: promotional calling requires a designated-series CLI. A campaign
        # cannot be approved with a plain 10-digit mobile number.
        CheckConstraint(
            "status IN ('draft','pending_approval','cancelled') OR caller_id_number IS NOT NULL",
            name="approved_campaign_requires_cli",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = uuid_fk("organizations.id")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    offer_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    agent_config_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    status: Mapped[CampaignStatus] = enum_column(
        CampaignStatus,
        constraint_name="campaign_status_valid",
        default=CampaignStatus.DRAFT,
        index=True,
    )

    target_centre_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False, server_default=text("ARRAY[]::uuid[]")
    )
    target_segment: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    source_type: Mapped[str] = mapped_column(String(24), nullable=False)

    scheduled_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scheduled_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Enforced at dial time in recipient local time, not by the schedule alone.
    daily_window_start: Mapped[time] = mapped_column(
        Time, nullable=False, server_default=text("'09:00'")
    )
    daily_window_end: Mapped[time] = mapped_column(
        Time, nullable=False, server_default=text("'21:00'")
    )

    max_concurrent_calls: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("5")
    )
    max_attempts_per_contact: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("3")
    )
    retry_gap_hours: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("24")
    )

    caller_id_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    dlt_template_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    dlt_entity_id: Mapped[str | None] = mapped_column(String(80), nullable=True)

    #: Whether §13.1's promotional rules apply.
    #:
    #: Stored rather than inferred from ``offer_id``. Every promotional check --
    #: the 140-series CLI, the consent requirement, the DND scrub -- turns on
    #: this one boolean, and inferring it from "has an offer attached" would
    #: make a compliance boundary depend on a data-entry accident. An operator
    #: who forgot to link the offer would get a campaign that skipped the
    #: consent check and dialled from a mobile, and every call would connect.
    is_promotional: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )

    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: True when the approver is also the creator: the owner's account, or a
    #: panel quick dial with ``OUTBOUND_QUICK_DIAL_SELF_APPROVE`` on. Stored so
    #: the four-eyes waiver is a fact on the row, not an inference.
    self_approved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pause_reason: Mapped[str | None] = mapped_column(String(160), nullable=True)
    complaint_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    #: Funnel counters, refreshed by the dialer.
    stats: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    #: Per-check removal counts from the compliance gate, so the UI can show
    #: exactly how many contacts each rule took out (§13.1).
    compliance_report: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    contacts: Mapped[list[CampaignContact]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )

    @property
    def is_dialable(self) -> bool:
        return self.status is CampaignStatus.RUNNING and self.approved_at is not None


class CampaignContact(Base, StandardMixin):
    """One target in a campaign."""

    __tablename__ = "campaign_contacts"
    __table_args__ = (
        UniqueConstraint("campaign_id", "phone_hash", name="uq_campaign_contact"),
        Index("ix_campaign_contacts_dialable", "campaign_id", "status", "next_attempt_at"),
        Index("ix_campaign_contacts_farmer", "farmer_id"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint(
            "status <> 'scrubbed_out' OR exclusion_reason IS NOT NULL",
            name="scrubbed_needs_reason",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    campaign_id: Mapped[uuid.UUID] = uuid_fk("campaigns.id", ondelete="CASCADE")
    farmer_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    phone_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)

    status: Mapped[ContactStatus] = enum_column(
        ContactStatus, constraint_name="contact_status_valid", default=ContactStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    outcome: Mapped[str | None] = mapped_column(String(60), nullable=True)
    interest_level: Mapped[InterestLevel | None] = enum_column(
        InterestLevel, constraint_name="interest_level_valid", nullable=True
    )
    dtmf_response: Mapped[str | None] = mapped_column(String(4), nullable=True)
    #: The call this contact's most recent attempt produced. Set by the media
    #: path when it recognises an outbound call, so the panel's contact card
    #: opens the right recording without a join on hash and time.
    call_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    whatsapp_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    whatsapp_message_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    exclusion_reason: Mapped[ExclusionReason | None] = enum_column(
        ExclusionReason, constraint_name="exclusion_reason_valid", nullable=True
    )

    campaign: Mapped[Campaign] = relationship(back_populates="contacts")


class WhatsAppMessage(Base, StandardMixin):
    """A template send or an inbound reply (§14).

    ``cost`` is stored per message because marketing templates cost roughly
    7.5x a utility template in India, and the admin panel must show the
    projected spend before a campaign is approved.
    """

    __tablename__ = "whatsapp_messages"
    __table_args__ = (
        Index("ix_whatsapp_provider_message", "provider_message_id", unique=True),
        Index("ix_whatsapp_farmer_time", "farmer_id", "created_at"),
        Index("ix_whatsapp_status", "status", "status_updated_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    farmer_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    call_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    template_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    category: Mapped[str | None] = mapped_column(String(24), nullable=True)
    #: Server-rendered from the database. Raw caller speech is never
    #: interpolated into a template parameter (§14).
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    direction: Mapped[MessageDirection] = enum_column(
        MessageDirection, constraint_name="wa_direction_valid"
    )
    provider_message_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[MessageStatus] = enum_column(
        MessageStatus, constraint_name="wa_status_valid", default=MessageStatus.QUEUED
    )
    status_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cost: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
