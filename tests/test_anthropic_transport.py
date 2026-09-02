"""Talking to Claude directly, one hop instead of two (§6.1, §6.4, §7).

§6.1 puts LiteLLM in front of the model and the reasons hold, but it is a
second process on the request path and §7 gives time-to-first-token 550 ms out
of a 1,200 ms turn. On a single-host deployment it is also one more thing that
has to be up before the helpline can answer a call. So ``LLM_GATEWAY`` chooses,
and both paths are the same :class:`LlmTransport` contract with §6.1's failure
ladder unchanged above them.

The shapes differ in one way that costs real money if it is got wrong.
Anthropic takes ``system`` as an array of blocks, and that array is where
``cache_control`` goes -- not on a message. The persona is roughly 900 of the
~2,000 input tokens on every turn of every call, so a cache marker on the wrong
object is a silent several-fold increase in the input bill with identical
replies. Nothing fails; §8's per-call cost just goes up.

Which is why these read the JSON that actually went over the wire.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from uaagro_domain.errors import ConfigurationError, VendorUnavailableError
from uaagro_domain.settings import Settings
from voice_worker.adapters.llm.anthropic_direct import AnthropicTransport, _model_name
from voice_worker.adapters.llm.gateway import HttpTransport, build_transport

PERSONA = "आप नवीन खुशहाली किसान सेवा केंद्र के सहायक हैं।"


def events(*texts: str, stop: bool = True) -> bytes:
    """A Messages API stream in the shape the vendor documents."""
    frames = [
        'event: message_start\ndata: {"type":"message_start","message":'
        '{"usage":{"input_tokens":2000,"cache_read_input_tokens":900}}}\n\n'
    ]
    frames += [
        "event: content_block_delta\ndata: "
        + json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": t}})
        + "\n\n"
        for t in texts
    ]
    if stop:
        frames.append('event: message_stop\ndata: {"type":"message_stop"}\n\n')
    return "".join(frames).encode("utf-8")


def build(handler: Any) -> tuple[AnthropicTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    settings = Settings(anthropic_api_key="test-not-a-real-key")
    transport = AnthropicTransport(settings=settings)
    transport._client = httpx.AsyncClient(
        base_url="https://api.anthropic.test",
        headers={"x-api-key": "test-not-a-real-key", "anthropic-version": "2023-06-01"},
        transport=httpx.MockTransport(record),
    )
    return transport, seen


async def drain(transport: AnthropicTransport, **overrides: Any) -> str:
    kwargs: dict[str, Any] = {
        "model": "claude-haiku-4-5-20251001",
        "system_blocks": [PERSONA, "caller: Ramesh, Barabanki"],
        "user_message": "डीएपी का रेट क्या है",
        "cacheable_prefix": PERSONA,
        "max_tokens": 122,
        "temperature": 0.3,
    }
    kwargs.update(overrides)
    return "".join([piece async for piece in transport.stream(**kwargs)])


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #


async def test_only_the_persona_carries_the_cache_marker() -> None:
    """§6.1's prompt caching keys on an exact prefix.

    Marked on the caller block instead, the prefix changes every call and the
    cache never hits; marked on nothing, it never hits either. Neither failure
    raises -- the replies are identical and the input bill is several times
    larger.
    """
    transport, seen = build(lambda _r: httpx.Response(200, content=events("जी")))
    try:
        await drain(transport)
    finally:
        await transport.aclose()

    system = json.loads(seen[0].content)["system"]
    assert system[0]["text"] == PERSONA
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in system[1]


async def test_the_per_turn_limits_go_over_the_wire() -> None:
    """The defect this whole area came from: both were configured, held in
    ``LlmSettings``, and never sent. §11.3 allows a dosage answer twice the
    length of an ordinary one, so the cap is per turn rather than per process.
    """
    transport, seen = build(lambda _r: httpx.Response(200, content=events("जी")))
    try:
        await drain(transport, max_tokens=210, temperature=0.3)
    finally:
        await transport.aclose()

    body = json.loads(seen[0].content)
    assert body["max_tokens"] == 210
    assert body["temperature"] == 0.3
    assert body["stream"] is True


async def test_the_system_prompt_is_not_sent_as_a_message() -> None:
    """A system block posted as a ``system``-role message is silently ignored
    by this API, and the agent answers with no persona at all -- in English,
    off-script, and passing §16.3's grounding check every time."""
    transport, seen = build(lambda _r: httpx.Response(200, content=events("जी")))
    try:
        await drain(transport)
    finally:
        await transport.aclose()

    body = json.loads(seen[0].content)
    assert [m["role"] for m in body["messages"]] == ["user"]
    assert body["system"][0]["type"] == "text"


def test_a_gateway_prefixed_model_name_still_works() -> None:
    """§6.4 says swapping the model is a config change. LiteLLM addresses
    models as ``anthropic/claude-…`` and the vendor's own API wants the bare
    id, so switching ``LLM_GATEWAY`` must not also require editing the model
    name."""
    assert _model_name("anthropic/claude-haiku-4-5-20251001") == "claude-haiku-4-5-20251001"
    assert _model_name("claude-haiku-4-5-20251001") == "claude-haiku-4-5-20251001"


# --------------------------------------------------------------------------- #
# The response
# --------------------------------------------------------------------------- #


async def test_text_deltas_stream_out_as_they_arrive() -> None:
    """§7: the first token starts TTS. Collecting the whole reply first would
    spend the synthesis budget waiting for words already written."""
    body = events("जी, ", "डीएपी ", "उपलब्ध है।")
    transport, _ = build(lambda _r: httpx.Response(200, content=body))
    try:
        assert await drain(transport) == "जी, डीएपी उपलब्ध है।"
    finally:
        await transport.aclose()


async def test_a_malformed_frame_does_not_end_the_call() -> None:
    """One bad frame mid-stream is not worth dropping a live call over."""
    body = (
        b"event: content_block_delta\ndata: {not json\n\n"
        + events("ठीक है।")
    )
    transport, _ = build(lambda _r: httpx.Response(200, content=body))
    try:
        assert await drain(transport) == "ठीक है।"
    finally:
        await transport.aclose()


async def test_a_rejected_request_reports_the_vendors_reason_not_the_key() -> None:
    """§23-6: an error path is the easiest place for a credential to reach a
    log line, and this one runs on the authenticated client's own response."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": {"type": "authentication_error", "message": "invalid x-api-key"}}
        )

    transport, _ = build(handler)
    try:
        with pytest.raises(VendorUnavailableError) as caught:
            await drain(transport)
    finally:
        await transport.aclose()

    detail = str(caught.value)
    assert "invalid x-api-key" in detail
    assert "test-not-a-real-key" not in detail


# --------------------------------------------------------------------------- #
# Choosing the path
# --------------------------------------------------------------------------- #


def test_the_gateway_setting_selects_the_transport() -> None:
    assert isinstance(build_transport(Settings(llm_gateway="litellm")), HttpTransport)
    assert isinstance(build_transport(Settings(llm_gateway="anthropic")), AnthropicTransport)


def test_an_unknown_gateway_fails_rather_than_defaulting() -> None:
    """A silent default here is a deployment quietly not using the gateway it
    is being billed and audited through (§6.1, §8)."""
    with pytest.raises(ConfigurationError) as caught:
        build_transport(Settings(llm_gateway="openrouter"))
    assert "openrouter" in caught.value.message
    assert "litellm" in caught.value.remedy


# --------------------------------------------------------------------------- #
# Where the requests actually go
# --------------------------------------------------------------------------- #


def test_both_spellings_of_a_base_url_reach_the_same_endpoint() -> None:
    """Half the ecosystem writes the base with `/v1`, half without.

    Both are fair readings, and picking one produces `/v1/v1/messages` for
    everyone who read it the other way -- a 404 that looks like a dead endpoint
    rather than a stray path segment.
    """
    from voice_worker.adapters.llm.anthropic_direct import normalise_base

    for written in (
        "https://api.example.test",
        "https://api.example.test/",
        "https://api.example.test/v1",
        "https://api.example.test/v1/",
    ):
        assert normalise_base(written) == "https://api.example.test"


async def test_the_configured_endpoint_is_the_one_used() -> None:
    """The defect: the transport pinned Anthropic's own host and ignored the
    configured base entirely, so a deployment pointed at a compatible proxy
    silently sent its key to the wrong door and got a 401."""
    seen: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=events("ok"))

    settings = Settings(
        anthropic_api_key="test-not-a-real-key",
        llm_base_url="https://proxy.example.test/v1",
    )
    transport = AnthropicTransport(settings=settings)
    transport._client = httpx.AsyncClient(
        base_url="https://proxy.example.test",
        transport=httpx.MockTransport(record),
    )
    try:
        await drain(transport)
    finally:
        await transport.aclose()

    assert seen == ["https://proxy.example.test/v1/messages"]


def test_the_endpoint_setting_does_not_answer_to_the_ecosystems_name(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """`ANTHROPIC_BASE_URL` belongs to the Anthropic SDKs and Claude Code.

    Developer machines commonly export it, and a process environment variable
    outranks `.env` -- so naming ours the same would let whatever else the host
    has configured decide where this helpline's traffic goes. It did exactly
    that once: the value in `.env` was ignored and the only symptom was a 401
    from a host nobody had chosen.
    """
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://someone-elses-host.test")
    monkeypatch.setenv("LLM_BASE_URL", "https://ours.test/v1")

    assert Settings().llm_base_url == "https://ours.test/v1"


def test_an_unedited_voice_id_reads_as_absent_not_as_a_voice() -> None:
    """`BAKBAK_VOICE_HI=FILL_ME` reached the synthesiser as a literal voice id
    and came back as an unexplained HTTP 422 -- which reads as a broken adapter
    rather than as the one line nobody had filled in (§0 rule 4)."""
    assert Settings(bakbak_voice_hi="FILL_ME").bakbak_voice_hi is None
    assert Settings(bakbak_voice_en="  ").bakbak_voice_en is None
