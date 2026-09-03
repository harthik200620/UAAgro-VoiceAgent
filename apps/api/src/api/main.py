"""FastAPI control plane.

Deliberately not in the audio path (§4.1): a slow admin query here can never add
latency to a live call, because the voice worker talks to Postgres through its
own pool with its own statement timeout.

Every response carries the §17 security headers and ``Cache-Control:
no-store`` -- nothing this service answers is public, and nothing between it
and the panel may keep a copy -- every request carries a correlation id, and
every domain error becomes a typed envelope naming what to do next (§22).

The interactive documentation exists in development only. It is a map of the
service, and outside a laptop the map is for the people who wrote it.
"""

from __future__ import annotations

import contextlib
import hmac
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import structlog
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.cors import CORSMiddleware

from uaagro_db.engine import dispose_engines
from uaagro_domain.errors import AuthorizationError, UAAgroError
from uaagro_domain.eventloop import install_fast_event_loop
from uaagro_domain.logging import configure_logging
from uaagro_domain.settings import Settings, get_defaults, get_settings
from uaagro_domain.telemetry import INSTRUMENTS

from .routers import (
    auth,
    health,
    panel_calls,
    panel_campaigns,
    panel_centres,
    panel_data,
    panel_dial,
    panel_flows,
    panel_knowledge,
    panel_live,
    panel_overview,
    panel_sources,
)
from .security.deps import SettingsDep
from .security.ratelimit import close_redis
from .services.jobs import close_jobs
from .services.sources import sync as sources_sync
from .services.worker_client import close_worker_client

log = structlog.get_logger(__name__)

#: §17 response headers. CSP is intentionally strict: this API serves JSON, so
#: nothing needs to be loaded or framed from it at all.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cross-Origin-Resource-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Before anything opens a socket. Uvicorn usually installs this
    # itself; calling it here makes the dependency ours and explicit,
    # and covers the paths that do not run under uvicorn at all.
    install_fast_event_loop()
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    settings.verify_production_readiness()
    INSTRUMENTS.setup(
        service_name="uaagro-api",
        endpoint=settings.otel_exporter_otlp_endpoint or None,
    )
    defaults = get_defaults()
    log.info(
        "api.started",
        env=settings.app_env,
        org=defaults.identity.org_name,
        languages=len(defaults.language_routes),
    )
    try:
        yield
    finally:
        await close_worker_client()
        await close_jobs()
        await close_redis()
        # An in-flight pull from the client's database is cancelled and its
        # run marked failed, rather than left "running" for ever.
        await sources_sync.shutdown()
        await dispose_engines()
        log.info("api.stopped")


_metrics_router = APIRouter(tags=["ops"])


def _internal_caller(request: Request, settings: Settings) -> bool:
    """Whether the request carries the token the services share.

    Closed when no token is configured, for the same reason the voice
    worker's ``/internal`` routes are: an exposition that names every route
    and its error rate is a map of the service for whoever asks.
    """
    expected = settings.internal_api_token
    presented = request.headers.get("x-internal-token")
    if not expected or presented is None:
        return False
    return hmac.compare_digest(presented, expected)


@_metrics_router.get("/metrics")
async def metrics(request: Request, settings: SettingsDep) -> Response:
    """§19's Prometheus scrape target for the control plane.

    Open on a laptop; everywhere else the scraper presents the internal token.
    """
    if settings.app_env != "development" and not _internal_caller(request, settings):
        raise AuthorizationError(action="scrape", resource="/metrics")
    body, content_type = INSTRUMENTS.exposition()
    return Response(content=body, media_type=content_type)


_DEVELOPMENT = get_settings().app_env == "development"

app = FastAPI(
    title="UA Agro control plane",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _DEVELOPMENT else None,
    redoc_url=None,
    openapi_url="/openapi.json" if _DEVELOPMENT else None,
)

# The admin app is served from its own origin and talks to this API server-side,
# so no browser origin needs cross-origin access. Left explicit rather than
# absent so a future change is a deliberate edit.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["authorization", "content-type", "x-csrf-token", "idempotency-key"],
)


def _forbid_caching(response: Response) -> None:
    """``no-store`` on every answer, keeping any stricter instruction a route set.

    A recording says ``private, no-store`` and an event stream says
    ``no-cache, no-transform``; both keep their words and gain the one that
    matters to a proxy deciding whether to keep a copy.
    """
    existing = response.headers.get("cache-control")
    if existing is None:
        response.headers["cache-control"] = "no-store"
    elif "no-store" not in existing.lower():
        response.headers["cache-control"] = f"no-store, {existing}"


@app.middleware("http")
async def request_context(
    request: Request, call_next: Callable[[Request], Awaitable[JSONResponse]]
) -> JSONResponse:
    """Attach a correlation id and the security headers to every response."""
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    structlog.contextvars.bind_contextvars(request_id=request_id, path=request.url.path)
    try:
        response = await call_next(request)
    finally:
        structlog.contextvars.unbind_contextvars("request_id", "path")
    response.headers["x-request-id"] = request_id
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    _forbid_caching(response)
    if get_settings().is_production:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


@app.exception_handler(UAAgroError)
async def domain_error_handler(_: Request, exc: UAAgroError) -> JSONResponse:
    """Render a typed error, including what the operator should do next (§22)."""
    if exc.http_status >= 500:
        log.error("api.error", code=exc.code, message=exc.message)
    else:
        log.info("api.rejected", code=exc.code)
    return JSONResponse(status_code=exc.http_status, content={"error": exc.to_dict()})


@app.exception_handler(SQLAlchemyError)
async def database_error_handler(_: Request, exc: SQLAlchemyError) -> JSONResponse:
    """Turn a database outage into an actionable 503, not a stack trace.

    §22 requires typed, actionable errors. A raw traceback also leaks the DSN
    and the schema to whoever triggered it, which a connection refusal from an
    unauthenticated endpoint would otherwise hand out freely.
    """
    log.error("api.database_unavailable", error=type(exc).__name__)
    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "code": "database_unavailable",
                "message": "The database is not reachable.",
                "remedy": "Check that Postgres is running and DATABASE_URL is correct. "
                "Locally: `make dev`. In production, check the RDS health alarm.",
                "context": {},
            }
        },
    )


app.include_router(health.router)
app.include_router(auth.router)
for panel in (
    panel_overview,
    panel_live,
    panel_calls,
    panel_campaigns,
    panel_dial,
    panel_flows,
    panel_knowledge,
    panel_centres,
    panel_data,
    panel_sources,
):
    app.include_router(panel.router)
app.include_router(_metrics_router)
