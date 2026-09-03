"""External data sources: the client's own database, read into the catalogue.

UA Agro keeps its stores, products and stock in a MySQL database of its own.
The platform's database stays PostgreSQL -- row-level security, vector search
and monthly partitions all depend on it -- so the client's data is *pulled*:
a :class:`DataSource` names the MySQL server and how its tables map onto ours,
and each :class:`DataSourceRun` is one pull, with what it wrote and what went
wrong.

The password is stored encrypted under the same data key that protects farmer
phone numbers, and is never returned by the API: a source row read back says
only that a password is set.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, StandardMixin, TimestampMixin, uuid_fk, uuid_pk

#: The kinds of source this platform can read. One today; the column is
#: checked so a second kind is a deliberate migration, not a typo.
SOURCE_KINDS: tuple[str, ...] = ("mysql",)
SYNC_SCHEDULES: tuple[str, ...] = ("manual", "hourly", "daily")
RUN_STATUSES: tuple[str, ...] = ("running", "ok", "failed")


class DataSource(Base, StandardMixin):
    """A client database the catalogue is synchronised from."""

    __tablename__ = "data_sources"
    __table_args__ = (
        CheckConstraint("kind IN ('mysql')", name="data_source_kind_valid"),
        CheckConstraint(
            "schedule IN ('manual', 'hourly', 'daily')", name="data_source_schedule_valid"
        ),
        CheckConstraint("port BETWEEN 1 AND 65535", name="data_source_port_range"),
        Index("ix_data_sources_org", "organization_id", "is_active"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = uuid_fk("organizations.id")
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'mysql'"))

    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3306"))
    database: Mapped[str] = mapped_column(String(128), nullable=False)
    user: Mapped[str] = mapped_column(String(128), nullable=False)
    #: AES-256-GCM under the platform data key (see ``uaagro_db.crypto``).
    #: NULL when the source authenticates without a password.
    password_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    tls: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    #: ``{"stores": {"table": ..., "columns": {our_field: their_column}},
    #: "products": ..., "stock": ...}``
    mapping: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    schedule: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default=text("'manual'")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    last_run_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    runs: Mapped[list[DataSourceRun]] = relationship(
        back_populates="source", cascade="all, delete-orphan", passive_deletes=True
    )


class DataSourceRun(Base, TimestampMixin):
    """One synchronisation: when, what it wrote, and why it stopped."""

    __tablename__ = "data_source_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'ok', 'failed')", name="data_source_run_status_valid"
        ),
        Index("ix_data_source_runs_source_started", "source_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    source_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("data_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default=text("'running'")
    )
    stores_written: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    products_written: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    stock_written: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Whatever the run wants to say beyond the counts: products missing from
    #: the source, rows skipped and why. Small; the audit log has the rest.
    report: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    started_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    source: Mapped[DataSource] = relationship(back_populates="runs")


__all__ = ("RUN_STATUSES", "SOURCE_KINDS", "SYNC_SCHEDULES", "DataSource", "DataSourceRun")
