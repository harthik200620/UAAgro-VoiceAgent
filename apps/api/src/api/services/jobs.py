"""Handing work to the background worker (§11.5, §13.1).

The API never runs a campaign or indexes a document itself: it writes the
row, enqueues the job by name and answers. ARQ's queue is the one the worker
already consumes, so a job enqueued here runs with the worker's session role
and the worker's timeouts.

The pause and stop instructions for a running campaign are not jobs. They are
a key the dialer reads before every dial, so an instruction survives a worker
restart and is visible to whichever process picks the campaign up.

The worker's heartbeat comes back the same way: a key it refreshes every
minute, read here so the panel can say whether anything is going to pick the
queued work up.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

import structlog
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from uaagro_domain.errors import VendorError
from uaagro_domain.settings import get_settings

from ..security.ratelimit import get_redis

log = structlog.get_logger(__name__)

#: Where the background worker says it is alive, and for how long that claim
#: stands. Mirrors ``worker.tasks.HEARTBEAT_KEY``; kept in step by a test.
HEARTBEAT_KEY = "uaagro:worker:heartbeat"
HEARTBEAT_TTL_S = 180

#: A Redis that has not answered by now is reported as silent, not waited on.
HEARTBEAT_READ_BUDGET_S = 2.0

_pool: ArqRedis | None = None


async def _queue() -> ArqRedis:
    global _pool
    if _pool is None:
        try:
            _pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
        except Exception as exc:
            raise VendorError(
                "The job queue is not reachable.",
                remedy="Check REDIS_URL and that Redis is running; the worker needs it too.",
            ) from exc
    return _pool


async def enqueue(job: str, *args: Any) -> None:
    queue = await _queue()
    await queue.enqueue_job(job, *args)
    log.info("jobs.enqueued", job=job)


def control_key(campaign_id: uuid.UUID | str) -> str:
    """Mirrors ``worker.tasks.campaign_control_key``; kept in step by a test."""
    return f"campaign:{campaign_id}:control"


async def set_control(campaign_id: uuid.UUID, instruction: str | None) -> None:
    """Write "paused" or "cancelled" for the dialer, or clear it."""
    queue = await _queue()
    key = control_key(campaign_id)
    if instruction is None:
        await queue.delete(key)
    else:
        await queue.set(key, instruction)


async def worker_last_seen() -> datetime | None:
    """When the background worker last said it was alive, or None.

    None also when Redis cannot be reached: from the panel's side a worker it
    cannot hear from is a worker it cannot see, and the status page says so
    rather than answering 502 for the whole page.
    """
    try:
        raw = await asyncio.wait_for(get_redis().get(HEARTBEAT_KEY), HEARTBEAT_READ_BUDGET_S)
    except Exception as exc:
        log.warning("jobs.heartbeat_unreadable", error=type(exc).__name__)
        return None
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw.decode() if isinstance(raw, bytes) else str(raw))
    except ValueError:
        return None


async def close_jobs() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


__all__ = (
    "HEARTBEAT_KEY",
    "HEARTBEAT_TTL_S",
    "close_jobs",
    "control_key",
    "enqueue",
    "set_control",
    "worker_last_seen",
)
