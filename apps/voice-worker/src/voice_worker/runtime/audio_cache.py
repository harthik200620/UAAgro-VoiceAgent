"""Pre-synthesised audio cache (§5.3, §8).

Two wins from one mechanism, and §5.3 calls the first one free:

**Latency.** The greeting is the first thing a farmer hears, and §11.1 wants it
out within about 50 ms of the start frame. Synthesising it on demand costs a
round trip to Sarvam plus time-to-first-byte -- roughly 250 ms that the caller
experiences as the phone being answered slowly. Cached, it is a dictionary
lookup and a frame loop.

**Cost.** §8 makes TTS the largest controllable line item, larger than STT and
far larger than the LLM. Caching the greeting, holds, closings, the transfer
notice, the disclosure and the top ~40 sentences removes 15-25% of billed
characters on a typical call.

The key includes every parameter that changes the audio -- provider, model,
speaker, language, codec, sample rate, pace -- because serving Hindi audio for
a Marathi call, or µ-law bytes to a linear16 provider, is worse than a cache
miss. Changing the bake-off speaker invalidates the cache by construction
rather than by anyone remembering to flush it.

A cache miss is always safe: the caller synthesises. A Redis outage therefore
degrades latency and cost, never correctness, and is logged rather than raised.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

import structlog

from ..adapters.tts.base import TtsConfig, TTSService
from . import audio as audio_utils

log = structlog.get_logger(__name__)

#: Cached audio is small (a few seconds of 8 kHz PCM) and never changes for a
#: given key, so it is worth keeping for a long time.
DEFAULT_TTL_SECONDS = 7 * 24 * 3600

KEY_PREFIX = "tts:audio:"

#: §5.3 names these for pre-synthesis at boot.
PRELOAD_KINDS: tuple[str, ...] = (
    "greeting",
    "hold",
    "closing",
    "transfer_notice",
    "disclosure",
    "safety_emergency",
    "reprompt",
    "spam_rejection",
)


def cache_key(text: str, config: TtsConfig, *, provider: str) -> str:
    """A key that changes whenever the audio would.

    Hashing the parameters rather than only the text is what stops a
    speaker change or a codec change from serving stale, wrong-sounding audio.
    """
    material = "|".join(
        [
            provider,
            config.model,
            config.speaker or "",
            config.language,
            config.codec.value,
            str(config.sample_rate),
            f"{config.pace:.2f}",
            text,
        ]
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"{KEY_PREFIX}{digest}"


@dataclass
class CacheStats:
    """Hit rate, for the §8 cost dashboard."""

    hits: int = 0
    misses: int = 0
    stores: int = 0
    errors: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def to_dict(self) -> dict[str, float | int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "stores": self.stores,
            "errors": self.errors,
            "hit_rate": round(self.hit_rate, 3),
        }


class AudioStore:
    """Where cached bytes live. Implemented by Redis and by memory."""

    async def get(self, key: str) -> bytes | None:  # pragma: no cover - interface
        raise NotImplementedError

    async def set(self, key: str, value: bytes, ttl_seconds: int) -> None:  # pragma: no cover
        raise NotImplementedError


@dataclass
class MemoryAudioStore(AudioStore):
    """In-process store.

    The real store for a single-VM deployment (§20 documents that path), and
    the store the tests use. TTL is ignored: the process lifetime is shorter
    than any TTL worth setting.
    """

    _items: dict[str, bytes] = field(default_factory=dict)

    async def get(self, key: str) -> bytes | None:
        return self._items.get(key)

    async def set(self, key: str, value: bytes, ttl_seconds: int) -> None:
        self._items[key] = value

    def __len__(self) -> int:
        return len(self._items)


class RedisAudioStore(AudioStore):
    """Redis-backed store, shared across workers.

    Worth sharing: on a multi-worker deployment the first call warms the
    greeting for every other worker, and a deploy does not re-pay synthesis
    cost for phrases that have not changed.
    """

    def __init__(self, client: object) -> None:
        self._client = client

    async def get(self, key: str) -> bytes | None:
        value = await self._client.get(key)  # type: ignore[attr-defined]
        if value is None:
            return None
        return value if isinstance(value, bytes) else bytes(value)

    async def set(self, key: str, value: bytes, ttl_seconds: int) -> None:
        await self._client.set(key, value, ex=ttl_seconds)  # type: ignore[attr-defined]


@dataclass
class AudioCache:
    """Pre-synthesised audio, keyed by everything that affects the sound."""

    store: AudioStore = field(default_factory=MemoryAudioStore)
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    stats: CacheStats = field(default_factory=CacheStats)
    #: Small in-process layer in front of the store, so the greeting does not
    #: cost a Redis round trip on the path §11.1 wants under 50 ms.
    _local: dict[str, bytes] = field(default_factory=dict, repr=False)

    async def get(self, text: str, config: TtsConfig, *, provider: str) -> bytes | None:
        key = cache_key(text, config, provider=provider)

        local = self._local.get(key)
        if local is not None:
            self.stats.hits += 1
            return local

        try:
            value = await self.store.get(key)
        except Exception as exc:
            # A cache outage must not break a call: the caller synthesises.
            self.stats.errors += 1
            log.warning("audio_cache.unavailable", error=type(exc).__name__)
            return None

        if value is None:
            self.stats.misses += 1
            return None

        self._local[key] = value
        self.stats.hits += 1
        return value

    async def put(self, text: str, config: TtsConfig, audio: bytes, *, provider: str) -> None:
        if not audio:
            return
        key = cache_key(text, config, provider=provider)
        self._local[key] = audio
        try:
            await self.store.set(key, audio, self.ttl_seconds)
            self.stats.stores += 1
        except Exception as exc:
            self.stats.errors += 1
            log.warning("audio_cache.store_failed", error=type(exc).__name__)

    async def warm(
        self,
        phrases: Mapping[str, str],
        tts: TTSService,
        config: TtsConfig,
    ) -> dict[str, int]:
        """Pre-synthesise the §5.3 phrase set at worker start.

        Returns audio duration per phrase, in milliseconds, so a boot log can
        show what was warmed rather than merely that warming ran.

        A phrase that fails to synthesise is skipped with a warning: a worker
        that refuses to start because one hold phrase failed is worse than one
        that starts and synthesises that phrase on demand.
        """
        warmed: dict[str, int] = {}
        for name, text in phrases.items():
            if not text.strip():
                continue
            existing = await self.get(text, config, provider=tts.provider)
            if existing is not None:
                warmed[name] = audio_utils.duration_ms(existing)
                continue
            try:
                audio = await tts.synthesise_all(text, config)
            except Exception as exc:
                log.warning("audio_cache.warm_failed", phrase=name, error=type(exc).__name__)
                continue
            if audio:
                await self.put(text, config, audio, provider=tts.provider)
                warmed[name] = audio_utils.duration_ms(audio)

        log.info(
            "audio_cache.warmed",
            phrases=len(warmed),
            total_ms=sum(warmed.values()),
            language=config.language,
        )
        return warmed

    def invalidate_local(self) -> None:
        """Drop the in-process layer. Used when a config version is published."""
        self._local.clear()


def characters_saved(cached_texts: Iterable[str]) -> int:
    """Billed characters avoided by serving from cache.

    §8 attributes this to the call so the saving is visible in the cost
    dashboard rather than asserted in a design document.
    """
    return sum(len(text) for text in cached_texts)
