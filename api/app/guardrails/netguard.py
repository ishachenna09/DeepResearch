"""SSRF guard for outbound fetches (used by fetch_page).

URLs arrive from a search engine, so the agent can be aimed at internal
infrastructure: 169.254.169.254 (cloud metadata -> IAM credentials), localhost
admin panels, RFC1918 hosts, or an attacker-controlled DNS name that resolves
to one of those.

Two separate problems, two defences:

1. *What* to connect to. Resolve the name, classify every returned address and
   refuse anything that is not globally routable. `ip.is_global` is the primary
   check; `BLOCKED_NETWORKS` adds ranges that are easy to forget (CGNAT,
   benchmarking, 6to4, NAT64, documentation nets) and that no research fetch
   should ever need.

2. *Rebinding.* A hostname can resolve to a public address during validation
   and a private one a millisecond later when httpx resolves it again. So the
   guard returns a `ResolvedTarget`: the caller connects to the validated IP
   literal, sends the original name in `Host`, and (for TLS) in the SNI
   extension. There is no second resolution to race.

Redirects are re-validated and re-pinned by the caller on every hop, because a
public URL can 302 to 127.0.0.1.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
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

# Ranges that `is_global` does not reliably cover across Python versions, plus
# ranges that are technically routable but never a legitimate research source.
BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",     # CGNAT (shared address space)
        "127.0.0.0/8",
        "169.254.0.0/16",    # link-local + cloud metadata
        "172.16.0.0/12",
        "192.0.0.0/24",      # IETF protocol assignments
        "192.0.2.0/24",      # TEST-NET-1
        "192.168.0.0/16",
        "198.18.0.0/15",     # benchmarking
        "198.51.100.0/24",   # TEST-NET-2
        "203.0.113.0/24",    # TEST-NET-3
        "224.0.0.0/4",       # multicast
        "240.0.0.0/4",       # reserved + broadcast
        "::/128",
        "::1/128",
        "64:ff9b::/96",      # NAT64: embeds an IPv4 destination
        "100::/64",          # discard-only
        "2001:db8::/32",     # documentation
        "2002::/16",         # 6to4: embeds an IPv4 destination
        "fc00::/7",          # unique local
        "fe80::/10",         # link-local
        "ff00::/8",          # multicast
    )
)


class UnsafeURLError(ValueError):
    """Raised when a URL would reach a non-public or disallowed destination."""


@dataclass(frozen=True)
class ResolvedTarget:
    """A URL whose host has been validated and pinned to one public address."""

    url: str
    scheme: str
    host: str
    port: int
    ip: str
    path: str
    query: str

    @property
    def _default_port(self) -> int:
        return 443 if self.scheme == "https" else 80

    def pinned_url(self) -> str:
        """The URL to actually request: the validated IP, never the name."""
        literal = f"[{self.ip}]" if ":" in self.ip else self.ip
        authority = literal if self.port == self._default_port else f"{literal}:{self.port}"
        path = self.path or "/"
        return f"{self.scheme}://{authority}{path}" + (f"?{self.query}" if self.query else "")

    def host_header(self) -> str:
        """What the server (and TLS SNI) must see, so virtual hosts still work."""
        return self.host if self.port == self._default_port else f"{self.host}:{self.port}"


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # IPv4-mapped and NAT64/6to4 addresses hide an IPv4 destination inside an
    # IPv6 literal; unwrap what we can, and block the wrappers explicitly.
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if not ip.is_global:
        return False
    return not any(ip.version == net.version and ip in net for net in BLOCKED_NETWORKS)


def _normalise_host(host: str) -> str:
    """Reject invisible garbage, strip the DNS root dot, IDNA-encode the name."""
    host = host.strip()
    if not host:
        raise UnsafeURLError("missing host")
    if any(char in host for char in "\x00\r\n\t ") or any(
        ord(char) < 0x20 for char in host
    ):
        raise UnsafeURLError("host contains control characters")

    host = host.rstrip(".").lower()
    if not host:
        raise UnsafeURLError("missing host")

    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass

    try:
        # A Unicode name must become its ASCII form *before* the blocklist and
        # DNS, so `localhost。` (ideographic full stop) cannot slip through.
        return host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise UnsafeURLError(f"invalid host: {host!r}") from exc


async def _getaddrinfo(host: str, port: int) -> list[tuple]:
    """Separate function so tests can resolve names without a network."""
    loop = asyncio.get_running_loop()
    return await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)


async def resolve_public_url(url: str) -> ResolvedTarget:
    """Validate scheme, port and host; pin the first public address."""
    raw = url.strip()
    if not raw:
        raise UnsafeURLError("missing URL")
    if any(char in raw for char in "\x00\r\n"):
        raise UnsafeURLError("URL contains control characters")

    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme not allowed: {parts.scheme!r}")
    if parts.username or parts.password:
        raise UnsafeURLError("credentials in URL are not allowed")

    host = parts.hostname
    if not host:
        raise UnsafeURLError("missing host")

    try:
        port = parts.port
    except ValueError as exc:
        # `http://example.com:99999/` raises inside urlsplit; without this it
        # escapes as a 500 instead of a clean 400.
        raise UnsafeURLError(f"invalid port: {exc}") from exc
    port = port or (443 if scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise UnsafeURLError(f"port not allowed: {port}")

    host = _normalise_host(host)
    if host in BLOCKED_HOSTNAMES:
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
        ip = str(literal)
    else:
        try:
            infos = await _getaddrinfo(host, port)
        except socket.gaierror as exc:
            raise UnsafeURLError(f"cannot resolve {host}: {exc}") from exc
        if not infos:
            raise UnsafeURLError(f"cannot resolve {host}")

        addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        for info in infos:
            # A link-local IPv6 sockaddr can carry a scope id ("fe80::1%eth0").
            text = str(info[4][0]).split("%", 1)[0]
            try:
                addresses.append(ipaddress.ip_address(text))
            except ValueError as exc:
                raise UnsafeURLError(f"{host} resolved to invalid address {text!r}") from exc

        # Reject the *whole* name if any answer is private. Picking "the first
        # public one" would let a mixed answer through on a different resolver.
        for address in addresses:
            if not _is_public(address):
                raise UnsafeURLError(f"{host} resolves to non-public address {address}")

        # Prefer IPv4: some hosts still have broken IPv6 paths, and a research
        # fetch is not the place to discover that.
        addresses.sort(key=lambda address: address.version)
        ip = str(addresses[0])

    return ResolvedTarget(
        url=raw,
        scheme=scheme,
        host=host,
        port=port,
        ip=ip,
        path=parts.path,
        query=parts.query,
    )


async def assert_public_url(url: str) -> str:
    """Backwards-compatible wrapper: validate and return the original URL."""
    return (await resolve_public_url(url)).url
