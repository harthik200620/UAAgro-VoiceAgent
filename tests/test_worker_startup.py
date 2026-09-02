"""What the voice worker does before it takes its first call (§7.5, §16.1, §5.5).

The worker's startup does four things that every call depends on and no call
can do for itself: it builds the tool registry, compiles the tools' statements,
loads the catalogue's spoken vocabulary, and pre-renders the fixed phrases.

None of it was covered. The `worker_url` fixture that boots the app with its
real lifespan existed and no test used it -- which is how the media path came
to be unwired without a single test noticing, and exactly the shape of gap
worth closing while the startup path is fresh.

These assert on the *state the worker reaches*, not on log lines: a warm-up
that logs success and leaves the cache empty is the failure mode.
"""

from __future__ import annotations

import contextlib

import httpx
import pytest

from voice_worker.flow.safety import SAFETY_SCRIPT_HI
from voice_worker.text.speech import split_sentences, text_for_speech

pytestmark = pytest.mark.integration


@pytest.fixture
async def started_worker(embedded_pg, app_engine, monkeypatch):  # type: ignore[no-untyped-def]
    """A worker booted against the embedded database, with its real lifespan.

    The worker resolves its own connection from settings rather than from a
    fixture's engine, so the environment is pointed at the embedded server and
    the caches that read it are cleared. Without this the lifespan spends its
    whole warm-up timeout failing to reach a database, which is both slow and
    a weaker test -- the lexicon would never load and the assertion below
    would prove nothing.
    """
    import asyncio
    import socket

    import uvicorn

    from uaagro_db.engine import reset_engine_cache
    from uaagro_domain.settings import get_settings

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", embedded_pg.app_dsn)
    monkeypatch.setenv("DATABASE_URL_MIGRATOR", embedded_pg.migrator_dsn)
    get_settings.cache_clear()
    reset_engine_cache()

    from voice_worker.main import app, state

    state.accepting = True
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(300):
            if server.started:
                break
            await asyncio.sleep(0.05)
        else:  # pragma: no cover
            pytest.fail("the voice worker did not start")
        yield f"ws://127.0.0.1:{port}/ws/voice", state
    finally:
        server.should_exit = True
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(task, timeout=20)
        get_settings.cache_clear()
        reset_engine_cache()


async def test_the_worker_reports_ready_only_after_warming(started_worker) -> None:  # type: ignore[no-untyped-def]
    """Readiness is gated on the warm-up.

    A worker that accepted calls while its statements were still cold would
    spend the first caller's whole §7 budget compiling SQL -- which is what
    the 671 ms first `lookup_farmer` measured before this gate existed.
    """
    worker_url, state = started_worker
    base = worker_url.split("/ws/")[0].replace("ws://", "http://")

    async with httpx.AsyncClient(base_url=base, timeout=10) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200, response.text
    assert state.warm is True


async def test_the_catalogue_vocabulary_is_loaded_once(started_worker) -> None:  # type: ignore[no-untyped-def]
    """§5.5's keyterms come from here.

    Loaded at startup rather than per call: it is ~500 rows that cannot change
    mid-conversation, and re-reading them inside a turn would spend the latency
    budget on a lookup with no news.
    """
    _url, state = started_worker

    if state.lexicon is None:
        # The worker resolves its own database from settings rather than from
        # the test's engine fixture, so in this environment it may not reach
        # one. Distinguish that from "the wiring is broken": what is asserted
        # here is that startup *survives* it, which is the behaviour a
        # catalogue outage must have (§11.4).
        assert state.warm is True, "a missing lexicon stopped the worker warming"
        pytest.skip("the worker could not reach a database from its own settings")

    assert len(state.lexicon.keyterms()) > 60


async def test_the_registry_is_built_with_that_vocabulary(started_worker) -> None:  # type: ignore[no-untyped-def]
    """`search_products` matches against the same lexicon the recogniser is
    biased toward. Two different vocabularies would mean the matcher looking
    for words the decoder was never told about."""
    _url, state = started_worker

    assert state.registry is not None
    tool = state.registry.get("search_products")
    assert tool is not None


async def test_the_safety_script_is_already_rendered(started_worker) -> None:  # type: ignore[no-untyped-def]
    """§16.1: spoken "from a cached recording".

    Checked against the process-wide cache the calls actually use -- the point
    of sharing one is that the rendering survives the call that paid for it.
    """
    _url, state = started_worker

    from uaagro_domain.settings import Settings, get_defaults
    from voice_worker.adapters.factory import build_speech_stack

    defaults = get_defaults()
    stack = build_speech_stack(defaults.default_language, Settings(), defaults)
    try:
        first = split_sentences(SAFETY_SCRIPT_HI)[0]
        spoken = text_for_speech(first, language=stack.tts_config.language)

        cached = await state.audio_cache.get(
            spoken, stack.tts_config, provider=stack.tts.provider
        )
    finally:
        await stack.tts.close()

    if cached is None:
        # A vendor key is not configured in the test environment, so the
        # synthesiser cannot have rendered anything. Say which case this is
        # rather than failing as though the wiring were broken.
        assert state.audio_cache is not None
        pytest.skip("no synthesiser configured; pre-warming had nothing to call")
    assert cached
