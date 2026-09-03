"""Talking to Claude without the gateway in the middle (§6.1, §6.4, §7).

§6.1 puts LiteLLM in front of the model, and the reasons hold: one place for
keys, one place for spend, and §6.4's "swapping the model is a config change".
:class:`~voice_worker.adapters.llm.gateway.HttpTransport` still does that and is
still the default in staging and production.

But LiteLLM is a second process on the request path, and §7 gives time-to-first
token 550 ms at p95 out of a 1,200 ms turn. A local proxy hop costs a few
milliseconds; a proxy that is cold, restarting, or simply not running costs the
whole turn -- and on a single-host deployment it is one more thing that has to
be up before the helpline can answer a call.

So this is the same contract against Anthropic's Messages API directly, chosen
with ``LLM_GATEWAY=anthropic``. Everything above it -- the §6.1 failure ladder,
the two timeouts, the per-turn token budget -- is unchanged, because both are
:class:`LlmTransport` implementations and the ladder lives in the gateway.

Two differences from the OpenAI-compatible shape that matter:

**System blocks are a list, not messages.** Anthropic takes ``system`` as an
array of text blocks, and that is where ``cache_control`` goes. §6.1's prompt
caching keys on an exact prefix and the persona is roughly 900 of the ~2,000
input tokens per turn, so marking the right block is most of the input cost.

**Usage arrives in the stream.** ``message_start`` carries the input tokens,
including ``cache_read_input_tokens`` -- which is what §8 needs to bill a call
honestly rather than assuming every turn paid full price for the persona.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

from uaagro_domain import fastjson
from uaagro_domain.errors import MissingCredentialError, VendorUnavailableError
from uaagro_domain.settings import Settings

from ..http import build_client, is_shared, prewarm_connection
from .gateway import TOTAL_RESPONSE_S, LlmTransport

log = structlog.get_logger(__name__)

DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
MESSAGES_PATH = "/v1/messages"
#: Free, and enough to open the TLS connection the next POST will reuse.
MODELS_PATH = "/v1/models"


def normalise_base(url: str) -> str:
    """Accept an API root written either way.

    Half the ecosystem writes a base URL including ``/v1`` because that is what
    the OpenAI clients want; the other half writes the bare host because that is
    what Anthropic's own SDK wants. Both are reasonable readings of "base URL",
    and getting it wrong produces ``/v1/v1/messages`` and a 404 that looks like
    a dead endpoint rather than a slash.

    So the trailing ``/v1`` is stripped here and added back with the path.
    """
    trimmed = url.strip().rstrip("/")
    return trimmed.removesuffix("/v1") if trimmed.endswith("/v1") else trimmed


@dataclass
class AnthropicTransport(LlmTransport):
    """Streaming against the Messages API, no gateway in between.

    The endpoint is configurable because "the Messages API" is not only
    Anthropic's own host: a compatible proxy speaks the same protocol, and
    pinning the host here would mean the transport silently ignored the base
    URL a deployment had configured -- which is exactly what it did before this
    field existed.
    """

    settings: Settings
    base_url: str | None = None
    _client: Any = field(default=None, repr=False)

    def _ensure_client(self) -> Any:
        if self._client is None:
            key = self.settings.anthropic_api_key
            if not key:
                # §0 rule 4: name the variable rather than failing obscurely on
                # the first turn of the first call.
                raise MissingCredentialError(
                    variable="ANTHROPIC_API_KEY",
                    needed_for="generating replies with Claude (§6.1)",
                )
            self._client = build_client(
                base_url=normalise_base(self.base_url or self.settings.llm_base_url),
                headers={
                    "x-api-key": key,
                    "anthropic-version": API_VERSION,
                    "content-type": "application/json",
                },
                # Generous, because the *first token* has its own much tighter
                # deadline in the gateway above. This one only has to stop a
                # stalled response holding a socket open forever.
                total_timeout_s=TOTAL_RESPONSE_S * 3,
            )
        return self._client

    async def prewarm(self) -> bool:
        """Open the connection before the first caller does (§7.5).

        Uses the model listing, which costs nothing -- no tokens are generated.
        Measured saving on the configured endpoint: 268 ms of first-token time
        on every turn that would otherwise have found the pool empty.
        """
        return await prewarm_connection(self._ensure_client(), MODELS_PATH)

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

        system: list[dict[str, Any]] = []
        for index, block in enumerate(system_blocks):
            entry: dict[str, Any] = {"type": "text", "text": block}
            if index == 0 and block == cacheable_prefix:
                # Marked explicitly rather than left to be inferred: an
                # inferred cache boundary moves whenever the prompt does, and a
                # moved boundary is a silent cache miss on every turn (§6.1).
                entry["cache_control"] = {"type": "ephemeral"}
            system.append(entry)

        payload: dict[str, Any] = {
            "model": _model_name(model),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": user_message}],
            "stream": True,
        }
        if system:
            payload["system"] = system

        async with client.stream("POST", MESSAGES_PATH, json=payload) as response:
            if response.status_code >= 400:
                await response.aread()
                raise VendorUnavailableError(
                    vendor="anthropic", service="llm", detail=_error_detail(response)
                )
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    # `event:` lines duplicate the `type` inside the JSON.
                    continue
                body = line[len("data:") :].strip()
                if not body:
                    continue
                try:
                    event = fastjson.loads(body)
                except fastjson.JsonError:
                    # A malformed frame mid-stream is not worth ending a live
                    # call over; the next one usually parses.
                    log.warning("llm.malformed_chunk", vendor="anthropic")
                    continue
                if not isinstance(event, dict):
                    continue

                kind = event.get("type")
                if kind == "message_start":
                    _log_usage(event)
                elif kind == "content_block_delta":
                    delta = event.get("delta")
                    if isinstance(delta, dict):
                        piece = delta.get("text")
                        if isinstance(piece, str) and piece:
                            yield piece
                elif kind == "error":
                    raise VendorUnavailableError(
                        vendor="anthropic",
                        service="llm",
                        detail=str(event.get("error", ""))[:200],
                    )
                elif kind == "message_stop":
                    return

    async def aclose(self) -> None:
        """Release this transport, leaving the shared connection open.

        A gateway is built per call. Closing the pooled connection here would
        mean every call paid the handshake that the previous call had just
        finished paying for.
        """
        if self._client is not None and not is_shared(self._client):
            await self._client.aclose()
        self._client = None


def _model_name(model: str) -> str:
    """Strip a LiteLLM routing prefix.

    The same ``LLM_PRIMARY_MODEL`` value has to work through both transports,
    and LiteLLM addresses models as ``anthropic/claude-…`` while the vendor's
    own API wants the bare id. Rejecting the prefixed form would make swapping
    ``LLM_GATEWAY`` a two-variable change, which is the opposite of §6.4.
    """
    return model.split("/", 1)[1] if model.startswith("anthropic/") else model


def _log_usage(event: dict[str, Any]) -> None:
    """Record what the turn's input actually cost (§8, §6.1).

    ``cache_read_input_tokens`` is the number that says whether prompt caching
    is working. Without it a broken cache looks exactly like a working one --
    same replies, quietly several times the input bill.
    """
    message = event.get("message")
    if not isinstance(message, dict):
        return
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return
    log.debug(
        "llm.usage",
        vendor="anthropic",
        input_tokens=usage.get("input_tokens"),
        cache_read=usage.get("cache_read_input_tokens"),
        cache_write=usage.get("cache_creation_input_tokens"),
    )


def _error_detail(response: Any) -> str:
    """The vendor's message, never the request that carried the key (§23-6)."""
    # Any parse failure just means there is no detail to add -- an error path
    # is not the place to raise a second error.
    with contextlib.suppress(Exception):
        payload = response.json()
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and error.get("message"):
                return f"{response.status_code}: {str(error['message'])[:200]}"
    return f"HTTP {response.status_code}"


__all__ = ("API_VERSION", "DEFAULT_BASE_URL", "AnthropicTransport", "normalise_base")
