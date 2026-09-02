"""Voice worker: the media-path service.

Exposes the WebSocket the telephony provider streams to, plus liveness and
readiness probes. It deliberately holds no admin surface -- §4.1 keeps the
control plane out of the audio path so a slow admin query can never add latency
to a live call.

Shutdown drains rather than kills (§20): on SIGTERM the worker stops accepting
new calls and lets in-flight ones finish, capped so a deploy cannot hang forever
on one stuck call.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import uuid
from collections.abc import AsyncIterator
from typing import Any

import structlog
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse
from sqlalchemy import select

from uaagro_db.engine import dispose_engines, incall_session
from uaagro_db.models import Organization
from uaagro_domain.enums import CallDirection, TelephonyProvider
from uaagro_domain.eventloop import install_fast_event_loop
from uaagro_domain.logging import configure_logging
from uaagro_domain.settings import get_defaults, get_settings
from uaagro_domain.telemetry import INSTRUMENTS

from .adapters.factory import build_speech_stack
from .adapters.http import close_shared_clients
from .adapters.llm.gateway import build_gateway
from .adapters.resilience import breaker_states
from .adapters.telephony.base import TelephonySerializer
from .adapters.telephony.exotel import ExotelSerializer
from .adapters.telephony.mulaw_providers import PlivoSerializer, TwilioSerializer
from .runtime import audio as audio_utils
from .runtime.assembly import CallPipeline, build_call_pipeline
from .runtime.audio_cache import AudioCache
from .runtime.audio_prewarm import prewarm
from .runtime.repository import NullCallRepository, SqlCallRepository
from .runtime.session import CallSession, TransportClosed
from .text.catalogue_lexicon import load_lexicon
from .text.lexicon import Lexicon
from .tools import build_registry
from .tools.base import ToolRegistry

log = structlog.get_logger(__name__)

#: Longest a drain waits for in-flight calls before the process exits (§20).
DRAIN_TIMEOUT_S = 600

#: Longest the startup warm-up may take before the worker comes up anyway.
WARMUP_TIMEOUT_S = 20

#: Placeholder opening audio. Phase 2 replaces this with the cached Sarvam
#: rendering of the §11.1 greeting; the point in Phase 1 is that the transport
#: round-trips audio the caller can actually hear.
_GREETING_PCM = audio_utils.tone(440, 600) + audio_utils.silence(120)


class WorkerState:
    """Process-wide state. Tracks live calls so a drain knows when it is done."""

    def __init__(self) -> None:
        self.accepting = True
        #: False until the tool statements are compiled. Readiness reports
        #: not-ready while it is false, so the load balancer does not route the
        #: first call of a deploy into a cold process -- see `_warm_up`.
        self.warm = False
        self.registry: ToolRegistry | None = None
        #: The catalogue's spoken vocabulary (§5.5). Loaded once: it is
        #: ~500 rows that do not change mid-call, and re-reading it per
        #: call would spend a turn's budget on a lookup with no news.
        self.lexicon: Lexicon | None = None
        #: One audio cache for the process, not one per call (§9.3).
        #: A per-call cache is thrown away with the call, so the greeting
        #: and the §16.1 safety script were re-synthesised every time --
        #: paying a vendor to render the same sentence on every call, and
        #: paying it during the opening 50 ms §11.1 budgets.
        self.audio_cache: AudioCache = AudioCache()
        #: Kept so the background pre-warm is not garbage collected
        #: mid-flight, and so shutdown can cancel it.
        self.prewarm_task: asyncio.Task[None] | None = None
        self.live_calls: set[asyncio.Task[Any]] = set()

    @property
    def concurrency(self) -> int:
        return len(self.live_calls)


state = WorkerState()

#: Resolved once per process. See `_resolve_organization`.
_organization_id: uuid.UUID | None = None


def _build_serializer(provider: TelephonyProvider) -> TelephonySerializer:
    """Select the serializer for the configured provider.

    §4.3 keeps these as separate implementations rather than one parameterised
    class, because the codecs differ and conflating them produces white noise.
    """
    match provider:
        case TelephonyProvider.EXOTEL | TelephonyProvider.SIMULATOR:
            return ExotelSerializer()
        case TelephonyProvider.PLIVO:
            return PlivoSerializer()
        case TelephonyProvider.TWILIO:
            return TwilioSerializer()
        case _:
            raise NotImplementedError(
                f"No serializer is implemented for {provider.value}."
            )


async def _prewarm_audio() -> None:
    """Render the fixed phrases into the shared cache (§16.1).

    Runs detached from startup. Holds its own speech stack because the one a
    call builds is per-language and short-lived, and closes it either way.
    """
    stack = None
    try:
        settings = get_settings()
        defaults = get_defaults()
        stack = build_speech_stack(defaults.default_language, settings, defaults)

        # Open the vendor connections before the first caller does. Measured
        # 2026-09-02: a cold TLS handshake costs 438 ms of the synthesiser's
        # 350 ms first-byte budget and 268 ms of the model's first token. Both
        # are pooled process-wide, so this one handshake serves every call.
        #
        # Concurrently and without raising: neither is required to start.
        await asyncio.gather(
            _prewarm_vendor(stack.tts),
            _prewarm_vendor(build_gateway(settings).transport),
            return_exceptions=True,
        )

        stored = await prewarm(state.audio_cache, stack.tts, stack.tts_config)
        log.info("worker.audio_prewarmed", phrases=stored)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("worker.audio_prewarm_failed", error=type(exc).__name__)
    finally:
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.tts.close()


async def _prewarm_vendor(adapter: object) -> None:
    """Open one vendor's pooled connection, if it knows how.

    Duck-typed rather than declared on the adapter interfaces: pre-warming is
    an HTTP-pool concern, and a WebSocket recogniser has nothing to pre-warm
    because its socket is per call. Requiring every adapter to implement a
    no-op would spread that detail across all of them.
    """
    hook = getattr(adapter, "prewarm", None)
    if hook is None:
        return
    with contextlib.suppress(Exception):
        await hook()


async def _warm_up() -> None:
    """Build the tool registry and compile its statements before taking calls.

    Measured: the first ``lookup_farmer`` in a fresh process takes 671 ms
    against 13 ms warm, almost all of it SQLAlchemy compiling the ORM statement
    and Postgres planning it. The tool hard-times-out at 400 ms, so without
    this the first farmer to call after every deploy is told the lookup is
    taking too long.

    Failures do not stop the worker. A database that is not up yet is a
    readiness problem the probe below already owns; refusing to start over a
    cold cache turns a slow first call into no calls at all.
    """
    # The lexicon first: the registry's product matcher takes it, and so does
    # every call's recogniser.
    try:
        async with incall_session() as session:
            state.lexicon = await load_lexicon(session)
    except Exception as exc:
        # A worker with no lexicon still answers calls -- product questions
        # just lose §5.5's boosting and fall back to full-text search. Refusing
        # to start would turn a catalogue problem into an outage.
        log.warning("worker.lexicon_unavailable", error=type(exc).__name__)

    state.registry = build_registry(lexicon=state.lexicon)

    # §16.1 requires the poisoning script to come "from a cached recording",
    # so the fixed phrases are rendered up front -- but in the *background*.
    #
    # Not awaited, and that is the whole point. Every other step here is local
    # and fast; this one calls a vendor over the network, and a synthesiser
    # that is down answers each request with a timeout. Awaiting it turns "the
    # voice API is having a bad minute" into "the worker will not come up
    # during a deploy", which trades a slow first safety script for no service
    # at all. The phrases render on demand until this finishes.
    state.prewarm_task = asyncio.create_task(_prewarm_audio())

    try:
        # Capped: a worker that cannot warm must still come up and serve, just
        # with a slow first call. An unbounded await here would turn a slow
        # database into a deploy that never finishes.
        async with asyncio.timeout(WARMUP_TIMEOUT_S):
            warmed = await state.registry.warm()
        log.info("worker.warm", tools=len(warmed))
    except Exception as exc:
        log.warning("worker.warm_failed", error=type(exc).__name__)
    finally:
        state.warm = True


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Before anything opens a socket. Uvicorn usually installs this
    # itself; calling it here makes the dependency ours and explicit,
    # and covers the paths that do not run under uvicorn at all.
    install_fast_event_loop()
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    settings.verify_production_readiness()
    # §19's instruments have existed since Phase 1 and have never been turned
    # on: `setup()` was called from nowhere, so every metric went to an
    # in-process list and no trace was ever exported.
    INSTRUMENTS.setup(
        service_name="uaagro-voice-worker",
        endpoint=settings.otel_exporter_otlp_endpoint or None,
    )
    defaults = get_defaults()
    log.info(
        "worker.started",
        provider=settings.telephony_provider.value,
        languages=len(defaults.language_routes),
        env=settings.app_env,
    )
    await _warm_up()
    try:
        yield
    finally:
        # Drain, never kill.
        state.accepting = False
        if state.prewarm_task is not None:
            state.prewarm_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await state.prewarm_task
        if state.live_calls:
            log.info("worker.draining", live_calls=state.concurrency)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*state.live_calls, return_exceptions=True),
                    timeout=DRAIN_TIMEOUT_S,
                )
        await close_shared_clients()
        await dispose_engines()
        log.info("worker.stopped")


app = FastAPI(
    title="UA Agro voice worker",
    version="0.1.0",
    lifespan=lifespan,
    # No interactive docs on the media service: it has one endpoint and no
    # business presenting a browsable surface to the internet.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/metrics")
async def metrics() -> Response:
    """§19's Prometheus scrape target.

    On the media service deliberately: the numbers §7 gates on -- turn latency,
    segment latency, TTFT, TTFB -- are only observable here.
    """
    body, content_type = INSTRUMENTS.exposition()
    return Response(content=body, media_type=content_type)


@app.get("/health/live")
async def health_live() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/health/ready")
async def health_ready() -> JSONResponse:
    """Readiness reflects whether new calls are being accepted.

    A draining worker reports not-ready so the load balancer stops sending it
    calls, while its in-flight calls continue.
    """
    if not state.accepting:
        return JSONResponse(
            {
                "status": "draining",
                "live_calls": state.concurrency,
                "vendors": breaker_states(),
            },
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    if not state.warm:
        # Not an error -- a deploy in progress. Routing a call here would spend
        # its whole §7 budget compiling statements.
        return JSONResponse(
            {"status": "warming", "live_calls": state.concurrency},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return JSONResponse(
        {
            "status": "ok",
            "live_calls": state.concurrency,
            # An open breaker is not un-readiness -- the worker still answers,
            # still speaks §16.1's cached phrases and still escalates to a
            # person. Taking it out of the load balancer would turn one
            # vendor's outage into no helpline at all. Reported so an operator
            # can see it, not acted on.
            "vendors": breaker_states(),
        }
    )


def _client_allowed(websocket: WebSocket) -> bool:
    """Source-IP check for the media socket (§17).

    An open media WebSocket is an open door: it accepts audio, burns vendor
    spend and can be used to enumerate call behaviour. An empty allowlist
    disables the check, which is correct for local development and is why the
    token check below still applies in production.
    """
    allowlist = get_settings().ip_allowlist
    if not allowlist:
        return True
    client = websocket.client
    if client is None:
        return False
    try:
        address = ipaddress.ip_address(client.host)
    except ValueError:
        return False
    return any(address in ipaddress.ip_network(entry, strict=False) for entry in allowlist)


def _token_valid(websocket: WebSocket) -> bool:
    """Signed-handshake check for the media socket (§17).

    When ``TELEPHONY_WS_TOKEN`` is unset the check is skipped, which is
    permitted only outside production -- ``verify_production_readiness`` refuses
    to start a production worker without it.
    """
    expected = get_settings().telephony_ws_token
    if not expected:
        return True
    presented = websocket.query_params.get("token") or websocket.headers.get("x-auth-token")
    if presented is None:
        return False
    import hmac

    return hmac.compare_digest(presented, expected)


async def _resolve_organization() -> uuid.UUID | None:
    """The organization this worker serves.

    Single-tenant by deployment (§3), so this is one row rather than a lookup
    keyed on the dialled number. Cached for the process: it cannot change while
    the worker is running, and it is on the path of every call's INIT.
    """
    global _organization_id
    if _organization_id is not None:
        return _organization_id
    try:
        async with incall_session() as session:
            _organization_id = await session.scalar(
                select(Organization.id).order_by(Organization.created_at).limit(1)
            )
    except Exception as exc:
        log.warning("worker.organization_lookup_failed", error=type(exc).__name__)
        return None
    return _organization_id


async def _build_pipeline(session: CallSession) -> CallPipeline:
    """Assemble the conversation for one call.

    Held here rather than inside :class:`CallSession` so the session keeps
    knowing only about transport and the call record -- which is what lets the
    protocol tests run without a model, a recogniser or a database.
    """
    settings = get_settings()
    defaults = get_defaults()
    registry = state.registry
    if registry is None:  # pragma: no cover -- lifespan builds it before serving
        registry = build_registry(lexicon=state.lexicon)
        state.registry = registry

    organization_id = await _resolve_organization()
    if organization_id is None:
        raise RuntimeError("no organization row; run `make db-seed`")

    async with incall_session() as db:
        return await build_call_pipeline(
            call_id=session.call_id,
            organization_id=organization_id,
            phone_hash=session.from_number_hash,
            session=db,
            registry=registry,
            settings=settings,
            defaults=defaults,
            send_frame=session.send_audio,
            clear_playback=session.clear_playback,
            lexicon=state.lexicon,
            cache=state.audio_cache,
        )


async def _enqueue_postcall(call_id: uuid.UUID) -> None:
    """Hand the finished call to the §11.5 pipeline.

    Through the queue, not inline: the socket is closing and the caller has
    gone, and holding the media task open to summarise would keep a worker slot
    for a minute after the call it belongs to ended.
    """
    settings = get_settings()
    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        await redis.enqueue_job("process_call", str(call_id))
    finally:
        await redis.close()


@app.websocket("/ws/voice")
async def voice_stream(websocket: WebSocket) -> None:
    """The telephony media socket.

    Configure Exotel with a Voicebot applet pointed here, followed by a Hangup
    applet (§4.3).
    """
    if not _client_allowed(websocket) or not _token_valid(websocket):
        log.warning("worker.ws_rejected", reason="handshake")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    if not state.accepting:
        # Draining: refuse politely so the provider retries another worker
        # rather than starting a call this process is about to abandon.
        await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER)
        return

    await websocket.accept()

    settings = get_settings()
    serializer = _build_serializer(settings.telephony_provider)
    persist = not websocket.query_params.get("no_persist")
    repository = SqlCallRepository() if persist else NullCallRepository()
    session = CallSession(
        transport=_WebSocketTransport(websocket),
        serializer=serializer,
        repository=repository,
        direction=CallDirection.INBOUND,
        greeting_pcm=_GREETING_PCM,
        pipeline_factory=_build_pipeline,
        # Only for a call that was actually recorded. A simulator run has no
        # `calls` row, and a post-call job for one would crash-loop the queue.
        on_finished=_enqueue_postcall if persist else None,
    )

    task = asyncio.current_task()
    if task is not None:
        state.live_calls.add(task)
    try:
        await session.run()
    finally:
        if task is not None:
            state.live_calls.discard(task)
        # The socket is frequently already gone by this point -- a dropped GSM
        # line aborts the TCP connection with no close handshake, so both
        # Starlette's disconnect and a state RuntimeError are normal here.
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close()


class _WebSocketTransport:
    """Adapts a FastAPI WebSocket to the session's narrow transport protocol."""

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket

    async def send_text(self, data: str) -> None:
        await self._websocket.send_text(data)

    async def receive_text(self) -> str:
        try:
            return await self._websocket.receive_text()
        except WebSocketDisconnect as exc:
            # Translated at the boundary so the session stays free of any
            # web-framework import, and so a dropped line is classified as
            # interrupted rather than as a system failure (§11.4).
            raise TransportClosed(str(exc)) from exc
