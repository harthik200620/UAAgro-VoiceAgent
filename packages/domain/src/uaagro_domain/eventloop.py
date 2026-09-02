"""Installing the fast event loop, explicitly (§7, §7.6).

`uvloop` is a drop-in libuv-backed replacement for asyncio's selector loop and
is materially faster at exactly what this system does: many small socket reads
and writes, on a 20 ms cadence, across many concurrent calls. Every millisecond
the loop spends scheduling is a millisecond of audio jitter.

It was already present -- as a transitive extra of ``uvicorn[standard]``, which
auto-detects and installs it. Two problems with leaving it there:

**It arrives by accident.** Nothing in this repository asks for it, nothing
tests for it, and no failure would follow from uvicorn reorganising its extras.
A performance characteristic the §7 budget depends on should not be a side
effect of somebody else's packaging.

**The background worker never had it at all.** ARQ has no equivalent of
uvicorn's auto-detection, so `arq worker.tasks.WorkerSettings` ran on the
selector loop -- including the dialer, which is the other place in this system
that holds many sockets open at once (§18).

So it is installed here, by name, from one function that every entry point
calls. On Windows -- where uvloop does not build -- this is a no-op and says so,
because the development machine is Windows and a loud failure there would be
noise about a platform nothing deploys to.
"""

from __future__ import annotations

import asyncio
import sys

import structlog

log = structlog.get_logger(__name__)

#: Annotated as a plain ``str`` on purpose. A type checker treats
#: ``sys.platform`` as a literal for the platform it is running on, and
#: would then declare the non-Windows branches below unreachable -- and stop
#: checking the code that every deployment actually runs.
_PLATFORM: str = sys.platform


def install_fast_event_loop() -> str:
    """Install uvloop if it is available. Returns the loop policy in force.

    Idempotent and safe to call from any entry point, including ones running
    under uvicorn, which may have installed it already.
    """
    # Import first, decide after. Branching on `sys.platform` before the
    # import would let a type checker running on Windows conclude the rest of
    # this function is unreachable -- and then never check it.
    try:
        import uvloop  # type: ignore[import-not-found, unused-ignore]
    except ImportError:
        if _PLATFORM == "win32":
            # uvloop is POSIX-only and the marker in pyproject.toml excludes
            # it here. Expected on the development machine, so not a warning.
            return "asyncio (windows)"
        # Anywhere else this is a deployment that lost a §7 assumption.
        log.warning("eventloop.uvloop_missing", platform=_PLATFORM)
        return "asyncio"

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    log.info("eventloop.uvloop_installed", version=getattr(uvloop, "__version__", "?"))
    return "uvloop"


def active_loop_name() -> str:
    """Which loop implementation is actually running. For the readiness probe."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return "none"
    return type(loop).__module__.split(".")[0]


__all__ = ("active_loop_name", "install_fast_event_loop")
