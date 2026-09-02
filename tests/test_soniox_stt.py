"""What the Soniox adapter actually says to the vendor, and hears back (§5.1, §5.2, §5.5).

Run against a real WebSocket server rather than a monkeypatched ``connect``.
The bugs worth catching in a streaming adapter are on the wire -- a config key
spelled the way the previous vendor spelled it, audio sent as text frames, a
turn state machine that works for one message and not for two in a row -- and a
patched connect proves none of that. The stand-in below speaks the protocol as
documented and records what it was sent.

The state machine is the substance here. Soniox publishes a *commit* and no
eager probability, so §5.2's speculative window is reconstructed from finality:
tokens stop being revised, a short timer runs out, and the pipeline is told to
start generating. That is a heuristic, and the tests that matter are the ones
where it is wrong -- the caller pauses and carries on -- because a speculative
generation that survives being cancelled is a severe bug (§5.2).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import pytest
import websockets

from uaagro_domain.settings import Settings
from voice_worker.adapters.stt.base import SttConfig, SttEvent, SttEventType
from voice_worker.adapters.stt.soniox import MAX_CONTEXT_CHARS, SonioxSTT


def token(text: str, *, final: bool = True, confidence: float | None = None,
          language: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"text": text, "is_final": final}
    if confidence is not None:
        entry["confidence"] = confidence
    if language is not None:
        entry["language"] = language
    return entry


class FakeSoniox:
    """A stand-in that speaks the documented protocol and records the config."""

    def __init__(self, script: list[list[dict[str, Any]]] | None = None) -> None:
        #: One entry per server message, each a list of tokens.
        self.script = script or []
        self.config: dict[str, Any] = {}
        self.audio_frames: list[bytes] = []
        self.text_frames: list[str] = []
        self._server: Any = None
        self.url = ""

    async def _handler(self, socket: Any) -> None:
        self.config = json.loads(await socket.recv())
        for message in self.script:
            await socket.send(json.dumps({"tokens": message}))
        # Keep the socket open so the client drives the rest of the exchange:
        # closing here would race the reader and turn a state-machine test into
        # a connection-teardown test.
        try:
            async for frame in socket:
                if isinstance(frame, bytes):
                    if not frame:
                        break
                    self.audio_frames.append(frame)
                else:
                    self.text_frames.append(frame)
        except websockets.ConnectionClosed:
            pass

    async def __aenter__(self) -> FakeSoniox:
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        # Cached rather than derived on each read: the socket list is empty
        # once the server closes, and assertions run after the block.
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def push(self, tokens: list[dict[str, Any]]) -> None:
        """Send one more server message to whoever is connected."""
        for connection in list(self._server.connections):
            await connection.send(json.dumps({"tokens": tokens}))


async def collect(stt: SonioxSTT, count: int, *, seconds: float = 5.0) -> list[SttEvent]:
    """The next ``count`` events, or fail saying how far it got."""
    events: list[SttEvent] = []

    async def drain() -> None:
        async for event in stt.events():
            events.append(event)
            if len(events) >= count:
                return

    try:
        async with asyncio.timeout(seconds):
            await drain()
    except TimeoutError:
        pytest.fail(f"expected {count} events, got {[e.type.value for e in events]}")
    return events


def config_for(**overrides: Any) -> SttConfig:
    base: dict[str, Any] = {
        "language": "hi-IN",
        "language_hints": ("hi", "en"),
        "model": "stt-rt-v5",
        "endpoint_latency_adjustment_level": 2,
        "endpoint_sensitivity": 0.2,
        "max_endpoint_delay_ms": 1200,
        "eager_after_final_ms": 30,
    }
    base.update(overrides)
    return SttConfig(**base)


# --------------------------------------------------------------------------- #
# The opening handshake
# --------------------------------------------------------------------------- #


async def test_the_endpoint_dials_reach_the_vendor() -> None:
    """§5.2's tuning is only tuning if it is in the request.

    All three are configured in `defaults.yaml` and none of them raises if it
    is dropped -- the stream still works, just with the vendor's own two-second
    backstop, which is most of a turn.
    """
    async with FakeSoniox() as server:
        stt = SonioxSTT(Settings(), url=server.url)
        await stt.start(config_for())
        await stt.close()

    assert server.config["enable_endpoint_detection"] is True
    assert server.config["endpoint_latency_adjustment_level"] == 2
    assert server.config["endpoint_sensitivity"] == 0.2
    assert server.config["max_endpoint_delay_ms"] == 1200
    assert server.config["language_hints"] == ["hi", "en"]


async def test_audio_is_declared_as_the_telephony_format() -> None:
    """§23-8: the 8 kHz linear16 arriving from the line is handed over
    unconverted, so nothing on the inbound path resamples."""
    async with FakeSoniox() as server:
        stt = SonioxSTT(Settings(), url=server.url)
        await stt.start(config_for())
        await stt.close()

    assert server.config["audio_format"] == "pcm_s16le"
    assert server.config["sample_rate"] == 8000
    assert server.config["num_channels"] == 1


async def test_the_catalogue_vocabulary_is_sent_as_context() -> None:
    """§5.5: bias the decode rather than correcting the transcript after.

    "एनपीके बारह बत्तीस सोलह" heard as four unrelated numbers is a failed turn;
    the boost is what stops that, and it is invisible when it is missing.
    """
    async with FakeSoniox() as server:
        stt = SonioxSTT(Settings(), url=server.url)
        await stt.start(config_for(keyterms=("डीएपी", "एनपीके बारह बत्तीस सोलह")))
        await stt.close()

    terms = server.config["context"]["terms"]
    # Longest first: the multi-word name is both the hardest to hear and the
    # most damaging to get wrong.
    assert terms[0] == "एनपीके बारह बत्तीस सोलह"
    assert "डीएपी" in terms


async def test_an_oversized_catalogue_is_cut_rather_than_sent_whole() -> None:
    """An unbounded context is a slow handshake on every call."""
    async with FakeSoniox() as server:
        stt = SonioxSTT(Settings(), url=server.url)
        await stt.start(config_for(keyterms=tuple(f"उत्पाद{i:04d}" for i in range(2000))))
        await stt.close()

    terms = server.config["context"]["terms"]
    assert sum(len(t) for t in terms) <= MAX_CONTEXT_CHARS


async def test_a_slow_speaker_gets_the_vendors_ceiling_not_the_specs_number() -> None:
    """§5.2 wants 8 s of patience for a caller who pauses; the vendor accepts
    at most 3 s. Sending 8000 anyway would be rejected at connect time and take
    the call with it, so the ceiling is applied here and knowingly."""
    async with FakeSoniox() as server:
        stt = SonioxSTT(Settings(), url=server.url)
        await stt.start(config_for(slow_speaker=True))
        await stt.close()

    assert server.config["max_endpoint_delay_ms"] == 3000


async def test_the_key_travels_in_the_config_and_nowhere_else() -> None:
    """§23-6. The URL is logged by half the stack; the config message is not."""
    async with FakeSoniox() as server:
        stt = SonioxSTT(Settings(), url=server.url)
        await stt.start(config_for())
        await stt.close()

    assert server.config["api_key"] == Settings().soniox_api_key
    assert "api_key" not in server.url


# --------------------------------------------------------------------------- #
# The turn state machine
# --------------------------------------------------------------------------- #


async def test_the_end_marker_commits_the_turn_without_appearing_in_it() -> None:
    """``<end>`` is a control token. Speaking it back would put a literal
    "<end>" into the transcript the LLM is asked to answer."""
    script = [
        [token("डीएपी ", confidence=0.9, language="hi")],
        [token("मिलेगा", confidence=0.8, language="hi"), token("<end>")],
    ]
    async with FakeSoniox(script) as server:
        stt = SonioxSTT(Settings(), url=server.url)
        await stt.start(config_for(eager_after_final_ms=5000))
        try:
            # speech_started, partial, end_of_turn.
            events = await collect(stt, 3)
        finally:
            await stt.close()

    kinds = [event.type for event in events]
    assert SttEventType.SPEECH_STARTED in kinds
    ended = [e for e in events if e.type is SttEventType.END_OF_TURN]
    assert len(ended) == 1
    assert ended[0].text == "डीएपी मिलेगा"
    assert "<end>" not in ended[0].text


async def test_confidence_measures_the_words_not_the_boundary() -> None:
    """§11.4 escalates on a rolling mean below 0.55 over three turns.

    The ``<end>`` token scores how sure the model is that the *turn* ended.
    Averaging it in would make a decisive endpoint look like clear audio and
    silence the escalation on exactly the noisy calls it exists for.
    """
    script = [
        [
            token("क्या", confidence=0.4, language="hi"),
            token(" रेट", confidence=0.5, language="hi"),
            token("<end>", confidence=0.99),
        ],
    ]
    async with FakeSoniox(script) as server:
        stt = SonioxSTT(Settings(), url=server.url)
        await stt.start(config_for(eager_after_final_ms=5000))
        try:
            events = await collect(stt, 2)
        finally:
            await stt.close()

    ended = next(e for e in events if e.type is SttEventType.END_OF_TURN)
    assert ended.confidence is not None
    assert abs(ended.confidence - 0.45) < 1e-6, "the boundary token was averaged in"


async def test_stable_final_text_starts_speculation_before_the_commit() -> None:
    """§5.2's ~300 ms overlap, rebuilt from the only signal Soniox gives.

    Flux publishes a probability the turn is over; Soniox publishes finality.
    A stretch where every token is confirmed and nothing provisional is
    outstanding is the same evidence, so a short timer on that condition is
    what starts the LLM early.
    """
    script = [[token("डीएपी का रेट", confidence=0.9, language="hi")]]
    async with FakeSoniox(script) as server:
        stt = SonioxSTT(Settings(), url=server.url)
        await stt.start(config_for(eager_after_final_ms=20))
        try:
            events = await collect(stt, 3)
        finally:
            await stt.close()

    eager = [e for e in events if e.type is SttEventType.EAGER_END_OF_TURN]
    assert eager, f"no speculation was triggered: {[e.type.value for e in events]}"
    assert eager[0].text == "डीएपी का रेट"
    assert eager[0].detail == "stable_final"


async def test_a_caller_who_carries_on_cancels_the_speculation() -> None:
    """The heuristic's failure case, and the one that has to be safe.

    A false eager is only ever a wasted generation -- provided ``TURN_RESUMED``
    is emitted, because §5.2 requires the speculative generation be cancelled
    on it. An orphaned generation that still speaks is a severe bug, so the
    event is surfaced on its own rather than folded into a transcript update.
    """
    async with FakeSoniox([[token("डीएपी", confidence=0.9)]]) as server:
        stt = SonioxSTT(Settings(), url=server.url)
        await stt.start(config_for(eager_after_final_ms=20))
        try:
            first = await collect(stt, 3)
            assert any(e.type is SttEventType.EAGER_END_OF_TURN for e in first)

            # The caller was only pausing.
            await server.push([token(" का रेट क्या", final=False)])
            resumed = await collect(stt, 2)
        finally:
            await stt.close()

    assert any(e.type is SttEventType.TURN_RESUMED for e in resumed), (
        f"speculation was left running: {[e.type.value for e in resumed]}"
    )


async def test_speculation_is_not_triggered_twice_for_the_same_words() -> None:
    """A second eager for the same words would start a second generation while
    the first is still running, and §8 would bill for both. Tokens flipping
    from provisional to final are the same words."""
    async with FakeSoniox([[token("रेट", final=False)]]) as server:
        stt = SonioxSTT(Settings(), url=server.url)
        await stt.start(config_for(eager_after_final_ms=20, eager_after_stable_ms=20))
        try:
            await collect(stt, 3)
            # The same word, now final: nothing the model was asked has changed.
            await server.push([token("रेट", confidence=0.9)])
            await asyncio.sleep(0.15)
            drained: list[SttEvent] = []
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(_drain_into(stt, drained), timeout=0.2)
        finally:
            await stt.close()

    assert [e for e in drained if e.type is SttEventType.EAGER_END_OF_TURN] == []
    assert [e for e in drained if e.type is SttEventType.TURN_RESUMED] == []


async def test_more_words_after_an_eager_cancel_it_and_speculate_again() -> None:
    """The first speculation was for "रेट"; the caller went on to say "रेट
    बताइए". Left alone, the commit would mismatch and the model would be
    asked from scratch. Instead the stale generation is cancelled (§5.2) and
    a new one starts on the fuller text -- one live generation at a time."""
    async with FakeSoniox([[token("रेट", confidence=0.9)]]) as server:
        stt = SonioxSTT(Settings(), url=server.url)
        await stt.start(config_for(eager_after_final_ms=20))
        try:
            await collect(stt, 3)
            await server.push([token(" बताइए", confidence=0.9)])
            later = await collect(stt, 3)
        finally:
            await stt.close()

    kinds = [e.type for e in later]
    assert SttEventType.TURN_RESUMED in kinds
    eager = [e for e in later if e.type is SttEventType.EAGER_END_OF_TURN]
    assert eager and eager[-1].text == "रेट बताइए"
    assert kinds.index(SttEventType.TURN_RESUMED) < kinds.index(SttEventType.EAGER_END_OF_TURN)


async def _drain_into(stt: SonioxSTT, sink: list[SttEvent]) -> None:
    async for event in stt.events():
        sink.append(event)


async def test_two_turns_in_a_row_do_not_leak_into_each_other() -> None:
    """The state reset is the part a single-turn test never reaches.

    Accumulated text, confidences and the eager flag all live on the adapter,
    so a missed reset shows up as the second turn's transcript containing the
    first one -- which the LLM would answer as one long question.
    """
    script = [
        [token("डीएपी", confidence=0.9), token("<end>")],
        [token("यूरिया", confidence=0.7), token("<end>")],
    ]
    async with FakeSoniox(script) as server:
        stt = SonioxSTT(Settings(), url=server.url)
        await stt.start(config_for(eager_after_final_ms=5000))
        try:
            # speech_started + end_of_turn, twice: a message that carries the
            # end marker commits without a partial in between.
            events = await collect(stt, 4)
        finally:
            await stt.close()

    ended = [e for e in events if e.type is SttEventType.END_OF_TURN]
    assert [e.text for e in ended] == ["डीएपी", "यूरिया"]
    assert ended[1].confidence is not None
    assert abs(ended[1].confidence - 0.7) < 1e-6, "the first turn's confidence carried over"


async def test_audio_goes_out_as_binary_frames_untouched() -> None:
    """Sent as text, the vendor sees base64 as speech and hears nothing."""
    pcm = bytes(range(256)) * 4
    async with FakeSoniox() as server:
        stt = SonioxSTT(Settings(), url=server.url)
        await stt.start(config_for())
        await stt.send_audio(pcm)
        await asyncio.sleep(0.1)
        await stt.close()

    assert server.audio_frames == [pcm]


async def test_finalise_asks_the_vendor_to_flush() -> None:
    """Used when the pipeline knows the turn is over before the model does --
    a DTMF keypress, or a transfer triggered mid-utterance."""
    async with FakeSoniox() as server:
        stt = SonioxSTT(Settings(), url=server.url)
        await stt.start(config_for())
        await stt.finalise()
        await asyncio.sleep(0.1)
        await stt.close()

    assert any(json.loads(frame).get("type") == "finalize" for frame in server.text_frames)


async def test_a_vendor_error_is_surfaced_rather_than_swallowed() -> None:
    """§11.4 decides whether to fall back; it cannot decide on silence."""
    async with FakeSoniox() as server:
        stt = SonioxSTT(Settings(), url=server.url)
        await stt.start(config_for())
        try:
            for connection in list(server._server.connections):
                await connection.send(
                    json.dumps({"error_code": 401, "error_message": "invalid api key"})
                )
            events = await collect(stt, 1)
        finally:
            await stt.close()

    assert events[0].type is SttEventType.ERROR
    assert "invalid api key" in events[0].detail


async def test_a_missing_key_names_the_variable() -> None:
    """§0 rule 4: fail at construction naming the variable, rather than at the
    first turn of the first call with a socket error."""
    from uaagro_domain.errors import MissingCredentialError

    settings = Settings(soniox_api_key=None)
    with pytest.raises(MissingCredentialError) as caught:
        SonioxSTT(settings)
    assert "SONIOX_API_KEY" in str(caught.value)
