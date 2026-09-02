"""Failing fast when a vendor is already down (§11.4, §7).

The retry ladders here are built for a vendor having a bad moment. For one that
is *down* they are exactly wrong: with the configured timeouts an unreachable
model costs 3,500 ms on the primary and 3,500 ms on the fallback before §11.4
escalates -- seven seconds of dead air, per turn, per call, for the length of
the outage. Everything needed to skip that was known after the first failure.

Retrying is also what stops the vendor recovering: a service shedding load gets
the full offered load straight back from every worker.

The tests that matter here are the ones about what is *not* a failure. A
breaker that opens on a 4xx takes a healthy vendor out of service because
somebody left a voice id unfilled, and then reports it as an outage.
"""

from __future__ import annotations

import asyncio

import pytest

from uaagro_domain.errors import (
    ConfigurationError,
    MissingCredentialError,
    VendorTimeoutError,
    VendorUnavailableError,
)
from voice_worker.adapters.resilience import (
    BreakerState,
    CircuitBreaker,
    CircuitOpen,
    breaker_for,
    breaker_states,
    reset_breakers,
)


@pytest.fixture(autouse=True)
def _clean_registry():  # type: ignore[no-untyped-def]
    reset_breakers()
    yield
    reset_breakers()


def make(**kwargs: object) -> CircuitBreaker:
    defaults: dict[str, object] = {
        "vendor": "testco",
        "service": "tts",
        "failure_threshold": 3,
        "recovery_timeout_s": 0.2,
    }
    defaults.update(kwargs)
    return CircuitBreaker(**defaults)  # type: ignore[arg-type]


async def boom(exc: BaseException) -> None:
    raise exc


async def fine() -> str:
    return "ok"


def unavailable() -> VendorUnavailableError:
    """The vendor answered badly, or not at all."""
    return VendorUnavailableError(vendor="testco", service="tts", detail="down")


def timed_out() -> VendorTimeoutError:
    return VendorTimeoutError(vendor="testco", service="tts", timeout_ms=3000)


def ours() -> ConfigurationError:
    """Our mistake, not theirs -- a voice id nobody filled in."""
    return ConfigurationError("bad voice id", remedy="fill BAKBAK_VOICE_HI")


# --------------------------------------------------------------------------- #
# Opening
# --------------------------------------------------------------------------- #


async def test_one_bad_turn_does_not_take_a_vendor_out_of_service() -> None:
    """A single timeout is ordinary on a mobile network and the ladder already
    handles it. Opening on one would make the breaker itself the outage."""
    breaker = make()

    with pytest.raises(VendorTimeoutError):
        await breaker.call(lambda: boom(timed_out()))

    assert breaker.state is BreakerState.CLOSED


async def test_repeated_failure_stops_the_waiting() -> None:
    """The saving this exists for: the fourth call is refused in microseconds
    instead of after another full timeout."""
    breaker = make()
    for _ in range(3):
        with pytest.raises(VendorUnavailableError):
            await breaker.call(lambda: boom(unavailable()))

    assert breaker.state is BreakerState.OPEN

    started = asyncio.get_running_loop().time()
    with pytest.raises(CircuitOpen):
        await breaker.call(fine)
    assert (asyncio.get_running_loop().time() - started) < 0.05


async def test_an_open_circuit_does_not_touch_the_vendor() -> None:
    """Withholding traffic is half the point -- a service shedding load cannot
    recover while every worker keeps offering it the same load."""
    breaker = make()
    attempts = 0

    async def count() -> str:
        nonlocal attempts
        attempts += 1
        raise unavailable()

    for _ in range(3):
        with pytest.raises(VendorUnavailableError):
            await breaker.call(count)
    assert attempts == 3

    for _ in range(5):
        with pytest.raises(CircuitOpen):
            await breaker.call(count)
    assert attempts == 3, "the vendor was called while the circuit was open"


# --------------------------------------------------------------------------- #
# What does NOT count as the vendor's fault
# --------------------------------------------------------------------------- #


async def test_our_own_configuration_mistake_never_opens_the_circuit() -> None:
    """The failure mode this guards against.

    An unset ``BAKBAK_VOICE_HI`` produces a 422 on every call. Counting those
    would take a perfectly healthy synthesiser out of service because of one
    unfilled line in a config file -- and then report it as a vendor outage,
    sending whoever is on call to the wrong dashboard.
    """
    breaker = make()

    for _ in range(6):
        with pytest.raises(ConfigurationError):
            await breaker.call(lambda: boom(ours()))

    assert breaker.state is BreakerState.CLOSED


async def test_a_missing_credential_never_opens_the_circuit() -> None:
    """§0 rule 4 wants that failure to name its variable every time, not to be
    replaced after three tries by "the vendor is down"."""
    breaker = make()

    for _ in range(6):
        with pytest.raises(MissingCredentialError):
            await breaker.call(
                lambda: boom(MissingCredentialError("BAKBAK_API_KEY", needed_for="tts"))
            )

    assert breaker.state is BreakerState.CLOSED


async def test_an_unrecognised_transport_error_does_count() -> None:
    """httpx and websockets raise their own hierarchies. Ignoring what we do
    not recognise would mean the breaker never fires on the errors it was
    built for."""
    breaker = make()

    for _ in range(3):
        with pytest.raises(ConnectionResetError):
            await breaker.call(lambda: boom(ConnectionResetError("peer went away")))

    assert breaker.state is BreakerState.OPEN


# --------------------------------------------------------------------------- #
# Recovering
# --------------------------------------------------------------------------- #


async def test_it_probes_once_and_closes_on_success() -> None:
    breaker = make(recovery_timeout_s=0.05)
    for _ in range(3):
        with pytest.raises(VendorUnavailableError):
            await breaker.call(lambda: boom(unavailable()))

    await asyncio.sleep(0.08)
    assert breaker.state is BreakerState.HALF_OPEN
    assert await breaker.call(fine) == "ok"
    assert breaker.state is BreakerState.CLOSED


async def test_only_one_probe_is_admitted() -> None:
    """A herd of probes against a service that has just come back is how it
    goes down again."""
    breaker = make(recovery_timeout_s=0.05)
    for _ in range(3):
        with pytest.raises(VendorUnavailableError):
            await breaker.call(lambda: boom(unavailable()))
    await asyncio.sleep(0.08)

    admitted = 0
    release = asyncio.Event()

    async def slow_probe() -> str:
        nonlocal admitted
        admitted += 1
        await release.wait()
        return "ok"

    tasks = [asyncio.create_task(breaker.call(slow_probe)) for _ in range(5)]
    await asyncio.sleep(0.02)
    assert admitted == 1, f"{admitted} probes were let through"

    release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert sum(1 for r in results if r == "ok") == 1
    assert sum(1 for r in results if isinstance(r, CircuitOpen)) == 4


async def test_a_failed_probe_reopens_rather_than_letting_everything_through() -> None:
    breaker = make(recovery_timeout_s=0.05)
    for _ in range(3):
        with pytest.raises(VendorUnavailableError):
            await breaker.call(lambda: boom(unavailable()))
    await asyncio.sleep(0.08)

    with pytest.raises(VendorUnavailableError):
        await breaker.call(lambda: boom(unavailable()))

    assert breaker.state is BreakerState.OPEN


async def test_a_success_forgets_earlier_isolated_failures() -> None:
    """Without this the count only ever rises, and three unrelated blips hours
    apart open the circuit on a vendor that answered perfectly in between."""
    breaker = make()

    for _ in range(2):
        with pytest.raises(VendorTimeoutError):
            await breaker.call(lambda: boom(timed_out()))

    await breaker.call(fine)

    with pytest.raises(VendorTimeoutError):
        await breaker.call(lambda: boom(timed_out()))
    assert breaker.state is BreakerState.CLOSED


# --------------------------------------------------------------------------- #
# The shared registry
# --------------------------------------------------------------------------- #


def test_one_breaker_per_vendor_shared_across_calls() -> None:
    """Adapters are rebuilt every call. A breaker that forgot at the end of a
    call would have learned nothing -- the whole value is that call #2 skips
    what call #1 spent seven seconds discovering."""
    assert breaker_for("bakbak", "tts") is breaker_for("bakbak", "tts")
    assert breaker_for("bakbak", "tts") is not breaker_for("soniox", "stt")


def test_breaker_states_are_reportable() -> None:
    """§19 and the readiness probe both want to say which vendor is unwell.

    An open breaker is deliberately *not* un-readiness: the worker still
    answers, still speaks §16.1's cached phrases and still escalates to a
    person. Draining it would turn one vendor's outage into no helpline.
    """
    breaker_for("bakbak", "tts")
    breaker_for("llm", "chat completion")

    states = breaker_states()

    assert states == {"bakbak:tts": "closed", "llm:chat completion": "closed"}


async def test_the_refusal_is_the_kind_callers_already_degrade_from() -> None:
    """`CircuitOpen` subclasses `VendorUnavailableError` on purpose: every
    caller already knows how to fall back from that, and the entire point is
    reaching that path immediately rather than after two timeouts."""
    breaker = make()
    for _ in range(3):
        with pytest.raises(VendorUnavailableError):
            await breaker.call(lambda: boom(unavailable()))

    with pytest.raises(VendorUnavailableError) as caught:
        await breaker.call(fine)

    assert isinstance(caught.value, CircuitOpen)
    assert "circuit open" in str(caught.value)
