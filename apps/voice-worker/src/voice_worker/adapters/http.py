"""One HTTP client policy for every vendor on the voice path (§7, §7.5).

Measured on the configured endpoints, 2 September 2026:

===================  ==========  ==========  ===========
                     cold        warm        saving
===================  ==========  ==========  ===========
LLM first token      1519 ms     1251 ms     268 ms
TTS first byte        741 ms      303 ms     438 ms
===================  ==========  ==========  ===========

"Cold" is not an unusual state. ``httpx`` expires a pooled connection after
**five seconds** of inactivity by default, and the gap between one turn's
request and the next is however long a farmer takes to think, hear the answer
and reply -- reliably longer than five seconds. So the default is not "reuse the
connection", it is "reuse it only inside a single burst", and a helpline pays a
fresh DNS lookup, TCP handshake and TLS negotiation on almost every turn.

That is 700 ms per turn spread across two vendors, for nothing. §7 budgets the
*whole* turn 1,200 ms at p95.

So connections are held for the length of a call and beyond, and the pool is
opened before the first caller arrives rather than by them. Both numbers here
are deliberately generous: an idle TLS session costs a socket, and a socket is
much cheaper than a handshake inside somebody's turn.
"""

from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: How long an idle connection is kept. Longer than any plausible gap between
#: two turns of one call, and longer than the gap between two calls on a busy
#: helpline -- which is the point: the second caller should not pay for the
#: first one hanging up.
KEEPALIVE_EXPIRY_S = 600.0

#: Enough for concurrent calls on one worker without unbounded growth. §7.6
#: caps a worker's concurrency well below this.
MAX_KEEPALIVE_CONNECTIONS = 32
MAX_CONNECTIONS = 64

#: A connect that takes this long has already spent more than §7 allows the
#: entire first token. Failing fast and letting the ladder move on beats
#: waiting on a host that is not answering.
CONNECT_TIMEOUT_S = 2.0


#: One client per endpoint, for the life of the process.
#:
#: Pooling inside a client is worthless if the client itself is short-lived,
#: and these adapters are built **per call**: `build_speech_stack` and
#: `build_gateway` both run inside `build_call_pipeline`. A per-instance client
#: means a per-call pool, which means every call re-handshakes and the keepalive
#: settings above never do anything. So the pool is held here instead, keyed by
#: where it connects to.
_SHARED: dict[str, Any] = {}


def build_client(
    *,
    base_url: str,
    headers: dict[str, str],
    total_timeout_s: float,
    connect_timeout_s: float = CONNECT_TIMEOUT_S,
    shared: bool = True,
) -> Any:
    """An ``httpx.AsyncClient`` tuned for a live phone call.

    Shared per endpoint by default, so a connection warmed by one call is still
    open for the next. The returned client is therefore **not owned by the
    caller**: closing it would take the connection away from every other call in
    flight. Adapters leave it alone and :func:`close_shared_clients` disposes of
    them at shutdown.

    ``shared=False`` returns a private client, for the rare caller that really
    does own its connection.

    ``httpx`` is imported lazily so constructing an adapter stays cheap in tests
    that never open a socket.
    """
    import httpx

    def make() -> Any:
        return httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=httpx.Timeout(total_timeout_s, connect=connect_timeout_s),
            limits=httpx.Limits(
                max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS,
                max_connections=MAX_CONNECTIONS,
                keepalive_expiry=KEEPALIVE_EXPIRY_S,
            ),
        )

    if not shared:
        return make()

    # Keyed on the credential too: two organisations on one worker must not
    # share a connection carrying the other's key.
    key = f"{base_url}|{sorted(headers.items())}"
    client = _SHARED.get(key)
    if client is None or client.is_closed:
        client = make()
        _SHARED[key] = client
    return client


def is_shared(client: Any) -> bool:
    """Whether this client belongs to the process rather than to its caller.

    Asked of the object rather than tracked with a flag on each adapter: a flag
    has to be set correctly at every construction site, and the one site that
    forgets either leaks a connection or closes one that other calls are still
    using. The registry already knows the answer.
    """
    return any(client is held for held in _SHARED.values())


async def close_shared_clients() -> None:
    """Dispose of every pooled client. Called once, at worker shutdown."""
    import contextlib

    for client in list(_SHARED.values()):
        with contextlib.suppress(Exception):
            await client.aclose()
    _SHARED.clear()


async def prewarm_connection(client: Any, path: str, *, timeout_s: float = 5.0) -> bool:
    """Open the TLS connection before a caller needs it.

    Issues the cheapest request the vendor offers -- a listing, never a
    generation -- purely so the handshake is already done. ``httpx`` pools by
    (scheme, host, port), so the connection this opens is the one the next
    ``POST`` reuses.

    Never raises. A vendor that is down at boot must not stop the worker from
    starting: the connection will be made on demand, slowly, which is worse
    than this and much better than no helpline.
    """
    import asyncio

    try:
        async with asyncio.timeout(timeout_s):
            response = await client.get(path)
        status = int(response.status_code)
        log.info("http.prewarmed", host=str(client.base_url), status=status)
        return status < 500
    except Exception as exc:
        log.warning("http.prewarm_failed", host=str(client.base_url), error=type(exc).__name__)
        return False


__all__ = (
    "CONNECT_TIMEOUT_S",
    "KEEPALIVE_EXPIRY_S",
    "MAX_CONNECTIONS",
    "MAX_KEEPALIVE_CONNECTIONS",
    "build_client",
    "close_shared_clients",
    "is_shared",
    "prewarm_connection",
)
