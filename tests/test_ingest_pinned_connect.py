"""D-SEC-10: a link the operator pasted is fetched from the address that was CHECKED, not from a
second lookup. A resolver that answers a public address first and a private one after (DNS
rebinding) must not move the connection to the private address.

No socket is opened: the connection goes to a fake network backend. Invented hosts only."""

from __future__ import annotations

import httpcore
import pytest

from kestrel.ingest import http as ingest_http
from kestrel.ingest.http import HttpClient, UnsafeURLError, _PinnedBackend

PUBLIC = "93.184.216.34"
PRIVATE = "10.0.0.5"
RESPONSE = (b"HTTP/1.1 200 OK\r\ncontent-type: text/plain\r\ncontent-length: 2\r\n"
            b"connection: close\r\n\r\nok")


class _Stream(httpcore.AsyncNetworkStream):
    def __init__(self) -> None:
        self._sent = False

    async def read(self, max_bytes, timeout=None):  # noqa: ASYNC109
        if self._sent:
            return b""
        self._sent = True
        return RESPONSE

    async def write(self, buffer, timeout=None):  # noqa: ASYNC109
        return None

    async def aclose(self):
        return None

    def get_extra_info(self, info):
        return None


class _Recorder(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.targets: list[str] = []

    async def connect_tcp(self, host, port, **kwargs):
        self.targets.append(host)
        return _Stream()

    async def connect_unix_socket(self, path, **kwargs):
        raise AssertionError("no unix socket")

    async def sleep(self, seconds):
        return None


@pytest.fixture
def rebinding(monkeypatch):
    """A resolver that is public for the two checks a fetch makes and private for any later
    lookup, a recording network backend, and no environment proxy."""
    calls: list[str] = []
    recorder = _Recorder()

    async def resolve(host, port):
        calls.append(host)
        return [PUBLIC] if len(calls) <= 2 else [PRIVATE]  # the two checks, then a re-lookup

    monkeypatch.setattr(ingest_http, "_resolve", resolve)
    monkeypatch.setattr(ingest_http, "_proxied", lambda url: False)
    real = ingest_http._PinnedBackend

    def pinned(host, ip, inner=None):
        return real(host, ip, recorder)

    monkeypatch.setattr(ingest_http, "_PinnedBackend", pinned)
    return calls, recorder


async def test_the_connection_goes_to_the_checked_address_even_if_dns_changes(rebinding):
    calls, recorder = rebinding
    async with HttpClient(user_agent="Kestrel-test", respect_robots=False) as client:
        r = await client.request("GET", "http://rebind.invalid.example/jobs", public_only=True,
                                 retries=1)
    assert r.status_code == 200 and r.text == "ok"
    assert recorder.targets == [PUBLIC], "the connection did not use the checked address"
    assert PRIVATE not in recorder.targets


async def test_a_private_answer_at_check_time_is_still_refused(monkeypatch):
    async def private(host, port):
        return [PRIVATE]

    monkeypatch.setattr(ingest_http, "_resolve", private)
    async with HttpClient(user_agent="Kestrel-test", respect_robots=False) as client:
        with pytest.raises(UnsafeURLError):
            await client.request("GET", "http://rebind.invalid.example/", public_only=True,
                                 retries=1)


async def test_the_pinned_backend_refuses_any_host_it_was_not_given():
    rec = _Recorder()
    backend = _PinnedBackend("ok.invalid.example", PUBLIC, rec)
    await backend.connect_tcp("OK.invalid.example.", 443)
    assert rec.targets == [PUBLIC]
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("other.invalid.example", 443)
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_unix_socket("/tmp/x")
    assert rec.targets == [PUBLIC]


async def test_a_link_on_an_internationalized_domain_reaches_the_checked_address():
    """httpx hands the connection the A-label spelling; the check names the same host in Unicode."""
    for given in ("b\u00fccher.example", "B\u00dcCHER.example"):
        rec = _Recorder()
        backend = _PinnedBackend(given, PUBLIC, rec)
        await backend.connect_tcp("xn--bcher-kva.example", 443)
        assert rec.targets == [PUBLIC]
        with pytest.raises(httpcore.ConnectError):  # a different name is still refused
            await backend.connect_tcp("xn--other-kva.example", 443)


async def test_an_internationalized_domain_is_fetched_end_to_end(monkeypatch):
    rec = _Recorder()
    seen: dict = {}

    async def resolve(host, port):
        seen["host"] = host
        return [PUBLIC]

    monkeypatch.setattr(ingest_http, "_resolve", resolve)
    real = ingest_http._PinnedBackend
    monkeypatch.setattr(ingest_http, "_PinnedBackend", lambda h, ips: real(h, ips, rec))
    async with HttpClient(user_agent="Kestrel-test", respect_robots=False) as client:
        await client.request("GET", "http://b\u00fccher.example/jobs", public_only=True,
                             retries=1)
    assert rec.targets == [PUBLIC]


# ---- every checked address is tried in order; the proxy decision is httpx's own
class _Flaky(_Recorder):
    """Refuses the addresses in `down` the way an unreachable route does."""

    def __init__(self, down: set[str]) -> None:
        super().__init__()
        self.down = down

    async def connect_tcp(self, host, port, **kwargs):
        self.targets.append(host)
        if host in self.down:
            raise httpcore.ConnectError("unreachable")
        return _Stream()


V6 = "2606:2800:220:1:248:1893:25c8:1946"


def _use(monkeypatch, recorder, addrs):
    async def resolve(host, port):
        return addrs

    monkeypatch.setattr(ingest_http, "_resolve", resolve)
    monkeypatch.setattr(ingest_http, "_proxied", lambda url: False)
    real = ingest_http._PinnedBackend
    monkeypatch.setattr(
        ingest_http, "_PinnedBackend", lambda h, ips, inner=None: real(h, ips, recorder)
    )


async def test_an_unreachable_first_address_falls_back_to_the_next_checked_one(monkeypatch):
    rec = _Flaky({V6})
    _use(monkeypatch, rec, [V6, PUBLIC])
    async with HttpClient(user_agent="Kestrel-test", respect_robots=False) as client:
        r = await client.request("GET", "http://dual.invalid.example/", public_only=True, retries=1)
    assert r.status_code == 200 and rec.targets == [V6, PUBLIC]


async def test_when_every_checked_address_is_down_the_request_fails_and_tries_no_other(monkeypatch):
    import httpx

    rec = _Flaky({V6, PUBLIC})
    _use(monkeypatch, rec, [V6, PUBLIC])
    async with HttpClient(user_agent="Kestrel-test", respect_robots=False) as client:
        with pytest.raises(httpx.ConnectError):
            await client.request("GET", "http://dual.invalid.example/", public_only=True, retries=1)
    assert set(rec.targets) == {V6, PUBLIC}


async def test_the_backend_never_connects_outside_its_checked_list():
    rec = _Flaky({PUBLIC})
    backend = _PinnedBackend("ok.invalid.example", [PUBLIC], rec)
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("ok.invalid.example", 443)
    assert rec.targets == [PUBLIC]


_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
               "all_proxy", "no_proxy", "REQUEST_METHOD")


@pytest.fixture
def clean_proxy_env(monkeypatch):
    for name in _PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("urllib.request.getproxies_registry", lambda: {}, raising=False)
    monkeypatch.setattr("urllib.request.getproxies_macosx_sysconf", lambda: {}, raising=False)
    return monkeypatch


@pytest.mark.parametrize(("name", "value", "url", "proxied"), [
    ("ALL_PROXY", "http://proxy.invalid:3128", "https://jobs.invalid.example/x", True),
    ("all_proxy", "proxy.invalid:3128", "http://jobs.invalid.example/x", True),
    ("HTTPS_PROXY", "http://proxy.invalid:3128", "https://jobs.invalid.example/x", True),
    ("HTTPS_PROXY", "http://proxy.invalid:3128", "http://jobs.invalid.example/x", False),
    (None, None, "https://jobs.invalid.example/x", False),
])
def test_the_proxy_decision_follows_httpx_for_all_proxy_and_the_scheme_proxies(
        clean_proxy_env, name, value, url, proxied):
    if name:
        clean_proxy_env.setenv(name, value)
    assert ingest_http._proxied(url) is proxied


@pytest.mark.parametrize(("no_proxy", "url", "proxied"), [
    ("jobs.invalid.example", "https://jobs.invalid.example/x", False),
    (".invalid.example", "https://jobs.invalid.example/x", False),
    ("*", "https://jobs.invalid.example/x", False),
    ("other.example", "https://jobs.invalid.example/x", True),
])
def test_no_proxy_is_honored_like_httpx(clean_proxy_env, no_proxy, url, proxied):
    clean_proxy_env.setenv("ALL_PROXY", "http://proxy.invalid:3128")
    clean_proxy_env.setenv("NO_PROXY", no_proxy)
    assert ingest_http._proxied(url) is proxied


def test_the_decision_is_the_one_httpx_makes_for_its_own_client(clean_proxy_env):
    import httpx

    clean_proxy_env.setenv("ALL_PROXY", "http://proxy.invalid:3128")
    clean_proxy_env.setenv("NO_PROXY", "skip.invalid.example")
    client = httpx.AsyncClient()
    for url in ("https://a.invalid.example/", "https://skip.invalid.example/"):
        via_proxy = client._transport_for_url(httpx.URL(url)) is not client._transport
        assert ingest_http._proxied(url) is via_proxy


async def test_a_system_proxy_httpx_would_not_use_keeps_the_pinned_direct_connection(
        clean_proxy_env):
    """A system setting that names only another scheme (the registry's ftp proxy) is not a proxy
    for this request: it must not send it down the unpinned client."""
    clean_proxy_env.setattr("urllib.request.getproxies", lambda: {"ftp": "http://proxy.invalid:21"})
    rec = _Recorder()

    async def resolve(host, port):
        return [PUBLIC]

    clean_proxy_env.setattr(ingest_http, "_resolve", resolve)
    real = ingest_http._PinnedBackend
    clean_proxy_env.setattr(
        ingest_http, "_PinnedBackend", lambda h, ips, inner=None: real(h, ips, rec)
    )
    async with HttpClient(user_agent="Kestrel-test", respect_robots=False) as client:
        r = await client.request("GET", "http://jobs.invalid.example/", public_only=True, retries=1)
    assert r.status_code == 200 and rec.targets == [PUBLIC]


async def test_a_proxied_request_goes_through_the_unpinned_client_not_a_direct_connection(
        clean_proxy_env):
    """With a proxy in force the proxy resolves the name; the check stays advisory (D-SEC-10)."""
    clean_proxy_env.setenv("ALL_PROXY", "http://proxy.invalid:3128")
    rec = _Recorder()

    async def resolve(host, port):
        return [PUBLIC]

    clean_proxy_env.setattr(ingest_http, "_resolve", resolve)
    real = ingest_http._PinnedBackend
    clean_proxy_env.setattr(
        ingest_http, "_PinnedBackend", lambda h, ips, inner=None: real(h, ips, rec)
    )
    async with HttpClient(user_agent="Kestrel-test", respect_robots=False) as client:
        with pytest.raises(Exception):  # noqa: B017 - the fake proxy host does not resolve
            await client.request("GET", "http://jobs.invalid.example/", public_only=True,
                                 retries=1)
    assert rec.targets == [], "a proxied request must not open a direct pinned connection"
