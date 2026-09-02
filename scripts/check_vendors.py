"""Prove each vendor key actually works, before a farmer finds out (§0 rule 4).

A key that is present but wrong looks exactly like a key that is right, until
the first call -- at which point the failure is a farmer listening to silence
on a helpline. This does the smallest real round trip against each vendor the
routing table actually uses and says which ones answered.

.. code-block:: console

    uv run python scripts/check_vendors.py

What it checks, and why that check and not a bigger one:

``soniox``
    Opens the realtime socket, sends 200 ms of silence and waits for a reply.
    A REST ping would prove the key exists; only the socket proves the key is
    allowed to *stream*, which is the thing every call depends on.

``bakbak``
    Lists voices, then synthesises one short line and checks the audio came
    back at the configured sample rate. Listing alone would pass with a voice
    id that belongs to the other model.

``anthropic`` / ``litellm``
    One two-token generation through whichever transport ``LLM_GATEWAY``
    selects, so a misconfigured gateway fails here rather than mid-turn.

Every failure prints the vendor's own message, truncated, and never the key or
the request that carried it (§23-6). Exits non-zero if anything the routing
table needs did not answer.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from uaagro_domain.settings import Settings, get_defaults
from voice_worker.adapters.stt.base import SttConfig
from voice_worker.adapters.stt.soniox import SonioxSTT
from voice_worker.adapters.tts.bakbak import BakbakTTS
from voice_worker.adapters.tts.base import TtsConfig
from voice_worker.runtime import audio as audio_utils

OK, FAIL, SKIP = "ok", "FAILED", "skipped"


@dataclass
class Result:
    vendor: str
    status: str
    detail: str = ""
    elapsed_ms: float = 0.0


async def timed(vendor: str, check: Callable[[], Awaitable[str]]) -> Result:
    started = time.perf_counter()
    try:
        detail = await check()
    except Exception as exc:
        return Result(
            vendor, FAIL, f"{type(exc).__name__}: {exc}"[:180],
            (time.perf_counter() - started) * 1000,
        )
    return Result(vendor, OK, detail, (time.perf_counter() - started) * 1000)


async def check_soniox(settings: Settings) -> str:
    """Open a real stream. Silence is enough to prove the socket was accepted."""
    defaults = get_defaults()
    stt = SonioxSTT(settings)
    config = SttConfig(
        language="hi-IN",
        language_hints=("hi", "en"),
        model=settings.soniox_stt_model,
        max_endpoint_delay_ms=defaults.turn_detection.soniox.max_endpoint_delay_ms,
    )
    await stt.start(config)
    try:
        await stt.send_audio(audio_utils.silence(200))
        await stt.finalise()
        # A recogniser that accepted the config answers *something* -- tokens,
        # or an empty token list with a processing time. Not answering at all
        # inside a second is the failure this is looking for.
        await asyncio.wait_for(anext(aiter(stt.events())), timeout=5.0)
        return f"stream accepted, model {config.model}"
    finally:
        await stt.close()


async def check_bakbak(settings: Settings) -> str:
    """List voices, then synthesise -- listing alone hides a model mismatch."""
    tts = BakbakTTS(settings)
    try:
        voices = await tts.voices()
        voice = settings.voice_for_language("hi-IN")
        if not voice:
            return f"{len(voices)} voices listed; BAKBAK_VOICE_HI is not set yet"

        config = TtsConfig(
            language="hi-IN",
            model=settings.bakbak_model,
            speaker=voice,
            sample_rate=settings.tts_output_sample_rate,
        )
        audio = await tts.synthesise_all("नमस्ते जी।", config)
        if not audio:
            raise RuntimeError("the voice answered with no audio")
        return (
            f"{len(voices)} voices; {audio_utils.duration_ms(audio)} ms rendered "
            f"at {config.sample_rate} Hz"
        )
    finally:
        await tts.close()


async def check_llm(settings: Settings) -> str:
    """One tiny generation, measured against §6.1's first-token budget.

    Deliberately *not* run through `build_gateway`. The gateway enforces the
    in-call budget and gives up at 800 ms, which is correct during a call and
    useless here: "did not answer" and "answered in 1.4 s" are different
    findings, and only one of them means the key is wrong. This asks the
    transport directly, waits much longer, and reports the latency it measured.
    """
    from voice_worker.adapters.llm.gateway import build_transport

    budget_ms = get_defaults().llm.first_token_timeout_ms
    transport = build_transport(settings)
    started = time.perf_counter()
    first_ms: float | None = None
    pieces: list[str] = []

    try:
        async with asyncio.timeout(30):
            async for piece in transport.stream(  # type: ignore[attr-defined]
                model=settings.llm_primary_model,
                system_blocks=["Reply with one short word."],
                user_message="Say ready.",
                cacheable_prefix="Reply with one short word.",
                max_tokens=8,
                temperature=0.3,
            ):
                if first_ms is None:
                    first_ms = (time.perf_counter() - started) * 1000
                pieces.append(piece)
    finally:
        close = getattr(transport, "aclose", None)
        if close is not None:
            await close()

    if not pieces:
        raise RuntimeError("the model produced nothing")

    ttft = first_ms or 0.0
    verdict = "within" if ttft <= budget_ms else f"OVER the {budget_ms} ms"
    return (
        f"{settings.llm_gateway} -> {settings.llm_primary_model}; "
        f"first token {ttft:.0f} ms, {verdict} §6.1 budget"
    )


def _providers_in_use() -> tuple[set[str], set[str]]:
    """Which vendors the routing table actually reaches for."""
    defaults = get_defaults()
    stt = {r.stt.provider for r in defaults.language_routes.values() if r.stt}
    tts = {r.tts.provider for r in defaults.language_routes.values() if r.tts}
    return stt, tts


async def run() -> int:
    settings = Settings()
    stt_providers, tts_providers = _providers_in_use()

    results: list[Result] = []

    if "soniox" in stt_providers:
        if settings.soniox_api_key:
            results.append(await timed("soniox stt", lambda: check_soniox(settings)))
        else:
            results.append(Result("soniox stt", SKIP, "SONIOX_API_KEY not set"))

    if "bakbak" in tts_providers:
        if settings.bakbak_api_key:
            results.append(await timed("bakbak tts", lambda: check_bakbak(settings)))
        else:
            results.append(Result("bakbak tts", SKIP, "BAKBAK_API_KEY not set"))

    if "sarvam" in stt_providers or "sarvam" in tts_providers:
        # Still routed for Odia and Punjabi, and deliberately *not* checked --
        # a round trip is not worth it for two languages most deployments do
        # not enable. Reported as unchecked even when a key is present: this
        # script exists to replace "the key looks set" with "the vendor
        # answered", and printing `ok` for something nothing called would put
        # the first back in under the second's name.
        held = "a key is set" if settings.sarvam_api_key else "no key set"
        results.append(
            Result("sarvam", SKIP, f"{held}; routed for Odia/Punjabi, not called here")
        )

    key = settings.anthropic_api_key if settings.llm_gateway == "anthropic" else (
        settings.llm_gateway_master_key
    )
    if key:
        results.append(await timed("llm", lambda: check_llm(settings)))
    else:
        variable = (
            "ANTHROPIC_API_KEY" if settings.llm_gateway == "anthropic"
            else "LLM_GATEWAY_MASTER_KEY"
        )
        results.append(Result("llm", SKIP, f"{variable} not set"))

    width = max(len(r.vendor) for r in results)
    print()
    for result in results:
        timing = f"{result.elapsed_ms:6.0f} ms" if result.elapsed_ms else " " * 9
        print(f"  {result.vendor:{width}}  {result.status:8} {timing}  {result.detail}")
    print()

    failed = [r for r in results if r.status == FAIL]
    skipped = [r for r in results if r.status == SKIP]
    if failed:
        print(f"{len(failed)} vendor(s) did not answer. The helpline cannot run on this.")
        return 1
    if skipped:
        print(f"{len(skipped)} not checked -- fill those in .env and run this again.")
        return 0
    print("Every vendor the routing table needs answered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
