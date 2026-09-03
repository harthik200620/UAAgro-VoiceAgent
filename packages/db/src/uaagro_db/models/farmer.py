"""Farmers, consent and do-not-call state (§10, §17, §18)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    LargeBinary,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from uaagro_domain.enums import ConsentChannel, ConsentType, LandUnit

from ..base import Base, StandardMixin, UserFacingMixin, enum_column, uuid_fk, uuid_pk


class Farmer(Base, UserFacingMixin):
    """A farmer.

    ``phone_hash`` is the only way in. There is deliberately no index on the
    encrypted column and no plaintext column at all -- a database copy on its
    own does not yield a callable phone list (§17).
    """

    __tablename__ = "farmers"
    __table_args__ = (
        Index("ix_farmers_village_district", "village", "district_id"),
        Index("ix_farmers_centre_segment", "assigned_centre_id", "farmer_segment"),
        CheckConstraint("length(phone_last4) = 4", name="phone_last4_length"),
        CheckConstraint(
            "land_area_value IS NULL OR land_area_value > 0", name="land_area_positive"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = uuid_fk("organizations.id")

    #: HMAC-SHA256(national_number, pepper). Unique, and the sole lookup key.
    phone_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False, unique=True)
    #: AES-256-GCM under a KMS-wrapped data key. Decryption is audited.
    phone_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    #: Shown in list views so the admin panel never needs to decrypt.
    phone_last4: Mapped[str] = mapped_column(String(4), nullable=False)

    full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    village: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    block: Mapped[str | None] = mapped_column(String(120), nullable=True)
    district_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True, index=True
    )
    pincode: Mapped[str | None] = mapped_column(String(6), nullable=True)

    preferred_language: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default=text("'hi-IN'")
    )
    secondary_language: Mapped[str | None] = mapped_column(String(12), nullable=True)

    land_area_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)
    land_area_unit: Mapped[LandUnit | None] = enum_column(
        LandUnit, constraint_name="land_area_unit_valid", nullable=True
    )
    primary_crops: Mapped[list[str]] = mapped_column(
        ARRAY(String(60)), nullable=False, server_default=text("ARRAY[]::varchar[]")
    )
    irrigation_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    soil_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    soil_test_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)

    assigned_centre_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True, index=True
    )
    farmer_segment: Mapped[str | None] = mapped_column(String(60), nullable=True)
    lifetime_value: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, server_default=text("0")
    )

    #: ``{slow_speaker, avg_asr_conf, noise_level}`` -- §5.2 raises the
    #: end-of-turn timeout for callers observed to pause, and the flag persists
    #: across calls so a farmer is not cut off twice.
    speech_profile: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_contact_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(String(40)), nullable=False, server_default=text("ARRAY[]::varchar[]")
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    consents: Mapped[list[ConsentRecord]] = relationship(
        back_populates="farmer", cascade="all, delete-orphan"
    )

    @property
    def is_slow_speaker(self) -> bool:
        return bool(self.speech_profile.get("slow_speaker", False))


class ConsentRecord(Base, StandardMixin):
    """Evidence that a farmer agreed to a specific kind of contact.

    §18: consent under the current TCCCPR amendment has a short validity window
    and must be re-acquired. ``expires_at`` is therefore mandatory for
    promotional types, and the campaign gate excludes expired rows
    automatically -- consent is never treated as permanent.
    """

    __tablename__ = "consent_records"
    __table_args__ = (
        Index("ix_consent_farmer_type_active", "farmer_id", "consent_type", "revoked_at"),
        CheckConstraint(
            "consent_type NOT IN ('promotional_voice','promotional_whatsapp') "
            "OR expires_at IS NOT NULL",
            name="promotional_consent_expires",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    farmer_id: Mapped[uuid.UUID] = uuid_fk("farmers.id", ondelete="CASCADE")
    consent_type: Mapped[ConsentType] = enum_column(
        ConsentType, constraint_name="consent_type_valid"
    )
    channel: Mapped[ConsentChannel] = enum_column(
        ConsentChannel, constraint_name="consent_channel_valid"
    )
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: What was said or signed, and where. Field-level sensitive (§17).
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    dlt_consent_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    source_call_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    farmer: Mapped[Farmer] = relationship(back_populates="consents")

    def is_valid_at(self, moment: datetime) -> bool:
        if self.revoked_at is not None and self.revoked_at <= moment:
            return False
        if self.granted_at > moment:
            return False
        return not (self.expires_at is not None and self.expires_at <= moment)


class DndStatus(Base, StandardMixin):
    """National preference register plus the internal do-not-call list.

    §18: a prior customer relationship does not exempt a DND-registered number.
    ``internal_dnc`` is written synchronously during the call that requests it
    (§13.2) -- never deferred to a background job that might fail.
    """

    __tablename__ = "dnd_status"
    __table_args__ = (Index("ix_dnd_scrub_freshness", "last_scrubbed_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    phone_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False, unique=True)
    ncpr_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    is_dnd: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    internal_dnc: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    internal_dnc_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    internal_dnc_source_call_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    last_scrubbed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source: Mapped[str | None] = mapped_column(String(60), nullable=True)

    @property
    def blocks_promotional_calls(self) -> bool:
        return self.is_dnd or self.internal_dnc
