"""Organisation, geography, centres and staff (§10)."""

from __future__ import annotations

import uuid
from datetime import datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
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

from uaagro_domain.enums import AccessLevel, Role

from ..base import Base, StandardMixin, UserFacingMixin, enum_column, uuid_fk, uuid_pk


class Organization(Base, StandardMixin):
    """The tenant. Multi-tenancy exists from day one (§17) so a second agri
    retailer can be onboarded without a migration."""

    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    brand_name: Mapped[str] = mapped_column(String(200), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Kolkata")
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class District(Base, StandardMixin):
    """A district, carrying its own bigha conversion factor.

    KB §7: the bigha is not fixed in Uttar Pradesh -- it varies by district and
    even by tehsil. Storing the factor here is what lets ``calculate_dose``
    answer in the farmer's own unit without guessing.
    """

    __tablename__ = "districts"
    __table_args__ = (
        UniqueConstraint("state", "name", name="uq_districts_state_name"),
        CheckConstraint("bigha_acres > 0", name="bigha_acres_positive"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    name_hi: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str] = mapped_column(String(80), nullable=False, default="Uttar Pradesh")
    code: Mapped[str | None] = mapped_column(String(16), nullable=True)

    #: Acres in one local bigha. Defaults to the UP pucca bigha.
    bigha_acres: Mapped[Decimal] = mapped_column(
        Numeric(8, 5), nullable=False, server_default=text("0.625")
    )
    #: False when the factor is the platform default rather than a locally
    #: confirmed value. The agent asks the farmer to confirm before advising.
    bigha_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    centres: Mapped[list[Centre]] = relationship(back_populates="district")


class Centre(Base, UserFacingMixin):
    """A Kisan Sewa Kendra retail centre."""

    __tablename__ = "centres"
    __table_args__ = (
        Index("ix_centres_district_active", "district_id", "is_active"),
        CheckConstraint("latitude IS NULL OR (latitude BETWEEN -90 AND 90)", name="latitude_range"),
        CheckConstraint(
            "longitude IS NULL OR (longitude BETWEEN -180 AND 180)", name="longitude_range"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = uuid_fk("organizations.id")
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    name_hi: Mapped[str | None] = mapped_column(String(200), nullable=True)
    district_id: Mapped[uuid.UUID] = uuid_fk("districts.id")
    block: Mapped[str | None] = mapped_column(String(120), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Spoken form: landmark first, then road, then town (KB §2).
    address_spoken_hi: Mapped[str | None] = mapped_column(Text, nullable=True)
    pincode: Mapped[str | None] = mapped_column(String(6), nullable=True, index=True)
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)

    manager_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True, index=True
    )
    #: Where a warm transfer lands when the manager is unreachable (§12.3).
    #: The person the agent names when it hands a call over. A name, not a
    #: user: most centre managers never sign in to the panel.
    manager_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    transfer_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    transfer_priority: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1")
    )

    open_time: Mapped[time] = mapped_column(Time, nullable=False, server_default=text("'08:00'"))
    close_time: Mapped[time] = mapped_column(Time, nullable=False, server_default=text("'19:00'"))
    working_days: Mapped[list[str]] = mapped_column(
        ARRAY(String(3)),
        nullable=False,
        server_default=text("ARRAY['mon','tue','wed','thu','fri','sat']::varchar[]"),
    )
    services_offered: Mapped[list[str]] = mapped_column(
        ARRAY(String(40)), nullable=False, server_default=text("ARRAY[]::varchar[]")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    #: The head office. The helpline answers stock and price for this centre
    #: when the caller's own centre is unknown, and says so. One per
    #: organisation, enforced by a partial unique index (migration 0009).
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    district: Mapped[District] = relationship(back_populates="centres")


class User(Base, UserFacingMixin):
    """A staff account. MFA is mandatory (§17), so ``mfa_secret_enc`` being
    NULL means enrolment is outstanding and login cannot complete."""

    __tablename__ = "users"
    __table_args__ = (Index("ix_users_email_lower", text("lower(email)"), unique=True),)

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = uuid_fk("organizations.id")
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[Role] = enum_column(Role, constraint_name="role_valid", index=True)

    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Field-level encrypted TOTP seed (§17). Never leaves the server.
    mfa_secret_enc: Mapped[bytes | None] = mapped_column(nullable=True)
    mfa_enrolled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_login_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    centre_access: Mapped[list[UserCentreAccess]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def mfa_enrolled(self) -> bool:
        return self.mfa_secret_enc is not None


class UserCentreAccess(Base, StandardMixin):
    """Which centres a user may see.

    This table is what the Postgres RLS policies read (via a session GUC), so a
    forgotten ``WHERE`` clause in the API cannot widen a centre manager's view.
    """

    __tablename__ = "user_centre_access"
    __table_args__ = (UniqueConstraint("user_id", "centre_id", name="uq_user_centre_access"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = uuid_fk("users.id", ondelete="CASCADE")
    centre_id: Mapped[uuid.UUID] = uuid_fk("centres.id", ondelete="CASCADE")
    access_level: Mapped[AccessLevel] = enum_column(
        AccessLevel, constraint_name="access_level_valid", default=AccessLevel.READ
    )

    user: Mapped[User] = relationship(back_populates="centre_access")


class RefreshToken(Base, StandardMixin):
    """Rotating refresh tokens with family-level reuse detection (§17).

    A replayed token revokes the entire ``family_id`` rather than just itself:
    replay means the token leaked, and every descendant must be assumed
    compromised.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (Index("ix_refresh_tokens_family_active", "family_id", "revoked_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = uuid_fk("users.id", ondelete="CASCADE")
    family_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    #: SHA-256 of the token. The token itself is never stored.
    token_hash: Mapped[bytes] = mapped_column(nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
