"""SSRF guard + pinned-fetch tests (Phase 9).

The guard is the highest-value security test in the project: a research agent
fetches URLs chosen by a search engine, which is exactly the input an attacker
can influence. These tests cover classification, host normalisation, the
resolved-IP contract, and the redirect path - all without a network.

Run from api/:
    ..\\.venv\\Scripts\\python.exe -m pytest tests -q
"""
from __future__ import annotations

import asyncio
import socket

import httpx
import pytest

from app.guardrails import netguard
from app.guardrails.netguard import UnsafeURLError, resolve_public_url
from app.mcp_tools.servers import web_tools

PUBLIC = "93.184.216.34"
PUBLIC_2 = "93.184.216.35"


def run(coro):
    return asyncio.run(coro)


def fake_resolver(mapping: dict[str, str | list[str]]):
    async def resolver(host: str, port: int) -> list[tuple]:
        value = mapping.get(host)
        if value is None:
            raise socket.gaierror(f"no such host: {host}")
        addresses = value if isinstance(value, list) else [value]
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
            for address in addresses
        ]

    return resolver


@pytest.fixture
def resolver(monkeypatch):
    mapping: dict[str, str | list[str]] = {"example.com": PUBLIC}
    monkeypatch.setattr(netguard, "_getaddrinfo", fake_resolver(mapping))
    return mapping


# ── blocked destinations, no DNS needed ───────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://localhost./",
        "http://LOCALHOST/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://100.64.0.1/",
        "http://198.18.0.1/",
        "http://224.0.0.1/",
        "http://255.255.255.255/",
        "http://0.0.0.0/",
        "http://[::ffff:127.0.0.1]/",
        "http://[2002:7f00:1::]/",
        "http://[64:ff9b::7f00:1]/",
        "http://[fe80::1]/",
        "http://[fc00::1]/",
        "file:///etc/passwd",
        "ftp://example.com/",
        "http://user:pass@example.com/",
        "http://example.com:22/",
        "http://example.com:6379/",
        "http://example.com:99999/",
        "http://exa mple.com/",
    ],
)
def test_unsafe_urls_are_rejected(url):
    with pytest.raises(UnsafeURLError):
        run(resolve_public_url(url))


# ── DNS-based destinations ─────────────────────────────────────────────────


def test_dns_name_that_resolves_private_is_rejected(resolver):
    resolver["internal.example"] = "10.0.0.5"
    with pytest.raises(UnsafeURLError, match="non-public"):
        run(resolve_public_url("http://internal.example/"))


def test_mixed_answer_is_rejected_even_if_one_address_is_public(resolver):
    resolver["mixed.example"] = [PUBLIC, "127.0.0.1"]
    with pytest.raises(UnsafeURLError):
        run(resolve_public_url("http://mixed.example/"))


@pytest.mark.parametrize("host", ["2130706433", "0x7f000001", "127.1"])
def test_integer_and_short_form_loopback_is_rejected(resolver, host):
    # `getaddrinfo` treats these as 127.0.0.1; the guard must judge the answer,
    # not the spelling.
    resolver[host] = "127.0.0.1"
    with pytest.raises(UnsafeURLError):
        run(resolve_public_url(f"http://{host}/"))


def test_unresolvable_name_is_a_client_error(resolver):
    with pytest.raises(UnsafeURLError, match="cannot resolve"):
        run(resolve_public_url("http://missing.example/"))


# ── the pinned target contract ─────────────────────────────────────────────


def test_public_url_returns_a_pinned_target(resolver):
    target = run(resolve_public_url("http://example.com/a?b=1"))
    assert target.host == "example.com"
    assert target.ip == PUBLIC
    assert target.pinned_url() == f"http://{PUBLIC}/a?b=1"
    assert target.host_header() == "example.com"


def test_https_target_keeps_sni_hostname(resolver):
    target = run(resolve_public_url("https://example.com/path"))
    assert target.pinned_url() == f"https://{PUBLIC}/path"
    assert target.host_header() == "example.com"


def test_public_ipv6_is_pinned_with_brackets(monkeypatch):
    address = "2606:2800:220:1:248:1893:25c8:1946"

    async def resolver(host: str, port: int):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, port, 0, 0))]

    monkeypatch.setattr(netguard, "_getaddrinfo", resolver)
    target = run(resolve_public_url("http://ipv6.example/"))
    assert target.ip == address
    assert target.pinned_url() == f"http://[{address}]/"


def test_host_is_normalised_before_resolution(monkeypatch):
    seen: list[str] = []

    async def resolver_spy(host: str, port: int):
        seen.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port))]

    monkeypatch.setattr(netguard, "_getaddrinfo", resolver_spy)
    target = run(resolve_public_url("  http://EXAMPLE.com./  "))
    assert target.host == "example.com"
    assert seen == ["example.com"]


def test_unicode_host_is_idna_encoded_before_resolution(monkeypatch):
    seen: list[str] = []

    async def resolver(host: str, port: int):
        seen.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port))]

    monkeypatch.setattr(netguard, "_getaddrinfo", resolver)
    target = run(resolve_public_url("http://bücher.example/"))
    assert seen == ["xn--bcher-kva.example"]
    assert target.host == "xn--bcher-kva.example"


def test_literal_ip_does_not_call_the_resolver(monkeypatch):
    async def boom(host: str, port: int):  # pragma: no cover - must not run
        raise AssertionError("resolver called for a literal IP")

    monkeypatch.setattr(netguard, "_getaddrinfo", boom)
    target = run(resolve_public_url(f"http://{PUBLIC}/"))
    assert target.ip == PUBLIC


def test_assert_public_url_wrapper_returns_the_human_url(resolver):
    assert run(netguard.assert_public_url("http://example.com/x")) == "http://example.com/x"


# ── fetch path: pinning, redirects, caps ───────────────────────────────────


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    )


def test_download_connects_to_the_ip_with_the_original_host_header(resolver):
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("host")))
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<html><title>Hello</title><body>ok</body></html>",
        )

    async def scenario():
        async with _client(handler) as client:
            return await web_tools._download("http://example.com/page", client)

    final_url, status, content_type, body, charset = run(scenario())
    assert seen == [(f"http://{PUBLIC}/page", "example.com")]
    assert final_url == "http://example.com/page"
    assert (status, content_type, charset) == (200, "text/html", "utf-8")
    assert b"Hello" in body


def test_redirect_is_re_resolved_and_re_pinned(resolver):
    resolver["second.example"] = PUBLIC_2
    seen: list[tuple[str, str | None, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                str(request.url),
                request.headers.get("host"),
                request.extensions.get("sni_hostname"),
            )
        )
        if request.headers.get("host") != "second.example":
            return httpx.Response(302, headers={"location": "https://second.example/next"})
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html><body>second</body></html>",
        )

    async def scenario():
        async with _client(handler) as client:
            return await web_tools._download("http://example.com/start", client)

    final_url, status, _ctype, body, _charset = run(scenario())
    assert final_url == "https://second.example/next"
    assert status == 200
    assert b"second" in body
    assert seen[0][:2] == (f"http://{PUBLIC}/start", "example.com")
    # HTTPS is pinned too, and TLS still sees the real name.
    assert seen[1][0] == f"https://{PUBLIC_2}/next"
    assert seen[1][1:] == ("second.example", "second.example")


def test_redirect_to_a_private_host_is_blocked_before_the_second_request(resolver):
    resolver["internal.example"] = "10.0.0.5"
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://internal.example/secret"})

    async def scenario():
        async with _client(handler) as client:
            await web_tools._download("http://example.com/", client)

    with pytest.raises(UnsafeURLError):
        run(scenario())
    # The redirect was validated before it was followed.
    assert calls == [f"http://{PUBLIC}/"]


def test_declared_content_length_over_the_cap_is_rejected(resolver):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "text/html",
                "content-length": str(web_tools.settings.MAX_FETCH_BYTES + 1),
            },
            content=b"x",
        )

    async def scenario():
        async with _client(handler) as client:
            await web_tools._download("http://example.com/", client)

    with pytest.raises(ValueError, match="too large"):
        run(scenario())


def test_non_text_content_type_is_rejected(resolver):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/zip"}, content=b"PK\x03\x04"
        )

    async def scenario():
        async with _client(handler) as client:
            await web_tools._download("http://example.com/", client)

    with pytest.raises(ValueError, match="unsupported content-type"):
        run(scenario())


class _FakeStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


def test_streamed_body_is_capped_without_buffering_everything():
    async def scenario():
        return await web_tools._read_capped(_FakeStream([b"a" * 6, b"b" * 6]), 10)

    body, oversized = run(scenario())
    assert body == b"a" * 6 + b"b" * 4
    assert oversized is True


def test_streamed_body_under_the_cap_is_complete():
    async def scenario():
        return await web_tools._read_capped(_FakeStream([b"abc", b"def"]), 10)

    assert run(scenario()) == (b"abcdef", False)
