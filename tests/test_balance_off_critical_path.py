"""Issue #106: the online balance read never delays the status polls and never shows zero.

The fake endpoint is held open by a gate the test controls (no real seconds are slept), which
stands for any reply slower than the old 4 s timeout. Invariant: /status/rail and /api/status
return without waiting for the balance; the balance fills in on a later poll; a failed or slow
read is unknown, never zero."""

from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest
import respx
from httpx import Response

from kestrel.services import hosted_balance as hb
from tests.test_hosted_balance import (
    _HOSTED_ON,
    KEY_URL,
    _fresh,
    _key_json,
    _ollama,
    _seed_web,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    hb.clear_cache()
    yield
    hb.clear_cache()


def _gated(gate: threading.Event, reply: Response):
    """An endpoint that answers only once the test opens the gate."""

    async def handler(request: httpx.Request) -> Response:
        await asyncio.to_thread(gate.wait, 30)  # held open until the test opens the gate
        return reply

    return handler


def _poll_until(check, *, limit_s: float = 10.0):
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        got = check()
        if got:
            return got
        time.sleep(0.02)
    raise AssertionError("the balance never filled in")


@respx.mock
def test_a_slow_balance_never_holds_up_the_status_polls(monkeypatch):
    client = _fresh(monkeypatch)
    _seed_web(*_HOSTED_ON)
    _ollama()
    gate = threading.Event()
    route = respx.get(KEY_URL).mock(side_effect=_gated(gate, _key_json(usage_monthly=4.2)))
    with client:
        try:
            rail = client.get("/status/rail")  # returns while the provider is still silent
            api = client.get("/api/status").json()
            assert rail.status_code == 200
            assert 'data-rail="balance"' not in rail.text and api["balance"] is None
            assert "$0" not in rail.text and "0.00" not in rail.text  # unknown, never zero
            gate.set()
            _poll_until(lambda: "$4.20 spent this month" in client.get("/status/rail").text)
            assert client.get("/api/status").json()["balance"]["label"] == "$4.20 spent this month"
            assert route.call_count == 1  # polls while it loaded did not send their own
        finally:
            gate.set()


@respx.mock
def test_a_failed_read_shows_unavailable_not_zero_and_does_not_block(monkeypatch):
    client = _fresh(monkeypatch)
    _seed_web(*_HOSTED_ON)
    _ollama()
    respx.get(KEY_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with client:
        first = client.get("/status/rail").text
        assert 'data-rail="balance"' not in first
        _poll_until(lambda: "Balance unavailable" in client.get("/status/rail").text)
        row = client.get("/api/status").json()["balance"]
        assert row["label"] == "Balance unavailable" and "0.00" not in row["label"]


@respx.mock
async def test_an_old_reading_answers_while_the_new_one_loads(sm, monkeypatch):
    from tests.test_hosted_balance import _settings

    clock = {"t": 1000.0}
    monkeypatch.setattr(hb, "_now", lambda: clock["t"])
    respx.get(KEY_URL).mock(return_value=_key_json(usage_monthly=1.3))
    async with sm() as s:
        assert await hb.rail_row_nowait(s, _settings()) is None  # cold: read starts, no line yet
        await asyncio.gather(*hb._background.values())
        assert (await hb.rail_row_nowait(s, _settings()))["label"] == "$1.30 spent this month"
        clock["t"] += hb.CACHE_TTL_S + 1
        gate = asyncio.Event()

        async def slow(request: httpx.Request) -> Response:
            await gate.wait()
            return _key_json(usage_monthly=2.0)

        respx.get(KEY_URL).mock(side_effect=slow)
        stale = await hb.rail_row_nowait(s, _settings())
        assert stale["label"] == "$1.30 spent this month"  # the old figure, not a blank or zero
        gate.set()
        await asyncio.gather(*hb._background.values())
        assert (await hb.rail_row_nowait(s, _settings()))["label"] == "$2.00 spent this month"


@respx.mock
async def test_the_read_waits_long_enough_for_a_reply_slower_than_four_seconds(sm):
    """A fake endpoint that needs 6 s: it times out under the old 4 s limit and answers under the
    current one. The simulated delay is read from the request's own timeout, no real sleep."""
    from tests.test_hosted_balance import _settings

    seen: list[float] = []

    def endpoint(request: httpx.Request) -> Response:
        allowed = request.extensions["timeout"]["read"]
        seen.append(allowed)
        if allowed < 6.0:
            raise httpx.ReadTimeout("the reply needed 6 s")
        return _key_json(usage_monthly=3.0)

    respx.get(KEY_URL).mock(side_effect=endpoint)
    async with sm() as s:
        row = await hb.rail_row(s, _settings())
    assert row["label"] == "$3.00 spent this month", seen
    assert seen and min(seen) > 4.0
