"""Log-mel features for Smart Turn (§5.2).

Smart Turn v3 takes an **80 x 800 log-mel spectrogram**, not a waveform. The
ONNX graph declares ``input_features: [batch, 80, 800]``, and 800 frames at a
10 ms hop is exactly the 8-second window §5.2 uses.

This module exists because the first version of the adapter fed it raw float32
samples. ONNX raised a shape error, the adapter's ``except`` caught it, logged a
warning and returned ``None`` -- so the detector silently never ran and every
Marathi and non-Flux call fell back to VAD-plus-silence. That is precisely the
failure the Smart Turn docstring warns about: a language labelled tier B while
behaving like tier C, with nothing in the logs that looks like a bug.

The transform is Whisper's, because Smart Turn was trained on Whisper features:
25 ms Hann window, 10 ms hop, 400-point FFT, 80 Slaney-scale mel bins, log10,
clamped 8 dB below the maximum and scaled to roughly [-1, 1]. Every constant
below is from that pipeline and none of them is tunable -- a mel filterbank that
is nearly right produces features that are plausible and wrong, which the model
will happily score.

Implemented in numpy rather than pulled from librosa or torch. numpy is already
a dependency; librosa brings a scientific stack and torch brings a gigabyte, and
this is eighty lines.
"""

from __future__ import annotations

import numpy as np

#: Whisper's feature parameters. Not tunable.
SAMPLE_RATE = 16_000
N_FFT = 400  # 25 ms
HOP_LENGTH = 160  # 10 ms
N_MELS = 80
#: 800 frames at a 10 ms hop. Matches the ONNX graph's declared input.
N_FRAMES = 800
#: Exactly the audio those frames need.
WINDOW_SAMPLES = N_FRAMES * HOP_LENGTH  # 128,000 = 8 s

#: Slaney mel-scale breakpoints, from librosa's ``mel_frequencies``.
_F_SP = 200.0 / 3.0
_MIN_LOG_HZ = 1000.0
_MIN_LOG_MEL = _MIN_LOG_HZ / _F_SP
_LOGSTEP = np.log(6.4) / 27.0


def _hz_to_mel(freq: np.ndarray) -> np.ndarray:
    """Slaney mel scale: linear below 1 kHz, logarithmic above."""
    mel = freq / _F_SP
    above = freq >= _MIN_LOG_HZ
    mel[above] = _MIN_LOG_MEL + np.log(freq[above] / _MIN_LOG_HZ) / _LOGSTEP
    return mel


def _mel_to_hz(mel: np.ndarray) -> np.ndarray:
    freq = mel * _F_SP
    above = mel >= _MIN_LOG_MEL
    freq[above] = _MIN_LOG_HZ * np.exp(_LOGSTEP * (mel[above] - _MIN_LOG_MEL))
    return freq


def _mel_filterbank() -> np.ndarray:
    """The 80 x 201 triangular filterbank, Slaney-normalised.

    Slaney normalisation -- dividing each filter by its bandwidth -- is what
    makes the filters equal-*area* rather than equal-*height*. Getting this
    wrong tilts the whole spectrum with frequency, which looks like a plausible
    spectrogram and is not the one the model was trained on.
    """
    fft_freqs = np.linspace(0.0, SAMPLE_RATE / 2.0, 1 + N_FFT // 2)

    lower_mel = _hz_to_mel(np.array([0.0]))[0]
    upper_mel = _hz_to_mel(np.array([SAMPLE_RATE / 2.0]))[0]
    mel_points = np.linspace(lower_mel, upper_mel, N_MELS + 2)
    hz_points = _mel_to_hz(mel_points.copy())

    diff = np.diff(hz_points)
    ramps = hz_points[:, np.newaxis] - fft_freqs[np.newaxis, :]

    lower = -ramps[:-2] / diff[:-1, np.newaxis]
    upper = ramps[2:] / diff[1:, np.newaxis]
    weights = np.maximum(0.0, np.minimum(lower, upper))

    enorm = 2.0 / (hz_points[2 : N_MELS + 2] - hz_points[:N_MELS])
    normalised: np.ndarray = (weights * enorm[:, np.newaxis]).astype(np.float32)
    return normalised


#: Built once. The filterbank is a fixed 80 x 201 matrix and rebuilding it per
#: turn would put a mel-scale conversion inside §7's budget for no reason.
_FILTERBANK = _mel_filterbank()
_WINDOW = np.hanning(N_FFT + 1)[:-1].astype(np.float32)


def log_mel_spectrogram(samples: np.ndarray) -> np.ndarray:
    """Whisper-style log-mel features, shaped ``(80, 800)``.

    Args:
        samples: Mono float32 at 16 kHz, in roughly [-1, 1]. Shorter input is
            zero-padded and longer input is trimmed to the most recent 8
            seconds -- older audio carries no signal about whether *this*
            utterance has ended.
    """
    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
    if audio.size >= WINDOW_SAMPLES:
        audio = audio[-WINDOW_SAMPLES:]
    else:
        # Padded at the **front**, so the utterance stays flush with the end of
        # the window. Padding the back would put the moment of interest --
        # whether the speaker just stopped -- in the middle of silence.
        audio = np.pad(audio, (WINDOW_SAMPLES - audio.size, 0))

    # Reflect-padded and centred, matching torch.stft's default. Without this
    # the frames are offset by half a window against what the model saw in
    # training.
    padded = np.pad(audio, (N_FFT // 2, N_FFT // 2), mode="reflect")

    frame_count = 1 + (padded.size - N_FFT) // HOP_LENGTH
    indices = (
        np.arange(N_FFT)[np.newaxis, :]
        + HOP_LENGTH * np.arange(frame_count)[:, np.newaxis]
    )
    frames = padded[indices] * _WINDOW

    spectrum = np.fft.rfft(frames, n=N_FFT, axis=1)
    # Whisper drops the final frame before taking magnitudes.
    magnitudes = np.abs(spectrum[:-1, :]) ** 2

    mel = _FILTERBANK @ magnitudes.T  # (80, frames)

    log_spec = np.log10(np.maximum(mel, 1e-10))
    # Clamped 8 dB below the peak: a dynamic-range floor, so a quiet stretch
    # does not stretch into noise.
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    log_spec = (log_spec + 4.0) / 4.0

    return _fit(log_spec.astype(np.float32))


def _fit(spec: np.ndarray) -> np.ndarray:
    """Trim or pad the time axis to exactly ``N_FRAMES``.

    The graph declares a fixed 800, so a frame count that is off by one from
    rounding is a shape error at inference -- caught by the adapter, logged as a
    warning, and indistinguishable from the detector simply not firing.
    """
    frames = spec.shape[1]
    if frames == N_FRAMES:
        return spec
    if frames > N_FRAMES:
        return spec[:, -N_FRAMES:]
    return np.pad(spec, ((0, 0), (N_FRAMES - frames, 0)))


__all__ = (
    "HOP_LENGTH",
    "N_FFT",
    "N_FRAMES",
    "N_MELS",
    "SAMPLE_RATE",
    "WINDOW_SAMPLES",
    "log_mel_spectrogram",
)
