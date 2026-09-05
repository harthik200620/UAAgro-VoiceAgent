"""The control plane's door into the voice worker (§15.1).

Three panel features need what only the media service has warm: the retriever
with its embedding model, the synthesiser, and the telephony adapter. Loading
any of those into the API would double the memory of the control plane and
put a gigabyte model next to the login endpoint; the worker already has them,
so the API asks it over a token-protected internal HTTP surface.

Failures are typed for the panel: a worker that is down or a token that is
missing becomes a 503 with a remedy naming the variable, never a stack trace.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from uaagro_domain.errors import ConfigurationError, VendorError
from uaagro_domain.settings import Settings, get_settings

log = structlog.get_logger(__name__)

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=3.0))
    return _client


async def close_worker_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


class WorkerClient:
    """Typed calls to ``/internal/*`` on the voice worker."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _headers(self) -> dict[str, str]:
        token = self.settings.internal_api_token
        if not token:
            raise ConfigurationError(
                "INTERNAL_API_TOKEN is not set.",
                remedy="Set the same INTERNAL_API_TOKEN on the API and the voice worker; "
                "the panel's question preview, voice preview and test calls go "
                "through it.",
            )
        return {"x-internal-token": token}

    async def _post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        url = f"{self.settings.voice_worker_url.rstrip('/')}{path}"
        try:
            response = await _http().post(url, json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            log.warning("worker_client.unreachable", path=path, error=type(exc).__name__)
            raise VendorError(
                "The voice worker is not reachable from the control plane.",
                remedy="Check VOICE_WORKER_URL and that the voice-worker service is running.",
            ) from exc
        if response.status_code == 403:
            raise ConfigurationError(
                "The voice worker refused the control plane's token.",
                remedy="INTERNAL_API_TOKEN must be identical on the API and the voice worker.",
            )
        return response

    async def search(
        self, question: str, *, language: str, answer: bool, direction: str = "inbound"
    ) -> dict[str, Any]:
        response = await self._post(
            "/internal/knowledge/search",
            {
                "question": question,
                "language": language,
                "answer": answer,
                "direction": direction,
            },
        )
        if response.status_code >= 400:
            raise VendorError(
                "The voice worker could not search the knowledge base.",
                remedy=_remedy_of(response),
            )
        return dict(response.json())

    async def preview(self, text: str, *, language: str) -> dict[str, Any]:
        response = await self._post(
            "/internal/speech/preview", {"text": text, "language": language}
        )
        if response.status_code >= 400:
            raise VendorError(
                "The voice worker could not render the preview.", remedy=_remedy_of(response)
            )
        return dict(response.json())

    async def speech(self, text: str, *, language: str) -> bytes:
        response = await self._post("/internal/speech", {"text": text, "language": language})
        if response.status_code >= 400:
            raise VendorError("The voice did not return audio.", remedy=_remedy_of(response))
        return response.content

    async def test_call(self, *, phone: str, config_id: str) -> str:
        response = await self._post("/internal/test-call", {"phone": phone, "configId": config_id})
        if response.status_code >= 400:
            raise VendorError("The test call could not be placed.", remedy=_remedy_of(response))
        return str(response.json().get("callSid") or "")


def _remedy_of(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"The voice worker answered {response.status_code}."
    remedy = payload.get("remedy") or payload.get("error")
    return str(remedy) if remedy else f"The voice worker answered {response.status_code}."


__all__ = ("WorkerClient", "close_worker_client")
