"""Smart Turn v3.1 semantic turn detection (§5.2).

An open-source model that judges whether a speaker has finished by looking at
the **waveform**, not the transcript -- so it hears the falling intonation of a
completed thought and the held pitch of someone still thinking. It covers 23
languages including Hindi and Marathi, runs on CPU in about 12 ms, and is 8 MB.

v3.1 (3 December 2025) is a drop-in replacement for the v3.0 the specification
names: same languages, materially better accuracy (English 88.3% to 94.7% on the
8 MB variant). Verified 31 August 2026.

**Two things this module has to be honest about.**

The model expects **16 kHz** audio and the telephony leg is 8 kHz, so this path
upsamples. §23-8 forbids resampling in the *output* path because it degrades
what the caller hears; upsampling an 8 kHz signal for a local classifier adds no
information but costs nothing perceptual, and the alternative is not running the
model at all. It is done here, once, close to the model that needs it.

And the model file is **not** bundled. It is an 8 MB download, not a credential,
so :func:`ensure_model` fails with the command that fetches it rather than
silently degrading to silence-only endpointing -- which would leave a language
labelled tier B behaving like tier C.
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from uaagro_domain.enums import TurnStrategy
from uaagro_domain.errors import ConfigurationError

from .base import TurnDecision, TurnDetector, TurnResult, TurnState, ends_mid_sentence

log = structlog.get_logger(__name__)

#: What the model was trained on. Telephony audio is 8 kHz and is upsampled.
MODEL_SAMPLE_RATE = 16_000
TELEPHONY_SAMPLE_RATE = 8_000

#: Longest audio window the model reads, in seconds. Older audio carries no
#: signal about whether *this* utterance has ended.
MAX_WINDOW_S = 8.0

def default_model_path() -> Path:
    """Where the weights live, honouring ``SMART_TURN_MODEL_PATH``."""
    from uaagro_domain.settings import get_settings

    return get_settings().smart_turn_model_path

#: Probability above which the turn is judged complete. Deliberately above the
#: midpoint: §19.2 weights a false cut above dead air, so the model has to be
#: fairly sure before it interrupts.
DEFAULT_THRESHOLD = 0.62

DOWNLOAD_HINT = (
    "Fetch it with:\n"
    "  uv run huggingface-cli download pipecat-ai/smart-turn-v3 "
    "--local-dir models\n"
    "or set SMART_TURN_MODEL_PATH to an existing copy."
)


def ensure_model(path: Path | None = None) -> Path:
    """Locate the ONNX weights, or fail with the command that fetches them.

    §0 rule 4: a missing artefact is a loud failure naming what is absent, not
    a quiet fallback. Falling back to silence endpointing here would leave
    Marathi labelled tier B while behaving like tier C.
    """
    path = path or default_model_path()
    if path.is_file():
        return path
    raise ConfigurationError(
        f"Smart Turn weights were not found at {path}.",
        remedy=DOWNLOAD_HINT,
        context={"path": str(path)},
    )


def upsample_8k_to_16k(pcm: bytes) -> bytes:
    """Linear interpolation from 8 kHz to 16 kHz, 16-bit mono.

    Adds no information -- it cannot -- but puts the signal at the rate the
    model expects. Written out rather than pulled from a DSP library because
    the whole operation is a loop over int16 pairs and the dependency would be
    larger than the code.
    """
    if not pcm:
        return b""
    count = len(pcm) // 2
    samples = struct.unpack(f"<{count}h", pcm[: count * 2])

    out: list[int] = []
    for index, sample in enumerate(samples):
        out.append(sample)
        nxt = samples[index + 1] if index + 1 < count else sample
        out.append((sample + nxt) // 2)
    return struct.pack(f"<{len(out)}h", *out)


def pcm_to_float32(pcm: bytes) -> list[float]:
    """Int16 PCM to the normalised floats the model takes."""
    count = len(pcm) // 2
    if count == 0:
        return []
    return [s / 32768.0 for s in struct.unpack(f"<{count}h", pcm[: count * 2])]


@dataclass
class SmartTurnDetector(TurnDetector):
    """Waveform-based semantic endpointing.

    The ONNX session is loaded once and reused. §7.5 is explicit that ONNX
    sessions are loaded at worker start and never per call -- a 12 ms inference
    behind a 300 ms model load would defeat the point.
    """

    strategy: TurnStrategy = TurnStrategy.SMART_TURN_V3
    #: §5.2: never end a turn before this much silence, however sure the model.
    min_delay_s: float = 0.30
    #: §5.2: end it by here regardless, so a confused model cannot stall a call.
    max_delay_s: float = 2.50
    threshold: float = DEFAULT_THRESHOLD
    model_path: Path | None = None
    #: Set when the model could not be loaded. The detector then behaves as
    #: plain silence endpointing and *says so* in every decision, so a degraded
    #: language is visible in the call log rather than merely quieter.
    degraded: bool = False
    _session: Any = field(default=None, repr=False)

    async def prepare(self) -> None:
        """Load the model at worker start.

        Called from the worker's startup rather than lazily, so a missing model
        is discovered at boot instead of on the first Marathi call.
        """
        if self._session is not None:
            return
        try:
            path = ensure_model(self.model_path)
            import onnxruntime

            options = onnxruntime.SessionOptions()
            # One thread: this runs alongside live calls and a model that
            # grabs every core would starve the audio loop it exists to serve.
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            self._session = await asyncio.to_thread(
                onnxruntime.InferenceSession,
                str(path),
                options,
                providers=["CPUExecutionProvider"],
            )
            log.info("turn.smart_turn_loaded", path=str(path))
        except (ConfigurationError, ImportError) as exc:
            self.degraded = True
            log.error(
                "turn.smart_turn_unavailable",
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )

    async def evaluate(self, state: TurnState) -> TurnResult:
        # §5.2 floor and ceiling apply whatever the model says.
        if state.silence_ms < self.min_delay_s * 1000:
            return TurnResult(TurnDecision.CONTINUE, reason="below the minimum delay")
        if state.silence_ms >= self.max_delay_s * 1000:
            return TurnResult(TurnDecision.END, reason="maximum delay reached")

        if self._session is None:
            await self.prepare()

        if self.degraded or self._session is None:
            return self._degraded_decision(state)

        probability = await self._infer(state.audio)
        if probability is None:
            return self._degraded_decision(state)

        threshold = self.threshold
        if ends_mid_sentence(state.transcript):
            # The transcript is a second opinion, not the decision. A trailing
            # conjunction makes the bar higher rather than overriding a model
            # that heard a completed contour.
            threshold = min(0.95, threshold + 0.15)

        if probability >= threshold:
            return TurnResult(
                TurnDecision.END, probability=probability, reason="model judged the turn complete"
            )
        return TurnResult(
            TurnDecision.CONTINUE, probability=probability, reason="model expects more speech"
        )

    def _degraded_decision(self, state: TurnState) -> TurnResult:
        """Silence endpointing, labelled as the fallback it is.

        Every decision carries the reason so a degraded language shows up in
        the call event log rather than merely sounding worse.
        """
        if ends_mid_sentence(state.transcript):
            # Audibly mid-sentence. Without the model there is nothing better
            # than the particle cue, so it decides.
            return TurnResult(
                TurnDecision.CONTINUE,
                reason="smart-turn model unavailable; trailing particle, still waiting",
            )
        if state.recogniser_endpointed:
            return TurnResult(
                TurnDecision.END,
                reason="smart-turn model unavailable; deferring to the recogniser VAD",
            )
        decision = (
            TurnDecision.END
            if state.silence_ms >= self.max_delay_s * 1000 * 0.6
            else TurnDecision.CONTINUE
        )
        return TurnResult(
            decision,
            reason="smart-turn model unavailable; falling back to silence endpointing",
        )

    async def _infer(self, audio: bytes) -> float | None:
        """Run the model over the tail of this turn's audio."""
        if not audio:
            return None

        window_bytes = int(MAX_WINDOW_S * TELEPHONY_SAMPLE_RATE * 2)
        tail = audio[-window_bytes:]
        samples = pcm_to_float32(upsample_8k_to_16k(tail))
        if not samples:
            return None

        try:
            return await asyncio.to_thread(self._run_session, samples)
        except Exception as exc:
            # A model failure must not drop the call; the fallback decides.
            log.warning("turn.smart_turn_inference_failed", error=type(exc).__name__)
            return None

    def _run_session(self, samples: list[float]) -> float:
        """Featurise, then infer.

        The model takes an 80 x 800 log-mel spectrogram, not a waveform. An
        earlier version fed it raw samples: ONNX raised a shape error, the
        caller's ``except`` swallowed it, and the detector silently never ran --
        so every Marathi call fell back to VAD while the admin panel reported
        tier B. See ``turn.features``.
        """
        import numpy

        from .features import log_mel_spectrogram

        features = log_mel_spectrogram(numpy.asarray(samples, dtype=numpy.float32))
        batch = features[numpy.newaxis, :, :]
        name = self._session.get_inputs()[0].name
        outputs = self._session.run(None, {name: batch})
        logit = float(numpy.asarray(outputs[0]).reshape(-1)[0])
        # The graph emits a logit, not a probability. Squashing it here keeps
        # DEFAULT_THRESHOLD meaningful as the probability §5.2 describes rather
        # than as an unbounded score nobody can reason about.
        return float(1.0 / (1.0 + numpy.exp(-logit)))

    async def close(self) -> None:
        self._session = None
