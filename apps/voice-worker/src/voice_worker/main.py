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
import hmac
import ipaddress
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import structlog
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse
from sqlalchemy import select

from uaagro_db.engine import dispose_engines, incall_session
from uaagro_db.models import Organization
from uaagro_domain.enums import CallDirection, FlowType, TelephonyProvider
from uaagro_domain.eventloop import install_fast_event_loop
from uaagro_domain.livefeed import LiveFeed, NullLiveFeed, RedisLiveFeed
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
from .knowledge.embeddings import E5Embedder
from .knowledge.retrieval import HybridRetriever
from .runtime import audio as audio_utils
from .runtime.assembly import (
    Caller,
    CallPipeline,
    build_agent,
    build_call_pipeline,
    find_outbound_contact,
    load_agent_settings,
)
from .runtime.audio_cache import AudioCache
from .runtime.audio_prewarm import prewarm
from .runtime.direction import OurNumbers
from .runtime.repository import NullCallRepository, SqlCallRepository
from .runtime.session import CallSession, TransportClosed
from .text.catalogue_lexicon import load_lexicon
from .text.lexicon import Lexicon
from .text.speech import text_for_speech
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
        #: Where every call's turns, activity and end are announced for the
        #: panel's live view (§15.1). A null feed until the lifespan has a
        #: Redis URL to give it.
        self.live_feed: LiveFeed = NullLiveFeed()
        #: Tier-2 retrieval with the dense half loaded once per process. The
        #: registry's `search_knowledge` tool and the panel's "try a question"
        #: share it, so both search the same corpus the same way.
        self.retriever: HybridRetriever | None = None
        self.embed_task: asyncio.Task[None] | None = None

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
            raise NotImplementedError(f"No serializer is implemented for {provider.value}.")


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


async def _warm_embedder(retriever: HybridRetriever) -> None:
    """Load the embedding model off the startup path."""
    embedder = retriever.embedder
    if embedder is None:
        return
    try:
        unavailable = await embedder.warm()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("worker.embedder_failed", error=type(exc).__name__)
        retriever.dense_unavailable_reason = type(exc).__name__
        return
    if unavailable is not None:
        retriever.dense_unavailable_reason = unavailable.reason
        return
    log.info("worker.embedder_ready")


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

    # The dense half of retrieval (§9). The model weights load in the
    # background for the same reason the audio pre-warm does: a worker that
    # waits on a 1.1 GB model at boot is a worker that cannot deploy, and
    # BM25 serves callers in the meantime, saying it is degraded.
    state.retriever = HybridRetriever(embedder=E5Embedder(threads=2))
    state.embed_task = asyncio.create_task(_warm_embedder(state.retriever))
    state.registry = build_registry(lexicon=state.lexicon, retriever=state.retriever)

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
    state.live_feed = RedisLiveFeed(settings.redis_url)
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
        for background in (state.prewarm_task, state.embed_task):
            if background is not None:
                background.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await background
        if state.live_calls:
            log.info("worker.draining", live_calls=state.concurrency)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*state.live_calls, return_exceptions=True),
                    timeout=DRAIN_TIMEOUT_S,
                )
        await state.live_feed.close()
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


@app.api_route("/telephony/twiml", methods=["GET", "POST"])
async def telephony_twiml(request: Request) -> Response:
    """The document Twilio fetches when a call we placed is answered.

    Twilio accepts either a TwiML document inline on the dial request or a URL
    to fetch one from. Inline is the tidier of the two -- nothing to host,
    nothing to keep in sync -- but a **trial account refuses it**: every
    request carrying `Twiml` comes back
    *"Invalid or disallowed parameters provided"*. So the document is served
    here instead, which is the arrangement Exotel and Plivo already use and
    which works on every Twilio account.

    Guarded by the same shared secret as the media socket. Without it, anyone
    who found this address would be handed a stream URL with the socket's
    token in it, which is the one thing on this worker worth stealing.
    """
    settings = get_settings()
    expected = settings.telephony_ws_token
    if expected:
        presented = request.query_params.get("token") or request.headers.get("x-auth-token")
        if presented is None or not hmac.compare_digest(presented, expected):
            log.warning("worker.twiml_rejected", reason="token")
            return Response(status_code=status.HTTP_403_FORBIDDEN)

    from .adapters.telephony.control import stream_twiml

    # The campaign contact, opaque and ours: `contact:<uuid>` or `test:<uuid>`.
    # Never a phone number -- §23-6 has no exception for a query string.
    reference = request.query_params.get("contact")
    document = stream_twiml(settings, reference if reference else None)
    log.info("worker.twiml_served", referenced=bool(reference))
    return Response(content=document, media_type="application/xml")


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
    return hmac.compare_digest(presented, expected)


# --------------------------------------------------------------------------- #
# The control plane's door (§15.1)
# --------------------------------------------------------------------------- #


def _internal_caller_allowed(request: Request) -> bool:
    """The shared secret between the API and this worker.

    Closed when no token is configured: these endpoints run retrieval and
    synthesis on the caller's behalf, and a worker on a reachable port with
    them open would be a free vendor account for whoever found it.
    """
    expected = get_settings().internal_api_token
    if not expected:
        return False
    presented = request.headers.get("x-internal-token")
    return presented is not None and hmac.compare_digest(presented, expected)


@app.post("/internal/knowledge/search")
async def internal_knowledge_search(request: Request) -> JSONResponse:
    """The panel's "try a question": what retrieval finds, and optionally what
    the agent would say.

    The same retriever and the same agent the phone path uses, so the panel
    is a window on the real thing rather than a demo of a similar one. The
    answer is generated only when asked, because it is a model call the
    operator pays for.
    """
    if not _internal_caller_allowed(request):
        return JSONResponse({"error": "forbidden"}, status_code=status.HTTP_403_FORBIDDEN)
    body = await request.json()
    question = str(body.get("question") or "").strip()
    language = str(body.get("language") or "hi")
    want_answer = bool(body.get("answer"))
    if not question:
        return JSONResponse({"error": "question is empty"}, status_code=422)
    # Which side of the panel is asking. The answer differs, because a
    # document can be marked for one direction, and an operator testing the
    # offer script should be shown what an offer call would actually find.
    direction = str(body.get("direction") or "inbound")
    retriever = state.retriever or HybridRetriever()

    started = time.perf_counter()
    async with incall_session() as db:
        found = await retriever.search(db, question, language=language, scope=direction)
    retrieval_ms = round((time.perf_counter() - started) * 1000)

    payload: dict[str, Any] = {
        "passages": [
            {
                "documentTitle": chunk.document_title,
                "section": chunk.section_path or None,
                "snippet": chunk.content[:600],
                "score": round(chunk.score, 3),
                "containsDose": chunk.contains_dose,
            }
            for chunk in found.chunks
        ],
        "retrievalMs": retrieval_ms,
        "degraded": found.degraded,
        "degradedReason": found.degraded_reason,
        "answer": None,
    }
    if want_answer:
        payload["answer"] = await _answer_for_panel(question, language, direction)
    return JSONResponse(payload)


async def _answer_for_panel(question: str, language: str, direction: str) -> dict[str, Any]:
    """Run one agent turn the way a call would, and time it."""
    settings = get_settings()
    defaults = get_defaults()
    registry = state.registry or build_registry(lexicon=state.lexicon)
    organization_id = await _resolve_organization()
    if organization_id is None:
        return {"text": None, "totalMs": None, "note": "No organisation is seeded."}
    started = time.perf_counter()
    async with incall_session() as db:
        try:
            agent_settings = await load_agent_settings(
                db, organization_id=organization_id, flow_type=FlowType.INBOUND
            )
        except Exception as exc:
            return {"text": None, "totalMs": None, "note": str(getattr(exc, "message", exc))}
    agent = build_agent(
        registry=registry,
        persona=agent_settings.system_prompt,
        gateway=build_gateway(settings),
        caller=Caller(language=language or defaults.default_language),
        call_id=uuid.uuid4(),
        direction=direction,
    )
    try:
        result = await agent.handle(question)
    except Exception as exc:
        log.warning("worker.panel_answer_failed", error=type(exc).__name__)
        return {
            "text": None,
            "totalMs": None,
            "note": f"The model did not answer ({type(exc).__name__}).",
        }
    note = None
    if result.escalation is not None and getattr(result.escalation, "should_transfer", False):
        note = "On a call this would be handed to a person."
    return {
        "text": result.text,
        "totalMs": round((time.perf_counter() - started) * 1000),
        "note": note,
    }


@app.post("/internal/speech/preview")
async def internal_speech_preview(request: Request) -> JSONResponse:
    """How a line will be spoken, without spending a vendor call.

    The normaliser that runs before every synthesised sentence (§5.3) is
    applied to the operator's text, and each token that changed is reported
    as a substitution -- "₹50" became "पचास रुपये" -- so the panel can show
    what the farmer will hear.
    """
    if not _internal_caller_allowed(request):
        return JSONResponse({"error": "forbidden"}, status_code=status.HTTP_403_FORBIDDEN)
    body = await request.json()
    text = " ".join(str(body.get("text") or "").split())
    language = str(body.get("language") or get_defaults().default_language)
    spoken = text_for_speech(text, language=language) if text else ""
    return JSONResponse(
        {
            "spoken": spoken,
            "words": len(spoken.split()),
            # Hindi at a helpline pace runs about 2.3 words a second.
            "seconds": round(len(spoken.split()) / 2.3, 1),
            "substitutions": _substitutions(text, spoken),
        }
    )


def _substitutions(original: str, spoken: str) -> list[dict[str, str]]:
    """Tokens the normaliser rewrote, paired with what replaced them.

    A token-level comparison rather than a diff library: the normaliser only
    ever expands a token into more words, so walking both lists and pairing
    an unchanged token with itself is enough to attribute every change.
    """
    pairs: list[dict[str, str]] = []
    before = original.split()
    after = spoken.split()
    j = 0
    for i, token in enumerate(before):
        if j < len(after) and after[j] == token:
            j += 1
            continue
        # Find where the original resumes; everything in between replaced it.
        resume = None
        for k in range(i + 1, len(before)):
            if before[k] in after[j:]:
                resume = after.index(before[k], j)
                break
        replacement = " ".join(after[j:resume] if resume is not None else after[j:])
        if replacement and replacement != token:
            pairs.append({"from": token, "to": replacement})
        j = resume if resume is not None else len(after)
    return pairs


@app.post("/internal/test-call")
async def internal_test_call(request: Request) -> JSONResponse:
    """Place a call to the operator with a draft script (§15.1).

    The number typed in the panel is the approved destination for this one
    call -- an operator ringing their own phone -- and the draft's id rides in
    the custom field so the media path speaks that version rather than the
    published one.
    """
    if not _internal_caller_allowed(request):
        return JSONResponse({"error": "forbidden"}, status_code=status.HTTP_403_FORBIDDEN)
    body = await request.json()
    phone = str(body.get("phone") or "").strip()
    config_id = str(body.get("configId") or "").strip()
    if not phone or not config_id:
        return JSONResponse({"error": "phone and configId are required"}, status_code=422)

    from uaagro_domain.phone import normalise_msisdn

    from .adapters.telephony.control import build_adapter

    settings = get_settings()
    try:
        destination = normalise_msisdn(phone).e164
        # Twilio deployments have one number and it is set as the promotional
        # caller ID; asking for a separate transactional one would refuse a
        # test call on an account that is otherwise ready to place it.
        caller_id = (
            settings.outbound_cli_transactional
            or settings.twilio_from_number
            or settings.require("outbound_cli_promotional", needed_for="placing a test call")
        )
        adapter = build_adapter(settings, approved=frozenset({destination}))
        sid = await adapter.originate(
            to=destination,
            from_=caller_id,
            callback_url=f"{settings.public_base_url}/ws/voice",
            custom_field=f"test:{config_id}",
        )
    except Exception as exc:
        message = getattr(exc, "message", str(exc))
        remedy = getattr(exc, "remedy", None)
        log.warning("worker.test_call_failed", error=type(exc).__name__)
        return JSONResponse(
            {"error": message, "remedy": remedy, "code": "telephony_unconfigured"},
            status_code=503,
        )
    return JSONResponse({"callSid": sid})


@app.post("/internal/speech")
async def internal_speech(request: Request) -> Response:
    """Render one line in the configured voice, for the script preview.

    Returns a WAV so a browser can play it directly. Goes through the same
    normaliser and the same cache as a live call, so what the operator hears
    is what the farmer will.
    """
    if not _internal_caller_allowed(request):
        return JSONResponse({"error": "forbidden"}, status_code=status.HTTP_403_FORBIDDEN)
    body = await request.json()
    text = str(body.get("text") or "").strip()
    language = str(body.get("language") or get_defaults().default_language)
    if not text:
        return JSONResponse({"error": "text is empty"}, status_code=422)

    settings = get_settings()
    defaults = get_defaults()
    stack = build_speech_stack(language, settings, defaults)
    try:
        spoken = text_for_speech(text, language=stack.tts_config.language)
        cached = await state.audio_cache.get(spoken, stack.tts_config, provider=stack.tts.provider)
        pcm = cached
        if pcm is None:
            pcm = await stack.tts.synthesise_all(spoken, stack.tts_config)
            if pcm:
                await state.audio_cache.put(
                    spoken, stack.tts_config, pcm, provider=stack.tts.provider
                )
    finally:
        with contextlib.suppress(Exception):
            await stack.tts.close()
    if not pcm:
        return JSONResponse({"error": "the voice returned no audio"}, status_code=502)
    return Response(
        content=audio_utils.write_wav(pcm),
        media_type="audio/wav",
        headers={"x-spoken-text": spoken.encode("ascii", "backslashreplace").decode("ascii")},
    )


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
        contact = None
        if session.direction is CallDirection.OUTBOUND:
            contact = await find_outbound_contact(
                db, contact_id=session.contact_id, phone_hash=session.farmer_number_hash
            )
            if contact is None:
                # A call we placed for a contact we cannot find is a call we
                # cannot script. Named loudly: it means the dialer and the
                # media path disagree, which is a bug, not a farmer's fault.
                log.error("worker.outbound_contact_unknown")
        return await build_call_pipeline(
            call_id=session.call_id,
            organization_id=organization_id,
            phone_hash=session.farmer_number_hash,
            session=db,
            registry=registry,
            settings=settings,
            defaults=defaults,
            send_frame=session.send_audio,
            clear_playback=session.clear_playback,
            lexicon=state.lexicon,
            cache=state.audio_cache,
            direction=session.direction,
            contact=contact,
            live_feed=state.live_feed,
            config_id=session.config_id,
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


def _transfer_adapter(approved: frozenset[str]) -> Any:
    """Call control for one hand-over (§12.3), built when it is needed.

    Built per transfer rather than at boot so a worker without telephony
    credentials still answers calls; the missing variable surfaces as a
    failed hand-over -- and a callback commitment -- not a worker that will
    not start.
    """
    from .adapters.telephony.control import build_adapter

    return build_adapter(get_settings(), approved)


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
        # Provisional. The start frame decides for real (see runtime.direction).
        direction=CallDirection.INBOUND,
        greeting_pcm=_GREETING_PCM,
        pipeline_factory=_build_pipeline,
        # Only for a call that was actually recorded. A simulator run has no
        # `calls` row, and a post-call job for one would crash-loop the queue.
        on_finished=_enqueue_postcall if persist else None,
        live_feed=state.live_feed,
        our_numbers=OurNumbers.from_settings(settings),
        transfer_adapter=_transfer_adapter,
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
