"""The event loop §7 depends on, made checkable (§7, §7.6).

`uvloop` is a libuv-backed drop-in for asyncio's selector loop, and it is faster
at exactly what this system does: many small socket reads and writes on a 20 ms
cadence across many concurrent calls. Time the loop spends scheduling is audio
jitter on somebody's phone.

It was already present in production — as a transitive extra of
`uvicorn[standard]`, which auto-detects and installs it. That is a fine outcome
reached by accident, and accidents are not a basis for a latency budget:
nothing in this repository asked for it, nothing tested for it, and uvicorn
reorganising its extras would have removed it with no failure anywhere.

The background worker never had it at all. ARQ has no equivalent auto-detection,
so the dialer — the other place in this system holding many sockets open at once
— ran on the selector loop.

These tests do not assert that uvloop *is* running, because it is POSIX-only and
the development machine is Windows. They assert the weaker thing that actually
catches regressions: that every entry point asks, and that a deployment which
lost it would say so rather than quietly running slower.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

from uaagro_domain.eventloop import active_loop_name, install_fast_event_loop

ROOT = Path(__file__).resolve().parents[1]

#: Every long-running process. All three hold sockets open under concurrency.
SERVICES = ("apps/voice-worker", "apps/api", "apps/worker")


def test_installing_is_idempotent_and_reports_what_it_did() -> None:
    """Called from entry points that may already be running under uvicorn, so
    it must be safe twice and must say which loop is in force."""
    first = install_fast_event_loop()
    second = install_fast_event_loop()

    assert first == second
    assert first in {"uvloop", "asyncio", "asyncio (windows)"}


@pytest.mark.skipif(sys.platform == "win32", reason="uvloop is POSIX-only")
def test_a_posix_deployment_actually_gets_uvloop() -> None:
    """On the platform that deploys, the fast loop is not optional.

    Skipped on Windows rather than weakened, so the assertion stays honest
    where it can be made.
    """
    assert install_fast_event_loop() == "uvloop"


async def test_the_running_loop_is_reportable() -> None:
    """The readiness probe says which loop a worker is on. Two workers behind
    one load balancer on different loops is a latency mystery nobody would
    otherwise be able to see."""
    assert active_loop_name() in {"asyncio", "uvloop"}


@pytest.mark.parametrize("service", SERVICES)
def test_every_service_declares_the_loop_it_needs(service: str) -> None:
    """Named in `pyproject.toml`, not inherited from someone else's extra.

    This is the regression that would otherwise be silent: `uvicorn[standard]`
    dropping uvloop from its extras, or a service moving off uvicorn, takes the
    loop away and nothing fails — the system just gets slower under load, which
    is the hardest kind of regression to attribute months later.
    """
    data = tomllib.loads((ROOT / service / "pyproject.toml").read_text(encoding="utf-8"))
    declared = " ".join(data["project"]["dependencies"])

    assert "uvloop" in declared, f"{service} does not ask for the loop it needs"
    # Marked, because uvloop does not build on Windows and the development
    # machine is Windows. An unmarked dependency makes `uv sync` fail there.
    assert "sys_platform != 'win32'" in declared


def test_the_background_worker_installs_it_before_arq_builds_a_loop() -> None:
    """ARQ has no uvloop auto-detection, so this has to happen at import time —
    after ARQ creates its loop is too late to set the policy."""
    source = (ROOT / "apps/worker/src/worker/tasks.py").read_text(encoding="utf-8")

    assert "install_fast_event_loop()" in source
    settings_at = source.index("class WorkerSettings")
    assert source.index("install_fast_event_loop()", settings_at) > settings_at, (
        "the loop is not installed on the ARQ entry point"
    )
