"""The Flask demo backend: a page, a probe, and a token that must not leak.

The demo is a client of the stack, never in it: these tests run with no
worker, no API and no network, handing the app a configuration and a mock
transport for the probes.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from flask import Flask
from flask.testing import FlaskClient

from demo import DemoConfig, create_app
from demo.views import probe_all

TOKEN = "tok/en with spaces"


def config(app_env: str = "development") -> DemoConfig:
    return DemoConfig(
        app_env=app_env,
        worker_public_url="http://worker.test:8080",
        api_public_url="https://api.test",
        ws_token=TOKEN,
        probe_timeout_s=0.2,
    )


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Flask:
    # /status.json probes the stack; a test must never wait on a socket.
    monkeypatch.setattr(
        "demo.views.probe_all",
        lambda cfg, transport=None: {
            "worker": {"ok": True, "status": 200, "detail": None, "body": None},
            "worker_ready": {
                "ok": False,
                "status": 503,
                "detail": "HTTP 503",
                "body": {"live_calls": 0},
            },
            "api": {"ok": True, "status": 200, "detail": None, "body": None},
        },
    )
    return create_app(config())


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


# --------------------------------------------------------------------------- #
# The socket address
# --------------------------------------------------------------------------- #


def test_the_socket_url_carries_the_scheme_and_the_encoded_token() -> None:
    assert config().socket_url() == (
        "ws://worker.test:8080/ws/voice?source=browser&token=tok%2Fen%20with%20spaces"
    )
    secure = DemoConfig(
        app_env="development",
        worker_public_url="https://worker.example",
        api_public_url="https://api.example",
        ws_token=None,
    )
    assert secure.socket_url() == "wss://worker.example/ws/voice?source=browser"


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #


def test_the_home_page_renders_without_waiting_on_a_probe(client: FlaskClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Voice worker" in html and "Control plane API" in html
    assert 'data-key="worker_ready"' in html and "checking" in html
    assert "http://worker.test:8080" in html
    assert "HTTP 503" not in html, "the probes belong to /status.json, not the render"


def test_the_call_page_embeds_the_socket_and_the_contact_being_answered(
    client: FlaskClient,
) -> None:
    contact = str(uuid.uuid4())
    page = client.get(f"/call?answer={contact}")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'data-socket="ws://worker.test:8080/ws/voice?source=browser&amp;token=' in html
    assert f'data-answer="{contact}"' in html
    assert "Pick up" in html
    assert "/static/call.js" in html


def test_a_contact_that_is_not_an_id_is_dropped_rather_than_echoed(client: FlaskClient) -> None:
    page = client.get("/call?answer=<script>alert(1)</script>")
    html = page.get_data(as_text=True)
    assert 'data-answer=""' in html
    assert "<script>alert" not in html


def test_the_call_page_is_development_only() -> None:
    client = create_app(config(app_env="production")).test_client()
    page = client.get("/call")
    assert page.status_code == 404
    assert TOKEN not in page.get_data(as_text=True)


def test_responses_carry_the_hardening_headers(client: FlaskClient) -> None:
    page = client.get("/")
    assert page.headers["X-Frame-Options"] == "DENY"
    assert page.headers["X-Content-Type-Options"] == "nosniff"
    assert page.headers["Cache-Control"] == "no-store"


# --------------------------------------------------------------------------- #
# Probes
# --------------------------------------------------------------------------- #


def test_probes_report_up_down_and_not_ready_from_the_real_endpoints() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/live":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/health/ready" and request.url.host == "worker.test":
            return httpx.Response(503, json={"status": "warming", "live_calls": 2})
        raise httpx.ConnectError("refused", request=request)

    status = probe_all(config(), transport=httpx.MockTransport(respond))
    assert status["worker"] == {"ok": True, "status": 200, "detail": None, "body": {"status": "ok"}}
    assert status["worker_ready"]["ok"] is False
    assert status["worker_ready"]["body"] == {"status": "warming", "live_calls": 2}
    assert status["api"] == {"ok": False, "status": None, "detail": "ConnectError", "body": None}


def test_the_status_endpoint_is_the_same_probes_as_json(client: FlaskClient) -> None:
    payload = json.loads(client.get("/status.json").get_data(as_text=True))
    assert set(payload) == {"worker", "worker_ready", "api"}
    assert payload["worker"]["ok"] is True
