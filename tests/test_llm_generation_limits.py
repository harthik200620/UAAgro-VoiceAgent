"""What the gateway actually asks the model for (§6.1, §6.4, §11.3).

`config/defaults.yaml` set `max_output_tokens: 220` and `temperature: 0.3`,
`LlmSettings` held both, and neither was ever put in the request. The model ran
unbounded at the provider's default temperature.

That is not a tidiness problem. An uncapped model drifts past §11.3's word
limit, the §16.3 validator rejects the answer, and the retry is a second full
generation inside the caller's turn -- so a missing parameter shows up as a
farmer waiting twice as long for a reply that is twice as long as it should be.

The test that would have caught it is the last one here: it reads the JSON body
actually sent over HTTP, rather than trusting that a value passed into a
function reached the wire.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence

import httpx

from uaagro_domain.settings import Settings, get_defaults
from voice_worker.adapters.llm.gateway import HttpTransport, LlmGateway, build_gateway
from voice_worker.flow.agent import TOKENS_PER_WORD_CEILING, _token_budget
from voice_worker.flow.validator import MAX_WORDS, MAX_WORDS_DOSAGE


class RecordingTransport:
    """Captures what the gateway asked for."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

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
        self.calls.append(
            {
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        yield "जी, ठीक है।"


async def _drain(gateway: LlmGateway, **kwargs: object) -> str:
    pieces = [
        piece
        async for piece, _rung in gateway.stream(
            system_blocks=["persona"],
            user_message="डीएपी का रेट",
            cacheable_prefix="persona",
            **kwargs,  # type: ignore[arg-type]
        )
    ]
    return "".join(pieces)


async def test_the_configured_limits_reach_the_model() -> None:
    """The defect: both were configured and neither was sent."""
    transport = RecordingTransport()
    gateway = LlmGateway(
        transport=transport,  # type: ignore[arg-type]
        primary_model="m",
        fallback_model="f",
        max_tokens=180,
        temperature=0.25,
    )

    await _drain(gateway)

    assert transport.calls == [
        {"model": "m", "max_tokens": 180, "temperature": 0.25}
    ]


async def test_a_caller_can_ask_for_a_smaller_budget() -> None:
    """§11.3 allows a dosage answer twice the length of an ordinary one, so the
    cap is per turn rather than per process."""
    transport = RecordingTransport()
    gateway = LlmGateway(
        transport=transport,  # type: ignore[arg-type]
        primary_model="m",
        fallback_model="f",
        max_tokens=220,
    )

    await _drain(gateway, max_tokens=120)

    assert transport.calls[0]["max_tokens"] == 120


def test_the_gateway_takes_its_defaults_from_config() -> None:
    """§2 and §6.4: model names are environment, generation limits are product
    decisions checked into `defaults.yaml`."""
    llm = get_defaults().llm
    gateway = build_gateway(Settings(), transport=RecordingTransport())  # type: ignore[arg-type]

    assert gateway.max_tokens == llm.max_output_tokens
    assert gateway.temperature == llm.temperature
    # The timeouts too. They matched the module constants by hand, so editing
    # `defaults.yaml` changed nothing -- a config value that cannot change
    # behaviour is documentation pretending to be configuration.
    assert gateway.ttft_budget_s == llm.first_token_timeout_ms / 1000
    assert gateway.total_budget_s == llm.total_timeout_ms / 1000


def test_an_ordinary_answer_gets_a_tighter_budget_than_a_dosage_one() -> None:
    """A model handed room for sixty words tends to use it, and every word past
    §11.3's cap is one the farmer waits for and the validator then rejects."""
    ordinary = _token_budget(is_dosage=False)
    dosage = _token_budget(is_dosage=True)

    assert ordinary < dosage
    # Generous against the estimate on purpose: a cap set too low truncates a
    # dosage answer mid-sentence and loses the pre-harvest interval and the
    # precaution §16.2 requires be spoken.
    assert ordinary >= MAX_WORDS * 3
    assert dosage >= MAX_WORDS_DOSAGE * 3
    assert TOKENS_PER_WORD_CEILING >= 3


async def test_the_request_body_carries_both_limits() -> None:
    """Read off the wire, not off a function argument.

    Every assertion above would still have passed while `_stream_once` built a
    payload without these keys -- which is exactly what it did. This is the one
    that checks the JSON the model actually receives.
    """
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        body = (
            'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=body)

    transport = HttpTransport(settings=Settings())
    transport._client = httpx.AsyncClient(
        base_url="http://llm.test", transport=httpx.MockTransport(handler)
    )
    try:
        pieces = [
            piece
            async for piece in transport.stream(
                model="claude-haiku-4-5-20251001",
                system_blocks=["persona"],
                user_message="रेट क्या है",
                cacheable_prefix="persona",
                max_tokens=122,
                temperature=0.3,
            )
        ]
    finally:
        await transport._client.aclose()

    assert pieces == ["ok"]
    assert captured["max_tokens"] == 122
    assert captured["temperature"] == 0.3
    assert captured["stream"] is True


async def test_the_persona_is_still_marked_cacheable() -> None:
    """§6.1's prompt caching keys on an exact prefix, and the persona is ~900
    of the ~2000 input tokens. Adding parameters to the payload must not
    disturb the block that carries `cache_control`."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, text="data: [DONE]\n\n")

    transport = HttpTransport(settings=Settings())
    transport._client = httpx.AsyncClient(
        base_url="http://llm.test", transport=httpx.MockTransport(handler)
    )
    try:
        async for _ in transport.stream(
            model="m",
            system_blocks=["persona", "caller"],
            user_message="q",
            cacheable_prefix="persona",
            max_tokens=100,
            temperature=0.3,
        ):
            pass
    finally:
        await transport._client.aclose()

    messages = captured["messages"]
    assert isinstance(messages, list)
    assert messages[0].get("cache_control") == {"type": "ephemeral"}
    assert "cache_control" not in messages[1]
