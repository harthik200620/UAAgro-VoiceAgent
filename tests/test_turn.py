"""Turn detection (§5.2, §19.2).

§19.2 measures two error rates per language: **false cuts** (the agent
interrupts the farmer) and **dead air** (the agent waits too long). False cuts
are weighted more heavily, and these tests encode that asymmetry — most of them
assert that the detector *keeps waiting*.
"""

from __future__ import annotations

import pytest

from uaagro_domain.enums import TurnStrategy
from voice_worker.adapters.stt.base import SttConfig, SttEvent, SttEventType
from voice_worker.turn.base import (
    DelegatedTurnDetector,
    TurnDecision,
    TurnState,
    VadSilenceTurnDetector,
    ends_mid_sentence,
    ends_on_a_terminal_word,
    last_word,
)

# --------------------------------------------------------------------------- #
# Hindi endpointing cues
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "transcript",
    [
        "मुझे डीएपी चाहिए और",
        "एक बीघे में",
        "गेहूँ का",
        "अगर बारिश हुई तो",
        "मतलब",
    ],
)
def test_a_trailing_particle_means_the_farmer_is_mid_sentence(transcript: str) -> None:
    """Nobody ends a sentence on "और". §5.2 grants these another 400 ms."""
    assert ends_mid_sentence(transcript)


@pytest.mark.parametrize(
    "transcript",
    ["डीएपी चाहिए", "यह ठीक है", "नहीं", "हाँ जी", "दस बोरी चाहिए"],
)
def test_terminal_words_are_recognised(transcript: str) -> None:
    assert ends_on_a_terminal_word(transcript)
    assert not ends_mid_sentence(transcript)


def test_last_word_handles_punctuation_and_devanagari() -> None:
    assert last_word("डीएपी चाहिए।") == "चाहिए"
    assert last_word("") == ""
    assert last_word("   ") == ""


# --------------------------------------------------------------------------- #
# VAD silence detector
# --------------------------------------------------------------------------- #


async def test_short_silence_does_not_end_the_turn() -> None:
    detector = VadSilenceTurnDetector()
    result = await detector.evaluate(TurnState(silence_ms=300, transcript="डीएपी"))
    assert result.decision is TurnDecision.CONTINUE


async def test_sustained_silence_ends_the_turn() -> None:
    detector = VadSilenceTurnDetector()
    result = await detector.evaluate(TurnState(silence_ms=900, transcript="डीएपी चाहिए"))
    assert result.decision is TurnDecision.END


async def test_a_trailing_particle_buys_more_time() -> None:
    """The same silence that ends a complete sentence must not end one that
    trails off on "और" -- that is a false cut, the expensive error."""
    detector = VadSilenceTurnDetector()
    silence = TurnState(silence_ms=1000, transcript="डीएपी और")
    complete = TurnState(silence_ms=1000, transcript="डीएपी चाहिए")

    assert (await detector.evaluate(silence)).decision is TurnDecision.CONTINUE
    assert (await detector.evaluate(complete)).decision is TurnDecision.END


async def test_slow_speakers_are_given_longer() -> None:
    """§5.2 persists this flag on the farmer record after two observed pauses,
    so a caller who was cut off once is not cut off again."""
    detector = VadSilenceTurnDetector()
    at_threshold = TurnState(silence_ms=900, transcript="डीएपी चाहिए")

    assert (await detector.evaluate(at_threshold)).decision is TurnDecision.END

    slow = TurnState(silence_ms=900, transcript="डीएपी चाहिए", slow_speaker=True)
    assert (await detector.evaluate(slow)).decision is TurnDecision.CONTINUE


async def test_the_backstop_always_fires() -> None:
    """A detector that never ends a turn would hold the line open forever."""
    detector = VadSilenceTurnDetector()
    result = await detector.evaluate(
        TurnState(silence_ms=20_000, transcript="डीएपी और", slow_speaker=True)
    )
    assert result.decision is TurnDecision.END
    assert "backstop" in result.reason


async def test_the_reason_is_always_recorded() -> None:
    """§12 tunes these thresholds against real data, which needs the reason
    and not just the outcome."""
    detector = VadSilenceTurnDetector()
    for state in (
        TurnState(silence_ms=100, transcript="डीएपी"),
        TurnState(silence_ms=2000, transcript="डीएपी चाहिए"),
        TurnState(silence_ms=1000, transcript="डीएपी और"),
    ):
        assert (await detector.evaluate(state)).reason


async def test_rule_based_detectors_do_not_invent_a_probability() -> None:
    """A number that looks like a model score but is not would mislead anyone
    reading the call event log."""
    detector = VadSilenceTurnDetector()
    result = await detector.evaluate(TurnState(silence_ms=100))
    assert result.probability is None


# --------------------------------------------------------------------------- #
# Delegated detector
# --------------------------------------------------------------------------- #


async def test_the_delegated_detector_never_decides() -> None:
    """On the Flux path the recogniser owns the decision. Two detectors both
    ending the turn would cut the caller off early."""
    detector = DelegatedTurnDetector()
    assert detector.strategy is TurnStrategy.FLUX_SEMANTIC
    for silence in (0, 500, 5_000, 60_000):
        result = await detector.evaluate(TurnState(silence_ms=silence, transcript="कुछ भी"))
        assert result.decision is TurnDecision.CONTINUE


# --------------------------------------------------------------------------- #
# STT event contract
# --------------------------------------------------------------------------- #


def test_only_end_of_turn_is_terminal() -> None:
    for kind in SttEventType:
        event = SttEvent(type=kind)
        assert event.is_terminal_for_turn == (kind is SttEventType.END_OF_TURN)


def test_eager_and_resumed_are_distinct_events() -> None:
    """§5.2: on TurnResumed the speculative generation must be cancelled. If
    resumption were folded into a transcript update the pipeline could not tell
    it needed to, and an orphaned generation that still speaks is a severe bug."""
    assert SttEventType.EAGER_END_OF_TURN is not SttEventType.TURN_RESUMED


def test_stt_config_defaults_match_the_specification() -> None:
    """§5.2 gives these thresholds explicitly."""
    config = SttConfig(language="hi-IN")
    assert config.eager_eot_threshold == 0.45
    assert config.eot_threshold == 0.75
    assert config.eot_timeout_ms == 6000
    assert config.sample_rate == 8000


def test_slow_speaker_raises_the_flux_backstop() -> None:
    """§5.2 raises eot_timeout_ms to 8000 for callers flagged slow."""
    from voice_worker.adapters.stt.deepgram_flux import DeepgramFluxSTT

    normal = SttConfig(language="hi-IN", eot_timeout_ms=6000)
    slow = SttConfig(language="hi-IN", eot_timeout_ms=6000, slow_speaker=True)

    assert DeepgramFluxSTT._eot_timeout(normal) == 6000
    assert DeepgramFluxSTT._eot_timeout(slow) == 8000


# --------------------------------------------------------------------------- #
# Smart Turn features (§5.2)
# --------------------------------------------------------------------------- #


def test_the_feature_shape_matches_what_the_model_declares() -> None:
    """Smart Turn v3 takes an 80 x 800 log-mel spectrogram, not a waveform.

    The first version of the adapter fed it raw float32 samples. ONNX raised a
    shape error, the adapter's `except` swallowed it, and the detector silently
    never ran -- so every Marathi call fell back to VAD while the admin panel
    reported tier B. Nothing in the logs looked like a bug.
    """
    import numpy as np

    from voice_worker.turn.features import N_FRAMES, N_MELS, log_mel_spectrogram

    features = log_mel_spectrogram(np.zeros(16_000, dtype=np.float32))
    assert features.shape == (N_MELS, N_FRAMES)
    assert features.dtype == np.float32


def test_short_audio_is_padded_at_the_front() -> None:
    """The utterance stays flush with the end of the window. Padding the back
    would put the moment of interest -- whether the speaker just stopped -- in
    the middle of silence."""
    import numpy as np

    from voice_worker.turn.features import N_FRAMES, log_mel_spectrogram

    tone = np.sin(2 * np.pi * 300 * np.arange(8000) / 16000).astype(np.float32)
    features = log_mel_spectrogram(tone)
    assert features.shape[1] == N_FRAMES
    # The last frames carry the signal; the first are the pad.
    assert features[:, -10:].std() > features[:, :10].std()


def test_long_audio_keeps_the_most_recent_window() -> None:
    """Older audio carries no signal about whether *this* utterance ended."""
    import numpy as np

    from voice_worker.turn.features import N_FRAMES, WINDOW_SAMPLES, log_mel_spectrogram

    quiet_then_loud = np.concatenate(
        [
            np.zeros(WINDOW_SAMPLES, dtype=np.float32),
            (0.5 * np.sin(2 * np.pi * 300 * np.arange(WINDOW_SAMPLES) / 16000)).astype(
                np.float32
            ),
        ]
    )
    features = log_mel_spectrogram(quiet_then_loud)
    assert features.shape[1] == N_FRAMES
    # It kept the loud half, so the features vary rather than sitting at the
    # dynamic-range floor.
    assert features.std() > 0.05


def test_the_features_are_bounded() -> None:
    """Whisper's normalisation clamps 8 dB below the peak and scales to roughly
    [-1, 1]. Unbounded features would be a normalisation that did not run."""
    import numpy as np

    from voice_worker.turn.features import log_mel_spectrogram

    rng = np.random.default_rng(0)
    for signal in (
        np.zeros(16_000, dtype=np.float32),
        (0.9 * rng.standard_normal(16_000)).astype(np.float32),
        (1e-6 * rng.standard_normal(16_000)).astype(np.float32),
    ):
        features = log_mel_spectrogram(signal)
        assert -2.5 < float(features.min()) <= float(features.max()) < 2.5
        assert np.isfinite(features).all()


def test_the_real_model_accepts_the_features() -> None:
    """The end-to-end check the shape assertions above cannot make.

    Skipped when the weights are absent -- they are an 8 MB download, not a
    committed artefact -- but when they are present this is what proves the
    detector actually runs.
    """
    import numpy as np
    import pytest as _pytest

    from voice_worker.turn.features import WINDOW_SAMPLES, log_mel_spectrogram
    from voice_worker.turn.smart_turn import default_model_path

    path = default_model_path()
    if not path.is_file():
        _pytest.skip(f"{path} not downloaded; run `make models`")

    import onnxruntime as ort

    session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    audio = (
        0.1 * np.sin(2 * np.pi * 220 * np.arange(WINDOW_SAMPLES) / 16000)
    ).astype(np.float32)
    features = log_mel_spectrogram(audio)

    outputs = session.run(None, {session.get_inputs()[0].name: features[None, :, :]})
    logit = float(np.asarray(outputs[0]).reshape(-1)[0])
    assert np.isfinite(logit)

    # The graph emits a logit; the adapter squashes it so DEFAULT_THRESHOLD
    # stays the probability §5.2 describes.
    probability = 1.0 / (1.0 + np.exp(-logit))
    assert 0.0 <= probability <= 1.0
