"""Failing fast when a vendor is already down (§11.4, §7).

The retry ladders in this codebase are built for a vendor that is *having a bad
moment*: try, wait, try the other model, then hand the caller to a person. They
are exactly wrong for a vendor that is **down**, because every call pays the
full wait again.

Concretely, with the timeouts as configured: an unreachable model costs
3,500 ms on the primary and 3,500 ms on the fallback before §11.4 escalates.
Seven seconds of dead air, on every turn, of every call, for as long as the
outage lasts -- while the caller hears nothing and cannot tell the line is
still open. The information needed to skip all of that was available after the
first failure.

Retrying also actively prevents recovery. A vendor shedding load under
pressure gets the full offered load back from every worker, continuously, which
is the classic way a brief degradation becomes a long outage.

So each vendor gets a breaker:

``closed``
    Normal. Calls go through and failures are counted.

``open``
    The last :attr:`failure_threshold` calls failed. New calls are refused
    *immediately* -- microseconds, not seconds -- so the pipeline reaches
    §11.4's cached phrase and its escalation at once instead of after two
    timeouts. The vendor gets no traffic and a chance to recover.

``half_open``
    After :attr:`recovery_timeout_s`, one call is let through as a probe. It
    succeeds and the breaker closes; it fails and the breaker opens again for
    another cooldown. One probe, not a thundering herd.

**What counts as a failure is the part worth getting right.** Only faults that
mean *the vendor cannot serve us*: timeouts, connection errors, 5xx. A 4xx is
this system's own mistake -- a voice id that was never filled in, a language the
vendor does not speak -- and tripping the breaker on those would take a working
vendor out of service because of a typo in a config file, then report it as an
outage. Those propagate untouched.

Written here rather than taken from a library for the same reason
:mod:`voice_worker.adapters.telephony.mulaw` is: it is a small amount of code
whose exact failure semantics matter more than its novelty, and the choices
above are specific to a phone call that a person is waiting on.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeVar

import structlog

from uaagro_domain.errors import UAAgroError, VendorTimeoutError, VendorUnavailableError

log = structlog.get_logger(__name__)

T = TypeVar("T")


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


#: Consecutive failures before a vendor is considered down.
#:
#: Three, not one. A single timeout is normal on a mobile network and the ladder
#: already handles it; opening on one would make the breaker itself the outage.
DEFAULT_FAILURE_THRESHOLD = 3

#: How long to leave it open before probing.
#:
#: Long enough that a restarting vendor is actually back, short enough that a
#: brief blip does not cost a whole call. A caller who reaches a person because
#: of an outage does not come back to the agent mid-call anyway.
DEFAULT_RECOVERY_TIMEOUT_S = 20.0


class CircuitOpen(VendorUnavailableError):
    """Refused without being attempted, because the vendor is known to be down.

    A subclass of :class:`VendorUnavailableError` on purpose: every caller
    already knows how to degrade from that, and the whole value here is
    reaching that degraded path *immediately* rather than after two timeouts.
    """

    def __init__(self, vendor: str, service: str, *, retry_in_s: float) -> None:
        super().__init__(
            vendor=vendor,
            service=service,
            detail=(
                f"circuit open after repeated failures; not retried for "
                f"another {retry_in_s:.0f}s"
            ),
        )
        self.retry_in_s = retry_in_s


@dataclass
class CircuitBreaker:
    """One vendor's health, shared by every call on this worker."""

    vendor: str
    service: str
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    recovery_timeout_s: float = DEFAULT_RECOVERY_TIMEOUT_S

    _failures: int = field(default=0, repr=False)
    _opened_at: float | None = field(default=None, repr=False)
    _probing: bool = field(default=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # -- state ------------------------------------------------------------- #

    @property
    def state(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if time.monotonic() - self._opened_at >= self.recovery_timeout_s:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    @property
    def retry_in_s(self) -> float:
        if self._opened_at is None:
            return 0.0
        return max(0.0, self.recovery_timeout_s - (time.monotonic() - self._opened_at))

    # -- the guard --------------------------------------------------------- #

    async def call(self, operation: Callable[[], Awaitable[T]]) -> T:
        """Run ``operation`` unless this vendor is already known to be down."""
        await self._admit()
        try:
            result = await operation()
        except BaseException as exc:
            self._record(exc)
            raise
        self._succeeded()
        return result

    async def _admit(self) -> None:
        """Raise immediately if the circuit is open, or claim the one probe."""
        async with self._lock:
            state = self.state
            if state is BreakerState.CLOSED:
                return
            if state is BreakerState.OPEN:
                raise CircuitOpen(self.vendor, self.service, retry_in_s=self.retry_in_s)
            # Half open: exactly one caller becomes the probe. The rest are
            # still refused, because a herd of probes against a vendor that has
            # just come back is how it goes down again.
            if self._probing:
                raise CircuitOpen(self.vendor, self.service, retry_in_s=0.0)
            self._probing = True
            log.info("breaker.probing", vendor=self.vendor, service=self.service)

    def _succeeded(self) -> None:
        if self._opened_at is not None:
            log.info("breaker.closed", vendor=self.vendor, service=self.service)
        self._failures = 0
        self._opened_at = None
        self._probing = False

    def _record(self, exc: BaseException) -> None:
        """Count a failure, but only if it says the *vendor* is unwell."""
        self._probing = False

        if isinstance(exc, CircuitOpen):
            # Refused by this breaker; not evidence about the vendor.
            return
        if not _is_vendor_fault(exc):
            # This system's own mistake -- a voice id nobody filled in, a
            # language the vendor does not speak. Tripping on these would take
            # a healthy vendor out of service over a typo in a config file and
            # then report it as an outage.
            return

        self._failures += 1
        if self._failures < self.failure_threshold:
            return

        # (Re)start the cooldown, including when this was the half-open probe.
        # Leaving `_opened_at` at its original value would keep the breaker
        # permanently half-open once the first probe failed, admitting a fresh
        # probe on every subsequent call -- which is the retry storm this
        # exists to prevent, only arriving one call at a time.
        reopening = self._opened_at is not None
        self._opened_at = time.monotonic()
        log.warning(
            "breaker.reopened" if reopening else "breaker.opened",
            vendor=self.vendor,
            service=self.service,
            failures=self._failures,
            cooldown_s=self.recovery_timeout_s,
        )

    def record_failure(self, exc: BaseException) -> None:
        """Report a failure from an operation this breaker could not wrap.

        A streaming generator and a long-lived socket cannot be run inside
        :meth:`call` -- doing so would consume the generator, or hold the
        breaker for the length of a call -- so those paths use :meth:`call`
        purely as an admission check and report the outcome here.
        """
        self._record(exc)

    def record_success(self) -> None:
        """Counterpart to :meth:`record_failure` for the same paths."""
        self._succeeded()

    def reset(self) -> None:
        """Forget everything. For tests and for a deliberate operator override."""
        self._failures = 0
        self._opened_at = None
        self._probing = False


def _is_vendor_fault(exc: BaseException) -> bool:
    """Whether this exception means the vendor cannot serve us.

    Timeouts and connection errors, yes. A refusal the vendor gave us a
    considered answer to, no -- see :class:`ConfigurationError` and the 4xx
    cases in the adapters.
    """
    if isinstance(exc, TimeoutError | VendorTimeoutError):
        return True
    if isinstance(exc, CircuitOpen):
        return False
    if isinstance(exc, VendorUnavailableError):
        return True
    if isinstance(exc, UAAgroError):
        # Every other domain error is ours: configuration, missing credentials,
        # a tool refusing. None of them says anything about vendor health.
        return False
    # Anything unrecognised is most likely a transport error from a vendor
    # client -- httpx, websockets -- and those are exactly what this is for.
    return True


#: One breaker per vendor and service, for the life of the process.
#:
#: Shared for the same reason the connection pool is: the adapters are rebuilt
#: for every call, and a breaker that forgets what it learned when the call
#: ends has learned nothing. The whole point is that call #2 benefits from what
#: call #1 discovered.
_BREAKERS: dict[str, CircuitBreaker] = {}


def breaker_for(vendor: str, service: str) -> CircuitBreaker:
    key = f"{vendor}:{service}"
    existing = _BREAKERS.get(key)
    if existing is None:
        existing = CircuitBreaker(vendor=vendor, service=service)
        _BREAKERS[key] = existing
    return existing


def breaker_states() -> dict[str, str]:
    """Every breaker and where it stands. Feeds the readiness probe and §19."""
    return {key: b.state.value for key, b in _BREAKERS.items()}


def reset_breakers() -> None:
    for b in _BREAKERS.values():
        b.reset()
    _BREAKERS.clear()


__all__ = (
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_RECOVERY_TIMEOUT_S",
    "BreakerState",
    "CircuitBreaker",
    "CircuitOpen",
    "breaker_for",
    "breaker_states",
    "reset_breakers",
)
