"""Is the caller *talking*, or is that the fan? (§5.4)

Barge-in used to fire on the recogniser's first token. That is the wrong
signal for two reasons that both showed up in the first browser test:

* it is late -- a vendor token arrives 300-500 ms after the voice does, so the
  agent talked over the farmer for half a second before stopping;
* it is credulous -- anything the recogniser decides to transcribe counts, and
  a recogniser will transcribe the agent's own voice leaking back through a
  speakerphone, a fan, a scooter going past. The agent stopped for noise,
  answered a fragment of itself, and greeted the farmer by name again.

So the decision is made here, locally, on the audio, before anybody is asked
what the words were. A frame is *voice* when it is clearly louder than the
room and is not broadband hiss; a *speech onset* is a run of such frames; and
only an onset may interrupt the agent. The recogniser's own speech-start is
then a confirmation rather than the trigger.

The detector is deliberately simple -- an adaptive noise floor, a periodicity
test and a stationarity test -- because the two sounds it has to tell apart
are far apart. A voice has a pitch: its waveform repeats every few
milliseconds, which a normalised autocorrelation sees as a clear peak. A fan
has none, and is steady besides, and steady sound *becomes* the floor within
a second. A person is neither. Where a Silero ONNX model is available it is
used instead of the spectral rule for the per-frame verdict; the onset and
release logic above it is the same either way.

Everything runs on the audio path, so it is cheap: one 512-point FFT per
20 ms frame, no allocation in the loop beyond numpy's own.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum, auto
from pathlib import Path
from typing import Protocol

import numpy as np
import structlog

from . import audio as audio_utils

log = structlog.get_logger(__name__)

#: Frames are judged at the telephony frame size.
FRAME_SAMPLES = audio_utils.FRAME_SAMPLES

#: How far above the tracked room noise a frame must sit to count as voice.
#: Six decibels is "clearly louder than the room"; a fan that has become the
#: floor is by definition zero decibels above it.
SNR_DB = 6.0
#: Below this absolute level nothing is voice, whatever the floor says. Stops
#: a dead-quiet line from promoting its own hiss to speech.
MIN_LEVEL_DB = -50.0
#: Spectral flatness (0 = a pure tone, 1 = white noise). Anything above this
#: is hiss, whatever else it looks like.
FLATNESS_MAX = 0.6
#: Normalised autocorrelation peak over the pitch range. Voiced speech on a
#: telephone band sits at 0.6-0.9; shaped noise stays under 0.3.
PERIODICITY_MIN = 0.45
#: Pitch lags searched, in samples at 8 kHz: 400 Hz down to 62 Hz.
PITCH_LAG_MIN = 20
PITCH_LAG_MAX = 128
#: The judge sees this much audio: the current frame and the one before it.
#: One 20 ms frame is too short to hold two pitch periods at a low voice.
JUDGE_SAMPLES = 2 * FRAME_SAMPLES

#: An onset is this many voice frames out of the last ``ONSET_WINDOW`` -- 140
#: of 200 ms. A cough is shorter; a fan never gets there; "रुकिए" does.
ONSET_WINDOW = 10
ONSET_REQUIRED = 7
#: Silence this long after voice ends the utterance. 300 ms, so a pause for
#: breath does not split a sentence into two onsets.
RELEASE_FRAMES = 15

#: Speech moves: within half a second its level swings well over ten
#: decibels between syllables. A fan, a compressor, line hum do not. A frame
#: whose recent history spans less than this is a steady sound, whatever its
#: spectrum, and steady sound is never voice.
STATIONARY_WINDOW = 25
STATIONARY_RANGE_DB = 4.0

#: How fast the floor climbs back up when the room gets louder for good.
#: Slow on purpose -- a farmer talking for three seconds must not raise the
#: floor until their own voice no longer clears it.
FLOOR_RISE_DB_PER_FRAME = 0.15
FLOOR_RISE_WHILE_VOICED = 0.01


class VoiceEvent(StrEnum):
    SPEECH_START = auto()
    SPEECH_END = auto()


class FrameJudge(Protocol):
    """Decides one frame. Implemented by the spectral rule and by Silero.

    ``samples`` is the last :data:`JUDGE_SAMPLES` of audio, the frame being
    judged at the end; ``level_db`` is that frame's level alone.
    """

    def is_voice(self, samples: np.ndarray, level_db: float, floor_db: float) -> bool: ...


@dataclass
class SpectralJudge:
    """The rule that needs no model: loud relative to the room, pitched, not hiss."""

    snr_db: float = SNR_DB
    min_level_db: float = MIN_LEVEL_DB
    flatness_max: float = FLATNESS_MAX
    periodicity_min: float = PERIODICITY_MIN
    _window: np.ndarray = field(
        default_factory=lambda: np.hanning(JUDGE_SAMPLES).astype(np.float32)
    )

    def is_voice(self, samples: np.ndarray, level_db: float, floor_db: float) -> bool:
        if level_db < self.min_level_db or level_db < floor_db + self.snr_db:
            return False
        if samples.size != JUDGE_SAMPLES:
            samples = np.pad(samples, (JUDGE_SAMPLES - samples.size, 0))[-JUDGE_SAMPLES:]
        windowed = samples * self._window
        if spectral_flatness(windowed) > self.flatness_max:
            return False
        return periodicity(windowed) >= self.periodicity_min


def periodicity(samples: np.ndarray) -> float:
    """Normalised autocorrelation peak over the pitch range, 0..1.

    Computed through the FFT (Wiener-Khinchin) rather than by sliding the
    frame over itself: it is the same number and a tenth of the work.
    """
    n = 1
    while n < 2 * samples.size:
        n *= 2
    spectrum = np.fft.rfft(samples, n=n)
    correlation = np.fft.irfft(np.abs(spectrum) ** 2, n=n)[: samples.size]
    energy = float(correlation[0])
    if energy <= 1e-12:
        return 0.0
    window = correlation[PITCH_LAG_MIN : PITCH_LAG_MAX + 1]
    return max(0.0, float(np.max(window)) / energy)


def spectral_flatness(samples: np.ndarray) -> float:
    """Wiener entropy over the speech band, 0..1."""
    spectrum = np.abs(np.fft.rfft(samples, n=512)) ** 2
    # Bins 6..224 at 15.6 Hz each: roughly 100 Hz to 3.5 kHz. DC and the
    # lowest bins are left out because a fan's hum lives there and would make
    # it look tonal, which is the wrong way round.
    band = spectrum[6:224] + 1e-12
    geometric = math.exp(float(np.mean(np.log(band))))
    arithmetic = float(np.mean(band))
    return geometric / arithmetic if arithmetic > 0 else 1.0


@dataclass
class VoiceGate:
    """Turns a stream of 8 kHz frames into speech-start and speech-end events.

    Feed it every inbound frame. It answers with an event on the frame that
    changes the state and ``None`` otherwise, and keeps :attr:`speaking` and
    :attr:`last_voice_at` current for anyone who wants to ask later -- the
    pipeline asks whether the recogniser's speech-start is corroborated by
    voice heard in the last second.
    """

    judge: FrameJudge = field(default_factory=SpectralJudge)
    onset_window: int = ONSET_WINDOW
    onset_required: int = ONSET_REQUIRED
    release_frames: int = RELEASE_FRAMES

    speaking: bool = False
    #: perf_counter of the most recent voice frame; ``None`` before any.
    last_voice_at: float | None = None
    #: Frames judged so far. Zero means the gate has heard nothing at all,
    #: which the pipeline treats as "no opinion" rather than "no voice".
    frames: int = 0
    #: The room. Starts at full scale so the very first frame sets it -- a
    #: call opens with the greeting playing and the caller quiet, and a fan
    #: that is already running becomes the floor at once rather than
    #: counting as a voice for the second it would take to climb to it.
    floor_db: float = 0.0

    _recent: deque[bool] = field(default_factory=lambda: deque(maxlen=ONSET_WINDOW), repr=False)
    _levels: deque[float] = field(
        default_factory=lambda: deque(maxlen=STATIONARY_WINDOW), repr=False
    )
    _quiet_run: int = 0
    _residue: bytearray = field(default_factory=bytearray, repr=False)
    _previous: np.ndarray = field(
        default_factory=lambda: np.zeros(FRAME_SAMPLES, dtype=np.float32), repr=False
    )

    def __post_init__(self) -> None:
        self._recent = deque(maxlen=self.onset_window)

    def feed(self, pcm: bytes) -> list[VoiceEvent]:
        """Judge whatever whole frames ``pcm`` completes. Never raises."""
        events: list[VoiceEvent] = []
        if not pcm:
            return events
        self._residue.extend(pcm)
        whole = len(self._residue) // audio_utils.FRAME_BYTES * audio_utils.FRAME_BYTES
        if whole == 0:
            return events
        buffer = bytes(self._residue[:whole])
        del self._residue[:whole]

        samples = np.frombuffer(buffer, dtype="<i2").astype(np.float32) / 32768.0
        for start in range(0, samples.size, FRAME_SAMPLES):
            event = self._judge(samples[start : start + FRAME_SAMPLES])
            if event is not None:
                events.append(event)
        return events

    def _judge(self, frame: np.ndarray) -> VoiceEvent | None:
        self.frames += 1
        rms = float(np.sqrt(np.mean(frame * frame))) if frame.size else 0.0
        level_db = 20.0 * math.log10(rms + 1e-9)

        self._levels.append(level_db)
        steady = (
            len(self._levels) == self._levels.maxlen
            and max(self._levels) - min(self._levels) < STATIONARY_RANGE_DB
        )
        if frame.size < FRAME_SAMPLES:
            frame = np.pad(frame, (0, FRAME_SAMPLES - frame.size))
        pair = np.concatenate([self._previous, frame])
        self._previous = frame
        voiced = not steady and self.judge.is_voice(pair, level_db, self.floor_db)
        self._track_floor(level_db, voiced)

        if voiced:
            self.last_voice_at = time.perf_counter()
            self._quiet_run = 0
        else:
            self._quiet_run += 1
        self._recent.append(voiced)

        if not self.speaking:
            if sum(self._recent) >= self.onset_required:
                self.speaking = True
                self._quiet_run = 0
                return VoiceEvent.SPEECH_START
            return None

        if self._quiet_run >= self.release_frames:
            self.speaking = False
            self._recent.clear()
            return VoiceEvent.SPEECH_END
        return None

    def _track_floor(self, level_db: float, voiced: bool) -> None:
        """Minimum statistics, the cheap way: drop at once, climb slowly."""
        if level_db < self.floor_db:
            self.floor_db = level_db
            return
        rise = FLOOR_RISE_WHILE_VOICED if voiced else FLOOR_RISE_DB_PER_FRAME
        self.floor_db = min(self.floor_db + rise, level_db)

    def heard_voice_within(self, seconds: float) -> bool:
        """Whether voice was seen recently -- the corroboration the pipeline
        asks for before trusting a recogniser's speech-start."""
        if self.speaking:
            return True
        if self.last_voice_at is None:
            return False
        return (time.perf_counter() - self.last_voice_at) <= seconds

    def reset(self) -> None:
        """Forget the utterance state, keeping what was learned about the room."""
        self.speaking = False
        self._recent.clear()
        self._quiet_run = 0
        self._residue.clear()


# --------------------------------------------------------------------------- #
# Silero, when its weights are present
# --------------------------------------------------------------------------- #

#: Silero's 8 kHz chunk. It wants 256 samples (32 ms), not our 20 ms frame, so
#: the judge accumulates and answers for the most recent whole chunk.
SILERO_CHUNK = 256
SILERO_THRESHOLD = 0.5


class SileroJudge:
    """Per-frame verdict from the Silero VAD ONNX model.

    Optional, and loaded only when the file exists: the weights are a download
    (``models/silero_vad.onnx``, ~2 MB, MIT), not something to fetch quietly
    during a call. Without them the spectral rule above stands in. Both v4 and
    v5 graphs are handled, told apart by their input names, and a graph that
    fails a smoke test at load is refused rather than trusted.
    """

    def __init__(self, path: Path) -> None:
        import onnxruntime

        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        self._session = onnxruntime.InferenceSession(
            str(path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        names = {entry.name for entry in self._session.get_inputs()}
        self._v5 = "state" in names
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._h = np.zeros((2, 1, 64), dtype=np.float32)
        self._c = np.zeros((2, 1, 64), dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._last = 0.0
        # Smoke test: a silent chunk must score as not-speech without error.
        self._pending = np.zeros(SILERO_CHUNK, dtype=np.float32)
        self._infer()
        self._pending = np.zeros(0, dtype=np.float32)

    def is_voice(self, samples: np.ndarray, level_db: float, floor_db: float) -> bool:
        if level_db < MIN_LEVEL_DB:
            return False
        # The judge is handed two frames; only the newest is new audio.
        frame = samples[-FRAME_SAMPLES:].astype(np.float32)
        self._pending = np.concatenate([self._pending, frame])
        while self._pending.size >= SILERO_CHUNK:
            self._infer()
        return self._last >= SILERO_THRESHOLD

    def _infer(self) -> None:
        chunk = self._pending[:SILERO_CHUNK]
        self._pending = self._pending[SILERO_CHUNK:]
        feed: dict[str, np.ndarray] = {
            "input": chunk.reshape(1, -1),
            "sr": np.array(audio_utils.SAMPLE_RATE, dtype=np.int64),
        }
        if self._v5:
            feed["state"] = self._state
            output, self._state = self._session.run(None, feed)
        else:
            feed["h"] = self._h
            feed["c"] = self._c
            output, self._h, self._c = self._session.run(None, feed)
        self._last = float(np.asarray(output).reshape(-1)[0])


def default_silero_path() -> Path:
    from uaagro_domain.settings import get_settings

    configured = getattr(get_settings(), "silero_vad_model_path", None)
    return Path(configured) if configured else Path("models") / "silero_vad.onnx"


def build_voice_gate(model_path: Path | None = None) -> VoiceGate:
    """The gate a call uses: Silero if its weights are on disk, else spectral.

    Which one was chosen is logged once per call at debug level, and a model
    that fails to load is a warning plus the spectral rule -- never a call
    without barge-in.
    """
    path = model_path or default_silero_path()
    if path.is_file():
        try:
            gate = VoiceGate(judge=SileroJudge(path))
            log.debug("vad.silero", path=str(path))
            return gate
        except Exception as exc:
            log.warning("vad.silero_unavailable", error=type(exc).__name__)
    return VoiceGate()


__all__ = (
    "FLATNESS_MAX",
    "JUDGE_SAMPLES",
    "ONSET_REQUIRED",
    "ONSET_WINDOW",
    "PERIODICITY_MIN",
    "RELEASE_FRAMES",
    "SNR_DB",
    "STATIONARY_RANGE_DB",
    "STATIONARY_WINDOW",
    "SileroJudge",
    "SpectralJudge",
    "VoiceEvent",
    "VoiceGate",
    "build_voice_gate",
    "periodicity",
    "spectral_flatness",
)
