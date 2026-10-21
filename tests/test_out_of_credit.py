"""An online key that runs out of credit says so plainly (#29 follow-up, D-LLM-8).

Fixture replies carry each provider's documented out-of-credit shape (REFERENCES.md): OpenModel
HTTP 402 INSUFFICIENT_BALANCE, OpenAI `credit_balance_exhausted`, OpenRouter HTTP 402, Anthropic
HTTP 402 `billing_error`. Every provider call is respx-mocked."""

from __future__ import annotations

import asyncio

import pytest
import respx
from httpx import Response

from kestrel.config import BackendConfig, get_settings
from kestrel.llm import credit
from kestrel.llm.client import LLMClient
from kestrel.llm.credit import OutOfCredit, is_out_of_credit
from kestrel.llm.guard import run_guarded
from kestrel.llm.schemas import P1Triage

_MSGS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "triage this"}]
_PROVIDER_TEXT = "SECRET-PROVIDER-TEXT-acct_123"

# provider -> (backend kwargs, url, reply, billing page fragment)
CASES = {
    "openmodel": (
        dict(base_url="https://api.openmodel.ai/v1", api_key_env="OPENMODEL_API_KEY",
             protocol="responses"),
        "https://api.openmodel.ai/v1/responses",
        Response(402, json={"error": {"code": "INSUFFICIENT_BALANCE", "message": _PROVIDER_TEXT}}),
        "console.openmodel.ai",
    ),
    "openai": (
        dict(base_url="https://api.openai.com/v1", api_key_env="OPENAI_API_KEY"),
        "https://api.openai.com/v1/chat/completions",
        Response(429, json={"error": {"code": "credit_balance_exhausted",
                                      "message": _PROVIDER_TEXT}}),
        "platform.openai.com",
    ),
    "openrouter": (
        dict(base_url="https://openrouter.ai/api/v1", api_key_env="OPENROUTER_API_KEY"),
        "https://openrouter.ai/api/v1/chat/completions",
        Response(402, json={"error": {"code": 402, "message": _PROVIDER_TEXT}}),
        "openrouter.ai/settings",
    ),
    "anthropic": (
        dict(base_url="https://api.anthropic.com/v1", api_key_env="ANTHROPIC_API_KEY",
             protocol="messages"),
        "https://api.anthropic.com/v1/messages",
        Response(402, json={"type": "error",
                            "error": {"type": "billing_error", "message": _PROVIDER_TEXT}}),
        "platform.claude.com",
    ),
}


def _settings(provider: str, monkeypatch):
    kw = CASES[provider][0]
    monkeypatch.setenv(kw["api_key_env"], "key-for-test")
    s = get_settings().model_copy(deep=True)
    s.llm.backends["frontier"] = BackendConfig(
        thinking_control="none", external=True, provider=provider, **kw
    )
    s.llm.tiers["frontier"].backend = "frontier"
    s.llm.tiers["frontier"].model = "some-model"
    return s


def _guard(client):
    return asyncio.run(run_guarded(client, tier="frontier", messages=_MSGS, model_cls=P1Triage,
                                   allow_external=True))


@pytest.mark.parametrize("provider", list(CASES))
@respx.mock
def test_each_providers_out_of_credit_reply_is_one_plain_non_retried_failure(
    provider, monkeypatch
):
    _kw, url, reply, page = CASES[provider]
    route = respx.post(url).mock(return_value=reply)
    res = _guard(LLMClient(_settings(provider, monkeypatch)))
    assert route.call_count == 1  # no repair, no length retry, no re-ask
    assert not res.ok and not res.quarantined and res.out_of_credit
    assert "run out" in res.error and page in res.error
    assert _PROVIDER_TEXT not in res.error and "HTTP" not in res.error
    assert res.egressed  # the provider received the request


@respx.mock
def test_after_one_failure_nothing_more_is_sent_to_that_backend(monkeypatch):
    url, reply = CASES["openmodel"][1:3]
    route = respx.post(url).mock(return_value=reply)
    client = LLMClient(_settings("openmodel", monkeypatch))
    first, second = _guard(client), _guard(client)
    assert route.call_count == 1
    assert second.out_of_credit and second.error == first.error
    assert not second.egressed  # the hold stopped it before anything was sent
    assert len(credit.notices()) == 1  # one notice, not one per role


@respx.mock
def test_a_new_key_or_an_explicit_clear_releases_the_hold(monkeypatch):
    url, reply = CASES["openmodel"][1:3]
    route = respx.post(url).mock(return_value=reply)
    client = LLMClient(_settings("openmodel", monkeypatch))
    _guard(client)
    credit.clear()
    _guard(client)
    assert route.call_count == 2
    monkeypatch.setenv("OPENMODEL_API_KEY", "a-different-key")
    _guard(client)
    assert route.call_count == 3


@respx.mock
def test_complete_raises_the_authored_error_and_never_falls_back(monkeypatch):
    url, reply = CASES["openrouter"][1:3]
    respx.post(url).mock(return_value=reply)
    client = LLMClient(_settings("openrouter", monkeypatch))
    with pytest.raises(OutOfCredit) as ei:
        asyncio.run(client.complete(tier="frontier", messages=_MSGS, allow_external=True))
    assert ei.value.provider == "openrouter" and _PROVIDER_TEXT not in str(ei.value)
    # No other online backend was touched: respx would have raised on an unmocked URL.


def test_classifier_edges():
    assert is_out_of_credit("generic", 402)
    assert is_out_of_credit("openai", 429, ("insufficient_quota",))
    assert not is_out_of_credit("openai", 429, ("rate_limit_exceeded",))
    assert not is_out_of_credit("openrouter", 429)
    assert not is_out_of_credit("anthropic", 400, (), "bad schema")
    assert is_out_of_credit("anthropic", 400, (), "Your credit balance is too low")


@respx.mock
def test_a_plain_rate_limit_is_still_a_transient_request_error(monkeypatch):
    url = CASES["openai"][1]
    respx.post(url).mock(return_value=Response(
        429, json={"error": {"code": "rate_limit_exceeded", "message": "slow down"}}))
    res = _guard(LLMClient(_settings("openai", monkeypatch)))
    assert not res.ok and not res.out_of_credit and res.error.startswith("request error")
    assert credit.notices() == []


def test_the_message_has_no_em_dash():
    for p in [*credit.PROVIDER_BILLING, "generic"]:
        assert "—" not in credit.credit_message(p)


# ------------------------------------------------------------------ worker, rail and Runs page
def _hold(monkeypatch, provider="openmodel"):
    monkeypatch.setenv(CASES[provider][0]["api_key_env"], "key-for-test")
    credit.mark_out_of_credit("frontier", provider, "key-for-test")


def test_the_rail_and_runs_page_show_one_authored_notice(monkeypatch):
    from fastapi.testclient import TestClient

    from kestrel.db import migrate
    from kestrel.web.app import build_app

    migrate.run_migrations()
    app = build_app(worker=False)
    _hold(monkeypatch)
    with TestClient(app) as c:
        rail = c.get("/status/rail").text
        runs = c.get("/runs").text
    for page in (rail, runs):
        assert page.count("Your OpenModel credit has run out") == 1
        assert "—" not in credit.credit_message("openmodel")
    assert 'data-rail="credit"' in rail and 'data-testid="credit-notice"' in runs
    credit.clear()
    with TestClient(app) as c:
        assert 'data-rail="credit"' not in c.get("/status/rail").text


def test_a_deferred_task_goes_back_unstarted_and_keeps_its_attempt():
    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker
    from kestrel.queue.queue import TaskQueue

    async def go():
        migrate.run_migrations()
        q = TaskQueue(get_sessionmaker())
        tid = await q.enqueue("t1", {"opportunity_id": 1})
        task = await q.lease("w", types=["t1"])
        assert task.attempts == 1
        assert await q.defer(tid, "credit", worker_id="w") == "pending"
        again = await q.get(tid)
        return again.status, again.attempts, again.last_error

    assert asyncio.run(go()) == ("pending", 0, "credit")


def test_the_worker_defers_the_task_and_leases_nothing_more_for_that_tier(monkeypatch):
    from sqlalchemy import select

    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker
    from kestrel.db.models import Task
    from kestrel.pipeline._shared import HandlerOutcome
    from kestrel.pipeline.worker import TierStats, Worker, held_types

    settings = _settings("openmodel", monkeypatch)
    settings.llm.tiers["tier1"].backend = "frontier"
    calls: list[int] = []

    async def handler(sm, client, settings, *, opportunity_id, **_kw):
        calls.append(opportunity_id)
        credit.mark_out_of_credit("frontier", "openmodel", "key-for-test")
        return HandlerOutcome("error", 1, error=credit.credit_message("openmodel"),
                              out_of_credit=True)

    async def go():
        migrate.run_migrations()
        sm = get_sessionmaker()
        worker = Worker(sm, LLMClient(settings), settings)
        for i in (1, 2, 3):
            await worker._queue.enqueue("t1", {"opportunity_id": i})
        stats = TierStats()
        await worker.drain_tier("t1", 1, handler, stats)
        async with sm() as s:
            rows = (await s.execute(select(Task).order_by(Task.id))).scalars().all()
        return stats, [(t.status, t.attempts) for t in rows], rows[0].last_error

    stats, rows, note = asyncio.run(go())
    assert len(calls) == 1 and held_types(settings) == ("t1",)
    assert rows == [("pending", 0)] * 3 and stats.errors == 0 and stats.completed == 0
    assert "credit has run out" in note


# --------------------------------------------------------- the hold follows each task's own model
def _two_group_settings(monkeypatch):
    s = _settings("openmodel", monkeypatch)
    s.llm.hosted_mode.enabled = True
    s.llm.hosted_mode.acknowledged_at = "2026-10-12T10:00:00+00:00"
    s.llm.hosted_mode.acknowledged_terms = s.llm.hosted_mode.disclosure_terms()
    return s


def test_a_held_hosted_override_group_is_not_leased_while_a_local_group_still_runs(monkeypatch):
    from sqlalchemy import select

    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker
    from kestrel.db.models import Task
    from kestrel.pipeline._shared import HandlerOutcome
    from kestrel.pipeline.worker import TierStats, Worker

    settings = _two_group_settings(monkeypatch)
    credit.mark_out_of_credit("frontier", "openmodel", "key-for-test")
    hosted = {"backend": "frontier", "model": "some-model"}
    local = {"backend": "local", "model": "kestrel-tier2"}
    ran: list[int] = []
    leases: list[int] = []

    async def handler(sm, client, settings, *, opportunity_id, **_kw):
        ran.append(opportunity_id)
        return HandlerOutcome("t2_done", 2)

    async def go():
        migrate.run_migrations()
        sm = get_sessionmaker()
        worker = Worker(sm, LLMClient(settings), settings)
        real = worker._queue.lease

        async def counting(*a, **kw):
            task = await real(*a, **kw)
            if task is not None:
                leases.append(task.id)
            return task

        worker._queue.lease = counting
        for i in (1, 2):
            await worker._queue.enqueue("t2", {"opportunity_id": i, "model_override": hosted})
        await worker._queue.enqueue("t2", {"opportunity_id": 3, "model_override": local})
        await asyncio.wait_for(worker.drain_tier("t2", 1, handler, TierStats()), 20)
        async with sm() as s:
            rows = (await s.execute(select(Task).order_by(Task.id))).scalars().all()
        return [(t.status, t.attempts) for t in rows]

    rows = asyncio.run(go())
    assert ran == [3] and len(leases) == 1  # the held group was never leased, not even once
    assert rows == [("pending", 0), ("pending", 0), ("done", 1)]


def test_a_held_configured_backend_does_not_starve_a_local_override(monkeypatch):
    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker
    from kestrel.pipeline._shared import HandlerOutcome
    from kestrel.pipeline.worker import TierStats, Worker

    settings = _two_group_settings(monkeypatch)
    settings.llm.tiers["tier2"].backend = "frontier"
    settings.llm.tiers["tier2"].model = "some-model"
    credit.mark_out_of_credit("frontier", "openmodel", "key-for-test")
    ran: list[int] = []

    async def handler(sm, client, settings, *, opportunity_id, **_kw):
        ran.append(opportunity_id)
        return HandlerOutcome("t2_done", 2)

    async def go():
        migrate.run_migrations()
        worker = Worker(get_sessionmaker(), LLMClient(settings), settings)
        await worker._queue.enqueue("t2", {"opportunity_id": 1})
        await worker._queue.enqueue(
            "t2", {"opportunity_id": 2, "model_override": {"backend": "local",
                                                           "model": "kestrel-tier2"}})
        await asyncio.wait_for(worker.drain_tier("t2", 1, handler, TierStats()), 20)

    asyncio.run(go())
    assert ran == [2]


_MAX_TOKENS_402 = (
    "This request requires more credits, or fewer max_tokens. You requested up to 8000 tokens, "
    "but can only afford 120"
)


def test_an_openrouter_402_about_max_tokens_is_not_an_empty_account():
    assert not is_out_of_credit("openrouter", 402, (), _MAX_TOKENS_402)
    assert is_out_of_credit("openrouter", 402, (), "Insufficient credits")
    assert is_out_of_credit("openmodel", 402, (), _MAX_TOKENS_402)  # only OpenRouter says this


@respx.mock
def test_a_too_big_request_says_so_and_does_not_hold_the_backend(monkeypatch):
    url = CASES["openrouter"][1]
    route = respx.post(url).mock(return_value=Response(
        402, json={"error": {"code": 402, "message": _MAX_TOKENS_402}}))
    client = LLMClient(_settings("openrouter", monkeypatch))
    first = _guard(client)
    second = _guard(client)
    assert route.call_count == 2  # the next request is still sent: nothing is held
    assert not first.ok and not first.out_of_credit and not first.quarantined
    assert "Your OpenRouter credit is too low for this request size." in first.error
    assert credit.notices() == [] and credit.held_provider("frontier", "key-for-test") is None
    assert not second.out_of_credit
