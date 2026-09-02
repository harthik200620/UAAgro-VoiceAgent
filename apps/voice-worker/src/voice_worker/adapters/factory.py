"""Building a language's speech stack from the §5.1 routing table.

§5.1 requires routing to be *declarative* -- a config table, never a chain of
``if`` statements -- so adding a language is an edit to
``config/defaults.yaml``. This module is the one place that reads that table and
returns concrete adapters, which is what keeps the rest of the pipeline free of
per-language branching.

The check worth having here is the **pairing check**. A recogniser either emits
its own end-of-turn events (Flux) or it does not (Sarvam). Pair a self-deciding
recogniser with a real turn detector and two things race to end the turn, so the
caller gets cut off early; pair a transcribe-only recogniser with the delegated
no-op detector and nothing ever ends the turn at all. Both are config mistakes
that produce terrible calls and no error, so :func:`build_speech_stack` refuses
to return a mismatched pair.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from uaagro_domain.enums import (
    VENDOR_DECIDED_TURN_STRATEGIES,
    QualityTier,
    TurnStrategy,
)
from uaagro_domain.errors import ConfigurationError
from uaagro_domain.settings import Defaults, LanguageRoute, Settings

from ..turn.base import DelegatedTurnDetector, TurnDetector, VadSilenceTurnDetector
from .stt.base import SttConfig, STTService
from .tts.base import TtsConfig, TTSService

log = structlog.get_logger(__name__)

#: Which turn strategy each self-deciding recogniser actually provides.
#:
#: Only used by the pairing check. Two vendor-decided strategies behave
#: identically at runtime -- both stand the local detector down -- so crossing
#: them raises nothing and changes no audio. What it changes is the engine name
#: §5.1 puts next to a language's quality tier in the admin panel, which is the
#: number someone will later use to decide whether a tier is honest.
VENDOR_DECIDED_BY_PROVIDER: dict[str, TurnStrategy] = {
    "soniox": TurnStrategy.SONIOX_ENDPOINT,
    "deepgram": TurnStrategy.FLUX_SEMANTIC,
}


@dataclass(frozen=True, slots=True)
class SpeechStack:
    """Everything one call's audio path needs, already paired and validated."""

    language: str
    #: The language actually served. Differs from ``language`` when the route
    #: delegates -- Bhojpuri is served by the Hindi engines (§5.1).
    served_by: str
    stt: STTService
    stt_config: SttConfig
    tts: TTSService
    tts_config: TtsConfig
    turn_detector: TurnDetector
    #: The tier reported to the admin panel. §5.1 requires this to be honest:
    #: a tier C language is labelled as measurably worse, not shipped as
    #: equivalent.
    quality_tier: QualityTier
    tier_note: str


def build_stt(
    route: LanguageRoute,
    settings: Settings,
    *,
    keyterms: tuple[str, ...] = (),
    slow_speaker: bool = False,
    defaults: Defaults | None = None,
) -> tuple[STTService, SttConfig]:
    """Construct the recogniser this route calls for."""
    if route.stt is None:
        raise ConfigurationError(
            f"Route {route.label_en!r} has no STT configured.",
            remedy="Give the language an `stt` block in config/defaults.yaml, or a "
            "`routes_to` pointing at a language that has one.",
        )

    flux = defaults.turn_detection.flux if defaults else None
    soniox = defaults.turn_detection.soniox if defaults else None
    config = SttConfig(
        language=route.tts.language if route.tts else route.stt.language_hints[0],
        language_hints=tuple(route.stt.language_hints),
        model=route.stt.model,
        keyterms=keyterms,
        slow_speaker=slow_speaker,
        eager_eot_threshold=flux.eager_eot_threshold if flux else 0.45,
        eot_threshold=flux.eot_threshold if flux else 0.75,
        eot_timeout_ms=flux.eot_timeout_ms if flux else 6000,
        # Both engines' end-of-turn settings are filled regardless of which one
        # serves this route: the adapter reads the pair it understands, and a
        # config assembled per provider is a config that can disagree with the
        # routing table it came from.
        endpoint_latency_adjustment_level=soniox.latency_adjustment_level if soniox else 0,
        endpoint_sensitivity=soniox.sensitivity if soniox else 0.0,
        max_endpoint_delay_ms=soniox.max_endpoint_delay_ms if soniox else 2000,
        eager_after_final_ms=soniox.eager_after_final_ms if soniox else 160,
        eager_after_stable_ms=soniox.eager_after_stable_ms if soniox else 300,
    )

    match route.stt.provider:
        case "soniox":
            from .stt.soniox import SonioxSTT

            return SonioxSTT(settings), config
        case "deepgram":
            from .stt.deepgram_flux import DeepgramFluxSTT

            return DeepgramFluxSTT(settings), config
        case "sarvam":
            from .stt.sarvam import SarvamSTT

            return SarvamSTT(settings), config
        case unknown:
            raise ConfigurationError(
                f"No STT adapter is implemented for provider {unknown!r}.",
                remedy="Use 'soniox', 'deepgram' or 'sarvam' in config/defaults.yaml, "
                "or add an adapter under voice_worker/adapters/stt/.",
                context={"provider": unknown},
            )


def build_tts(
    route: LanguageRoute, settings: Settings, *, defaults: Defaults | None = None
) -> tuple[TTSService, TtsConfig]:
    """Construct the synthesiser this route calls for."""
    if route.tts is None:
        raise ConfigurationError(
            f"Route {route.label_en!r} has no TTS configured.",
            remedy="Give the language a `tts` block in config/defaults.yaml.",
        )

    config = TtsConfig(
        language=route.tts.language,
        model=route.tts.model,
        speaker=_voice_for(route, settings),
        pace=defaults.tts.pace if defaults else 0.97,
        sample_rate=settings.tts_output_sample_rate,
        codec=settings.tts_output_codec,
    )

    match route.tts.provider:
        case "bakbak":
            from .tts.bakbak import BakbakTTS

            return BakbakTTS(settings), config
        case "sarvam":
            from .tts.sarvam import SarvamTTS

            return SarvamTTS(settings), config
        case unknown:
            raise ConfigurationError(
                f"No TTS adapter is implemented for provider {unknown!r}.",
                remedy="Use 'bakbak' or 'sarvam' in config/defaults.yaml, or add an "
                "adapter under voice_worker/adapters/tts/.",
                context={"provider": unknown},
            )


def _voice_for(route: LanguageRoute, settings: Settings) -> str | None:
    """Which voice speaks this language.

    Environment first, routing table second. The order matters: voice ids are
    account-scoped, so a value checked into ``defaults.yaml`` is at best a
    default for one deployment, and a deployment that overrode it in its own
    environment must not have that silently ignored.

    ``None`` is a legitimate answer and the adapters treat it as a missing
    credential (§0 rule 4). Substituting some other voice would mean a caller
    hearing a person nobody chose in the §5.3 bake-off.
    """
    assert route.tts is not None
    if route.tts.provider == "bakbak":
        return settings.voice_for_language(route.tts.language) or route.tts.voice
    return settings.sarvam_tts_speaker_hi or route.tts.voice


def build_turn_detector(route: LanguageRoute, *, defaults: Defaults | None = None) -> TurnDetector:
    """Construct the turn detector this route calls for (§5.2)."""
    if route.turn is None:
        raise ConfigurationError(
            f"Route {route.label_en!r} has no turn strategy configured.",
            remedy="Give the language a `turn` block in config/defaults.yaml.",
        )

    match route.turn.strategy:
        case TurnStrategy.FLUX_SEMANTIC | TurnStrategy.SONIOX_ENDPOINT as vendor_decided:
            # Both recognisers decide the turn themselves, so the local
            # detector stands down. They stay separate strategies rather than
            # collapsing into one "delegated" value because §5.1 reports the
            # engine that made the decision, and "flux_semantic" on a Soniox
            # route would be a lie in the admin panel.
            return DelegatedTurnDetector(vendor_decided)

        case TurnStrategy.SMART_TURN_V3:
            from ..turn.smart_turn import SmartTurnDetector

            smart = defaults.turn_detection.smart_turn_v3 if defaults else None
            return SmartTurnDetector(
                min_delay_s=smart.min_delay_s if smart else 0.30,
                max_delay_s=smart.max_delay_s if smart else 2.50,
            )

        case TurnStrategy.VAD_SILENCE:
            vad = defaults.turn_detection.vad_silence if defaults else None
            return VadSilenceTurnDetector(
                stop_secs=vad.stop_secs if vad else 0.85,
                trailing_particle_grace_ms=vad.trailing_particle_grace_ms if vad else 400,
            )


def build_speech_stack(
    language: str,
    settings: Settings,
    defaults: Defaults,
    *,
    keyterms: tuple[str, ...] = (),
    slow_speaker: bool = False,
) -> SpeechStack:
    """Resolve a language to a validated, paired speech stack.

    Raises:
        ConfigurationError: for an unknown language, an unimplemented provider,
            or a recogniser and turn detector that cannot work together.
    """
    served_by, route = defaults.resolve_language(language)

    stt, stt_config = build_stt(
        route, settings, keyterms=keyterms, slow_speaker=slow_speaker, defaults=defaults
    )
    tts, tts_config = build_tts(route, settings, defaults=defaults)
    detector = build_turn_detector(route, defaults=defaults)

    _assert_compatible(stt, detector, language=language)

    log.info(
        "speech_stack.built",
        requested=language,
        served_by=served_by,
        stt=stt.provider,
        tts=tts.provider,
        turn=detector.strategy.value,
        tier=route.quality_tier.value,
    )
    return SpeechStack(
        language=language,
        served_by=served_by,
        stt=stt,
        stt_config=stt_config,
        tts=tts,
        tts_config=tts_config,
        turn_detector=detector,
        quality_tier=route.quality_tier,
        tier_note=route.tier_note_en,
    )


def _assert_compatible(stt: STTService, detector: TurnDetector, *, language: str) -> None:
    """Refuse a recogniser and detector that would fight or stall.

    Neither mistake raises at runtime. A double-ender cuts the caller off
    early; a no-ender holds the turn open until the backstop fires. Both feel
    like a broken agent and neither leaves a stack trace, so they are caught
    here where the pairing is decided.
    """
    delegated = detector.strategy in VENDOR_DECIDED_TURN_STRATEGIES

    if stt.emits_turn_events and not delegated:
        raise ConfigurationError(
            f"Language {language!r} pairs {stt.provider}, which emits its own "
            f"end-of-turn events, with the {detector.strategy.value} detector. Both "
            f"would try to end the turn and the caller would be cut off early.",
            remedy="Set this language's turn strategy to the one its recogniser "
            "provides -- 'soniox_endpoint' or 'flux_semantic' -- in "
            "config/defaults.yaml, or route it to a recogniser that only transcribes.",
            context={"language": language, "stt": stt.provider},
        )

    expected = VENDOR_DECIDED_BY_PROVIDER.get(stt.provider)
    if delegated and expected is not None and detector.strategy is not expected:
        raise ConfigurationError(
            f"Language {language!r} is recognised by {stt.provider} but its turn "
            f"strategy is {detector.strategy.value!r}, which belongs to another "
            f"engine. The call would work and the admin panel would name the wrong "
            f"vendor for the decision.",
            remedy=f"Set this language's turn strategy to {expected.value!r} in "
            "config/defaults.yaml.",
            context={"language": language, "stt": stt.provider},
        )

    if not stt.emits_turn_events and delegated:
        raise ConfigurationError(
            f"Language {language!r} pairs {stt.provider}, which only transcribes, with "
            f"the delegated detector. Nothing would ever end the turn.",
            remedy="Give this language a real turn strategy -- 'smart_turn_v3' where it "
            "covers the language, otherwise 'vad_silence' -- in config/defaults.yaml.",
            context={"language": language, "stt": stt.provider},
        )


def describe_routes(defaults: Defaults) -> list[dict[str, str]]:
    """Every configured language and how it is served.

    Feeds the admin panel's language list. §5.1 requires the quality tier to be
    shown rather than implied, so a tier C language reads as measurably worse
    instead of appearing alongside Hindi with no distinction.
    """
    rows: list[dict[str, str]] = []
    for code in sorted(defaults.language_routes):
        served_by, route = defaults.resolve_language(code)
        rows.append(
            {
                "code": code,
                "label": route.label_en,
                "native": route.label_native,
                "served_by": served_by,
                "stt": route.stt.provider if route.stt else "-",
                "tts": route.tts.provider if route.tts else "-",
                "turn": route.turn.strategy.value if route.turn else "-",
                "tier": route.quality_tier.value,
                "note": route.tier_note_en,
            }
        )
    return rows
