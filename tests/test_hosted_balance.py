"""#29: the remaining balance of the user's own OpenRouter key, in the status rail.

The one outbound call is `GET /key` on OpenRouter, only when hosted mode is on and acknowledged,
the key is present and the user has not chosen free models only (unless they ask for the balance).
Anthropic, OpenAI and every other provider make zero network calls: they show local spend from the
transmission log. Offline: respx stands in for OpenRouter."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
import structlog
from httpx import Response
from sqlalchemy import select

from kestrel.config import HostedMode, get_settings
from kestrel.db.models import FrontierTransmission, Opportunity, Setting
from kestrel.services import hosted_balance as hb

KEY_URL = "https://openrouter.ai/api/v1/key"
SECRET = "sk-or-v1-supersecretvalue1234567890"
_ACK = "2026-10-12T10:00:00+00:00"


def _settings(*, free_only=False, active=True, key=SECRET, anthropic_key=None, openai_key=None):
    s = get_settings()
    s.llm.hosted_mode.enabled = active
    s.llm.hosted_mode.acknowledged_at = _ACK if active else None
    s.llm.hosted_mode.acknowledged_terms = s.llm.hosted_mode.disclosure_terms() if active else None
    s.llm.hosted_mode.free_only = free_only
    s.llm.backends["frontier"].api_key = key
    s.llm.backends["anthropic"].api_key = anthropic_key
    s.llm.backends["openai"].api_key = openai_key
    return s


def _key_json(**data) -> Response:
    return Response(200, json={"data": {"label": "k", "usage": 1.3, "limit": None,
                                       "limit_remaining": None, "is_free_tier": False, **data}})


@pytest.fixture(autouse=True)
def _clean_cache():
    hb.clear_cache()
    yield
    hb.clear_cache()


# ---------------------------------------------------------------------- who may be asked
def test_only_an_opted_in_openrouter_key_is_ever_asked():
    assert hb.eligible_backend(_settings(), show_balance=False) is not None
    # hosted mode off, or not acknowledged: no call
    assert hb.eligible_backend(_settings(active=False), show_balance=False) is None
    # free models only: no call unless the user asked for the balance
    assert hb.eligible_backend(_settings(free_only=True), show_balance=False) is None
    assert hb.eligible_backend(_settings(free_only=True), show_balance=True) is not None
    # no key saved
    assert hb.eligible_backend(_settings(key=None), show_balance=False) is None


def test_anthropic_and_openai_are_never_eligible():
    s = _settings(key=None, anthropic_key="sk-ant-xyz", openai_key="sk-openai-xyz")
    assert hb.eligible_backend(s, show_balance=True) is None


# ------------------------------------------------------------------------- the reading
@respx.mock
async def test_the_line_is_this_months_spend_with_or_without_a_cap(sm):
    # Owner decision: one short line, the same either way. Never lifetime usage, never a cap.
    for data in ({"usage": 41.5, "usage_monthly": 1.3},
                 {"limit": 10.0, "limit_remaining": 4.2, "usage": 41.5, "usage_monthly": 1.3}):
        hb.clear_cache()
        respx.get(KEY_URL).mock(return_value=_key_json(**data))
        async with sm() as s:
            row = await hb.rail_row(s, _settings())
        assert row["label"] == "$1.30 spent this month" and row["kind"] == "spent_month"
        assert "management key" in row["note"]
        assert "left" not in row["label"] and "limit" not in row["label"].lower()


@respx.mock
async def test_no_monthly_figure_reads_unavailable_not_zero(sm):
    respx.get(KEY_URL).mock(return_value=_key_json(limit=5.0, limit_remaining=-0.2))
    async with sm() as s:
        assert (await hb.rail_row(s, _settings()))["kind"] == "unavailable"


@respx.mock
async def test_a_failure_degrades_to_balance_unavailable(sm):
    route = respx.get(KEY_URL).mock(return_value=Response(503))
    async with sm() as s:
        row = await hb.rail_row(s, _settings())
        again = await hb.rail_row(s, _settings())
    assert row["label"] == "Balance unavailable" and row["state"] == "warn"
    assert again["label"] == "Balance unavailable"
    assert route.call_count == 1  # a failure is remembered briefly, not retried on every poll


@respx.mock
async def test_a_network_error_degrades_too(sm):
    respx.get(KEY_URL).mock(side_effect=httpx.ConnectError("no route"))
    async with sm() as s:
        assert (await hb.rail_row(s, _settings()))["label"] == "Balance unavailable"


@respx.mock
async def test_a_reply_that_is_not_json_degrades(sm):
    respx.get(KEY_URL).mock(return_value=Response(200, text="<html>"))
    async with sm() as s:
        assert (await hb.rail_row(s, _settings()))["label"] == "Balance unavailable"


# --------------------------------------------------------------------------- the cache
@respx.mock
async def test_the_cache_prevents_a_second_call_until_it_expires(sm, monkeypatch):
    route = respx.get(KEY_URL).mock(return_value=_key_json(limit=10.0, limit_remaining=4.2))
    clock = {"t": 1000.0}
    monkeypatch.setattr(hb, "_now", lambda: clock["t"])
    async with sm() as s:
        await hb.rail_row(s, _settings())
        await hb.rail_row(s, _settings())
        assert route.call_count == 1
        clock["t"] += hb.CACHE_TTL_S - 1
        await hb.rail_row(s, _settings())
        assert route.call_count == 1
        clock["t"] += 2
        await hb.rail_row(s, _settings())
        assert route.call_count == 2


@respx.mock
async def test_a_changed_key_is_a_different_cache_entry_and_the_raw_key_is_not_in_it(sm):
    route = respx.get(KEY_URL).mock(return_value=_key_json(limit=10.0, limit_remaining=4.2))
    async with sm() as s:
        await hb.rail_row(s, _settings())
        await hb.rail_row(s, _settings(key="sk-or-v1-another-key-0987654321"))
    assert route.call_count == 2
    digests = {k[2] for k in hb._cache}
    assert len(digests) == 2 and all(len(d) == 16 for d in digests)  # one entry per key
    assert SECRET not in json.dumps([list(k) for k in hb._cache])


# ------------------------------------------------------------------- the request itself
@respx.mock
async def test_the_request_carries_only_the_auth_header_and_no_body(sm):
    route = respx.get(KEY_URL).mock(return_value=_key_json(limit=10.0, limit_remaining=4.2))
    async with sm() as s:
        await hb.rail_row(s, _settings())
    req = route.calls[0].request
    assert req.method == "GET" and req.content == b"" and req.url.query == b""
    assert req.headers["authorization"] == f"Bearer {SECRET}"
    # none of the backend's attribution headers, and nothing about the user
    assert "http-referer" not in req.headers and "x-title" not in req.headers
    assert set(req.headers) <= {"authorization", "accept", "accept-encoding", "connection",
                                "host", "user-agent"}


# --------------------------------------------------------------- no network when not opted in
@respx.mock
async def test_nothing_is_fetched_for_free_only_or_other_providers(sm):
    route = respx.route().mock(return_value=Response(500))
    async with sm() as s:
        assert await hb.rail_row(s, _settings(free_only=True)) is None
        assert await hb.rail_row(s, _settings(active=False)) is None
        other = _settings(key=None, anthropic_key="sk-ant-xyz", openai_key="sk-openai-xyz")
        assert await hb.rail_row(s, other) is None
    assert route.call_count == 0  # zero requests to any host


@respx.mock
async def test_the_show_balance_setting_opts_a_free_only_user_in(sm):
    route = respx.get(KEY_URL).mock(return_value=_key_json(usage=0.0, usage_monthly=0.0))
    async with sm() as s:
        s.add(Setting(key=hb.SHOW_BALANCE_KEY, value=True))
        await s.commit()
    async with sm() as s:
        row = await hb.rail_row(s, _settings(free_only=True))
    assert route.call_count == 1 and row["kind"] == "spent_month"


# --------------------------------------------------------------------- local spend, no network
async def _spend(sm, *costs, ago_days=0.0, provider="anthropic"):
    async with sm() as s:
        opp = Opportunity(track="employment", opp_type="job_posting", title="t", org_name="o",
                          url=f"https://x.example/{len(costs)}{ago_days}{provider}",
                          stage="t2_done")
        s.add(opp)
        await s.flush()
        when = (datetime.now(UTC) - timedelta(days=ago_days)).isoformat()
        for n, cost in enumerate(costs):
            s.add(FrontierTransmission(
                opportunity_id=opp.id, created_at=when, provider=provider,
                base_url="https://x", model="m", payload={},
                payload_sha256=f"{n}{ago_days}{provider}", status="sent", cost_usd=cost,
            ))
        await s.commit()


@respx.mock
async def test_other_providers_show_this_months_spend_from_the_local_log(sm):
    route = respx.route().mock(return_value=Response(500))
    other = _settings(key=None, anthropic_key="sk-ant-xyz")
    await _spend(sm, 0.40, 0.25)
    await _spend(sm, 9.0, ago_days=40)  # last month's does not count
    async with sm() as s:
        row = await hb.rail_row(s, other)
    assert row["kind"] == "local_spend" and row["label"] == "$0.65 spent this month on deeper reads"
    assert route.call_count == 0


async def test_an_unreported_cost_is_unknown_never_zero(sm):
    other = _settings(key=None, anthropic_key="sk-ant-xyz")
    await _spend(sm, None, None)
    async with sm() as s:
        assert (await hb.rail_row(s, other))["label"] == (
            "Deeper reads this month: cost not reported")
    await _spend(sm, 0.5, None, provider="openai", ago_days=0.1)
    async with sm() as s:
        assert (await hb.rail_row(s, other))["label"] == (
            "$0.50 spent this month on deeper reads, plus some unreported")


async def test_no_transmissions_means_no_line(sm):
    async with sm() as s:
        assert await hb.rail_row(s, _settings(key=None, anthropic_key="sk-ant-xyz")) is None
        assert await hb.rail_row(s, _settings(key=None)) is None  # no key at all


# ----------------------------------------------------------------------------- key hygiene
@respx.mock
async def test_the_key_never_reaches_the_logs_or_the_row(sm):
    respx.get(KEY_URL).mock(side_effect=httpx.ConnectError(f"boom {SECRET}"))
    with structlog.testing.capture_logs() as logs:
        async with sm() as s:
            row = await hb.rail_row(s, _settings())
    assert SECRET not in json.dumps(logs, default=str)
    assert SECRET not in json.dumps(row, default=str)


# --------------------------------------------------------------------------- the web layer
def _fresh(monkeypatch):
    from tests.test_hosted_mode import _fresh_app

    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
    return _fresh_app()


def _seed_web(*rows: tuple[str, object]) -> None:
    from kestrel.db.engine import forget_engine, get_sessionmaker

    async def go():
        async with get_sessionmaker()() as s:
            for k, v in rows:
                s.add(Setting(key=k, value=v))
            await s.commit()

    asyncio.run(go())
    forget_engine()


_HOSTED_ON = (
    ("llm.hosted_mode.enabled", True),
    ("llm.hosted_mode.acknowledged_at", _ACK),
    ("llm.hosted_mode.acknowledged_terms", HostedMode().disclosure_terms()),
)


def _ollama() -> None:
    """The rail also asks the local Ollama what is loaded; that is not under test here."""
    respx.route(host="127.0.0.1").mock(return_value=Response(200, json={"models": []}))


@respx.mock
def test_the_rail_shows_the_row_and_polls_use_the_cache(monkeypatch):
    client = _fresh(monkeypatch)
    _seed_web(*_HOSTED_ON)
    _ollama()
    route = respx.get(KEY_URL).mock(return_value=_key_json(usage_monthly=4.2))
    first = client.get("/status/rail").text
    second = client.get("/status/rail").text
    assert 'data-rail="balance"' in first and "$4.20 spent this month" in first
    assert "$4.20 spent this month" in second
    label = client.get("/api/status").json()["balance"]["label"]
    assert label == "$4.20 spent this month"
    assert route.call_count == 1
    assert SECRET not in first + second


@respx.mock
def test_the_rail_has_no_balance_row_when_nothing_is_fetched(monkeypatch):
    client = _fresh(monkeypatch)  # hosted mode off
    _ollama()
    route = respx.get(KEY_URL).mock(return_value=Response(500))
    html = client.get("/status/rail").text
    assert 'data-rail="balance"' not in html and route.call_count == 0
    assert client.get("/api/status").json()["balance"] is None


def test_settings_toggle_stores_the_choice_and_shows_only_with_an_openrouter_key(monkeypatch):
    client = _fresh(monkeypatch)
    page = client.get("/settings/hosted").text
    assert 'name="show_balance"' in page and "Always show balance" in page
    assert "one request that carries your key" in page.split('data-testid="balance-details"')[1]
    assert "—" not in page.split("Always show balance")[1][:900]
    r = client.post("/settings/hosted/balance", data={"show_balance": "on"})
    assert r.status_code == 200 and 'name="show_balance"' in r.text

    async def stored():
        from kestrel.db.engine import forget_engine, get_sessionmaker

        async with get_sessionmaker()() as s:
            row = (await s.execute(select(Setting).where(Setting.key == hb.SHOW_BALANCE_KEY))
                   ).scalar_one_or_none()
        forget_engine()
        return None if row is None else row.value

    assert asyncio.run(stored()) is True
    client.post("/settings/hosted/balance", data={})
    assert asyncio.run(stored()) is False


def test_a_spare_openrouter_key_is_not_asked_when_hosted_mode_uses_another_provider():
    # An Anthropic user with a leftover OpenRouter key in .env must not have it queried or shown.
    from kestrel.config import TierConfig

    s = _settings(anthropic_key="sk-ant-test")
    s.llm.tiers["frontier"] = s.llm.tiers["frontier"].model_copy(update={"backend": "anthropic"})
    s.llm.hosted_tiers = {t: TierConfig(backend="anthropic", model="claude-x")
                          for t in ("tier1", "tier2")}
    assert hb.eligible_backend(s, show_balance=True) is None


@respx.mock
async def test_concurrent_polls_as_the_cache_expires_send_one_request(sm):
    async def slow(request):
        await asyncio.sleep(0.05)
        return _key_json(limit=10.0, limit_remaining=4.2)

    route = respx.get(KEY_URL).mock(side_effect=slow)
    name, backend = hb.eligible_backend(_settings(), show_balance=False)
    await asyncio.gather(*(hb._reading(name, backend) for _ in range(4)))
    assert route.call_count == 1


async def test_month_spend_counts_every_billed_send_not_only_sent(sm):
    # A deeper read that left the machine was billed even when its reply quarantined or failed.
    await _spend(sm, 0.10)
    async with sm() as s:
        opp = (await s.execute(select(Opportunity))).scalars().first()
        when = datetime.now(UTC).isoformat()
        s.add(FrontierTransmission(
            opportunity_id=opp.id, created_at=when, provider="anthropic", base_url="https://x",
            model="m", payload={}, payload_sha256="q1", status="quarantined", cost_usd=0.05,
        ))
        s.add(FrontierTransmission(
            opportunity_id=opp.id, created_at=when, provider="anthropic", base_url="https://x",
            model="m", payload={}, payload_sha256="n1", status="failed", cost_usd=None,
            egressed=False,
        ))
        await s.commit()
        row = await hb.local_spend(s)
    assert row is not None and "$0.15" in row["label"]
    assert "unreported" not in row["label"]  # the unsent row is neither cost nor unknown
