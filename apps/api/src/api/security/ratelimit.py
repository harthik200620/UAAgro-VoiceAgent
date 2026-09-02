"""Redis rate limiting (§17).

A fixed-window counter, which is the right trade here: a sliding-log limiter is
more precise but stores every request, and the thing being protected -- login,
export and search -- needs a cheap ceiling rather than exact fairness.

Limits are per IP *and* per identity where an identity is known, because a
credential-stuffing run rotates addresses while a compromised account does not.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from functools import lru_cache

import redis.asyncio as redis
import structlog

from uaagro_domain.errors import RateLimitedError
from uaagro_domain.settings import get_settings

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Limit:
    """A ceiling: ``requests`` per ``window_seconds``."""

    requests: int
    window_seconds: int

    @property
    def key_suffix(self) -> str:
        return f"{self.requests}p{self.window_seconds}"


#: §17 asks for strict limits on auth, export and search. A farmer helpline's
#: admin panel is low-traffic, so these are deliberately tight.
LOGIN_LIMIT = Limit(requests=8, window_seconds=300)
LOGIN_PER_ACCOUNT_LIMIT = Limit(requests=5, window_seconds=900)
REFRESH_LIMIT = Limit(requests=60, window_seconds=300)
SEARCH_LIMIT = Limit(requests=120, window_seconds=60)
EXPORT_LIMIT = Limit(requests=5, window_seconds=3600)
DEFAULT_LIMIT = Limit(requests=600, window_seconds=60)


REDIS_CONNECT_TIMEOUT_S = 3.0
REDIS_SOCKET_TIMEOUT_S = 5.0


@lru_cache(maxsize=1)
def get_redis() -> redis.Redis:
    # redis-py ships no type information, so the untyped constructor is
    # narrowed here rather than by relaxing mypy across the package.
    client: redis.Redis = redis.from_url(  # type: ignore[no-untyped-call]
        get_settings().redis_url,
        decode_responses=True,
        # A Redis that stops answering must not hold every request open:
        # the limiter fails open on an error, and it should get one quickly.
        socket_connect_timeout=REDIS_CONNECT_TIMEOUT_S,
        socket_timeout=REDIS_SOCKET_TIMEOUT_S,
    )
    return client


def reset_redis_cache() -> None:
    get_redis.cache_clear()


async def check(scope: str, identity: str, limit: Limit) -> None:
    """Consume one unit of ``limit`` for ``identity``.

    Raises:
        RateLimitedError: when the ceiling is reached, carrying the retry delay.

    A Redis outage does **not** block requests. Rate limiting is a protective
    measure, not an authorisation control -- failing closed here would turn a
    cache blip into a full outage of the admin panel during a peak, while
    failing open leaves authentication itself untouched.
    """
    window = limit.window_seconds
    bucket = int(time.time()) // window
    key = f"rl:{scope}:{limit.key_suffix}:{identity}:{bucket}"

    try:
        client = get_redis()
        pipeline = client.pipeline()
        pipeline.incr(key)
        pipeline.expire(key, window + 1)
        count, _ = await pipeline.execute()
    except Exception as exc:
        log.warning("ratelimit.unavailable", scope=scope, error=type(exc).__name__)
        return

    if int(count) > limit.requests:
        retry_after = window - (int(time.time()) % window)
        log.info("ratelimit.exceeded", scope=scope, limit=limit.requests)
        raise RateLimitedError(retry_after_s=max(1, retry_after))


async def close_redis() -> None:
    if get_redis.cache_info().currsize:
        await get_redis().aclose()
    reset_redis_cache()
