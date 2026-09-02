"""Language routing to concrete adapters (§5.1, §5.2).

§5.1 is a table, and this is the only module allowed to read it. The tests that
earn their place are the ones about **pairing**: a recogniser that decides turns
for itself put behind a detector that also decides, or one that decides neither.
Both are config mistakes that produce a terrible call and no exception, so they
are caught at build time and asserted here.
"""

from __future__ import annotations

import pytest

from uaagro_domain.enums import QualityTier, TurnStrategy
from uaagro_domain.errors import ConfigurationError
from uaagro_domain.settings import Settings, get_defaults
from voice_worker.adapters.factory import (
    build_speech_stack,
    describe_routes,
)
from voice_worker.turn.base import TurnDecision, TurnState
from voice_worker.turn.smart_turn import (
    SmartTurnDetector,
    ensure_model,
    pcm_to_float32,
    upsample_8k_to_16k,
)


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def defaults():  # type: ignore[no-untyped-def]
    return get_defaults()


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


def test_hindi_gets_soniox_with_delegated_turn_detection(settings, defaults) -> None:  # type: ignore[no-untyped-def]
    stack = build_speech_stack("hi-IN", settings, defaults)
    assert stack.stt.provider == "soniox"
    assert stack.stt.emits_turn_events
    assert stack.turn_detector.strategy is TurnStrategy.SONIOX_ENDPOINT
    assert stack.tts.provider == "bakbak"
    assert stack.quality_tier is QualityTier.A


def test_the_delegated_detector_names_the_engine_that_decided(settings, defaults) -> None:  # type: ignore[no-untyped-def]
    """§5.1 reports the engine behind a language's tier.

    The delegated detector does nothing either way, so it would have been
    easier to leave it hard-coded to ``flux_semantic``. That would put one
    vendor's name against another vendor's behaviour in the admin panel --
    the kind of small inaccuracy that survives right up until someone debugs a
    turn-taking complaint with it.
    """
    hindi = build_speech_stack("hi-IN", settings, defaults)
    assert hindi.turn_detector.strategy.value == "soniox_endpoint"

    on_flux = defaults.model_copy(deep=True)
    on_flux.language_routes["hi-IN"].stt.provider = "deepgram"  # type: ignore[union-attr]
    on_flux.language_routes["hi-IN"].turn.strategy = TurnStrategy.FLUX_SEMANTIC  # type: ignore[union-attr]
    assert build_speech_stack("hi-IN", settings, on_flux).turn_detector.strategy.value == (
        "flux_semantic"
    )


def test_marathi_no_longer_splits_recognition_from_turn_detection(settings, defaults) -> None:  # type: ignore[no-untyped-def]
    """It used to need Sarvam plus a local ONNX model. One engine now."""
    stack = build_speech_stack("mr-IN", settings, defaults)
    assert stack.stt.provider == "soniox"
    assert stack.stt.emits_turn_events
    assert stack.turn_detector.strategy is TurnStrategy.SONIOX_ENDPOINT


def test_malayalam_keeps_tier_c_until_it_is_measured(settings, defaults) -> None:  # type: ignore[no-untyped-def]
    """It gained semantic endpointing, which is a real improvement and not a
    reason to relabel it A before §19.2 has measured anything (§5.1)."""
    stack = build_speech_stack("ml-IN", settings, defaults)
    assert stack.turn_detector.strategy is TurnStrategy.SONIOX_ENDPOINT
    assert stack.quality_tier is QualityTier.C
    assert "pending" in stack.tier_note.lower()


def test_odia_still_uses_the_local_detector(settings, defaults) -> None:  # type: ignore[no-untyped-def]
    """The one language not moved, so the transcribe-only pairing stays
    exercised rather than becoming dead code that nothing builds."""
    stack = build_speech_stack("or-IN", settings, defaults)
    assert stack.stt.provider == "sarvam"
    assert not stack.stt.emits_turn_events
    assert stack.turn_detector.strategy is TurnStrategy.VAD_SILENCE


def test_bhojpuri_rides_the_hindi_engines_but_keeps_its_own_tier(settings, defaults) -> None:  # type: ignore[no-untyped-def]
    """§5.1: route to Hindi, do not attempt a separate model -- and stay honest
    about the resulting quality instead of borrowing Hindi's tier A."""
    stack = build_speech_stack("bho", settings, defaults)
    assert stack.served_by == "hi-IN"
    assert stack.stt.provider == "soniox"
    assert stack.quality_tier is QualityTier.B
    assert stack.language == "bho"


def test_every_configured_language_builds(settings, defaults) -> None:  # type: ignore[no-untyped-def]
    """A language in the table that cannot be built is a latent outage for
    whoever calls in speaking it."""
    for code in defaults.language_routes:
        stack = build_speech_stack(code, settings, defaults)
        assert stack.stt is not None
        assert stack.tts is not None


def test_an_unknown_language_fails_with_an_actionable_message(settings, defaults) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ConfigurationError) as caught:
        build_speech_stack("xx-YY", settings, defaults)
    assert "config/defaults.yaml" in caught.value.remedy


def test_slow_speaker_flag_reaches_the_recogniser(settings, defaults) -> None:  # type: ignore[no-untyped-def]
    """§5.2 raises the end-of-turn backstop for callers observed to pause."""
    normal = build_speech_stack("hi-IN", settings, defaults)
    slow = build_speech_stack("hi-IN", settings, defaults, slow_speaker=True)
    assert not normal.stt_config.slow_speaker
    assert slow.stt_config.slow_speaker


def test_keyterms_reach_the_recogniser(settings, defaults) -> None:  # type: ignore[no-untyped-def]
    """§5.5: bias the decode with the catalogue rather than correcting after."""
    stack = build_speech_stack("hi-IN", settings, defaults, keyterms=("डीएपी", "यूरिया"))
    assert stack.stt_config.keyterms == ("डीएपी", "यूरिया")


def test_the_tts_codec_follows_the_telephony_provider(settings, defaults) -> None:  # type: ignore[no-untyped-def]
    """§23-8: ask the synthesiser for the provider's own codec so nothing
    resamples. Exotel is linear16."""
    stack = build_speech_stack("hi-IN", settings, defaults)
    assert stack.tts_config.codec == settings.tts_output_codec
    assert stack.tts_config.sample_rate == 8000


# --------------------------------------------------------------------------- #
# The pairing check
# --------------------------------------------------------------------------- #


def test_a_self_deciding_recogniser_with_a_second_detector_is_refused(
    settings,  # type: ignore[no-untyped-def]
    defaults,  # type: ignore[no-untyped-def]
) -> None:
    """Two things racing to end the turn cuts the caller off early, and neither
    raises at runtime."""
    broken = defaults.model_copy(deep=True)
    broken.language_routes["hi-IN"].turn.strategy = TurnStrategy.VAD_SILENCE  # type: ignore[union-attr]

    with pytest.raises(ConfigurationError) as caught:
        build_speech_stack("hi-IN", settings, broken)
    assert "cut off early" in caught.value.message
    assert "soniox_endpoint" in caught.value.remedy


def test_a_transcribe_only_recogniser_with_no_detector_is_refused(
    settings,  # type: ignore[no-untyped-def]
    defaults,  # type: ignore[no-untyped-def]
) -> None:
    """Nothing would ever end the turn, and the call would sit until a
    backstop fired."""
    broken = defaults.model_copy(deep=True)
    # Odia, because it is the one language still on a transcribe-only
    # recogniser. Marathi used to be and no longer is, which is precisely why
    # this is pinned to a route the table still has rather than to whichever
    # language happened to be on Sarvam when the test was written.
    broken.language_routes["or-IN"].turn.strategy = TurnStrategy.SONIOX_ENDPOINT  # type: ignore[union-attr]

    with pytest.raises(ConfigurationError) as caught:
        build_speech_stack("or-IN", settings, broken)
    assert "ever end the turn" in caught.value.message


def test_an_unimplemented_provider_names_itself(settings, defaults) -> None:  # type: ignore[no-untyped-def]
    broken = defaults.model_copy(deep=True)
    broken.language_routes["hi-IN"].stt.provider = "whisper"  # type: ignore[union-attr]

    with pytest.raises(ConfigurationError) as caught:
        build_speech_stack("hi-IN", settings, broken)
    assert "whisper" in caught.value.message


# --------------------------------------------------------------------------- #
# Route description, for the admin panel
# --------------------------------------------------------------------------- #


def test_describe_routes_covers_every_language_with_a_tier(defaults) -> None:  # type: ignore[no-untyped-def]
    rows = describe_routes(defaults)
    assert len(rows) == len(defaults.language_routes)
    assert all(row["tier"] in {"A", "B", "C"} for row in rows)
    assert all(row["note"] for row in rows), "a tier with no explanation is not honest"


def test_delegated_languages_report_what_serves_them(defaults) -> None:  # type: ignore[no-untyped-def]
    rows = {row["code"]: row for row in describe_routes(defaults)}
    assert rows["bho"]["served_by"] == "hi-IN"
    assert rows["hi-IN"]["served_by"] == "hi-IN"


# --------------------------------------------------------------------------- #
# Smart Turn helpers
# --------------------------------------------------------------------------- #


def test_upsampling_doubles_the_sample_count() -> None:
    """The model wants 16 kHz; the telephony leg is 8 kHz."""
    import struct

    pcm = struct.pack("<4h", 0, 1000, -1000, 500)
    out = upsample_8k_to_16k(pcm)
    assert len(out) == len(pcm) * 2


def test_upsampling_preserves_the_original_samples() -> None:
    """Interpolation inserts between samples; it must not alter them."""
    import struct

    original = [0, 1000, -1000, 500]
    out = struct.unpack("<8h", upsample_8k_to_16k(struct.pack("<4h", *original)))
    assert list(out[::2]) == original


def test_upsampling_handles_empty_audio() -> None:
    assert upsample_8k_to_16k(b"") == b""


def test_pcm_converts_to_normalised_floats() -> None:
    import struct

    values = pcm_to_float32(struct.pack("<3h", 0, 32767, -32768))
    assert values[0] == 0.0
    assert 0.99 < values[1] <= 1.0
    assert values[2] == -1.0


def test_a_missing_model_fails_with_the_download_command() -> None:
    """§0 rule 4: an absent artefact is a loud failure naming the fix. Falling
    back silently would leave Marathi labelled tier B while behaving worse."""
    from pathlib import Path

    with pytest.raises(ConfigurationError) as caught:
        ensure_model(Path("models/definitely-not-here.onnx"))
    assert "huggingface-cli download" in caught.value.remedy


async def test_smart_turn_respects_the_minimum_delay() -> None:
    """§5.2 floors the wait however sure the model is."""
    detector = SmartTurnDetector(min_delay_s=0.30)
    result = await detector.evaluate(TurnState(silence_ms=100, transcript="डीएपी"))
    assert result.decision is TurnDecision.CONTINUE
    assert "minimum delay" in result.reason


async def test_smart_turn_respects_the_maximum_delay() -> None:
    """A confused model must not be able to stall a call."""
    detector = SmartTurnDetector(max_delay_s=2.5)
    result = await detector.evaluate(TurnState(silence_ms=3000, transcript="डीएपी और"))
    assert result.decision is TurnDecision.END
    assert "maximum delay" in result.reason


async def test_a_missing_model_degrades_visibly_not_silently() -> None:
    """The fallback is acceptable; hiding it is not. Every decision says so,
    so a degraded language shows up in the call log."""
    detector = SmartTurnDetector(model_path=__import__("pathlib").Path("nope.onnx"))
    result = await detector.evaluate(TurnState(silence_ms=1800, transcript="डीएपी चाहिए"))
    assert detector.degraded
    assert "unavailable" in result.reason
