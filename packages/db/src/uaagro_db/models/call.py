"""Call records, turns and events (§10).

These are the largest tables by two orders of magnitude at 80 centres with
seasonal peaks (§3), so all four are **range-partitioned monthly** on
``started_at``.

Two consequences of partitioning that the schema has to live with:

* Postgres requires the partition key in every unique constraint, so the
  natural key is ``(call_ref, started_at)`` rather than ``call_ref`` alone. A
  lookup by provider SID during a live call goes through Redis, which already
  holds ephemeral per-call state (§4.1).
* ``call_turns`` and ``call_events`` carry no foreign key to ``calls``. A FK to
  a partitioned table would have to reference ``(id, started_at)``, and the
  check runs on every insert -- these rows are written from the audio path,
  where §7.6 forbids paying for anything avoidable. Referential integrity is
  maintained by the writer, which creates the parent row first (§11.1 INIT).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from uaagro_domain.enums import (
    CallDirection,
    CallOutcome,
    CallStatus,
    TelephonyProvider,
    TransferReason,
    TurnRole,
)

from ..base import Base, TimestampMixin, enum_column, uuid_pk

#: Applied to every partitioned table's ``__table_args__``.
_PARTITION_BY_STARTED_AT = {"postgresql_partition_by": "RANGE (started_at)"}


class Call(Base, TimestampMixin):
    """One call, inbound or outbound.

    §1 N8: every call produces a complete record -- including calls that failed,
    were rejected as spam, or were abandoned. The row is written at INIT and
    updated incrementally, so a worker crash never loses the call.
    """

    __tablename__ = "calls"
    __table_args__ = (
        UniqueConstraint("call_ref", "started_at", name="uq_calls_ref_started"),
        Index("ix_calls_started_at_desc", text("started_at DESC")),
        Index("ix_calls_farmer_started", "farmer_id", text("started_at DESC")),
        Index("ix_calls_campaign_status", "campaign_id", "status"),
        Index("ix_calls_centre_started", "centre_id", text("started_at DESC")),
        Index("ix_calls_status_started", "status", text("started_at DESC")),
        CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds >= 0", name="duration_non_negative"
        ),
        CheckConstraint(
            "sentiment_score IS NULL OR sentiment_score BETWEEN -1 AND 1",
            name="sentiment_in_range",
        ),
        _PARTITION_BY_STARTED_AT,
    )

    # Composite PK: Postgres requires the partition key in the primary key.
    id: Mapped[uuid.UUID] = uuid_pk()
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    #: Provider call SID.
    call_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    direction: Mapped[CallDirection] = enum_column(
        CallDirection, constraint_name="call_direction_valid", index=True
    )
    provider: Mapped[TelephonyProvider] = enum_column(
        TelephonyProvider, constraint_name="call_provider_valid"
    )

    #: Hashed, never plaintext (§23-6).
    from_number_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32), nullable=True)
    to_number_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32), nullable=True)

    farmer_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    centre_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    language_detected: Mapped[str | None] = mapped_column(String(12), nullable=True)
    language_final: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    #: Which published agent_configs version served this call, for A/B and
    #: rollback attribution.
    agent_config_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    billable_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[CallStatus] = enum_column(
        CallStatus, constraint_name="call_status_valid", default=CallStatus.QUEUED
    )
    outcome: Mapped[CallOutcome | None] = enum_column(
        CallOutcome, constraint_name="call_outcome_valid", nullable=True
    )
    disposition: Mapped[str | None] = mapped_column(String(80), nullable=True)
    sentiment_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    csat_proxy: Mapped[float | None] = mapped_column(Float, nullable=True)

    intents: Mapped[list[str]] = mapped_column(
        ARRAY(String(60)), nullable=False, server_default=text("ARRAY[]::varchar[]")
    )
    entities: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    was_transferred: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    transfer_reason: Mapped[TransferReason | None] = enum_column(
        TransferReason, constraint_name="transfer_reason_valid", nullable=True
    )
    transfer_to_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    transfer_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    transfer_wait_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transfer_completed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    recording_object_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    recording_duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transcript_ready: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    summary_hi: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    action_items: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    #: ``{telephony, stt, tts, llm, compute, total}`` in INR (§8).
    cost_breakdown: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    #: Decimal, like every other Numeric column here. Money summed as float
    #: drifts (₹1.2+₹2.1+₹1.9+₹0.9 = 6.100000000000001), and §8 checks this
    #: against the per-call ceiling and the daily spend cap.
    cost_total_inr: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    #: ``{p50, p95, max, turn_count, per_segment{}}`` in ms (§7).
    latency_stats: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def is_terminal(self) -> bool:
        from uaagro_domain.enums import TERMINAL_CALL_STATUSES

        return self.status in TERMINAL_CALL_STATUSES


class CallTurn(Base, TimestampMixin):
    """One conversational turn, with its own latency and grounding trail.

    ``retrieved_chunk_ids`` and ``tool_results`` are what make §1 N1 auditable:
    every factual claim must trace to a tool call or a retrieved document, and
    an unciteable claim is a test failure (§9).
    """

    __tablename__ = "call_turns"
    __table_args__ = (
        UniqueConstraint("call_id", "turn_index", "started_at", name="uq_call_turn_index"),
        Index("ix_call_turns_call", "call_id", "turn_index"),
        Index("ix_call_turns_low_confidence", "asr_confidence"),
        CheckConstraint("turn_index >= 0", name="turn_index_non_negative"),
        CheckConstraint(
            "asr_confidence IS NULL OR asr_confidence BETWEEN 0 AND 1",
            name="asr_confidence_in_range",
        ),
        _PARTITION_BY_STARTED_AT,
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False
    )

    call_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    #: Denormalised from the parent call purely so the RLS policy is a column
    #: comparison. Resolving the centre through a subquery against ``calls``
    #: would put a correlated lookup on every row of the largest table in the
    #: system, on a path the audio loop writes to.
    centre_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    turn_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    role: Mapped[TurnRole] = enum_column(TurnRole, constraint_name="turn_role_valid")

    text_original: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: After numeric normalisation and lexicon correction (§5.5).
    text_normalised: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(String(12), nullable=True)

    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    audio_offset_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    asr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    was_interrupted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    was_barge_in: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    tool_calls: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    tool_results: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    retrieved_chunk_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False, server_default=text("ARRAY[]::uuid[]")
    )

    llm_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: ``{eot, stt, tool, llm_ttft, tts_ttfb, total}`` in ms (§7).
    latency_ms: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class CallEvent(Base):
    """Append-only event stream for a call.

    Written from the audio path, so it has no ``updated_at`` and no trigger --
    events are never modified.
    """

    __tablename__ = "call_events"
    __table_args__ = (
        Index("ix_call_events_call_time", "call_id", "event_at"),
        Index("ix_call_events_type", "event_type", text("started_at DESC")),
        _PARTITION_BY_STARTED_AT,
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False
    )

    call_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class DtmfEvent(Base):
    """Keypresses. §13.2 accepts DTMF and speech identically -- a farmer in a
    field may not be able to press a key, and one in a market may not be heard.
    """

    __tablename__ = "dtmf_events"
    __table_args__ = (
        Index("ix_dtmf_events_call", "call_id", "received_at"),
        CheckConstraint("digit ~ '^[0-9*#]$'", name="dtmf_digit_valid"),
        _PARTITION_BY_STARTED_AT,
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False
    )

    call_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    digit: Mapped[str] = mapped_column(String(1), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: What the agent had just asked, so a stray keypress is not misread as an
    #: offer confirmation.
    context: Mapped[str | None] = mapped_column(String(80), nullable=True)


#: Tables that are range-partitioned monthly on ``started_at``. The migration
#: and ``ensure_partitions`` both read this list rather than repeating it.
PARTITIONED_TABLES: tuple[str, ...] = (
    "calls",
    "call_turns",
    "call_events",
    "dtmf_events",
)
