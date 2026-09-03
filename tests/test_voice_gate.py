"""The voice gate: a person interrupts the agent, a fan does not (§5.4).

The signals here are synthetic on purpose. A recording would prove the gate
handles one room; these prove the two properties that matter -- steady
broadband sound never becomes an onset, and a modulated harmonic sound over
that same fan does -- and they run in milliseconds without a fixture file.
"""

from __future__ import annotations

import numpy as np

from voice_worker.runtime import audio as audio_utils
from voice_worker.runtime.vad import VoiceEvent, VoiceGate, periodicity, spectral_flatness

RATE = audio_utils.SAMPLE_RATE


def pcm(samples: np.ndarray) -> bytes:
    return (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def _rms_to(samples: np.ndarray, rms: float) -> np.ndarray:
    current = float(np.sqrt(np.mean(samples * samples))) or 1.0
    return samples * (rms / current)


def fan(seconds: float, *, rms: float = 0.02, seed: int = 1) -> np.ndarray:
    """Steady, broadband, low-heavy: white noise through a one-pole low-pass."""
    rng = np.random.default_rng(seed)
    white = rng.normal(size=int(seconds * RATE)).astype(np.float32)
    shaped = np.empty_like(white)
    acc = 0.0
    for i, x in enumerate(white):
        acc = 0.85 * acc + 0.15 * x
        shaped[i] = acc
    return _rms_to(shaped, rms)


def voice(seconds: float, *, rms: float = 0.2, f0: float = 140.0) -> np.ndarray:
    """A voiced sound: harmonics of a pitch, with a 4 Hz syllable envelope."""
    t = np.arange(int(seconds * RATE)) / RATE
    tone = np.zeros_like(t, dtype=np.float32)
    for k in range(1, 22):
        if k * f0 > 3400:
            break
        tone += (1.0 / k) * np.sin(2 * np.pi * k * f0 * t).astype(np.float32)
    envelope = 0.15 + 0.85 * (0.5 - 0.5 * np.cos(2 * np.pi * 4.0 * t))
    return _rms_to(tone * envelope.astype(np.float32), rms)


def quiet(seconds: float, *, rms: float = 0.001, seed: int = 2) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return _rms_to(rng.normal(size=int(seconds * RATE)).astype(np.float32), rms)


def feed(gate: VoiceGate, samples: np.ndarray) -> list[tuple[int, VoiceEvent]]:
    """Feed 20 ms frames; return (frame index, event) for every event."""
    events: list[tuple[int, VoiceEvent]] = []
    step = audio_utils.FRAME_SAMPLES
    for index, start in enumerate(range(0, samples.size - step + 1, step)):
        for event in gate.feed(pcm(samples[start : start + step])):
            events.append((index, event))
    return events


def test_periodicity_tells_a_voice_from_noise() -> None:
    window = np.hanning(320).astype(np.float32)
    assert periodicity(voice(0.04)[:320] * window) > 0.6
    assert periodicity(fan(0.04, rms=0.1)[:320] * window) < 0.35
    assert periodicity(quiet(0.04, rms=0.1)[:320] * window) < 0.35
    assert spectral_flatness(quiet(0.04, rms=0.1)[:320] * window) > 0.6


def test_a_fan_never_becomes_speech() -> None:
    gate = VoiceGate()
    events = feed(gate, fan(4.0, rms=0.03))
    assert events == [], f"the fan produced {events}"
    assert not gate.speaking


def test_a_fan_switched_on_mid_call_does_not_interrupt() -> None:
    """Louder than the room it started in, but steady -- so never voice."""
    gate = VoiceGate()
    events = feed(gate, np.concatenate([quiet(1.0), fan(3.0, rms=0.05)]))
    assert events == [], f"the fan produced {events}"


def test_a_voice_over_the_fan_is_an_onset_and_then_an_end() -> None:
    gate = VoiceGate()
    background = fan(3.5, rms=0.02)
    speech = voice(0.8)
    mixed = background.copy()
    start = int(1.5 * RATE)
    mixed[start : start + speech.size] += speech

    events = feed(gate, mixed)

    starts = [i for i, e in events if e is VoiceEvent.SPEECH_START]
    ends = [i for i, e in events if e is VoiceEvent.SPEECH_END]
    assert len(starts) == 1, f"expected one onset, got {events}"
    # Within the utterance, and early in it: 75 frames is 1.5 s.
    assert 75 <= starts[0] <= 75 + 15, f"onset at frame {starts[0]}"
    assert ends and ends[0] > starts[0]
    assert not gate.speaking
    assert gate.heard_voice_within(60.0)


def test_a_fan_after_digital_silence_is_not_an_onset() -> None:
    """A muted line reads as -180 dB; the fan that follows is loud, sudden and
    flat. Loud and sudden is what a syllable is too -- flat is what it is
    not."""
    gate = VoiceGate()
    zeros = np.zeros(RATE, dtype=np.float32)
    assert feed(gate, np.concatenate([zeros, fan(3.0, rms=0.05)])) == []
    assert gate.floor_db >= -70.0


def test_a_click_is_not_an_onset() -> None:
    gate = VoiceGate()
    burst = quiet(0.06, rms=0.3, seed=5)
    events = feed(gate, np.concatenate([quiet(1.0), burst, quiet(1.0)]))
    assert not any(e is VoiceEvent.SPEECH_START for _, e in events)


def test_silero_hears_a_person_and_not_a_fan() -> None:
    """The optional model, on real speech. Skipped without the weights.

    Real speech, because Silero was trained on it: the harmonic tone the
    other tests use scores under 0.4 with it, while a synthesised Hindi
    question scores 0.78 on average and 0.84 over a fan. The fixture is that
    question, 1.3 s at 8 kHz.
    """
    from pathlib import Path

    import pytest

    from voice_worker.runtime.vad import SileroJudge

    path = Path("models/silero_vad.onnx")
    if not path.is_file():
        pytest.skip("models/silero_vad.onnx not present")

    clip = Path("fixtures/audio/hindi_question_8k.wav")
    if not clip.is_file():
        pytest.skip("fixtures/audio/hindi_question_8k.wav not present")
    wav = clip.read_bytes()
    speech = np.frombuffer(audio_utils.read_wav(wav), dtype="<i2").astype(np.float32) / 32768
    background = fan(1.5 + speech.size / RATE + 1.0, rms=0.02)
    mixed = background.copy()
    start = int(1.5 * RATE)
    mixed[start : start + speech.size] += speech

    gate = VoiceGate(judge=SileroJudge(path))
    events = feed(gate, mixed)
    starts = [i for i, e in events if e is VoiceEvent.SPEECH_START]
    assert len(starts) == 1 and 75 <= starts[0] <= 100, events
    assert any(e is VoiceEvent.SPEECH_END for _, e in events)

    quiet_gate = VoiceGate(judge=SileroJudge(path))
    assert feed(quiet_gate, np.concatenate([quiet(1.0), fan(3.0, rms=0.05)])) == []
    muted_gate = VoiceGate(judge=SileroJudge(path))
    zeros = np.zeros(RATE, dtype=np.float32)
    assert feed(muted_gate, np.concatenate([zeros, fan(3.0, rms=0.05)])) == []


def test_the_gate_has_no_opinion_before_it_hears_anything() -> None:
    gate = VoiceGate()
    assert gate.frames == 0
    assert not gate.heard_voice_within(10.0)
