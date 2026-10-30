"""Adzuna search board (B2, D-SRC-15). The API is respx-mocked from a response built from the
documented shape (tests/fixtures/adzuna_search.json, synthetic values, no real people); nothing
here touches the network. Under test: field mapping (including the partial-description marker and
a predicted salary), paging and the request budget, the profile-titles fallback, a missing key
(partial, never a failure), 429 and 5xx (partial, keeps what was collected), a rejected key (an
authored message), the keys never leaking, and that roles go through the D-SRC-13 title drop."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import respx
import structlog
from httpx import Response
from sqlalchemy import select

from kestrel import paths
from kestrel.config import get_settings
from kestrel.db.models import Opportunity, ScrapeRun, Source
from kestrel.ingest.adapters import get_adapter
from kestrel.ingest.adapters.adzuna import (
    KEY_PROBLEM_PHRASE,
    NEEDS_KEY,
    SNIPPET_NOTE,
    AdzunaAdapter,
    is_key_problem,
    to_posting,
)
from kestrel.ingest.base import AdapterError, SourceConfigError
from kestrel.ingest.http import HttpClient
from kestrel.ingest.runner import run_source

FIX = Path(__file__).parent / "fixtures"
SEARCH = json.loads((FIX / "adzuna_search.json").read_text(encoding="utf-8"))
EMPTY = json.loads((FIX / "adzuna_empty.json").read_text(encoding="utf-8"))
API = "https://api.adzuna.com/v1/api/jobs/us/search/1"
APP_ID = "az-app-id-7391"
APP_KEY = "az-secret-key-0123456789"
CFG = {"keyword": "analyst"}
ITEMS = SEARCH["results"]


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID_KEY", APP_ID)
    monkeypatch.setenv("ADZUNA_APP_KEY", APP_KEY)


def _http() -> HttpClient:
    return HttpClient(user_agent=get_settings().ingest.user_agent, respect_robots=False)


def _page(items: list | None = None, count: int | None = None) -> dict:
    doc = json.loads(json.dumps(SEARCH))
    doc["results"] = ITEMS if items is None else items
    if count is not None:
        doc["count"] = count
    return doc


def _query(call) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(str(call.request.url)).query).items()}


def _route(page: int):
    return respx.get(f"https://api.adzuna.com/v1/api/jobs/us/search/{page}")


# ------------------------------------------------------------------------------ field mapping
def test_a_full_ad_maps_every_field_the_pipeline_reads():
    p = to_posting(ITEMS[0])
    assert p.external_id == "5000000001" and p.title == "Program Analyst"
    assert p.org_name == "Harbor Point Analytics" and p.location_raw == "Columbus, Ohio"
    assert p.url.startswith("https://www.adzuna.com/land/ad/5000000001")
    assert p.apply_url == p.url  # the redirect page is the posting link (and Adzuna's credit link)
    assert (p.comp_min, p.comp_max, p.comp_currency, p.comp_interval) == (
        62000.0, 78000.0, "USD", "year")
    assert p.date_posted == "2026-10-27T14:03:11Z" and p.employment_type == "Full-time, Permanent"
    assert p.raw["salary_predicted"] is False and p.raw["category"] == "Admin Jobs"
    assert p.is_remote is None and p.description_md is None


def test_the_description_is_marked_partial_in_the_text_and_the_record():
    for item in ITEMS:
        p = to_posting(item)
        assert p.raw["description_partial"] is True
        assert p.description_html.rstrip().endswith(f"<em>{SNIPPET_NOTE}</em></p>")
        assert "—" not in SNIPPET_NOTE
    assert "Analyze program data" in to_posting(ITEMS[0]).description_html


def test_a_predicted_salary_is_never_posted_pay():
    p = to_posting(ITEMS[1])  # salary_is_predicted "1"
    assert (p.comp_min, p.comp_max, p.comp_currency, p.comp_interval) == (None, None, None, None)
    assert p.raw["salary_predicted"] is True
    assert (p.raw["predicted_min"], p.raw["predicted_max"]) == (55000.0, 70000.0)
    numeric = dict(ITEMS[1], salary_is_predicted=1)
    assert to_posting(numeric).comp_min is None


def test_missing_pay_company_and_a_one_sided_range():
    no_pay = to_posting(ITEMS[2])
    assert no_pay.comp_min is None and no_pay.employment_type == "Part-time, Contract"
    one = to_posting(ITEMS[4])  # only salary_max, no company, no display name
    assert one.org_name is None and one.location_raw == "Columbus, Ohio"
    assert (one.comp_min, one.comp_max, one.comp_interval) == (65000.0, 65000.0, "year")


def test_a_malformed_entry_is_skipped_not_fatal():
    assert to_posting({}) is None and to_posting("x") is None
    p = to_posting({"id": 7, "title": "Clerk"})
    assert p.external_id == "7" and p.title == "Clerk" and p.comp_min is None


# --------------------------------------------------------------------- request, paging, budget
@respx.mock
async def test_sends_the_documented_parameters_and_no_headers_with_keys():
    route = _route(1).mock(return_value=Response(200, json=_page(count=5)))
    cfg = {**CFG, "location": "Columbus, Ohio", "date_posted_days": 3, "results_per_page": 20}
    async with _http() as http:
        got = await AdzunaAdapter().fetch(cfg, http)
    assert len(got) == 5 and got.partial_error is None
    assert _query(route.calls[0]) == {
        "app_id": APP_ID, "app_key": APP_KEY, "what": "analyst", "where": "Columbus, Ohio",
        "max_days_old": "3", "results_per_page": "20", "sort_by": "date",
        "content-type": "application/json",
    }
    assert APP_KEY not in json.dumps(dict(route.calls[0].request.headers))


@respx.mock
@pytest.mark.parametrize(
    ("place", "what", "has_where"),
    [("Remote", "analyst remote", False), ("", "analyst", False),
     ("United States", "analyst", False), ("Columbus, Ohio", "analyst", True)],
)
async def test_remote_and_nationwide_places(place, what, has_where):
    route = _route(1).mock(return_value=Response(200, json=_page(count=5)))
    async with _http() as http:
        await AdzunaAdapter().fetch({**CFG, "location": place}, http)
    q = _query(route.calls[0])
    assert q["what"] == what and ("where" in q) is has_where


@respx.mock
async def test_follows_pages_up_to_the_cap_and_stops_at_the_last_page():
    r1 = _route(1).mock(return_value=Response(200, json=_page(ITEMS[:2], count=150)))
    r2 = _route(2).mock(return_value=Response(200, json=_page(ITEMS[2:4], count=150)))
    r3 = _route(3).mock(return_value=Response(200, json=_page(ITEMS[4:], count=150)))
    async with _http() as http:
        got = await AdzunaAdapter().fetch({**CFG, "max_pages": 2, "results_per_page": 2}, http)
    assert (r1.call_count, r2.call_count, r3.call_count) == (1, 1, 0)  # the cap, not the count
    assert [p.external_id for p in got] == [i["id"] for i in ITEMS[:4]]


@respx.mock
async def test_it_stops_when_the_count_says_there_is_no_next_page():
    r1 = _route(1).mock(return_value=Response(200, json=_page(count=5)))
    r2 = _route(2).mock(return_value=Response(200, json=_page(count=5)))
    async with _http() as http:
        got = await AdzunaAdapter().fetch({**CFG, "max_pages": 5, "results_per_page": 5}, http)
    assert r1.call_count == 1 and r2.call_count == 0 and len(got) == 5


@respx.mock
async def test_an_empty_result_is_an_empty_ok_batch():
    _route(1).mock(return_value=Response(200, json=EMPTY))
    async with _http() as http:
        got = await AdzunaAdapter().fetch(CFG, http)
    assert list(got) == [] and got.partial_error is None


@respx.mock
async def test_no_keyword_searches_each_profile_title_once_and_dedupes():
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_dir().joinpath("profile.yml").write_text(
        "employment:\n  employment_terms:\n    target_titles: [Program Analyst, Data Analyst]\n",
        encoding="utf-8",
    )
    route = _route(1).mock(return_value=Response(200, json=_page(count=5)))
    async with _http() as http:
        got = await AdzunaAdapter().fetch({}, http)
    assert [_query(c)["what"] for c in route.calls] == ["Program Analyst", "Data Analyst"]
    assert len(got) == 5  # the second search returned the same ads


@respx.mock
async def test_the_request_budget_is_capped_across_titles():
    titles = ", ".join(f"Title{i}" for i in range(5))
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_dir().joinpath("profile.yml").write_text(
        f"employment:\n  employment_terms:\n    target_titles: [{titles}]\n", encoding="utf-8")
    for n in range(1, 6):
        _route(n).mock(return_value=Response(200, json=_page(ITEMS[:1], count=100000)))
    async with _http() as http:
        got = await AdzunaAdapter().fetch(
            {"max_pages": 5, "results_per_page": 1}, http)
    total = sum(r.call_count for r in respx.routes)
    assert total == 8 and "limit of requests" in got.partial_error


# ---------------------------------------------------------------- missing key, errors, secrecy
@respx.mock
@pytest.mark.parametrize("which", ["ADZUNA_APP_ID_KEY", "ADZUNA_APP_KEY"])
async def test_a_missing_key_is_a_plain_partial_batch_and_asks_nothing(monkeypatch, which):
    route = _route(1).mock(return_value=Response(200, json=_page(count=5)))
    monkeypatch.delenv(which, raising=False)
    async with _http() as http:
        got = await AdzunaAdapter().fetch(CFG, http)
    assert list(got) == [] and got.partial_error == NEEDS_KEY and route.call_count == 0
    assert is_key_problem(got.partial_error) and KEY_PROBLEM_PHRASE in NEEDS_KEY


@pytest.mark.parametrize("status", [429, 500, 502])
@respx.mock
async def test_429_and_5xx_are_partial_and_keep_what_was_collected(status):
    _route(1).mock(return_value=Response(200, json=_page(ITEMS[:2], count=100)))
    r2 = _route(2).mock(return_value=Response(status, json={"exception": "provider words"}))
    r3 = _route(3).mock(return_value=Response(200, json=_page()))
    async with _http() as http:
        got = await AdzunaAdapter().fetch({**CFG, "max_pages": 3, "results_per_page": 2}, http)
    assert [p.external_id for p in got] == ["5000000001", "5000000002"]
    assert got.partial_error and "provider words" not in got.partial_error
    assert r2.call_count == 1 and r3.call_count == 0  # never retried, never asked for page 3
    assert "—" not in got.partial_error


@pytest.mark.parametrize(
    ("status", "needle"),
    [(401, KEY_PROBLEM_PHRASE), (403, KEY_PROBLEM_PHRASE), (400, "rejected the search")],
)
@respx.mock
async def test_a_refusal_becomes_an_authored_adapter_error(status, needle):
    _route(1).mock(return_value=Response(status, json={"exception": "provider words"}))
    async with _http() as http:
        with pytest.raises(AdapterError) as ei:
            await AdzunaAdapter().fetch(CFG, http)
    text = str(ei.value)
    assert needle in text and "provider words" not in text
    assert APP_KEY not in text and APP_ID not in text
    assert is_key_problem(text) is (status in (401, 403))


@respx.mock
async def test_an_unreadable_reply_is_an_adapter_error():
    _route(1).mock(return_value=Response(200, json={"unexpected": True}))
    async with _http() as http:
        with pytest.raises(AdapterError, match="could not read"):
            await AdzunaAdapter().fetch(CFG, http)


@respx.mock
async def test_the_keys_never_reach_a_structlog_event_or_an_error():
    _route(1).mock(return_value=Response(401))
    with structlog.testing.capture_logs() as logs:
        async with _http() as http:
            with pytest.raises(AdapterError) as ei:
                await AdzunaAdapter().fetch(CFG, http)
    for secret in (APP_KEY, APP_ID):
        assert secret not in str(ei.value) and secret not in json.dumps(logs, default=str)


@respx.mock
@pytest.mark.usefixtures("_isolate_data_home")
async def test_the_keys_do_not_reach_kestrel_log_even_through_httpxs_own_request_line():
    """httpx logs each request line at INFO with the full URL, and Adzuna's keys ride in the query
    string, so the log file's handler masks them."""
    from kestrel import logging_setup

    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    logging_setup._configured = False
    try:
        paths.ensure_dirs()
        logging_setup.configure_logging("INFO")
        _route(1).mock(return_value=Response(200, json=_page(count=5)))
        async with _http() as http:
            await AdzunaAdapter().fetch(CFG, http)
        for h in root.handlers:
            h.flush()
        text = (paths.logs_dir() / "kestrel.log").read_text(encoding="utf-8")
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()
        root.setLevel(level)
        logging_setup._configured = False
        structlog.reset_defaults()
    # Compare each logged URL's parsed host exactly, not a substring of the log text.
    logged_hosts = [urlparse(u).hostname for u in re.findall(r"https?://[^\s\"']+", text)]
    assert logged_hosts.count("api.adzuna.com") >= 1, "the request line should still be logged"
    assert APP_KEY not in text and APP_ID not in text and "app_key=***" in text


@respx.mock
async def test_the_api_call_does_not_fetch_robots_txt():
    """api.adzuna.com disallows all crawlers; this is the published API used with the user's own
    key (documented in the module), so no robots.txt request is made even when the client
    respects robots."""
    robots = respx.get("https://api.adzuna.com/robots.txt").mock(
        return_value=Response(200, text="User-agent: *\nDisallow: /\n"))
    route = _route(1).mock(return_value=Response(200, json=_page(count=5)))
    async with HttpClient(user_agent=get_settings().ingest.user_agent,
                          respect_robots=True) as http:
        got = await AdzunaAdapter().fetch(CFG, http)
    assert len(got) == 5 and route.call_count == 1 and robots.call_count == 0


def test_config_validation():
    a = AdzunaAdapter()
    a.validate_config({})
    a.validate_config({**CFG, "location": "Remote", "max_pages": 5, "results_per_page": 50,
                       "date_posted_days": 60, "app_id_env": "MY_ID_KEY"})
    for bad in (
        {"app_key": "x"}, {"app_id": "x"}, {"app_key_env": "not a name"}, {"keyword": 5},
        {"max_pages": 0}, {"max_pages": 6}, {"results_per_page": 51}, {"date_posted_days": 0},
        {"date_posted_days": 61},
    ):
        with pytest.raises(SourceConfigError):
            a.validate_config(bad)
    assert get_adapter("adzuna") is not None


# ------------------------------------------------- the run: D-SRC-13 drop, partial, no failure
async def _source(sm, **cfg) -> Source:
    async with sm() as s:
        src = Source(track="employment", kind="adzuna", name="Analysts on Adzuna",
                     config={**CFG, **cfg}, enabled=True, cadence_hours=24)
        s.add(src)
        await s.commit()
        await s.refresh(src)
        return src


def _profile() -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_dir().joinpath("profile.yml").write_text(
        "employment:\n  employment_terms:\n"
        "    target_titles: [Program Analyst, Management Analyst, Data Analyst]\n"
        "  geo_rules:\n    remote_country_allowlist: [US]\n", encoding="utf-8")


@respx.mock
async def test_roles_go_through_the_title_drop_and_keep_the_partial_marker(sm):
    _profile()
    _route(1).mock(return_value=Response(200, json=_page(count=5)))
    src = await _source(sm)
    async with _http() as http:
        out = await run_source(sm, src.id, http=http)
    assert out.status == "ok", out.error
    c = out.counts
    assert c["fetched"] == 5 and c["dropped_unrelated_title"] == 1  # Forestry Technician
    assert c["new"] == 4
    async with sm() as s:
        opps = (await s.execute(select(Opportunity))).scalars().all()
        assert "Forestry Technician" not in {o.title for o in opps}
        analyst = next(o for o in opps if o.title == "Program Analyst")
        assert analyst.comp_min == 62000 and analyst.source_id == src.id
        assert SNIPPET_NOTE in analyst.description_md
        predicted = next(o for o in opps if o.title == "Data Analyst")
        assert predicted.comp_min is None  # an estimate never becomes the role's pay


@respx.mock
async def test_a_run_without_keys_says_so_in_the_run_record_and_is_not_a_failure(sm, monkeypatch):
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)
    route = _route(1).mock(return_value=Response(200, json=_page(count=5)))
    src = await _source(sm)
    async with _http() as http:
        out = await run_source(sm, src.id, http=http)
    assert out.status == "partial" and out.error == NEEDS_KEY and route.call_count == 0
    assert out.counts["fetched"] == 0 and out.counts["errors"] == 0
    async with sm() as s:
        row = await s.get(Source, src.id)
        run = (await s.execute(select(ScrapeRun))).scalar_one()
        assert row.consecutive_failures == 0 and row.enabled
        assert run.status == "partial" and run.error == NEEDS_KEY


@respx.mock
async def test_a_429_run_is_partial_keeps_the_page_and_never_counts_a_failure(sm):
    _profile()
    _route(1).mock(return_value=Response(200, json=_page(ITEMS[:2], count=100)))
    _route(2).mock(return_value=Response(429))
    src = await _source(sm, max_pages=3, results_per_page=2)
    async with _http() as http:
        out = await run_source(sm, src.id, http=http)
    assert out.status == "partial" and "slow down" in out.error and out.counts["new"] == 2
    async with sm() as s:
        assert (await s.get(Source, src.id)).consecutive_failures == 0


@respx.mock
async def test_a_rejected_key_is_a_failure_that_names_the_fix(sm):
    _route(1).mock(return_value=Response(401))
    src = await _source(sm)
    async with _http() as http:
        out = await run_source(sm, src.id, http=http)
    assert out.status == "partial" and KEY_PROBLEM_PHRASE in out.error
    async with sm() as s:
        assert (await s.get(Source, src.id)).consecutive_failures == 1
