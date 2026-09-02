"""Handing work to the background worker (§11.5, §13.1).

The API never runs a campaign or indexes a document itself: it writes the
row, enqueues the job by name and answers. ARQ's queue is the one the worker
already consumes, so a job enqueued here runs with the worker's session role
and the worker's timeouts.

The pause and stop instructions for a running campaign are not jobs. They are
a key the dialer reads before every dial, so an instruction survives a worker
restart and is visible to whichever process picks the campaign up.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from uaagro_domain.errors import VendorError
from uaagro_domain.settings import get_settings

log = structlog.get_logger(__name__)

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


async def close_jobs() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


__all__ = ("close_jobs", "control_key", "enqueue", "set_control")
