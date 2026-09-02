"""Declarative base, mixins and column conventions.

§10: every table carries ``id``, ``created_at``, ``updated_at``, ``created_by``,
and ``deleted_at`` where deletion is user-facing.

Enums are stored as ``text`` with a CHECK constraint rather than a native
Postgres enum. Adding a value to a native enum takes an ``ALTER TYPE`` that can
block during a seasonal peak (§3); a CHECK constraint is replaced without a
table rewrite.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import DateTime, ForeignKey, MetaData, func, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: Deterministic constraint names. Without these Alembic autogenerate emits
#: server-assigned names and a downgrade cannot find what to drop.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    #: ``dict[str, Any]`` annotations become JSONB. Every jsonb column in §10
    #: (composition, entities, cost_breakdown, latency_stats, ...) relies on it.
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONB}


def uuid_pk() -> Mapped[uuid.UUID]:
    """Primary key column.

    ``gen_random_uuid()`` is in the Postgres core from version 13, so this needs
    no extension. Earlier builds required pgcrypto for it; that dependency was
    dropped once it proved unnecessary.
    """
    return mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )


def uuid_fk(
    target: str,
    *,
    nullable: bool = False,
    ondelete: str = "RESTRICT",
    index: bool = True,
) -> Mapped[uuid.UUID]:
    return mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(target, ondelete=ondelete),
        nullable=nullable,
        index=index,
    )


def enum_column(
    enum_cls: type[StrEnum],
    *,
    constraint_name: str,
    nullable: bool = False,
    default: StrEnum | None = None,
    index: bool = False,
    length: int = 48,
) -> Mapped[Any]:
    """A text column restricted to an enum's values by CHECK constraint.

    ``native_enum=False`` emits ``VARCHAR(n)`` plus a ``CHECK (col IN (...))``
    rather than a Postgres enum type, for the reason in the module docstring:
    extending a native enum takes an ``ALTER TYPE`` that can block during a
    seasonal peak, while a CHECK constraint is replaced without a table rewrite.

    ``values_callable`` stores the enum's *value* (``"centre_manager"``) rather
    than its member name (``"CENTRE_MANAGER"``), which is what every query,
    fixture and RLS policy expects to see in the column.
    """
    return mapped_column(
        SAEnum(
            enum_cls,
            native_enum=False,
            length=length,
            create_constraint=True,
            name=constraint_name,
            values_callable=lambda members: [m.value for m in members],
            validate_strings=True,
        ),
        nullable=nullable,
        default=default,
        server_default=text(f"'{default.value}'") if default is not None else None,
        index=index,
    )


class TimestampMixin:
    """``created_at`` / ``updated_at``, both server-side.

    ``updated_at`` is maintained by a trigger installed in the initial
    migration, not by the ORM: rows are also written by SQL migrations, the
    seed loader and background jobs, and a Python-side ``onupdate`` would leave
    those paths stale.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ActorMixin:
    """Who created the row. Nullable because seeds and the agent write rows too."""

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True, index=False
    )


class SoftDeleteMixin:
    """Soft delete for user-facing records (§10).

    Deleting a farmer, a product or a centre must not orphan call history, and
    a DPDP erasure request writes a tombstone rather than a hard delete (§17).
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class StandardMixin(TimestampMixin, ActorMixin):
    """The common case: timestamps plus an actor, no soft delete."""


class UserFacingMixin(TimestampMixin, ActorMixin, SoftDeleteMixin):
    """Timestamps, actor and soft delete."""


def utcnow() -> datetime:
    """Timezone-aware now. Never use ``datetime.utcnow`` -- it returns naive."""
    from datetime import UTC

    return datetime.now(UTC)
