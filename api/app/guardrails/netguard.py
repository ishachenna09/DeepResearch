"""SSRF guard for outbound fetches (used by fetch_page).

URLs arrive from a search engine, so the agent can be aimed at internal
infrastructure: 169.254.169.254 (cloud metadata -> IAM credentials), localhost
admin panels, RFC1918 hosts, or an attacker-controlled DNS name that resolves
to one of those.

Defence: resolve the name, classify every returned address, refuse anything
that is not public - and re-check on every redirect hop, because a public URL
can 302 to 127.0.0.1.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {80, 443}
BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata",
    "metadata.google.internal",
    "instance-data",
}


class UnsafeURLError(ValueError):
    """Raised when a URL would reach a non-public or disallowed destination."""


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def assert_public_url(url: str) -> str:
    """Validate scheme, port and host; every resolved address must be public."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()

    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme not allowed: {parts.scheme!r}")
    if parts.username or parts.password:
        raise UnsafeURLError("credentials in URL are not allowed")

    host = parts.hostname
    if not host:
        raise UnsafeURLError("missing host")

    port = parts.port or (443 if scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise UnsafeURLError(f"port not allowed: {port}")
    if host.lower() in BLOCKED_HOSTNAMES:
        raise UnsafeURLError(f"host not allowed: {host}")

    # NOTE: separate try/except - UnsafeURLError subclasses ValueError, so a
    # shared block would swallow our own rejection.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        if not _is_public(literal):
            raise UnsafeURLError(f"non-public address: {host}")
        return url

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"cannot resolve {host}: {exc}") from exc
    if not infos:
        raise UnsafeURLError(f"cannot resolve {host}")

    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if not _is_public(addr):
            raise UnsafeURLError(f"{host} resolves to non-public address {addr}")

    return url
