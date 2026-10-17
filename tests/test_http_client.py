"""The shared ingestion HttpClient enforces the constraint-7 politeness every signal source relies
on: robots.txt is honored when check_robots is set, and per-host rate limiting spaces requests.
Exercised directly so the PE portfolio and RSS adapters inherit the guarantee."""

from __future__ import annotations

import time

import pytest
import respx
from httpx import Response

from kestrel.ingest.http import HttpClient, _HostLimiter, _safe_url, has_contact_address


@respx.mock
async def test_robots_disallow_blocks_request():
    respx.get("https://example.com/robots.txt").mock(
        return_value=Response(200, text="User-agent: *\nDisallow: /private\n")
    )
    respx.get("https://example.com/private/page").mock(return_value=Response(200, text="secret"))
    respx.get("https://example.com/public/page").mock(return_value=Response(200, text="ok"))

    async with HttpClient(user_agent="Kestrel/1.0 (test)", respect_robots=True) as http:
        with pytest.raises(PermissionError):
            await http.request("GET", "https://example.com/private/page", check_robots=True)
        r = await http.request("GET", "https://example.com/public/page", check_robots=True)
        assert r.status_code == 200


@respx.mock
async def test_check_robots_false_skips_robots_entirely():
    # An API endpoint (EDGAR/SAM) is fetched with check_robots=False; robots.txt is never requested.
    route = respx.get("https://api.example.com/data").mock(
        return_value=Response(200, json={"ok": 1})
    )
    async with HttpClient(user_agent="Kestrel/1.0 (test)", respect_robots=True) as http:
        await http.request("GET", "https://api.example.com/data", check_robots=False)
    assert route.called


def test_safe_url_drops_query_and_fragment():
    assert _safe_url("https://api.example.com/v2/search?api_key=SECRET&x=1#f") == (
        "https://api.example.com/v2/search"
    )
    assert _safe_url("https://api.example.com/plain") == "https://api.example.com/plain"


def test_has_contact_address_rejects_placeholders():
    assert has_contact_address("Kestrel/1.0 (ops@kestrel-example.org)") is True
    assert has_contact_address("Kestrel/1.0 (operator@example.com)") is False
    assert has_contact_address("Kestrel/1.0 (+https://github.com/kestrel-app)") is False
    assert has_contact_address(None) is False


@respx.mock
async def test_error_strings_never_carry_the_query_string(monkeypatch):
    """A credential in the query string must not reach ScrapeRun.error/notifications/logs via the
    error text: both the retry-exhausted 403 path and the raise-for-status path strip it."""
    import kestrel.ingest.http as http_mod

    async def _no_sleep(_s):  # skip the 403 backoff so the test does not take seconds
        return None

    monkeypatch.setattr(http_mod.asyncio, "sleep", _no_sleep)
    respx.get("https://api.example.com/search").mock(return_value=Response(403, text="nope"))
    respx.get("https://api.example.com/missing").mock(return_value=Response(404, text="gone"))
    async with HttpClient(user_agent="Kestrel/1.0 (test)", respect_robots=False) as http:
        with pytest.raises(Exception) as exc_403:
            await http.get_json("https://api.example.com/search?api_key=SECRET123&ncode=5")
        with pytest.raises(Exception) as exc_404:
            await http.get_json("https://api.example.com/missing?api_key=SECRET123")
    for exc in (exc_403.value, exc_404.value):
        assert "SECRET123" not in str(exc)
        assert "api_key" not in str(exc)
    assert "403" in str(exc_403.value) and "404" in str(exc_404.value)


async def test_host_limiter_spaces_requests():
    limiter = _HostLimiter()
    rate = 20.0  # 50ms minimum interval
    start = time.monotonic()
    await limiter.wait("host", rate)  # first is immediate
    await limiter.wait("host", rate)  # second waits ~1/rate
    await limiter.wait("host", rate)  # third waits another ~1/rate
    elapsed = time.monotonic() - start
    assert elapsed >= 2.0 / rate * 0.8  # two intervals, allowing scheduler slack


async def test_host_limiter_zero_rate_never_waits():
    limiter = _HostLimiter()
    start = time.monotonic()
    for _ in range(5):
        await limiter.wait("host", 0.0)
    assert time.monotonic() - start < 0.05


# --------------------------------------------------------------- hard rule 7, at the HTTP layer
# "Only public data, politely: respect robots.txt, send a descriptive User-Agent (mandatory for SEC
# EDGAR), rate-limit every source." robots.txt was already guarded. These two were not: deleting
# both the `await self._limiter.wait(...)` call and the User-Agent header from HttpClient left the
# whole ingestion suite green, because the limiter was only ever tested in isolation and no test
# anywhere asserted on the outgoing header.


def test_every_request_carries_the_configured_user_agent():
    """SEC EDGAR returns a hard 403 without a descriptive UA, so this is not cosmetic."""
    import asyncio

    import httpx

    from kestrel.ingest.http import HttpClient

    seen: dict = {}

    async def _run():
        async with HttpClient(user_agent="Kestrel/9.9 (+contact: someone@example.com)") as c:

            def _handler(request: httpx.Request) -> httpx.Response:
                seen["ua"] = request.headers.get("User-Agent")
                return httpx.Response(200, text="ok")

            c._client = httpx.AsyncClient(
                transport=httpx.MockTransport(_handler),
                headers={"User-Agent": c.user_agent},
            )
            await c.request("GET", "https://example.invalid/page")

    asyncio.run(_run())
    assert seen["ua"] == "Kestrel/9.9 (+contact: someone@example.com)"
    assert "Kestrel" in seen["ua"], "a descriptive User-Agent is mandatory (hard rule 7)"


def test_every_request_goes_through_the_rate_limiter():
    """The limiter being correct in isolation is not the property that matters. What matters is
    that `request()` actually calls it, which is what a polite client means."""
    import asyncio

    import httpx

    from kestrel.ingest.http import HttpClient

    calls: list = []

    async def _run():
        async with HttpClient(user_agent="Kestrel/1.0 (+contact: op@example.com)") as c:
            real_wait = c._limiter.wait

            async def _spy(host, rate):
                calls.append((host, rate))
                return await real_wait(host, rate)

            c._limiter.wait = _spy
            c._client = httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, text="ok"))
            )
            await c.request("GET", "https://example.invalid/a")
            await c.request("GET", "https://example.invalid/b", rate_per_sec=0.5)

    asyncio.run(_run())
    assert len(calls) == 2, "request() did not go through the per-host rate limiter"
    assert calls[0][0] == "example.invalid"
    assert calls[1][1] == 0.5, "an explicit per-source rate was not honored"
