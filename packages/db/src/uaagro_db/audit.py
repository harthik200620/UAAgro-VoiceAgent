"""Tamper-evident audit log (§17).

``row_hash = SHA256(prev_hash || canonical_payload)``. Each row commits to every
row before it, so altering or deleting a historical entry invalidates every hash
that follows and :func:`verify_chain` finds the exact break.

Three things make the chain actually hold:

* **Total order.** Rows are sequenced by ``chain_index`` from a Postgres
  sequence, not by timestamp -- two audit events can share a millisecond, and a
  chain needs an unambiguous predecessor.
* **Serialised appends.** A transaction-scoped advisory lock is taken before
  reading the tail, so two concurrent writers cannot both build on the same
  ``prev_hash`` and produce a fork.
* **Canonical serialisation.** The payload is hashed from sorted-key, separator-
  fixed JSON. Without that, a dict iteration-order change would appear as
  tampering on every subsequent verification.

The application role is granted INSERT but not UPDATE or DELETE on this table
(see :mod:`uaagro_db.rls`), so the chain cannot be rewritten through the app
even if a bug tried to.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_domain.enums import AuditAction

from .models import AuditLog

#: Arbitrary but fixed key for the advisory lock that serialises appends.
_APPEND_LOCK_KEY = 0x11A6_0A17

#: The genesis row has no predecessor.
GENESIS_PREV_HASH: bytes | None = None


def canonical_payload(
    *,
    chain_index: int,
    actor_user_id: uuid.UUID | None,
    action: AuditAction,
    resource_type: str,
    resource_id: str | None,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    at: datetime,
    request_id: str | None,
) -> bytes:
    """Deterministic bytes for one audit row.

    ``sort_keys`` and fixed separators make the encoding independent of dict
    ordering and of the Python version; ``default=str`` keeps UUIDs, Decimals
    and datetimes hashable without a bespoke encoder per call site.
    """
    document = {
        "chain_index": chain_index,
        "actor_user_id": str(actor_user_id) if actor_user_id else None,
        "action": action.value,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "before": before,
        "after": after,
        # Microsecond precision, always UTC, so the same event hashes the same
        # regardless of the server's local timezone.
        "at": at.astimezone(UTC).isoformat(timespec="microseconds"),
        "request_id": request_id,
    }
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")


def compute_row_hash(prev_hash: bytes | None, payload: bytes) -> bytes:
    digest = hashlib.sha256()
    digest.update(prev_hash if prev_hash is not None else b"")
    digest.update(payload)
    return digest.digest()


async def append_audit(
    session: AsyncSession,
    *,
    action: AuditAction,
    resource_type: str,
    resource_id: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    actor_ip: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    request_id: str | None = None,
    at: datetime | None = None,
) -> AuditLog:
    """Append one row, extending the chain.

    Must run inside the caller's transaction: the advisory lock is
    transaction-scoped, and the audit row should commit or roll back with the
    change it describes rather than becoming a record of something that never
    happened.
    """
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _APPEND_LOCK_KEY})

    tail = (
        await session.execute(
            select(AuditLog.row_hash).order_by(AuditLog.chain_index.desc()).limit(1)
        )
    ).scalar_one_or_none()

    chain_index = int(
        (await session.execute(text("SELECT nextval('audit_log_chain_seq')"))).scalar_one()
    )
    moment = at or datetime.now(UTC)
    payload = canonical_payload(
        chain_index=chain_index,
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        before=before,
        after=after,
        at=moment,
        request_id=request_id,
    )
    row = AuditLog(
        chain_index=chain_index,
        actor_user_id=actor_user_id,
        actor_ip=actor_ip,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        before=before,
        after=after,
        at=moment,
        request_id=request_id,
        prev_hash=tail,
        row_hash=compute_row_hash(tail, payload),
    )
    session.add(row)
    await session.flush()
    return row


@dataclass(frozen=True, slots=True)
class ChainVerification:
    ok: bool
    rows_checked: int
    broken_at: int | None = None
    reason: str | None = None


async def verify_chain(session: AsyncSession, *, batch_size: int = 1000) -> ChainVerification:
    """Walk the whole chain and report the first inconsistency.

    Run daily (§17). Streams in batches because this table only grows.
    """
    total = int((await session.execute(select(func.count()).select_from(AuditLog))).scalar_one())
    if total == 0:
        return ChainVerification(ok=True, rows_checked=0)

    previous_hash: bytes | None = GENESIS_PREV_HASH
    checked = 0
    offset = 0

    while offset < total:
        rows = (
            (
                await session.execute(
                    select(AuditLog)
                    .order_by(AuditLog.chain_index.asc())
                    .offset(offset)
                    .limit(batch_size)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            break

        for row in rows:
            if row.prev_hash != previous_hash:
                return ChainVerification(
                    ok=False,
                    rows_checked=checked,
                    broken_at=row.chain_index,
                    reason="prev_hash does not match the preceding row",
                )
            payload = canonical_payload(
                chain_index=row.chain_index,
                actor_user_id=row.actor_user_id,
                action=row.action,
                resource_type=row.resource_type,
                resource_id=row.resource_id,
                before=row.before,
                after=row.after,
                at=row.at,
                request_id=row.request_id,
            )
            if compute_row_hash(row.prev_hash, payload) != row.row_hash:
                return ChainVerification(
                    ok=False,
                    rows_checked=checked,
                    broken_at=row.chain_index,
                    reason="row_hash does not match the row contents",
                )
            previous_hash = row.row_hash
            checked += 1

        offset += len(rows)

    return ChainVerification(ok=True, rows_checked=checked)
