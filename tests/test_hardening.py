"""The hardening pass of 5 September 2026 (§17).

What an operator can type into the panel must not become a way for the
server to reach its own network, and what a client can send must not be a
way to exhaust it. Each test here is one such door, closed.
"""

from __future__ import annotations

import ipaddress
from typing import Any

import httpx
import pytest

from uaagro_domain import netsafety
from uaagro_domain.errors import ValidationError

# --------------------------------------------------------------------------- #
# Addresses a server-side fetch may reach
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "169.254.169.254",
        "10.0.0.5",
        "172.16.3.4",
        "192.168.1.9",
        "0.0.0.0",
        "fd00::1",
    ],
)
def test_addresses_inside_a_deployment_are_not_public(address: str) -> None:
    assert not netsafety.is_public(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "2606:4700::1111"])
def test_public_addresses_are_public(address: str) -> None:
    assert netsafety.is_public(ipaddress.ip_address(address))


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "postgres",
        "redis",
        "minio",
        "metadata.google.internal",
        "169.254.169.254",
        "127.0.0.1",
        "[::1]",
    ],
)
def test_the_stack_and_the_metadata_endpoint_are_refused_by_name_or_address(host: str) -> None:
    with pytest.raises(ValidationError):
        netsafety.ensure_reachable_target(host, purpose="a test")


def test_a_private_network_is_refused_unless_the_deployment_allows_it() -> None:
    with pytest.raises(ValidationError) as caught:
        netsafety.ensure_reachable_target("10.20.30.40", purpose="the data source connection")
    assert "SOURCES_ALLOW_PRIVATE_NETWORKS" in (caught.value.remedy or "")
    netsafety.ensure_reachable_target(
        "10.20.30.40", purpose="the data source connection", allow_private_networks=True
    )
    # The host itself and the metadata range stay refused whatever the flag says.
    for host in ("127.0.0.1", "169.254.169.254"):
        with pytest.raises(ValidationError):
            netsafety.ensure_reachable_target(host, purpose="x", allow_private_networks=True)


def test_a_refusal_never_names_the_address_it_resolved_to(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(netsafety, "resolve", lambda host: [ipaddress.ip_address("10.9.8.7")])
    with pytest.raises(ValidationError) as caught:
        netsafety.ensure_reachable_target("db.client.example", purpose="a test")
    assert "10.9.8.7" not in caught.value.message
    assert "10.9.8.7" not in (caught.value.remedy or "")


# --------------------------------------------------------------------------- #
# The knowledge crawler
# --------------------------------------------------------------------------- #


def _crawler_client(pages: dict[str, httpx.Response]) -> httpx.AsyncClient:
    def respond(request: httpx.Request) -> httpx.Response:
        return pages.get(str(request.url), httpx.Response(404))

    return httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=False)


async def test_the_crawler_refuses_an_address_inside_the_deployment() -> None:
    from voice_worker.knowledge.extract import fetch_site

    for url in (
        "http://127.0.0.1:8000/",
        "http://169.254.169.254/latest/meta-data/",
        "http://postgres:5432/",
    ):
        with pytest.raises(ValidationError):
            await fetch_site(url, client=_crawler_client({}))


async def test_a_redirect_into_the_deployment_is_dropped_not_followed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from voice_worker.knowledge import extract

    # The site itself is public; the second page redirects to the metadata
    # endpoint. The first page must be read, the redirect must not be.
    monkeypatch.setattr(
        extract,
        "ensure_reachable_target",
        lambda host, **_: (
            (_ for _ in ()).throw(ValidationError("no", remedy=""))
            if host.startswith("169.254")
            else None
        ),
    )
    html = (
        b"<html><title>Shop</title><body><p>Urea 266 rupees</p><a href='/go'>go</a></body></html>"
    )
    pages = {
        "https://shop.example/": httpx.Response(
            200, content=html, headers={"content-type": "text/html"}
        ),
        "https://shop.example/go": httpx.Response(
            302, headers={"location": "http://169.254.169.254/"}
        ),
    }
    fetched: list[str] = []
    transport = httpx.MockTransport(
        lambda request: (
            fetched.append(str(request.url)),
            pages.get(str(request.url), httpx.Response(404)),
        )[1]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        result = await extract.fetch_site("https://shop.example/", client=client)
    assert "Urea 266" in result.markdown
    assert "http://169.254.169.254/" not in fetched, "the redirect target was fetched"


async def test_a_page_is_read_only_up_to_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from voice_worker.knowledge import extract

    monkeypatch.setattr(extract, "ensure_reachable_target", lambda host, **_: None)
    monkeypatch.setattr(extract, "MAX_PAGE_BYTES", 200)
    body = b"<html><title>Big</title><body><p>" + b"x" * 5000 + b"</p></body></html>"
    pages = {
        "https://shop.example/": httpx.Response(
            200, content=body, headers={"content-type": "text/html"}
        )
    }
    async with _crawler_client(pages) as client:
        result = await extract.fetch_site("https://shop.example/", client=client)
    assert len(result.markdown) < 1000


# --------------------------------------------------------------------------- #
# The API: request ceilings
# --------------------------------------------------------------------------- #


@pytest.fixture
async def api() -> Any:
    from api.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


async def test_an_oversized_body_is_refused_before_any_route_reads_it(
    api: httpx.AsyncClient,
) -> None:
    from api.main import MAX_REQUEST_BYTES

    response = await api.post(
        "/auth/login",
        content=b"{}",
        headers={"content-length": str(MAX_REQUEST_BYTES + 1), "content-type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_a_body_with_no_declared_length_is_refused(api: httpx.AsyncClient) -> None:
    async def chunks() -> Any:
        yield b"{}"

    response = await api.post(
        "/auth/login", content=chunks(), headers={"content-type": "application/json"}
    )
    assert response.status_code == 411


async def test_the_probes_are_never_rate_limited(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api import main
    from uaagro_domain.errors import RateLimitedError

    async def always_limited(scope: str, identity: str, limit: Any) -> None:
        raise RateLimitedError(retry_after_s=7)

    from uaagro_domain.settings import reset_caches

    monkeypatch.setattr(main.ratelimit, "check", always_limited)
    # The suite turns the ceiling off; this test turns it back on.
    monkeypatch.setenv("API_RATE_LIMIT_PER_MINUTE", "600")
    reset_caches()
    try:
        limited = await api.get("/admin/overview")
        probe = await api.get("/health/live")
    finally:
        monkeypatch.undo()
        reset_caches()
    assert limited.status_code == 429
    assert limited.headers.get("retry-after") == "7"
    assert probe.status_code != 429


async def test_the_data_source_connector_refuses_the_deployment_itself_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services.sources import mysql
    from uaagro_domain.settings import reset_caches

    monkeypatch.setenv("APP_ENV", "production")
    reset_caches()
    try:
        spec = mysql.ConnectionSpec(host="127.0.0.1", port=3306, database="d", user="u")
        with pytest.raises(ValidationError):
            async with mysql.connect(spec):
                pass
    finally:
        monkeypatch.undo()
        reset_caches()
