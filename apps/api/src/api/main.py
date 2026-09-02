"""FastAPI control plane.

Deliberately not in the audio path (§4.1): a slow admin query here can never add
latency to a live call, because the voice worker talks to Postgres through its
own pool with its own statement timeout.

Every response carries the §17 security headers, every request carries a
correlation id, and every domain error becomes a typed envelope naming what to
do next (§22).
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import structlog
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.cors import CORSMiddleware

from uaagro_db.engine import dispose_engines
from uaagro_domain.errors import UAAgroError
from uaagro_domain.eventloop import install_fast_event_loop
from uaagro_domain.logging import configure_logging
from uaagro_domain.settings import get_defaults, get_settings
from uaagro_domain.telemetry import INSTRUMENTS

from .routers import admin, auth, health
from .security.ratelimit import close_redis

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
        await close_redis()
        await dispose_engines()
        log.info("api.stopped")


_metrics_router = APIRouter(tags=["ops"])


@_metrics_router.get("/metrics")
async def metrics() -> Response:
    """§19's Prometheus scrape target for the control plane."""
    body, content_type = INSTRUMENTS.exposition()
    return Response(content=body, media_type=content_type)


app = FastAPI(
    title="UA Agro control plane",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    openapi_url="/openapi.json",
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
app.include_router(admin.router)

app.include_router(_metrics_router)
