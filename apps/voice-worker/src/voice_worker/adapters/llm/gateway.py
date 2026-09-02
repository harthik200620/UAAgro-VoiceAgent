"""The LiteLLM gateway client (§6.1, §6.4).

All LLM traffic goes through one self-hosted LiteLLM process in the same VPC.
The worker knows only an OpenAI-compatible endpoint, which is what makes §6.4's
"make the swap a config change" true rather than aspirational.

§6.1's hard requirements, and what each costs if it is missing:

**Streaming always.** The first token starts TTS. §7 gives the whole turn
1,200 ms and time-to-first-token 550 ms of it; waiting for a complete response
before synthesising would spend the TTS budget waiting for words already
written.

**800 ms to first token, 4 s total.** Two separate timeouts, because they fail
differently. A slow first token means nothing is happening and the caller is in
silence; a slow *total* means the model is producing an essay, which the §11.3
length cap would reject anyway.

**The failure ladder.** Breach → secondary model. Second breach → cached hold
phrase and retry once. Third → escalate. Each rung buys one more chance while
keeping the caller informed, and the last one hands them to a person rather than
trying a fourth time.

The gateway is also where §6.1 puts prompt caching, so the client marks the
persona block as the cacheable prefix and never varies it within a call.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog

from uaagro_domain import fastjson
from uaagro_domain.errors import (
    ConfigurationError,
    MissingCredentialError,
    VendorTimeoutError,
)
from uaagro_domain.settings import Settings

from ..resilience import CircuitBreaker, CircuitOpen, breaker_for

log = structlog.get_logger(__name__)

#: §6.1. Two timeouts because they represent different failures.
TIME_TO_FIRST_TOKEN_S = 0.8
TOTAL_RESPONSE_S = 4.0
#: Connect separately and tightly. A connect that takes a second has already
#: spent more than §7 allows the whole first token, and retrying a dead host
#: fast beats waiting on it.
CONNECT_TIMEOUT_S = 1.0

#: §6.1's ladder. One retry on the fallback model, then a human.
MAX_ATTEMPTS = 3


class Rung(StrEnum):
    """Where on §6.1's failure ladder a turn ended up."""

    PRIMARY = "primary"
    FALLBACK = "fallback"
    HOLD_AND_RETRY = "hold_and_retry"
    ESCALATE = "escalate"


@dataclass(frozen=True, slots=True)
class Completion:
    """One finished generation, with the numbers §19 tracks."""

    text: str
    model: str
    rung: Rung
    ttft_ms: float
    total_ms: float
    input_tokens: int = 0
    output_tokens: int = 0
    #: How many of the input tokens the gateway served from cache (§6.1).
    cached_tokens: int = 0

    @property
    def within_ttft_budget(self) -> bool:
        return self.ttft_ms <= TIME_TO_FIRST_TOKEN_S * 1000


class LlmTransport(ABC):
    """What the gateway client needs from the network.

    A seam, so the ladder above can be tested without a running LiteLLM. The
    real implementation is :class:`HttpTransport`; the tests supply one that
    fails on demand, which is the only way to exercise a fallback path that by
    definition does not happen when everything works.

    Declared as a plain method returning an async iterator rather than as
    ``async def``: an async generator is not a coroutine, and typing it as one
    makes every implementation look wrong to the checker.
    """

    @abstractmethod
    def stream(
        self,
        *,
        model: str,
        system_blocks: Sequence[str],
        user_message: str,
        cacheable_prefix: str,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        """Yield the completion in pieces, as the gateway produces them.

        ``max_tokens`` and ``temperature`` are parameters rather than transport
        state because the cap is per turn: §11.3 allows a dosage answer twice
        the length of an ordinary one, and sending the larger budget on every
        turn invites the model to use it.
        """


@dataclass
class HttpTransport(LlmTransport):
    """OpenAI-compatible streaming against the LiteLLM endpoint."""

    settings: Settings
    _client: Any = field(default=None, repr=False)

    def _ensure_client(self) -> Any:
        if self._client is None:

            key = self.settings.llm_gateway_master_key
            if not key:
                # §0 rule 4: name the variable rather than failing obscurely at
                # the first turn of the first call.
                raise MissingCredentialError(
                    variable="LLM_GATEWAY_MASTER_KEY",
                    needed_for="the LiteLLM gateway (§6.1)",
                )
            from ..http import build_client

            self._client = build_client(
                base_url=self.settings.llm_gateway_url,
                headers={"Authorization": f"Bearer {key}"},
                total_timeout_s=TOTAL_RESPONSE_S * 3,
                connect_timeout_s=CONNECT_TIMEOUT_S,
            )
        return self._client

    async def stream(
        self,
        *,
        model: str,
        system_blocks: Sequence[str],
        user_message: str,
        cacheable_prefix: str,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:

        client = self._ensure_client()
        messages: list[dict[str, Any]] = []
        for index, block in enumerate(system_blocks):
            entry: dict[str, Any] = {"role": "system", "content": block}
            if index == 0 and block == cacheable_prefix:
                # §6.1: the static prefix is cached. Marked explicitly rather
                # than left to the gateway to infer, because an inferred cache
                # boundary moves when the prompt does.
                entry["cache_control"] = {"type": "ephemeral"}
            messages.append(entry)
        messages.append({"role": "user", "content": user_message})

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            # Both of these were configured in `defaults.yaml`, held in
            # `LlmSettings`, and never sent. The model ran unbounded at the
            # provider's default temperature, which is why answers drifted past
            # §11.3's word cap often enough for the validator's retry -- and a
            # retry is a whole second generation inside the caller's turn.
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        async with client.stream("POST", "/v1/chat/completions", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                body = line.removeprefix("data: ").strip()
                if body == "[DONE]":
                    return
                try:
                    chunk = fastjson.loads(body)
                except fastjson.JsonError:
                    # A malformed chunk mid-stream is not worth ending a live
                    # call over; the next one usually parses.
                    log.warning("llm.malformed_chunk")
                    continue
                for choice in chunk.get("choices", []):
                    piece = choice.get("delta", {}).get("content")
                    if piece:
                        yield piece

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


@dataclass
class LlmGateway:
    """§6.1's timeouts, fallback and streaming."""

    transport: LlmTransport
    primary_model: str
    fallback_model: str
    ttft_budget_s: float = TIME_TO_FIRST_TOKEN_S
    total_budget_s: float = TOTAL_RESPONSE_S
    #: Defaults when a caller does not say. §11.3's ordinary answer is ~35
    #: words; the dosage case asks for its own, larger cap explicitly.
    max_tokens: int = 220
    temperature: float = 0.3

    @property
    def _breaker(self) -> CircuitBreaker:
        """Shared across calls: a breaker that forgets at the end of a call has
        learned nothing, and the point is that call #2 skips what call #1 spent
        seven seconds discovering."""
        return breaker_for("llm", "chat completion")

    async def stream(
        self,
        *,
        system_blocks: Sequence[str],
        user_message: str,
        cacheable_prefix: str = "",
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[tuple[str, Rung]]:
        """Stream a completion, walking §6.1's ladder on failure.

        Yields ``(text, rung)`` so the caller can start TTS on the first piece
        and still know which model produced it -- §19 tracks the fallback rate,
        and a system quietly running on its secondary model all week is one
        nobody has noticed is degraded.

        Raises:
            VendorTimeoutError: When every rung has been exhausted. §6.1's last
                step is escalation to a human, which is the caller's decision to
                sequence, not this client's.
        """
        for attempt, model in enumerate((self.primary_model, self.fallback_model)):
            rung = Rung.PRIMARY if attempt == 0 else Rung.FALLBACK
            try:
                async for piece in self._stream_once(
                    model=model,
                    system_blocks=system_blocks,
                    user_message=user_message,
                    cacheable_prefix=cacheable_prefix,
                    max_tokens=max_tokens if max_tokens is not None else self.max_tokens,
                    temperature=(
                        temperature if temperature is not None else self.temperature
                    ),
                ):
                    yield piece, rung
                return
            except (TimeoutError, Exception) as exc:
                # Deliberately broad. Every vendor client raises its own
                # exception hierarchy, and a gateway that only caught the ones
                # it knew about would let an unfamiliar httpx error end a live
                # call instead of falling back. The failure is logged with its
                # type so an unexpected one is still visible.
                if isinstance(exc, MissingCredentialError):
                    raise
                log.warning(
                    "llm.attempt_failed",
                    model=model,
                    rung=rung.value,
                    error=type(exc).__name__,
                )
                if isinstance(exc, CircuitOpen):
                    # Both rungs go through the same endpoint, so a second
                    # attempt would be refused by the same breaker after
                    # spending another turn's worth of nothing. §11.4's cached
                    # phrase and escalation are reached now instead.
                    break
                continue

        raise VendorTimeoutError(
            vendor="llm",
            service="chat completion",
            timeout_ms=int(self.total_budget_s * 1000),
        )

    async def _stream_once(
        self,
        *,
        model: str,
        system_blocks: Sequence[str],
        user_message: str,
        cacheable_prefix: str,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        """One attempt, with both timeouts enforced separately."""
        started = time.perf_counter()
        stream = self.transport.stream(
            model=model,
            system_blocks=system_blocks,
            user_message=user_message,
            cacheable_prefix=cacheable_prefix,
            max_tokens=max_tokens,
            temperature=temperature,
        ).__aiter__()

        # First token: its own deadline. Silence on the line is the failure
        # here, and it is the one the caller actually experiences.
        # The breaker guards the *first token* only. Once tokens are flowing
        # the vendor has demonstrably answered, and counting a mid-stream drop
        # as an outage would open the circuit on a caller who hung up.
        first = await self._breaker.call(
            lambda: asyncio.wait_for(stream.__anext__(), timeout=self.ttft_budget_s)
        )
        ttft_ms = (time.perf_counter() - started) * 1000
        log.info("llm.ttft", model=model, ttft_ms=round(ttft_ms, 1))
        yield first

        # The rest shares one deadline for the whole response. Applying the
        # per-token timeout to every chunk would let a model that emits a token
        # every 700 ms run indefinitely.
        while True:
            elapsed = time.perf_counter() - started
            remaining = self.total_budget_s - elapsed
            if remaining <= 0:
                raise TimeoutError(f"{model} exceeded {self.total_budget_s}s total")
            try:
                yield await asyncio.wait_for(stream.__anext__(), timeout=remaining)
            except StopAsyncIteration:
                return


def build_gateway(settings: Settings, transport: LlmTransport | None = None) -> LlmGateway:
    """Wire the gateway from settings (§6.4: the swap is a config change).

    The model names come from the environment and the generation limits from
    `config/defaults.yaml`, which is the split §2 draws: what differs per
    deployment is a variable, what is a product decision is checked in.
    """
    from uaagro_domain.settings import get_defaults

    llm = get_defaults().llm
    return LlmGateway(
        transport=transport or build_transport(settings),
        primary_model=settings.llm_primary_model,
        fallback_model=settings.llm_fallback_model,
        max_tokens=llm.max_output_tokens,
        temperature=llm.temperature,
        # From config too. These matched the module constants by hand, which
        # meant editing `defaults.yaml` changed nothing -- §6.4 says swapping
        # the model is a config change, and a timeout that only lives in Python
        # is not part of that.
        ttft_budget_s=llm.first_token_timeout_ms / 1000,
        total_budget_s=llm.total_timeout_ms / 1000,
    )


def build_transport(settings: Settings) -> LlmTransport:
    """Pick the path to the model (§6.4).

    ``litellm`` keeps §6.1's gateway in front: one place for keys, one place for
    spend, and one place to swap a model. ``anthropic`` removes the proxy hop --
    worth having on a single-host deployment, where LiteLLM is one more process
    that has to be up before the helpline can answer, and where §7's 550 ms
    time-to-first-token budget does not have a spare round trip in it.

    Unknown values fail here rather than defaulting, because the failure mode
    of a silent default is a deployment quietly not using the gateway it is
    being billed and audited through.
    """
    match settings.llm_gateway:
        case "litellm":
            return HttpTransport(settings=settings)
        case "anthropic":
            from .anthropic_direct import AnthropicTransport

            return AnthropicTransport(settings=settings)
        case unknown:
            raise ConfigurationError(
                f"LLM_GATEWAY={unknown!r} is not a transport this worker implements.",
                remedy="Use 'litellm' (§6.1's gateway) or 'anthropic' (direct, one "
                "fewer hop). Adding a third means an LlmTransport under "
                "voice_worker/adapters/llm/.",
                context={"llm_gateway": unknown},
            )


__all__ = (
    "CONNECT_TIMEOUT_S",
    "MAX_ATTEMPTS",
    "TIME_TO_FIRST_TOKEN_S",
    "TOTAL_RESPONSE_S",
    "Completion",
    "HttpTransport",
    "LlmGateway",
    "LlmTransport",
    "Rung",
    "build_gateway",
    "build_transport",
)
