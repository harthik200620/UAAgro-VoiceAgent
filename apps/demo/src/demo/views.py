"""The three pages, and the probes behind the first one."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
from flask import Blueprint, Response, abort, current_app, jsonify, render_template, request

from .config import DemoConfig

demo = Blueprint("demo", __name__)

#: The number the call page greets by. A seeded farmer, so a fresh stack
#: greets the operator by name and proves the lookup on the first call.
DEFAULT_CALLER = "+919993338278"


def config() -> DemoConfig:
    return current_app.config["DEMO"]  # type: ignore[no-any-return]


# --------------------------------------------------------------------------- #
# Probes
# --------------------------------------------------------------------------- #


def _probe(client: httpx.Client, url: str) -> dict[str, Any]:
    """One health endpoint: up, down, or up-but-not-ready, with what it said."""
    try:
        response = client.get(url)
    except httpx.HTTPError as exc:
        return {"ok": False, "status": None, "detail": type(exc).__name__, "body": None}
    body: Any = None
    if response.headers.get("content-type", "").startswith("application/json"):
        try:
            body = response.json()
        except ValueError:
            body = None
    return {
        "ok": response.status_code < 300,
        "status": response.status_code,
        "detail": None if response.status_code < 300 else f"HTTP {response.status_code}",
        "body": body,
    }


def probe_all(
    cfg: DemoConfig, *, transport: httpx.BaseTransport | None = None
) -> dict[str, dict[str, Any]]:
    """Every service the demo depends on, as the home page reports it.

    ``transport`` is for the tests, which hand in a mock instead of a network.
    """
    targets = {
        "worker": f"{cfg.worker_public_url}/health/live",
        "worker_ready": f"{cfg.worker_public_url}/health/ready",
        "api": f"{cfg.api_public_url}/health/ready",
    }
    # Side by side: three timeouts in a row would be four and a half seconds
    # of nothing on the page, and a stopped worker is the case worth
    # designing for. The client is thread-safe and shared.
    with (
        httpx.Client(timeout=cfg.probe_timeout_s, transport=transport) as client,
        ThreadPoolExecutor(max_workers=len(targets)) as pool,
    ):
        results = pool.map(lambda url: _probe(client, url), targets.values())
        return dict(zip(targets, results, strict=True))


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #


@demo.get("/")
def home() -> str:
    """The page renders at once; the browser asks ``/status.json`` for the probes."""
    return render_template("home.html", cfg=config())


@demo.get("/status.json")
def status() -> Response:
    return jsonify(probe_all(config()))


@demo.get("/call")
def call() -> str:
    """Talk to the agent, or answer a simulated outbound call as the farmer."""
    cfg = config()
    if not cfg.enabled:
        abort(404, description="The browser call page is development-only.")
    answering = _contact_id(request.args.get("answer"))
    return render_template(
        "call.html",
        cfg=cfg,
        socket=cfg.socket_url(),
        answering=answering,
        caller=DEFAULT_CALLER,
    )


def _contact_id(raw: str | None) -> str:
    """The contact to answer for, or empty. Anything that is not an id is
    dropped rather than echoed into the page."""
    if not raw:
        return ""
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        return ""


__all__ = ("DEFAULT_CALLER", "demo", "probe_all")
