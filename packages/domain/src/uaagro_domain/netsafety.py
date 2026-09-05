"""Which addresses a server-side fetch may reach (§17).

Two features take an address from an operator and connect to it from inside
the deployment: the knowledge base crawls a website, and the Data page reads
the client's MySQL database. Inside the deployment, "an address" includes
the database, Redis, the object store, the other services on the compose
network and the cloud's metadata endpoint -- none of which the operator
should be able to make the server talk to by typing a hostname.

So every such connection resolves the name first and refuses anything that
is not a public address. Loopback, link-local (the metadata range),
multicast, reserved and unspecified addresses are refused everywhere. The
private ranges (10/8, 172.16/12, 192.168/16, fc00::/7) are refused for the
crawler -- a public website is public -- and allowed for the database
connector only when the deployment says the client's database lives on a
private network (a VPN or peering), because that is a real arrangement.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable

from .errors import ValidationError

#: Hostnames that can never be a client's server: the deployment's own.
_STACK_NAMES: frozenset[str] = frozenset(
    {
        "localhost",
        "postgres",
        "redis",
        "minio",
        "api",
        "voice-worker",
        "worker",
        "admin",
        "caddy",
        "litellm",
        "prometheus",
        "grafana",
        "metadata",
        "metadata.google.internal",
    }
)


def resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every address a hostname currently resolves to (or the literal itself)."""
    bare = host.strip("[]")
    try:
        return [ipaddress.ip_address(bare)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(bare, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValidationError(
            f"{host!r} does not resolve.", remedy="Check the spelling of the hostname."
        ) from exc
    seen: dict[str, ipaddress.IPv4Address | ipaddress.IPv6Address] = {}
    for info in infos:
        seen.setdefault(str(info[4][0]), ipaddress.ip_address(str(info[4][0])))
    return list(seen.values())


def is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def is_private_network(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """RFC 1918 / fc00::/7 -- a LAN, not the host itself and not the metadata range."""
    return address.is_private and not (
        address.is_loopback or address.is_link_local or address.is_unspecified
    )


def ensure_reachable_target(
    host: str, *, purpose: str, allow_private_networks: bool = False
) -> None:
    """Refuse a host that points back into the deployment.

    Raises :class:`ValidationError` naming the reason, never the address it
    resolved to -- that would turn the refusal into a resolver for the very
    network it protects.
    """
    if host.strip().lower().rstrip(".") in _STACK_NAMES or host.endswith(".internal"):
        raise ValidationError(
            f"{host!r} is not an address {purpose} may use.",
            remedy="Give the public hostname or address of the server.",
        )
    for address in resolve(host):
        if is_public(address):
            continue
        if allow_private_networks and is_private_network(address):
            continue
        raise ValidationError(
            f"{host!r} resolves to an address inside this deployment's network, "
            f"which {purpose} may not reach.",
            remedy="Give a public hostname or address."
            + (
                ""
                if allow_private_networks
                else " A server on a private network needs SOURCES_ALLOW_PRIVATE_NETWORKS=true."
            ),
        )


def all_public(hosts: Iterable[str]) -> bool:
    try:
        for host in hosts:
            ensure_reachable_target(host, purpose="this fetch")
    except ValidationError:
        return False
    return True


__all__ = (
    "all_public",
    "ensure_reachable_target",
    "is_private_network",
    "is_public",
    "resolve",
)
