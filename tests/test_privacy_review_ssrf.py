"""S4: a link the operator supplies is never fetched when it points at this computer or a private
network, on the first hop or any redirect, and a page is capped at 2 MB. DNS and HTTP are mocked;
nothing touches the network."""

from __future__ import annotations

import httpx
import pytest
import respx

from kestrel.ingest import http as ih
from kestrel.ingest.http import HttpClient, UnsafeURLError

PUBLIC = "93.184.216.34"
DNS = {
    "localhost": ["127.0.0.1"],
    "rebind.example": ["10.1.2.3"],
    "mixed.example": [PUBLIC, "192.168.0.9"],
    "meta.example": ["169.254.169.254"],
    "v6loop.example": ["::1"],
    "mapped.example": ["::ffff:127.0.0.1"],
    "good.example": [PUBLIC],
    "other.example": [PUBLIC],
}


@pytest.fixture(autouse=True)
def _dns(monkeypatch):
    async def resolve(host, port):
        if host not in DNS:
            raise OSError("no such host")
        return DNS[host]

    monkeypatch.setattr(ih, "_resolve", resolve)


def _client() -> HttpClient:
    return HttpClient(user_agent="kestrel-test (t@kestrel.test)", respect_robots=False,
                      max_attempts=1)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x", "http://localhost/x", "http://10.0.0.5/x", "http://172.16.0.1/x",
    "http://192.168.1.1/x", "http://169.254.169.254/latest/meta-data", "http://[::1]/x",
    "http://0.0.0.0/x", "http://100.64.0.1/x", "http://224.0.0.1/x", "http://240.0.0.1/x",
    "http://[::ffff:127.0.0.1]/x", "http://[fe80::1]/x", "http://[fd00::1]/x",
    "http://rebind.example/x", "http://mixed.example/x", "http://meta.example/x",
    "http://v6loop.example/x", "http://mapped.example/x",
    "ftp://good.example/x", "file:///etc/passwd",
])
async def test_private_loopback_and_reserved_addresses_are_refused(url):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route().mock(return_value=httpx.Response(200, text="secret"))
        async with _client() as http:
            with pytest.raises(UnsafeURLError):
                await http.request("GET", url, public_only=True)
        assert not route.called


async def test_a_public_host_is_fetched():
    with respx.mock() as mock:
        mock.get("http://good.example/p").mock(return_value=httpx.Response(200, text="hello"))
        async with _client() as http:
            r = await http.request("GET", "http://good.example/p", public_only=True)
    assert r.status_code == 200 and r.text == "hello"


async def test_a_redirect_into_a_private_network_is_refused_on_that_hop():
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://good.example/start").mock(return_value=httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest/meta-data"}))
        inner = mock.get("http://169.254.169.254/latest/meta-data").mock(
            return_value=httpx.Response(200, text="creds"))
        async with _client() as http:
            with pytest.raises(UnsafeURLError):
                await http.request("GET", "http://good.example/start", public_only=True)
        assert not inner.called


async def test_a_redirect_by_name_to_a_private_host_is_refused():
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://good.example/start").mock(return_value=httpx.Response(
            301, headers={"location": "http://rebind.example/admin"}))
        inner = mock.get("http://rebind.example/admin").mock(return_value=httpx.Response(200))
        async with _client() as http:
            with pytest.raises(UnsafeURLError):
                await http.request("GET", "http://good.example/start", public_only=True)
        assert not inner.called


async def test_public_redirects_are_followed_a_few_hops():
    with respx.mock() as mock:
        mock.get("http://good.example/a").mock(return_value=httpx.Response(
            302, headers={"location": "http://other.example/b"}))
        mock.get("http://other.example/b").mock(return_value=httpx.Response(200, text="end"))
        async with _client() as http:
            r = await http.request("GET", "http://good.example/a", public_only=True)
    assert r.text == "end"


async def test_a_redirect_loop_stops():
    with respx.mock() as mock:
        mock.get("http://good.example/a").mock(return_value=httpx.Response(
            302, headers={"location": "http://good.example/a"}))
        async with _client() as http:
            with pytest.raises(UnsafeURLError, match="redirects too many"):
                await http.request("GET", "http://good.example/a", public_only=True)


async def test_a_page_over_two_megabytes_is_refused():
    with respx.mock() as mock:
        mock.get("http://good.example/big").mock(
            return_value=httpx.Response(200, content=b"x" * 2_100_000))
        async with _client() as http:
            with pytest.raises(UnsafeURLError, match="2 MB"):
                await http.request("GET", "http://good.example/big", public_only=True)


async def test_robots_txt_is_fetched_with_the_same_guard():
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://good.example/robots.txt").mock(return_value=httpx.Response(
            302, headers={"location": "http://169.254.169.254/robots.txt"}))
        inner = mock.get("http://169.254.169.254/robots.txt").mock(
            return_value=httpx.Response(200))
        mock.get("http://good.example/p").mock(return_value=httpx.Response(200, text="x"))
        http = HttpClient(user_agent="kestrel-test (t@kestrel.test)", respect_robots=True,
                          max_attempts=1)
        async with http:
            with pytest.raises(UnsafeURLError):
                await http.request("GET", "http://good.example/p", check_robots=True,
                                   public_only=True)
        assert not inner.called


async def test_a_pasted_posting_link_to_this_computer_is_refused_plainly():
    from kestrel.services.applied_import import FetchRefused, fetch_posting

    with respx.mock(assert_all_called=False) as mock:
        route = mock.route().mock(return_value=httpx.Response(200, text="admin"))
        async with _client() as http:
            with pytest.raises(FetchRefused, match="private network"):
                await fetch_posting(http, "http://127.0.0.1:8765/api/settings")
        assert not route.called


def test_the_feed_check_refuses_a_link_to_this_computer(_isolate_data_home, monkeypatch):
    import asyncio

    from fastapi.testclient import TestClient

    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.db.engine import forget_engine, reset_engine
    from kestrel.web.app import build_app

    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()
    client = TestClient(build_app(worker=False))
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route().mock(return_value=httpx.Response(200, text="<rss/>"))
        r = client.post("/sources/check/feed", data={"url": "http://127.0.0.1:9/feed"})
        assert not route.called
    assert "private network" in r.text
