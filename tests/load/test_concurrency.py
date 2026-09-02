"""Sustained concurrent-call load (§19, §21 Phase 8).

§21's gate: *50 concurrent calls, sustained, within the §7 latency budget*. §19
adds "ramp to find the true ceiling and record it in the runbook."

Marked ``load`` and deselected by default. A ten-minute test in the default
suite is a test people stop running, and the point of this one is that it gets
run before a release rather than that it runs constantly.

**What this measures and what it does not.** It drives the real worker over the
real Exotel protocol through the real simulator, so it exercises the WebSocket
handling, the frame pacing, the session bookkeeping and the turn loop under
contention -- which is where concurrency bugs live. It does **not** exercise the
vendors: STT, TTS and the LLM are stubbed, because 50 concurrent live calls
against Deepgram and Sarvam costs real money on every run and measures their
capacity rather than ours.

That makes the numbers here a floor on our own overhead, not a prediction of
production latency. §7's real gate needs vendors in the loop and is a Phase 5
item with a live account. Saying so is the point: a green load test that
silently omitted the network hops would be worse than no load test.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import struct
import time
from dataclasses import dataclass, field

import pytest

from uaagro_domain.telemetry import (
    TOTAL_CEILING_MS,
    evaluate_budget,
    percentile,
)

pytestmark = [pytest.mark.load, pytest.mark.integration]

#: §21's gate.
TARGET_CONCURRENCY = 50
#: §19: sustained. Shorter here than the ten minutes §19 asks for, because the
#: failure modes this catches -- accept-loop contention, per-call task leaks,
#: unbounded buffers -- appear in the first minute or not at all. The full
#: ten-minute soak is a release step in the runbook, not a per-commit one.
SUSTAIN_SECONDS = 60.0

#: 20 ms of 8 kHz 16-bit PCM, the frame size the protocol uses.
FRAME_MS = 20
FRAME_SAMPLES = 8000 * FRAME_MS // 1000


def _frame(hz: int = 300) -> bytes:
    return b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * hz * t / 8000)))
        for t in range(FRAME_SAMPLES)
    )


@dataclass
class CallResult:
    """One synthetic call's outcome."""

    connected: bool = False
    frames_sent: int = 0
    frames_received: int = 0
    first_audio_ms: float | None = None
    error: str | None = None
    #: Wall-clock gap between consecutive sends. §19 cares about this because a
    #: worker that falls behind on pacing is one whose audio arrives in bursts,
    #: which sounds like stuttering rather than like latency.
    max_send_gap_ms: float = 0.0


@dataclass
class LoadReport:
    calls: list[CallResult] = field(default_factory=list)

    @property
    def connected(self) -> int:
        return sum(1 for c in self.calls if c.connected)

    @property
    def failed(self) -> list[CallResult]:
        return [c for c in self.calls if c.error is not None]

    def first_audio_latencies(self) -> list[float]:
        return [c.first_audio_ms for c in self.calls if c.first_audio_ms is not None]

    def summary(self) -> str:
        latencies = self.first_audio_latencies()
        gaps = [c.max_send_gap_ms for c in self.calls if c.connected]
        return (
            f"{self.connected}/{len(self.calls)} connected, "
            f"{len(self.failed)} failed; "
            f"first audio p50 {percentile(latencies, 0.5):.0f} ms "
            f"p95 {percentile(latencies, 0.95):.0f} ms; "
            f"worst send gap {max(gaps, default=0):.0f} ms"
        )


async def _one_call(url: str, index: int, duration_s: float) -> CallResult:
    """Drive one call at real time, as the simulator does."""
    import websockets

    result = CallResult()
    frame = _frame(hz=200 + (index % 8) * 40)
    started = time.perf_counter()

    try:
        async with websockets.connect(url, open_timeout=10) as socket:
            await socket.send(json.dumps({"event": "connected"}))
            await socket.send(
                json.dumps(
                    {
                        "event": "start",
                        "stream_sid": f"load-{index}",
                        "start": {
                            "call_sid": f"call-{index}",
                            "from": f"9999{index:06d}",
                            "to": "911800000000",
                        },
                    }
                )
            )
            result.connected = True

            async def receive() -> None:
                async for message in socket:
                    payload = json.loads(message)
                    if payload.get("event") == "media":
                        result.frames_received += 1
                        if result.first_audio_ms is None:
                            result.first_audio_ms = (
                                time.perf_counter() - started
                            ) * 1000

            reader = asyncio.create_task(receive())
            try:
                import base64

                encoded = base64.b64encode(frame).decode("ascii")
                deadline = time.perf_counter() + duration_s
                last = time.perf_counter()
                while time.perf_counter() < deadline:
                    await socket.send(
                        json.dumps(
                            {
                                "event": "media",
                                "stream_sid": f"load-{index}",
                                "media": {"payload": encoded},
                            }
                        )
                    )
                    result.frames_sent += 1
                    now = time.perf_counter()
                    result.max_send_gap_ms = max(
                        result.max_send_gap_ms, (now - last) * 1000
                    )
                    last = now
                    # Real time. Blasting frames as fast as the socket accepts
                    # them hides every timing bug the pipeline can have, and
                    # timing bugs are the ones that matter.
                    await asyncio.sleep(FRAME_MS / 1000)

                await socket.send(json.dumps({"event": "stop"}))
            finally:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader
    except Exception as exc:
        result.error = type(exc).__name__

    return result


async def run_load(
    url: str, *, concurrency: int, duration_s: float
) -> LoadReport:
    """Open ``concurrency`` calls at once and hold them for ``duration_s``.

    Bounded by its own deadline rather than by a pytest timeout plugin: a load
    run that hangs should report what it managed before hanging, and a plugin
    that kills the process discards exactly that.
    """
    results = await asyncio.wait_for(
        asyncio.gather(
            *(_one_call(url, index, duration_s) for index in range(concurrency)),
            return_exceptions=False,
        ),
        timeout=duration_s * 3 + 30,
    )
    return LoadReport(calls=list(results))


async def test_fifty_concurrent_calls(worker_url: str) -> None:
    """§21's Phase 8 gate."""
    report = await run_load(
        worker_url, concurrency=TARGET_CONCURRENCY, duration_s=SUSTAIN_SECONDS
    )

    # Every call connects. A worker that drops calls under load has found its
    # ceiling below the target, which is the thing this is here to discover.
    assert report.failed == [], f"{len(report.failed)} calls failed: {report.summary()}"
    assert report.connected == TARGET_CONCURRENCY, report.summary()

    # Send pacing is *reported*, not asserted, and the distinction is the
    # honest one. The number measured here is fifty asyncio tasks sharing the
    # harness's own event loop, on Windows, where the default timer resolution
    # is around 15 ms -- so a 20 ms sleep overshoots before the worker is
    # involved at all. Measured at 87 ms against a first draft that asserted
    # 80 ms, which would have failed the build for a property of the test.
    #
    # A real pacing measurement needs the client off-box. That belongs with the
    # Phase 5 telephony gate, where the frames come from Exotel.
    worst_gap = max(c.max_send_gap_ms for c in report.calls)
    print(f"\n  {report.summary()}")  # noqa: T201 -- the report is the deliverable
    print(f"  harness send gap (not a worker measurement): {worst_gap:.0f} ms")  # noqa: T201

    latencies = report.first_audio_latencies()
    assert latencies, "no audio came back from any call"
    budget = evaluate_budget(latencies)
    assert budget.within_budget, (
        f"{budget.summary()} -- note this excludes vendor round trips, so "
        f"a breach here is our own overhead alone"
    )


async def test_ramp_finds_the_ceiling(worker_url: str) -> None:
    """§19: ramp to find the true ceiling and record it in the runbook.

    This does not assert a number. It reports one -- the point of a ceiling is
    that nobody knows it in advance, and a test that asserted a value would
    either be trivially true or would fail on a slower CI box for no reason
    anyone could act on.
    """
    ceiling = 0
    for concurrency in (10, 25, 50, 75, 100):
        report = await run_load(worker_url, concurrency=concurrency, duration_s=8.0)
        latencies = report.first_audio_latencies()
        p95 = percentile(latencies, 0.95) if latencies else 0.0
        healthy = not report.failed and p95 <= TOTAL_CEILING_MS

        print(  # noqa: T201 -- the report is the deliverable
            f"  {concurrency:>3} concurrent: {report.summary()}"
            f" {'OK' if healthy else 'DEGRADED'}"
        )
        if not healthy:
            break
        ceiling = concurrency

    print(f"\n  measured ceiling: {ceiling} concurrent calls")  # noqa: T201
    assert ceiling >= TARGET_CONCURRENCY, (
        f"the worker degraded below the §21 target of {TARGET_CONCURRENCY}"
    )
