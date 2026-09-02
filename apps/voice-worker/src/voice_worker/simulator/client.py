"""Telephony simulator (§4.2, §21 Phase 1).

Speaks the same Exotel AgentStream protocol a real call uses, so the voice
pipeline is developable and testable **without placing a phone call**. This is a
Phase 1 deliverable rather than an afterthought: without it, every change to the
media path would need a live DID, a real handset and somebody in Barabanki.

What it reproduces faithfully:

* the ``connected`` / ``start`` / ``media`` / ``dtmf`` / ``stop`` sequence;
* base64 linear16 at 8 kHz -- the codec Exotel actually carries (§4.3);
* **real-time pacing**, one 20 ms frame every 20 ms. Blasting the file as fast
  as the socket accepts it would hide every timing bug the pipeline can have,
  and timing bugs are the ones that matter here (§7);
* mid-call socket drops, so the §11.4 interrupted-call path is exercised.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import websockets

from ..runtime import audio as audio_utils

#: One 20 ms frame every 20 ms.
FRAME_INTERVAL_S = audio_utils.FRAME_MS / 1000


@dataclass(slots=True)
class SimulatedCall:
    """Result of one simulated call."""

    call_sid: str
    stream_sid: str
    frames_sent: int = 0
    frames_received: int = 0
    audio_received: bytes = b""
    #: Milliseconds from the ``start`` frame to the first audio byte back. This
    #: is the number §11.1 wants under ~50 ms once the greeting is cached.
    first_audio_ms: float | None = None
    clears_received: int = 0
    events_received: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def audio_duration_ms(self) -> int:
        return audio_utils.duration_ms(self.audio_received)


class TelephonySimulator:
    """Drives one simulated call against a worker's ``/ws/voice``."""

    def __init__(
        self,
        url: str,
        *,
        from_number: str = "919999000001",
        to_number: str = "918001234567",
        account_sid: str = "uaagro_sim",
        realtime: bool = True,
    ) -> None:
        self._url = url
        self._from = from_number
        self._to = to_number
        self._account_sid = account_sid
        self._realtime = realtime
        self.call_sid = f"sim-{uuid.uuid4().hex[:16]}"
        self.stream_sid = f"stream-{uuid.uuid4().hex[:16]}"

    async def place_call(
        self,
        pcm: bytes,
        *,
        dtmf_after_ms: dict[int, str] | None = None,
        drop_after_ms: int | None = None,
        listen_tail_ms: int = 1500,
    ) -> SimulatedCall:
        """Stream ``pcm`` to the worker and collect what comes back.

        Args:
            pcm: linear16 mono 8 kHz caller audio.
            dtmf_after_ms: keypresses to inject, keyed by offset into the call.
            drop_after_ms: close the socket abruptly at this offset, simulating
                a dropped GSM line. The worker must persist a partial record
                and mark the call interrupted (§11.4).
            listen_tail_ms: how long to keep listening after the audio ends.
        """
        result = SimulatedCall(call_sid=self.call_sid, stream_sid=self.stream_sid)

        async with websockets.connect(self._url, max_size=None) as socket:
            receiver = asyncio.create_task(self._receive(socket, result))
            try:
                await socket.send(json.dumps({"event": "connected", "protocol": "Call"}))
                await socket.send(self._start_message())
                self._start_monotonic = time.perf_counter()

                await self._stream_audio(socket, pcm, result, dtmf_after_ms or {}, drop_after_ms)

                if drop_after_ms is not None:
                    # Abort the TCP transport rather than sending a close frame.
                    # A dropped rural GSM line gives the worker no close
                    # handshake at all, and a graceful close would exercise a
                    # different path from the one §11.4 is about.
                    transport = getattr(socket, "transport", None)
                    if transport is not None and hasattr(transport, "abort"):
                        transport.abort()
                    else:  # pragma: no cover - depends on the websockets backend
                        await socket.close(code=1001)
                    return result

                await asyncio.sleep(listen_tail_ms / 1000)
                await socket.send(json.dumps({"event": "stop", "stream_sid": self.stream_sid}))
                await asyncio.sleep(0.2)
            finally:
                receiver.cancel()
                try:
                    await receiver
                except (asyncio.CancelledError, websockets.ConnectionClosed):
                    pass

        return result

    def _start_message(self) -> str:
        return json.dumps(
            {
                "event": "start",
                "stream_sid": self.stream_sid,
                "start": {
                    "stream_sid": self.stream_sid,
                    "call_sid": self.call_sid,
                    "account_sid": self._account_sid,
                    "from": self._from,
                    "to": self._to,
                    "media_format": {
                        "encoding": "audio/x-raw",
                        "sample_rate": audio_utils.SAMPLE_RATE,
                        "bit_rate": "16",
                    },
                },
            }
        )

    async def _stream_audio(
        self,
        socket: Any,
        pcm: bytes,
        result: SimulatedCall,
        dtmf_after_ms: dict[int, str],
        drop_after_ms: int | None,
    ) -> None:
        pending_dtmf = sorted(dtmf_after_ms.items())
        elapsed_ms = 0
        next_send = time.perf_counter()

        for frame in audio_utils.iter_frames(pcm):
            while pending_dtmf and pending_dtmf[0][0] <= elapsed_ms:
                _, digit = pending_dtmf.pop(0)
                await socket.send(
                    json.dumps(
                        {
                            "event": "dtmf",
                            "stream_sid": self.stream_sid,
                            "dtmf": {"digit": digit},
                        }
                    )
                )

            if drop_after_ms is not None and elapsed_ms >= drop_after_ms:
                return

            await socket.send(
                json.dumps(
                    {
                        "event": "media",
                        "stream_sid": self.stream_sid,
                        "media": {"payload": base64.b64encode(frame).decode("ascii")},
                    }
                )
            )
            result.frames_sent += 1
            elapsed_ms += audio_utils.FRAME_MS

            if self._realtime:
                # Sleep to the next slot rather than a flat 20 ms, so send
                # latency does not accumulate into drift across a long call.
                next_send += FRAME_INTERVAL_S
                delay = next_send - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)

    async def _receive(self, socket: Any, result: SimulatedCall) -> None:
        """Collect the worker's outbound frames until cancelled."""
        async for message in socket:
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                result.errors.append("worker sent a non-JSON message")
                continue

            event = payload.get("event")
            result.events_received.append(str(event))

            if event == "media":
                encoded = payload.get("media", {}).get("payload", "")
                try:
                    chunk = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError):
                    result.errors.append("worker sent an undecodable media payload")
                    continue
                if result.first_audio_ms is None:
                    start = getattr(self, "_start_monotonic", None)
                    if start is not None:
                        result.first_audio_ms = (time.perf_counter() - start) * 1000
                result.audio_received += chunk
                result.frames_received += 1
            elif event == "clear":
                result.clears_received += 1
