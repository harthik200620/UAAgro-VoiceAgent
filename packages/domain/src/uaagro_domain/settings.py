"""Runtime configuration.

Two sources, deliberately separate:

* ``config/defaults.yaml`` -- non-secret behaviour: thresholds, budgets, the
  language routing table. Checked into the repo, editable without a redeploy.
* the environment -- credentials and endpoints only. Never in the repo, never in
  an image, never in the database (§17).

§0 rule 4: a missing credential does not degrade behaviour silently. Adapters
call :meth:`Settings.require` at construction and raise
:class:`MissingCredentialError` naming the variable.
"""

from __future__ import annotations

import functools
import os
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .enums import AudioCodec, QualityTier, TelephonyProvider, TurnStrategy
from .errors import ConfigurationError, MissingCredentialError

Environment = Literal["development", "test", "staging", "production"]


# --------------------------------------------------------------------------- #
# defaults.yaml -- typed
# --------------------------------------------------------------------------- #


class SttRoute(BaseModel):
    provider: str
    model: str
    language_hints: list[str] = Field(default_factory=list)


class TurnRoute(BaseModel):
    strategy: TurnStrategy


class TtsRoute(BaseModel):
    provider: str
    model: str
    #: The route language in BCP-47 (``hi-IN``), never the vendor's own code.
    #: Vendors disagree -- Sarvam wants ``hi-IN``, Bakbak wants ``hi`` and
    #: ``en-in`` -- and the mapping belongs in the adapter, which is the only
    #: place that knows one vendor from another (§4.1). Putting a vendor code
    #: here would also leak into the audio cache key and the admin panel.
    language: str
    #: Vendor voice identifier. Bakbak voice ids are account-scoped UUIDs, so
    #: this is normally left empty here and supplied per deployment through
    #: ``BAKBAK_VOICE_*``; §5.3 requires the choice come from the bake-off
    #: rather than from a documentation example either way.
    voice: str | None = None


class LanguageRoute(BaseModel):
    """One row of the §5.1 routing matrix.

    A route either resolves locally (``stt``/``turn``/``tts`` present) or defers
    to another language via ``routes_to`` -- which is how Bhojpuri and Awadhi
    ride the Hindi path without a separate model (§5.1).
    """

    label_en: str
    label_native: str
    quality_tier: QualityTier
    tier_note_en: str
    aliases: list[str] = Field(default_factory=list)
    routes_to: str | None = None
    stt: SttRoute | None = None
    turn: TurnRoute | None = None
    tts: TtsRoute | None = None

    @property
    def is_delegated(self) -> bool:
        return self.routes_to is not None


class FluxTurnSettings(BaseModel):
    eager_eot_threshold: float
    eot_threshold: float
    eot_timeout_ms: int
    eot_timeout_ms_slow_speaker: int


class SonioxEndpointSettings(BaseModel):
    """§5.2 tuning for Soniox's semantic endpointing.

    Soniox exposes the end-of-turn decision as three dials rather than Flux's
    single threshold, and they trade against each other: firing sooner ends the
    turn on less audio, so the model has had less chance to revise the words it
    already emitted.
    """

    #: 0-3. Higher shortens the wait after speech stops.
    latency_adjustment_level: int
    #: -1.0 to 1.0. Higher makes an endpoint more likely on the same audio.
    sensitivity: float
    #: 500-3000 ms. The backstop: how long after speech stops an endpoint is
    #: emitted regardless.
    max_endpoint_delay_ms: int
    #: How long the recogniser may sit with every token finalised before the
    #: pipeline treats the turn as *probably* over and starts generating
    #: speculatively (§5.2). Soniox has no eager-EOT probability of its own;
    #: this is the substitute, and it is a heuristic rather than a vendor
    #: signal -- see the adapter.
    eager_after_final_ms: int


class SmartTurnSettings(BaseModel):
    min_delay_s: float
    max_delay_s: float


class VadTurnSettings(BaseModel):
    stop_secs: float
    trailing_particle_grace_ms: int


class TurnDetectionSettings(BaseModel):
    flux: FluxTurnSettings
    soniox: SonioxEndpointSettings
    smart_turn_v3: SmartTurnSettings
    vad_silence: VadTurnSettings
    slow_speaker_pause_threshold_ms: int
    slow_speaker_observations_required: int


class TtsCacheSettings(BaseModel):
    enabled: bool
    preload: list[str]
    top_sentence_cache_size: int


class TtsSettings(BaseModel):
    pace: float
    max_answer_words: int
    max_dosage_answer_words: int
    sentence_chunking: bool
    cache: TtsCacheSettings


class BargeInSettings(BaseModel):
    enabled: bool
    send_provider_clear: bool
    cancel_deadline_ms: int
    suppress_during_disclosure_ms: int


class SegmentBudget(BaseModel):
    p50: int
    p95: int


class LatencyBudget(BaseModel):
    network_in: SegmentBudget
    turn_commit: SegmentBudget
    stt_final: SegmentBudget
    tool_execution: SegmentBudget
    llm_ttft: SegmentBudget
    tts_ttfb: SegmentBudget
    network_out: SegmentBudget
    total_no_tool: SegmentBudget
    total_with_tool: SegmentBudget


class LlmSettings(BaseModel):
    temperature: float
    max_output_tokens: int
    target_input_tokens_per_turn: int
    first_token_timeout_ms: int
    total_timeout_ms: int
    max_tool_calls_per_turn: int
    history_verbatim_turns: int
    history_summary_refresh_every: int
    prompt_caching: bool


class ToolSettings(BaseModel):
    p95_budget_ms: int
    hard_timeout_ms: int
    retries: int


class KnowledgeSettings(BaseModel):
    chunk_tokens_min: int
    chunk_tokens_max: int
    chunk_overlap_ratio: float
    rrf_k: int
    rerank_candidates: int
    rerank_keep: int
    answer_cache_default_ttl_s: int
    answer_cache_forbidden_fields: list[str]


class CallHandlingSettings(BaseModel):
    silence_first_prompt_s: int
    silence_second_prompt_s: int
    silence_abandon_s: int
    hold_patience_s: int
    low_asr_confidence: float
    low_asr_consecutive_turns_to_escalate: int
    same_intent_failures_to_escalate: int


class EscalationSettings(BaseModel):
    transfer_ring_timeout_s: int
    whisper_max_s: int
    max_transfers_per_caller_per_day: int
    sentiment_decline_turns: int
    sentiment_escalate_below: float
    high_value_order_ceiling_inr: int
    reasons: list[str]


class SpamScreeningSettings(BaseModel):
    enabled: bool
    max_calls_per_hour: int
    max_calls_per_day: int
    repeat_abandon_threshold: int
    rejection_message_key: str
    rejection_max_duration_s: int


class ComplianceSettings(BaseModel):
    calling_window_start: str
    calling_window_end: str
    max_attempts_per_contact: int
    min_hours_between_attempts: int
    retry_gap_hours_busy: int
    rolling_contact_cap_per_farmer_per_week: int
    promotional_cli_series: str
    consent_validity_days: int
    dnd_scrub_max_age_hours: int
    complaint_threshold_auto_pause: int
    retention_days_recordings: int
    retention_days_transcripts: int


class SeedRate(BaseModel):
    vendor: str
    service: str
    unit: str
    rate: Decimal
    currency: str


class CostSettings(BaseModel):
    usd_inr_rate: Decimal
    max_cost_per_call_inr: Decimal
    daily_spend_cap_inr: Decimal
    seed_rates: list[SeedRate]


class Identity(BaseModel):
    org_name: str
    brand_name: str
    helpline_tollfree: str
    missed_call_number: str
    support_email: str
    default_timezone: str
    currency: str


class Defaults(BaseModel):
    """The whole of ``config/defaults.yaml``, typed."""

    identity: Identity
    language_routes: dict[str, LanguageRoute]
    default_language: str
    turn_detection: TurnDetectionSettings
    tts: TtsSettings
    barge_in: BargeInSettings
    latency_budget_ms: LatencyBudget
    llm: LlmSettings
    tools: ToolSettings
    knowledge: KnowledgeSettings
    call_handling: CallHandlingSettings
    escalation: EscalationSettings
    spam_screening: SpamScreeningSettings
    compliance: ComplianceSettings
    cost: CostSettings

    def resolve_language(self, code: str) -> tuple[str, LanguageRoute]:
        """Resolve a language code to the route that actually serves it.

        Follows ``routes_to`` once (Bhojpuri to Hindi) and matches aliases, so
        the pipeline asks for ``bho`` and receives the Hindi STT/TTS pair while
        the *reported* tier stays the one declared for Bhojpuri.
        """
        route = self.language_routes.get(code)
        if route is None:
            for key, candidate in self.language_routes.items():
                if code in candidate.aliases:
                    route, code = candidate, key
                    break
        if route is None:
            raise ConfigurationError(
                f"No language route is configured for {code!r}.",
                remedy="Add the language to language_routes in config/defaults.yaml. "
                "Routing is declarative -- it is never an if-statement in the pipeline.",
                context={"requested": code, "available": sorted(self.language_routes)},
            )
        if route.routes_to is not None:
            target = self.language_routes.get(route.routes_to)
            if target is None:
                raise ConfigurationError(
                    f"Language {code!r} delegates to {route.routes_to!r}, which is not configured.",
                    remedy="Fix the routes_to target in config/defaults.yaml.",
                    context={"from": code, "to": route.routes_to},
                )
            # Serve with the target engines but keep the declared tier honest.
            merged = target.model_copy(
                update={
                    "quality_tier": route.quality_tier,
                    "tier_note_en": route.tier_note_en,
                    "label_en": route.label_en,
                    "label_native": route.label_native,
                }
            )
            return route.routes_to, merged
        return code, route


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #


#: Every field that holds a credential.
#:
#: One list, used three ways: the placeholder validator below, the
#: production readiness check, and the write-only Settings screen (§15.1).
#: Three copies would drift, and the copy that drifts is the one that stops
#: masking a key.
SECRET_FIELDS: tuple[str, ...] = (
    "exotel_sid",
    "exotel_api_key",
    "exotel_api_token",
    "telephony_webhook_secret",
    "plivo_auth_id",
    "plivo_auth_token",
    "soniox_api_key",
    "bakbak_api_key",
    "deepgram_api_key",
    "sarvam_api_key",
    "sarvam_tts_speaker_hi",
    "anthropic_api_key",
    "llm_gateway_master_key",
    "wa_access_token",
    "wa_app_secret",
    "kms_key_id",
    "phone_hash_pepper",
    "local_dek_base64",
    "jwt_signing_key",
    "inbound_did",
    "outbound_cli_promotional",
    "outbound_cli_transactional",
    "telephony_ws_token",
    "s3_access_key_id",
    "s3_secret_access_key",
    "wa_phone_number_id",
    "wa_business_account_id",
    "wa_webhook_verify_token",
)


#: Fields where a copied-but-unedited placeholder must read as absent.
#:
#: Every credential, plus the handful of non-secret values that are equally
#: unusable when left at their placeholder -- a voice id among them, because
#: sending ``FILL_ME`` to a vendor produces a validation error rather than the
#: message naming the variable that §0 rule 4 requires.
PLACEHOLDER_FIELDS: tuple[str, ...] = (
    *SECRET_FIELDS,
    "bakbak_voice_hi",
    "bakbak_voice_en",
)


class Settings(BaseSettings):
    """Environment-sourced configuration. Secrets live here and nowhere else."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Environment = "development"
    log_level: str = "INFO"
    log_json: bool = False

    # --- telephony -------------------------------------------------------- #
    telephony_provider: TelephonyProvider = TelephonyProvider.EXOTEL
    exotel_sid: str | None = None
    exotel_api_key: str | None = None
    exotel_api_token: str | None = None
    exotel_subdomain: str = "api.exotel.com"
    inbound_did: str | None = None
    outbound_cli_promotional: str | None = None
    outbound_cli_transactional: str | None = None
    telephony_ws_token: str | None = None
    #: Signs and verifies provider webhooks (§17). Absent means webhooks are
    #: refused rather than trusted -- an unauthenticated webhook can mark a call
    #: answered or a consent granted.
    telephony_webhook_secret: str | None = None
    #: Where the provider reaches this worker: whisper audio, answer callbacks.
    #: Must be the externally-resolvable origin, not the container's.
    public_base_url: str = "http://localhost:8080"

    # Plivo, the §21 portability proof. No credentials exist for this project,
    # so the adapter is written and unexercised -- see docs/ARCHITECTURE.md.
    plivo_auth_id: str | None = None
    plivo_auth_token: str | None = None
    telephony_ip_allowlist: str = ""

    # --- speech ----------------------------------------------------------- #
    # Which engines serve which language is decided by `language_routes` in
    # config/defaults.yaml, not here. These are the credentials and the vendor
    # endpoints -- §2 keeps the two apart so that changing who serves Marathi
    # is a config edit and changing a key is a deploy secret.
    stt_primary: str = "soniox"
    stt_secondary: str = "sarvam_realtime"

    #: Soniox realtime recognition: one engine for every language the helpline
    #: serves, with vendor-fused semantic endpointing (§5.1, §5.2).
    soniox_api_key: str | None = None
    soniox_stt_model: str = "stt-rt-v5"
    soniox_ws_url: str = "wss://stt-rt.soniox.com/transcribe-websocket"

    # Deepgram Flux and Sarvam remain wired and tested. Flux is the fallback
    # for the Hindi/English path; Sarvam still serves the two languages Soniox
    # and Bakbak do not cover (§5.1). Neither is required to run.
    deepgram_api_key: str | None = None
    deepgram_stt_model: str = "flux-general-multi"
    sarvam_api_key: str | None = None
    sarvam_stt_model: str = "saaras:v3-realtime"

    tts_provider: str = "bakbak"
    #: Raya's Bakbak synthesis. REST base; the streaming path is
    #: ``POST {base}/text-to-speech/stream`` (§5.3).
    bakbak_api_key: str | None = None
    bakbak_base_url: str = "https://hub.getraya.app/v1"
    #: ``standard`` or ``m1``. A voice id belongs to exactly one model, so this
    #: and the voice ids below have to be changed together.
    bakbak_model: str = "standard"
    #: Voice ids are account-scoped UUIDs from ``GET /v1/voices``. §5.3 forbids
    #: picking one from a documentation example, so there is no default: an
    #: unset voice fails loudly naming the variable rather than speaking in
    #: whichever voice happened to be first.
    bakbak_voice_hi: str | None = None
    bakbak_voice_en: str | None = None
    #: Every other language, as ``mr-IN=<uuid>,ta-IN=<uuid>``. One variable
    #: rather than eleven, because most deployments set none of them.
    bakbak_voices: str = ""

    sarvam_tts_model: str = "bulbul:v3"
    sarvam_tts_speaker_hi: str | None = None
    tts_output_codec: AudioCodec = AudioCodec.LINEAR16
    tts_output_sample_rate: int = 8000

    # --- local models ------------------------------------------------------ #
    # Downloads, not credentials, so they carry defaults and an absent file is
    # a loud failure naming the fetch command rather than a missing secret.
    # Overridable because a container usually mounts them read-only from a
    # shared volume instead of baking a gigabyte into the image.
    smart_turn_model_path: Path = Path("models/smart-turn-v3.1.onnx")
    e5_model_dir: Path = Path("models/multilingual-e5-base")

    # --- llm -------------------------------------------------------------- #
    llm_gateway: str = "litellm"
    llm_gateway_url: str = "http://litellm:4000"
    #: Where the direct transport sends Messages API requests.
    #:
    #: Separate from ``llm_gateway_url`` on purpose: that one addresses the
    #: LiteLLM container and carries a bearer master key, this one addresses a
    #: Messages-API-shaped endpoint and carries ``x-api-key``. One variable
    #: meaning two protocols is how a deployment sends the right key to the
    #: wrong door. Either spelling of the base is accepted -- with or without a
    #: trailing ``/v1``.
    #:
    #: Named ``LLM_BASE_URL`` rather than ``ANTHROPIC_BASE_URL`` because the
    #: latter belongs to the wider ecosystem: the Anthropic SDKs and Claude
    #: Code read it, developer machines commonly export it, and a process
    #: environment variable outranks ``.env``. Borrowing that name means this
    #: service's endpoint is decided by whatever else the host happens to have
    #: configured -- silently, and only visibly as a 401 from a host you did
    #: not choose.
    llm_base_url: str = "https://api.anthropic.com"
    llm_gateway_master_key: str | None = None
    llm_primary_model: str = "claude-haiku-4-5-20251001"
    llm_fallback_model: str = "claude-sonnet-5"
    anthropic_api_key: str | None = None

    # --- embeddings ------------------------------------------------------- #
    embedding_model: str = "intfloat/multilingual-e5-base"
    embedding_dim: int = 768

    # --- whatsapp --------------------------------------------------------- #
    whatsapp_provider: str = "meta_cloud"
    wa_phone_number_id: str | None = None
    wa_business_account_id: str | None = None
    wa_access_token: str | None = None
    wa_webhook_verify_token: str | None = None
    wa_app_secret: str | None = None

    # --- data ------------------------------------------------------------- #
    database_url: str = "postgresql+asyncpg://uaagro_app:uaagro_dev_pw@localhost:5432/uaagro"
    database_url_migrator: str = "postgresql+asyncpg://uaagro:uaagro_dev_pw@localhost:5432/uaagro"
    database_pool_size: int = 10
    #: §4.1: the in-call engine's hard cap. A query that would blow the §7
    #: budget is killed by Postgres rather than allowed to stall a live call.
    database_incall_statement_timeout_ms: int = 120
    redis_url: str = "redis://localhost:6379/0"

    # --- storage ------------------------------------------------------------ #
    s3_endpoint: str = "http://localhost:9000"
    s3_bucket: str = "uaagro-recordings"
    s3_region: str = "ap-south-1"
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_force_path_style: bool = True

    # --- cryptography (§17) -------------------------------------------------- #
    #: Wraps the data key that encrypts stored phone numbers. Required in
    #: production; `verify_production_readiness` fails loudly naming it.
    kms_key_id: str | None = None
    #: HMAC pepper for the farmer phone lookup index. Effectively permanent:
    #: rotating it orphans every farmer row.
    phone_hash_pepper: str | None = None
    #: Development data key, base64, 32 bytes. In production the DEK is wrapped
    #: by KMS and this is ignored.
    local_dek_base64: str | None = None
    jwt_signing_key: str | None = None
    #: §17: the refresh cookie is `Secure` everywhere except local http.
    #: `verify_production_readiness` refuses to start a production process with
    #: this false, because a refresh token sent over plaintext is a session
    #: anyone on the path can take.
    session_cookie_secure: bool = False

    # --- budget guards (§8) -------------------------------------------------- #
    max_cost_per_call_inr: Decimal = Decimal("25")
    daily_spend_cap_inr: Decimal = Decimal("5000")
    #: §20's per-worker ceiling. Surfaced on §15.1's dashboard so headroom is
    #: visible rather than inferred.
    max_concurrent_calls: int = 100
    #: §13.1's dialer pacing.
    outbound_calls_per_minute: int = 20

    # --- observability ---------------------------------------------------- #
    otel_exporter_otlp_endpoint: str | None = None
    sentry_dsn: str | None = None

    @field_validator(*PLACEHOLDER_FIELDS, mode="before")
    @classmethod
    def _blank_placeholder_to_none(cls, value: Any) -> Any:
        """Treat the ``.env.example`` placeholder as absent.

        Otherwise a copied-but-unedited ``.env`` would ship ``FILL_ME`` to a
        vendor as a credential and fail with an opaque 401 instead of a clear
        MissingCredentialError.

        This covers more than the credentials. An unedited ``BAKBAK_VOICE_HI``
        reached the synthesiser as the literal voice id ``FILL_ME`` and came
        back as an unexplained HTTP 422 -- which reads as a broken adapter
        rather than as the one line of configuration nobody had filled in.
        """
        if isinstance(value, str) and value.strip() in {"", "FILL_ME", "changeme", "change-me"}:
            return None
        return value

    @property
    def is_production(self) -> bool:
        return self.app_env in ("staging", "production")

    @property
    def ip_allowlist(self) -> list[str]:
        return [c.strip() for c in self.telephony_ip_allowlist.split(",") if c.strip()]

    @staticmethod
    def secret_field_names() -> tuple[str, ...]:
        """Every credential field, for the write-only Settings screen (§15.1)."""
        return SECRET_FIELDS

    def voice_for_language(self, language: str) -> str | None:
        """The configured Bakbak voice id for a route language, if any.

        Resolution order: the explicit ``BAKBAK_VOICES`` map, then the two
        convenience variables for the languages this helpline actually runs on.
        Matching is on the BCP-47 route code (``hi-IN``) and then on its bare
        subtag (``hi``), so ``BAKBAK_VOICES=hi=<uuid>`` and ``hi-IN=<uuid>``
        both work -- an operator copying a code out of the vendor's voice list
        should not get silence for it.

        Returns ``None`` rather than a fallback voice. §5.3 chooses the voice in
        a bake-off; substituting an arbitrary one would mean a caller hearing a
        different person than the one that was tested.
        """
        wanted = language.strip()
        bare = wanted.split("-")[0].lower()

        for entry in self.bakbak_voices.split(","):
            code, _, voice = entry.partition("=")
            code, voice = code.strip(), voice.strip()
            if not code or not voice:
                continue
            if code.lower() in (wanted.lower(), bare):
                return voice

        if bare == "hi":
            return self.bakbak_voice_hi
        if bare == "en":
            return self.bakbak_voice_en
        return None

    def require(self, field: str, *, needed_for: str) -> str:
        """Return a credential or raise naming the variable (§0 rule 4)."""
        value = getattr(self, field, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise MissingCredentialError(field.upper(), needed_for=needed_for)
        return str(value)

    def verify_production_readiness(self) -> None:
        """Fail fast at boot if a production deployment is missing a control.

        These are the settings whose absence would silently weaken security
        rather than break a feature, so they are checked eagerly instead of
        waiting for the first request that needs them.
        """
        if not self.is_production:
            return
        required = {
            "kms_key_id": "envelope encryption of stored phone numbers",
            "phone_hash_pepper": "the farmer phone lookup index",
            "jwt_signing_key": "admin session signing",
            "telephony_ws_token": "authenticating the telephony media WebSocket",
        }
        for field, needed_for in required.items():
            self.require(field, needed_for=needed_for)
        if not self.session_cookie_secure:
            raise ConfigurationError(
                "SESSION_COOKIE_SECURE is false in a production environment.",
                remedy="Set SESSION_COOKIE_SECURE=true so refresh cookies are never sent "
                "over plaintext HTTP.",
            )


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def _find_defaults_path() -> Path:
    """Locate ``config/defaults.yaml`` from an env override or the repo root."""
    override = os.environ.get("UAAGRO_DEFAULTS_PATH")
    if override:
        path = Path(override)
        if not path.is_file():
            raise ConfigurationError(
                f"UAAGRO_DEFAULTS_PATH points at {path}, which does not exist.",
                remedy="Point UAAGRO_DEFAULTS_PATH at config/defaults.yaml, or unset it "
                "to use the copy in the repository root.",
                context={"path": str(path)},
            )
        return path

    for parent in [Path.cwd(), *Path(__file__).resolve().parents]:
        candidate = parent / "config" / "defaults.yaml"
        if candidate.is_file():
            return candidate

    raise ConfigurationError(
        "config/defaults.yaml was not found.",
        remedy="Run from the repository root, or set UAAGRO_DEFAULTS_PATH to the file.",
    )


@functools.lru_cache(maxsize=1)
def get_defaults() -> Defaults:
    path = _find_defaults_path()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Defaults.model_validate(raw)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_caches() -> None:
    """Drop cached configuration. Tests use this after mutating the environment."""
    get_defaults.cache_clear()
    get_settings.cache_clear()


__all__ = [
    "Defaults",
    "Environment",
    "LanguageRoute",
    "LatencyBudget",
    "Settings",
    "get_defaults",
    "get_settings",
    "reset_caches",
]

# Annotated re-export kept for routers that inject settings as a dependency.
SettingsDep = Annotated[Settings, "injected application settings"]
