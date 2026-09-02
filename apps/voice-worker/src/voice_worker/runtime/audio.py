"""Audio framing and WAV helpers for the 8 kHz telephony path.

Everything here works in **linear16 mono at 8 kHz** -- the format Exotel carries
and the format the TTS is asked to synthesise directly. §23-8 forbids
resampling in the hot path, so there is deliberately no resampler in this
module: if a sample rate is wrong, the fix is to ask the vendor for the right
one, not to convert it here.
"""

from __future__ import annotations

import io
import wave
from collections.abc import Iterator

SAMPLE_RATE = 8000
SAMPLE_WIDTH = 2  # 16-bit
CHANNELS = 1

#: 20 ms frame -- 160 samples, 320 bytes. The telephony standard.
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH

SILENCE_FRAME = b"\x00" * FRAME_BYTES


def frame_count(pcm: bytes) -> int:
    """Whole frames in ``pcm``, rounding up."""
    return (len(pcm) + FRAME_BYTES - 1) // FRAME_BYTES


def duration_ms(pcm: bytes) -> int:
    return len(pcm) * 1000 // (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)


def iter_frames(pcm: bytes, *, pad_final: bool = True) -> Iterator[bytes]:
    """Split PCM into 20 ms frames.

    A short trailing frame is zero-padded by default. Sending a partial frame
    to the provider is legal but makes downstream byte accounting inconsistent,
    and the played-byte count is what barge-in uses to truncate the assistant
    message (§5.4) -- so it needs to be exact.
    """
    for offset in range(0, len(pcm), FRAME_BYTES):
        frame = pcm[offset : offset + FRAME_BYTES]
        if len(frame) < FRAME_BYTES:
            if not pad_final:
                if frame:
                    yield frame
                return
            frame = frame + b"\x00" * (FRAME_BYTES - len(frame))
        yield frame


def read_wav(data: bytes) -> bytes:
    """Extract raw PCM from a WAV file, asserting the telephony format.

    Raises:
        ValueError: if the file is not 8 kHz 16-bit mono. Silently accepting
            another format would mean silently playing noise down the line.
    """
    with wave.open(io.BytesIO(data), "rb") as handle:
        if handle.getnchannels() != CHANNELS:
            raise ValueError(
                f"Expected mono audio, got {handle.getnchannels()} channels. "
                f"Convert with: ffmpeg -i in.wav -ac 1 -ar 8000 -sample_fmt s16 out.wav"
            )
        if handle.getsampwidth() != SAMPLE_WIDTH:
            raise ValueError(
                f"Expected 16-bit samples, got {handle.getsampwidth() * 8}-bit. "
                f"Convert with: ffmpeg -i in.wav -ac 1 -ar 8000 -sample_fmt s16 out.wav"
            )
        if handle.getframerate() != SAMPLE_RATE:
            raise ValueError(
                f"Expected {SAMPLE_RATE} Hz, got {handle.getframerate()} Hz. The telephony "
                f"leg is 8 kHz and nothing in the pipeline resamples (spec 23-8). "
                f"Convert with: ffmpeg -i in.wav -ac 1 -ar 8000 -sample_fmt s16 out.wav"
            )
        return handle.readframes(handle.getnframes())


def write_wav(pcm: bytes) -> bytes:
    """Wrap raw PCM in a WAV container at the telephony format."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(SAMPLE_WIDTH)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm)
    return buffer.getvalue()


def tone(frequency_hz: int, duration_ms_: int, *, amplitude: float = 0.25) -> bytes:
    """A sine tone, used as placeholder audio by the stub worker and fixtures.

    Not speech, and not pretending to be: the Phase 1 stub proves the transport
    round-trips audio. Real synthesis arrives with the TTS adapter in Phase 2.
    """
    import math
    import struct

    sample_count = SAMPLE_RATE * duration_ms_ // 1000
    peak = int(32767 * max(0.0, min(1.0, amplitude)))
    samples = (
        int(peak * math.sin(2 * math.pi * frequency_hz * index / SAMPLE_RATE))
        for index in range(sample_count)
    )
    return struct.pack(f"<{sample_count}h", *samples)


def silence(duration_ms_: int) -> bytes:
    return b"\x00" * (SAMPLE_RATE * SAMPLE_WIDTH * duration_ms_ // 1000)
