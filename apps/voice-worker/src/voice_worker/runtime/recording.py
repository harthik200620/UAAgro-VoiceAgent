"""Call recording capture (§11.5, §17, §18).

A recording is the most sensitive artefact this system produces. It is a
farmer's voice, their name, their village, what they grow and what they owe --
and unlike the database, it cannot be selectively redacted after the fact. So
two things are true of every recording, and each is enforced rather than
documented:

**Its location is never logged.** §23-6 puts a recording URL in the same
category as a phone number, and for the same reason: a presigned URL in a log
aggregator is a copy of the recording in a log aggregator. Every log line
carries the object *key* and never a URL, and the store (``uaagro_db.storage``)
produces no URLs at all -- the control plane streams bytes through its own
authenticated route.

**It is written once the line has closed.** Capture happens on the audio path
-- a few bytes appended per frame -- and the write happens in the session's
close path, after the last frame, so a slow bucket can never become a dropped
call. The key is derived from the call reference, so a retry overwrites rather
than accumulates.

Retention (§18) is a lifecycle rule on the bucket keyed by the date prefix in
:func:`object_key`; the intent travels on the object as metadata so an auditor
can see it without reading a bucket policy.
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog

log = structlog.get_logger(__name__)

SAMPLE_RATE = 8000
SAMPLE_WIDTH = 2  # 16-bit PCM
CHANNELS = 2  # caller and agent, kept separate -- see RecordingBuffer

#: How much audio one call may hold in memory before the buffer refuses more.
#: At 8 kHz 16-bit stereo that is roughly 30 minutes, comfortably past §7's
#: expected call length, and it is a ceiling rather than a target: a call that
#: reaches it has gone wrong in some other way and should not also exhaust the
#: worker.
MAX_BUFFERED_BYTES = 8000 * 2 * 2 * 60 * 30


@dataclass
class RecordingBuffer:
    """Accumulates a call's audio in memory, both legs kept apart.

    Two channels rather than a mix, because §19's evaluation harness needs the
    caller's audio without the agent talking over it to measure WER, and a
    mixdown cannot be un-mixed. The stereo file is also what makes a disputed
    call reviewable -- who said what, not just what was said.

    Silence-padded on write rather than timestamped: the two legs arrive at
    different rates and a naive interleave would drift. Padding the shorter leg
    to the longer one keeps them aligned to within one frame, which is what a
    reviewer needs.
    """

    caller: bytearray = field(default_factory=bytearray, repr=False)
    agent: bytearray = field(default_factory=bytearray, repr=False)
    truncated: bool = False

    def add_caller(self, pcm: bytes) -> None:
        self._add(self.caller, pcm)

    def add_agent(self, pcm: bytes) -> None:
        self._add(self.agent, pcm)

    def _add(self, target: bytearray, pcm: bytes) -> None:
        if len(self.caller) + len(self.agent) + len(pcm) > MAX_BUFFERED_BYTES:
            if not self.truncated:
                # Once, loudly. A recording that silently stops halfway is worse
                # than one marked incomplete, because nobody knows to distrust it.
                log.error("recording.truncated", limit_bytes=MAX_BUFFERED_BYTES)
                self.truncated = True
            return
        target.extend(pcm)

    @property
    def duration_s(self) -> float:
        frames = max(len(self.caller), len(self.agent)) // SAMPLE_WIDTH
        return frames / SAMPLE_RATE

    @property
    def is_empty(self) -> bool:
        return not self.caller and not self.agent

    def to_wav(self) -> bytes:
        """Interleave both legs into a two-channel WAV.

        WAV rather than raw PCM: an operator reviewing a disputed call opens it
        in whatever is on their laptop, and a headerless file that plays as
        static in every player is not reviewable.
        """
        length = max(len(self.caller), len(self.agent))
        # Both legs padded to the same length before interleaving. Without this
        # the shorter one runs out mid-file and the remaining frames pair real
        # audio with nothing, which plays as one channel abruptly going mono.
        caller = bytes(self.caller).ljust(length, b"\x00")
        agent = bytes(self.agent).ljust(length, b"\x00")

        interleaved = bytearray(length * 2)
        for index in range(0, length - 1, SAMPLE_WIDTH):
            offset = index * 2
            interleaved[offset : offset + 2] = caller[index : index + 2]
            interleaved[offset + 2 : offset + 4] = agent[index : index + 2]

        out = io.BytesIO()
        with wave.open(out, "wb") as handle:
            handle.setnchannels(CHANNELS)
            handle.setsampwidth(SAMPLE_WIDTH)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(bytes(interleaved))
        return out.getvalue()


def object_key(*, call_ref: str, started_at: datetime) -> str:
    """Where a recording lives in the bucket.

    Date-partitioned so a lifecycle rule can expire a whole day's prefix, and so
    an auditor answering "what did you hold on 3 March" lists one prefix rather
    than scanning the bucket.

    The key carries the call reference and nothing else. A key containing a
    phone number would put a phone number in every access log line that touches
    the object (§23-6).
    """
    day = started_at.astimezone(UTC)
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in call_ref)[:80]
    return f"recordings/{day:%Y/%m/%d}/{safe or 'call'}.wav"


def pcm_duration_s(pcm: bytes) -> float:
    """Seconds of 8 kHz 16-bit mono audio in ``pcm``."""
    return (len(pcm) // SAMPLE_WIDTH) / SAMPLE_RATE


def silence(seconds: float) -> bytes:
    """``seconds`` of digital silence at the call's sample rate."""
    return b"\x00" * (int(seconds * SAMPLE_RATE) * SAMPLE_WIDTH)


__all__ = (
    "CHANNELS",
    "MAX_BUFFERED_BYTES",
    "SAMPLE_RATE",
    "SAMPLE_WIDTH",
    "RecordingBuffer",
    "object_key",
    "pcm_duration_s",
    "silence",
)
