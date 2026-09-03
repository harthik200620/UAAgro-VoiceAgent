"""Tickets, agent configuration, screening, audit and rate cards (§10, §17)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
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
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from uaagro_domain.enums import (
    AuditAction,
    FlowType,
    SpamAction,
    SpamRuleType,
    TicketPriority,
    TicketStatus,
    TicketType,
)

from ..base import Base, StandardMixin, UserFacingMixin, enum_column, uuid_fk, uuid_pk


class Ticket(Base, UserFacingMixin):
    """Work created by a call.

    §11.4: nothing dead-ends. Every failure path terminates in a resolved
    answer, a human, or a ticket with a callback commitment the farmer was told
    about out loud -- which is why ``due_at`` exists and is not optional for
    callbacks.
    """

    __tablename__ = "tickets"
    __table_args__ = (
        Index("ix_tickets_centre_status", "centre_id", "status", "priority"),
        Index("ix_tickets_assigned_open", "assigned_to_user_id", "status"),
        Index(
            "ix_tickets_due",
            "due_at",
            postgresql_where=text("status IN ('open','in_progress')"),
        ),
        CheckConstraint(
            "type <> 'callback' OR due_at IS NOT NULL", name="callback_needs_commitment"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = uuid_fk("organizations.id")
    ticket_ref: Mapped[str] = mapped_column(String(24), nullable=False, unique=True)
    centre_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    farmer_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    call_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    type: Mapped[TicketType] = enum_column(TicketType, constraint_name="ticket_type_valid")
    priority: Mapped[TicketPriority] = enum_column(
        TicketPriority, constraint_name="ticket_priority_valid", default=TicketPriority.P2
    )
    status: Mapped[TicketStatus] = enum_column(
        TicketStatus, constraint_name="ticket_status_valid", default=TicketStatus.OPEN
    )

    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    assigned_to_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentConfig(Base, UserFacingMixin):
    """A versioned, publishable agent configuration (§15 Flows & Prompts).

    §1 N6: prompts never leave the server. This row is readable only by
    privileged roles and is never serialised into a client bundle.
    """

    __tablename__ = "agent_configs"
    __table_args__ = (
        UniqueConstraint("organization_id", "flow_type", "version", name="uq_agent_config_version"),
        # At most one published config per flow. A partial unique index is the
        # only way to make "exactly one live version" a database guarantee
        # rather than a service-layer hope.
        Index(
            "uq_agent_config_published",
            "organization_id",
            "flow_type",
            unique=True,
            postgresql_where=text("is_published AND deleted_at IS NULL"),
        ),
        CheckConstraint(
            "NOT is_published OR (published_at IS NOT NULL AND published_by_user_id IS NOT NULL)",
            name="published_requires_publisher",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = uuid_fk("organizations.id")
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    flow_type: Mapped[FlowType] = enum_column(FlowType, constraint_name="flow_type_valid")
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    is_published: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    greeting_template: Mapped[str] = mapped_column(Text, nullable=False)
    closing_template: Mapped[str] = mapped_column(Text, nullable=False)
    tool_allowlist: Mapped[list[str]] = mapped_column(
        ARRAY(String(60)), nullable=False, server_default=text("ARRAY[]::varchar[]")
    )

    escalation_rules: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    language_routes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    llm_settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    tts_settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    guardrails: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    #: What the agent says, step by step, as the panel edits it (§13.2, §15).
    #: For an outbound flow: the message, the follow-up question, what each
    #: key does, the closing. Structured rather than one prompt so the legally
    #: fixed lines can be shown locked while the rest stays editable. Empty
    #: means "the defaults in code", which is what every version before the
    #: panel could edit scripts carried.
    script: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    changelog: Mapped[str | None] = mapped_column(Text, nullable=True)


class SpamRule(Base, StandardMixin):
    """Inbound screening (§11.1).

    Defaults are deliberately loose: a false positive on a farmer helpline costs
    far more than an answered spam call, and every rejection is reversible from
    the admin panel.
    """

    __tablename__ = "spam_rules"
    __table_args__ = (
        CheckConstraint("threshold IS NULL OR threshold > 0", name="spam_threshold_positive"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = uuid_fk("organizations.id")
    rule_type: Mapped[SpamRuleType] = enum_column(
        SpamRuleType, constraint_name="spam_rule_type_valid"
    )
    pattern: Mapped[str | None] = mapped_column(String(200), nullable=True)
    action: Mapped[SpamAction] = enum_column(
        SpamAction, constraint_name="spam_action_valid", default=SpamAction.FLAG
    )
    threshold: Mapped[int | None] = mapped_column(Integer, nullable=True)
    window_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    hit_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    #: Operators reverse false positives from the review queue; the counter
    #: makes an over-aggressive rule visible rather than silently costly.
    false_positive_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )


class NumberBlocklist(Base, StandardMixin):
    __tablename__ = "number_blocklist"
    __table_args__ = (Index("ix_number_blocklist_active", "phone_hash", "blocked_until"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    phone_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False, unique=True)
    reason: Mapped[str] = mapped_column(String(200), nullable=False)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    added_by_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)


class AuditLog(Base):
    """Append-only, hash-chained audit trail (§17).

    ``row_hash = SHA256(prev_hash || canonical_payload)``. A daily job walks the
    chain and alerts on a break. There is no ``updated_at`` and no soft delete:
    an audit row that can be modified is not an audit row.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_at_desc", text("at DESC")),
        Index("ix_audit_log_actor", "actor_user_id", text("at DESC")),
        Index("ix_audit_log_resource", "resource_type", "resource_id"),
        Index("ix_audit_log_action", "action", text("at DESC")),
        # The chain is ordered by this monotonic sequence, not by timestamp:
        # two rows can share a millisecond, and ordering must be total.
        UniqueConstraint("chain_index", name="uq_audit_log_chain_index"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    chain_index: Mapped[int] = mapped_column(BigInteger, nullable=False)

    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    actor_ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    action: Mapped[AuditAction] = enum_column(AuditAction, constraint_name="audit_action_valid")
    resource_type: Mapped[str] = mapped_column(String(60), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(120), nullable=True)

    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    prev_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32), nullable=True)
    row_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)


class ApiKey(Base, StandardMixin):
    """Machine credentials. Scoped, hashed, expiring (§17)."""

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = uuid_fk("organizations.id")
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    #: Argon2 of the key. The key itself is shown once at creation and never
    #: stored or returned again.
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    #: Non-secret lookup prefix, so verification does not scan every row.
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(String(60)), nullable=False, server_default=text("ARRAY[]::varchar[]")
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VendorRate(Base, StandardMixin):
    """The live rate card (§8).

    Rates are data, refreshed by a daily job, so a vendor price change shows up
    as a cost regression in the dashboard rather than as a surprise at the end
    of the month.
    """

    __tablename__ = "vendor_rates"
    __table_args__ = (
        UniqueConstraint(
            "vendor", "service", "unit", "effective_from", name="uq_vendor_rate_effective"
        ),
        Index("ix_vendor_rates_lookup", "vendor", "service", text("effective_from DESC")),
        CheckConstraint("rate >= 0", name="rate_non_negative"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    vendor: Mapped[str] = mapped_column(String(60), nullable=False)
    service: Mapped[str] = mapped_column(String(60), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'INR'"))
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(400), nullable=True)
    #: When a human last confirmed this against the vendor's published price
    #: (§24). Surfaced on the compliance dashboard.
    verified_at: Mapped[date | None] = mapped_column(Date, nullable=True)


class IdempotencyKey(Base, StandardMixin):
    """§22: idempotency keys on every mutating endpoint and background job.

    A retried campaign approval or a duplicated webhook must not produce two
    effects; the stored response is replayed instead.
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("scope", "key", name="uq_idempotency_scope_key"),
        Index("ix_idempotency_expiry", "expires_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    scope: Mapped[str] = mapped_column(String(80), nullable=False)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
