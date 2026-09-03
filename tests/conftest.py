"""Shared test fixtures.

Tests that need Postgres are marked ``integration`` and skip cleanly when the
stack is not up, so ``make test`` is useful on a laptop without Docker running.
The CI pipeline runs with the stack and therefore executes them.
"""

from __future__ import annotations

import asyncio
import base64
import os
import socket
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import pytest


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def _postgres_available() -> bool:
    host = os.environ.get("TEST_PG_HOST", "127.0.0.1")
    port = int(os.environ.get("TEST_PG_PORT", "5432"))
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def postgres_available() -> bool:
    return _postgres_available()


@pytest.fixture(scope="session")
def embedded_pg(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    """A migrated, seeded Postgres 16 for the integration tests.

    Session-scoped: starting a server and loading 1,190 seed rows takes long
    enough that doing it per test would discourage writing the tests that
    matter. Tests that mutate data clean up after themselves.
    """
    pytest.importorskip("pgserver", reason="pgserver provides the embedded Postgres")
    from tests import pgfixture

    root = tmp_path_factory.mktemp("pg")
    server, pg = pgfixture.start(root)
    pgfixture.seed(migrator_dsn=pg.migrator_dsn, app_dsn=pg.app_dsn)
    try:
        yield pg
    finally:
        server.cleanup()


@pytest.fixture
async def app_engine(embedded_pg):  # type: ignore[no-untyped-def]
    """An engine connected as ``uaagro_app`` -- the role RLS actually applies to.

    Connecting as the migrator would silently bypass the policies even though
    they are FORCEd, because FORCE still exempts a superuser. Every RLS
    assertion in this suite therefore runs through this engine.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(embedded_pg.app_dsn, poolclass=None)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def migrator_engine(embedded_pg):  # type: ignore[no-untyped-def]
    """Superuser engine, for setup and assertions about the schema itself."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(embedded_pg.migrator_dsn)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True, scope="session")
def _test_environment() -> Iterator[None]:
    """Deterministic key material and a non-production APP_ENV for every test.

    Set before any settings object is constructed. Real credentials are never
    needed: the tests that would use a vendor are marked ``vendor`` and are
    deselected by default.
    """
    previous = dict(os.environ)
    os.environ.update(
        {
            "APP_ENV": "test",
            "PHONE_HASH_PEPPER": base64.b64encode(b"test-pepper-32-bytes-exactly!!!!").decode(),
            "LOCAL_DEK_BASE64": base64.b64encode(b"test-dek-32-bytes-exactly-ok!!!!").decode(),
            "JWT_SIGNING_KEY": base64.b64encode(b"test-jwt-signing-key-32-bytes!!!").decode(),
            "LOG_LEVEL": "WARNING",
            # Placeholder vendor keys. Adapter construction only *reads*
            # these -- no network call happens until a stream opens -- so
            # they let the factory wiring be tested without credentials.
            # Any test that would actually reach a vendor is marked
            # `vendor` and deselected by default.
            "SONIOX_API_KEY": "test-not-a-real-key",
            "BAKBAK_API_KEY": "test-not-a-real-key",
            "SARVAM_API_KEY": "test-not-a-real-key",
            "DEEPGRAM_API_KEY": "test-not-a-real-key",
            "SARVAM_TTS_SPEAKER_HI": "test-speaker",
            # Every Bakbak-served language, so that building a stack for one
            # of them exercises the real voice lookup rather than skipping it.
            # Real deployments set two of these; the routing table currently
            # reaches for nine.
            "BAKBAK_VOICE_HI": "00000000-0000-4000-8000-00000000hi01".replace("hi", "aa"),
            "BAKBAK_VOICE_EN": "00000000-0000-4000-8000-00000000en01".replace("en", "bb"),
            "BAKBAK_VOICES": ",".join(
                f"{code}=00000000-0000-4000-8000-0000000000{index:02d}"
                for index, code in enumerate(
                    ("mr-IN", "ml-IN", "bn-IN", "ta-IN", "te-IN", "kn-IN", "gu-IN"), start=10
                )
            ),
        }
    )
    from uaagro_domain.settings import reset_caches

    reset_caches()
    yield
    os.environ.clear()
    os.environ.update(previous)
    reset_caches()


@pytest.fixture
async def panel(app_engine):  # type: ignore[no-untyped-def]
    """The panel's API on the embedded database, signed in as a seeded user.

    See ``tests/panel_support.py``: authentication is stubbed, authorisation
    and row-level security are not.
    """
    from tests.panel_support import panel_client

    async with panel_client(app_engine) as http:
        yield http


@pytest.fixture
def quiet_queue(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """The job queue and the campaign control key, recorded rather than sent."""
    from api.services import jobs

    seen: dict[str, list[Any]] = {"enqueued": [], "control": []}

    async def enqueue(job: str, *args: Any) -> None:
        seen["enqueued"].append((job, args))

    async def set_control(campaign_id: Any, instruction: str | None) -> None:
        seen["control"].append((campaign_id, instruction))

    monkeypatch.setattr(jobs, "enqueue", enqueue)
    monkeypatch.setattr(jobs, "set_control", set_control)
    return seen


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., None]]:
    """Set environment variables for one test and reload the settings.

    The settings object is cached for the life of the process; a test that
    changes the environment has to drop that cache, and drop it again on the
    way out so the next test does not inherit the change.
    """
    from uaagro_domain.settings import reset_caches

    def apply(**values: str) -> None:
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        reset_caches()

    yield apply
    monkeypatch.undo()
    reset_caches()


@pytest.fixture
def cipher():  # type: ignore[no-untyped-def]
    from uaagro_db.crypto import build_cipher
    from uaagro_domain.settings import Settings

    return build_cipher(Settings())


@pytest.fixture
async def worker_url() -> AsyncIterator[str]:
    """Run a voice worker in-process and yield its WebSocket URL.

    Uvicorn in a background task rather than a subprocess: the test needs the
    worker's exceptions to surface as test failures, not vanish into another
    process's stderr.
    """
    import uvicorn

    from voice_worker.main import app, state

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on")
    server = uvicorn.Server(config)
    # The worker module is imported once per session, so its drain flag survives
    # from the previous test's shutdown. Reset before starting, not after: a
    # teardown-time reset is undone by the lifespan shutdown that follows it.
    state.accepting = True
    task = asyncio.create_task(server.serve())

    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:  # pragma: no cover - only on a badly overloaded machine
        task.cancel()
        pytest.fail("voice worker did not start within 5 seconds")

    try:
        yield f"ws://127.0.0.1:{port}/ws/voice?no_persist=1"
    finally:
        # Drain flag is reset at setup, not here -- see the comment above.
        server.should_exit = True
        await asyncio.wait_for(task, timeout=15)
