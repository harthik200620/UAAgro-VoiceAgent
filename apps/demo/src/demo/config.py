"""What the demo needs to know, and nothing else.

A frozen record built once from the deployment's settings. The demo never
touches a database, a vendor or a key: it has the worker's public address,
the API's, the token the media socket expects, and whether this is a
development environment -- which decides whether the call page is served
at all, because that page embeds the token.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from uaagro_domain.settings import Settings

#: How long a status probe waits for a service before calling it down. A
#: demo page that hangs for thirty seconds on a stopped worker is worse than
#: one that says "down" in one and a half.
PROBE_TIMEOUT_S = 1.5


@dataclass(frozen=True, slots=True)
class DemoConfig:
    app_env: str
    #: Where a *browser* reaches the voice worker -- not the service name a
    #: container would use, the address the operator's machine can open.
    worker_public_url: str
    api_public_url: str
    ws_token: str | None
    probe_timeout_s: float = PROBE_TIMEOUT_S

    @classmethod
    def from_settings(cls, settings: Settings) -> DemoConfig:
        return cls(
            app_env=settings.app_env,
            worker_public_url=settings.voice_worker_public_url.rstrip("/"),
            api_public_url=settings.api_public_url.rstrip("/"),
            ws_token=settings.telephony_ws_token,
        )

    @property
    def enabled(self) -> bool:
        """The call page is development-only: it carries the socket token."""
        return self.app_env == "development"

    def socket_url(self) -> str:
        """The media socket, with the query the worker expects from a browser.

        The socket checks the token on every connection and this page is
        the one place that may hand it out -- opening the page is the whole
        setup. The alternative was the operator pasting a token into a query
        string and getting a bare 403 when they forgot.
        """
        scheme = "wss" if self.worker_public_url.startswith("https://") else "ws"
        host = self.worker_public_url.split("://", 1)[-1]
        query = "?source=browser"
        if self.ws_token:
            query += f"&token={quote(self.ws_token, safe='')}"
        return f"{scheme}://{host}/ws/voice{query}"


__all__ = ("PROBE_TIMEOUT_S", "DemoConfig")
