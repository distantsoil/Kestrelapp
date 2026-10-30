"""USAJobs search board (B1, D-SRC-15). The API is respx-mocked from a response built from the
documented shape (tests/fixtures/usajobs_search.json, synthetic values, no real people); nothing
here touches the network. Under test: field mapping, pagination and its caps, the profile-titles
fallback, a missing key or email (partial, never a failure), 429 and 5xx (partial, keeps what was
collected), a rejected key (an authored message), the key never leaking, and that roles go through
the D-SRC-13 area and title drop."""

from __future__ import annotations

import json
import logging
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
from kestrel.ingest.adapters.usajobs import (
    KEY_PROBLEM_PHRASE,
    NEEDS_KEY,
    UsaJobsAdapter,
    is_key_problem,
    to_posting,
)
from kestrel.ingest.base import AdapterError, SourceConfigError
from kestrel.ingest.http import HttpClient
from kestrel.ingest.runner import run_source

FIX = Path(__file__).parent / "fixtures"
SEARCH = json.loads((FIX / "usajobs_search.json").read_text(encoding="utf-8"))
EMPTY = json.loads((FIX / "usajobs_empty.json").read_text(encoding="utf-8"))
API = "https://data.usajobs.gov/api/search"
KEY = "uj-secret-key-0123456789"
EMAIL = "analyst@example.com"
CFG = {"keyword": "analyst", "contact_email": EMAIL}
ITEMS = SEARCH["SearchResult"]["SearchResultItems"]


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("USAJOBS_API_KEY", KEY)


def _http(robots: bool = False) -> HttpClient:
    return HttpClient(user_agent=get_settings().ingest.user_agent, respect_robots=robots)


def _page(n: int, pages: int, items: list | None = None) -> dict:
    doc = json.loads(json.dumps(SEARCH))
    doc["SearchResult"]["SearchResultItems"] = ITEMS if items is None else items
    doc["SearchResult"]["UserArea"]["NumberOfPages"] = str(pages)
    return doc


def _query(call) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(str(call.request.url)).query).items()}


# ------------------------------------------------------------------------------ field mapping
def test_a_full_announcement_maps_every_field_the_pipeline_reads():
    p = to_posting(ITEMS[0])
    assert p.external_id == "800000001" and p.title == "Management and Program Analyst"
    assert p.org_name == "Office of Example Programs"
    assert p.url == "https://www.usajobs.gov/job/800000001"
    assert p.apply_url == "https://www.usajobs.gov/job/800000001"
    assert p.location_raw == "Austin, Texas"
    assert (p.comp_min, p.comp_max, p.comp_currency, p.comp_interval) == (
        85000.0, 110000.0, "USD", "year")
    assert p.date_posted == "2026-10-20" and p.employment_type == "Full-time"
    html = p.description_html
    assert "Closing date: 2026-11-14" in html
    for needle in ("Analyze program operations", "Review program data", "Advise managers",
                   "specialized experience", "U.S. citizen", "bachelor", "Health insurance"):
        assert needle in html
    assert "Apply online through the official announcement" not in html  # how-to is not the job
    assert p.raw == {"position_id": "ZZ-26-0001", "department": "Department of Example Affairs",
                     "closes": "2026-11-14"}
    assert p.description_md is None


def test_remote_and_telework_stay_unknown_unless_the_api_says():
    by_id = {p.external_id: p for p in map(to_posting, ITEMS)}
    assert (by_id["800000001"].is_remote, by_id["800000001"].remote_hint) == (None, "Hybrid")
    assert (by_id["800000002"].is_remote, by_id["800000002"].remote_hint) == (True, "Remote")
    assert (by_id["800000004"].is_remote, by_id["800000004"].remote_hint) == (False, "On-site")
    assert (by_id["800000003"].is_remote, by_id["800000003"].remote_hint) == (None, None)
    assert (by_id["800000005"].is_remote, by_id["800000005"].remote_hint) == (None, None)


def test_pay_intervals_unpaid_roles_and_multiple_locations():
    by_id = {p.external_id: p for p in map(to_posting, ITEMS)}
    hourly = by_id["800000004"]
    assert (hourly.comp_min, hourly.comp_max, hourly.comp_interval) == (19.5, 24.1, "hour")
    unpaid = by_id["800000005"]  # "Without Compensation": no pay is stated, none is invented
    assert (unpaid.comp_min, unpaid.comp_max, unpaid.comp_currency, unpaid.comp_interval) == (
        None, None, None, None)
    assert hourly.location_raw == "Multiple Locations"  # not a joined list the area check misreads
    assert by_id["800000005"].apply_url == "https://apply.example.gov/announcement/800000005"


def test_a_malformed_entry_is_skipped_not_fatal():
    assert to_posting({}) is None and to_posting({"MatchedObjectDescriptor": []}) is None
    assert to_posting({"MatchedObjectDescriptor": {}}) is None
    p = to_posting({"MatchedObjectId": 7, "MatchedObjectDescriptor": {"PositionTitle": "Clerk"}})
    assert p.external_id == "7" and p.title == "Clerk" and p.comp_min is None


# --------------------------------------------------------------------- request, paging, caps
@respx.mock
async def test_sends_the_key_and_email_headers_and_the_documented_parameters():
    route = respx.get(API).mock(return_value=Response(200, json=_page(1, 1)))
    cfg = {**CFG, "location": "Austin, Texas", "date_posted_days": 3, "results_per_page": 50}
    async with _http() as http:
        got = await UsaJobsAdapter().fetch(cfg, http)
    assert len(got) == 5 and got.partial_error is None
    sent = route.calls[0].request
    assert sent.headers["authorization-key"] == KEY and sent.headers["user-agent"] == EMAIL
    assert _query(route.calls[0]) == {
        "Keyword": "analyst", "LocationName": "Austin, Texas", "DatePosted": "3",
        "ResultsPerPage": "50", "WhoMayApply": "Public", "Fields": "Full", "Page": "1",
    }
    assert KEY not in str(sent.url)


@respx.mock
@pytest.mark.parametrize(
    ("place", "extra", "absent"),
    [("Remote", {"RemoteIndicator": "True"}, "LocationName"),
     ("", {}, "LocationName"), ("United States", {}, "LocationName")],
)
async def test_remote_and_nationwide_places_send_no_location(place, extra, absent):
    route = respx.get(API).mock(return_value=Response(200, json=_page(1, 1)))
    async with _http() as http:
        await UsaJobsAdapter().fetch({**CFG, "location": place}, http)
    q = _query(route.calls[0])
    assert absent not in q and {k: q.get(k) for k in extra} == extra


@respx.mock
async def test_follows_pages_up_to_the_cap_and_stops_at_the_last_page():
    route = respx.get(API).mock(side_effect=[
        Response(200, json=_page(1, 5, ITEMS[:2])), Response(200, json=_page(2, 5, ITEMS[2:4])),
        Response(200, json=_page(3, 5, ITEMS[4:])), Response(200, json=_page(4, 5, ITEMS)),
    ])
    async with _http() as http:
        got = await UsaJobsAdapter().fetch({**CFG, "max_pages": 3}, http)
    assert [_query(c)["Page"] for c in route.calls] == ["1", "2", "3"]
    assert [p.external_id for p in got] == [i["MatchedObjectId"] for i in ITEMS]



@respx.mock
async def test_it_stops_at_the_last_page_the_reply_names():
    route = respx.get(API).mock(side_effect=[Response(200, json=_page(1, 2, ITEMS[:3])),
                                             Response(200, json=_page(2, 2, ITEMS[3:]))])
    async with _http() as http:
        got = await UsaJobsAdapter().fetch({**CFG, "max_pages": 9}, http)
    assert route.call_count == 2 and len(got) == 5  # NumberOfPages said two


@respx.mock
async def test_an_empty_result_is_an_empty_ok_batch():
    respx.get(API).mock(return_value=Response(200, json=EMPTY))
    async with _http() as http:
        got = await UsaJobsAdapter().fetch(CFG, http)
    assert list(got) == [] and got.partial_error is None


@respx.mock
async def test_no_keyword_searches_each_profile_title_once_and_dedupes(sm):
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_dir().joinpath("profile.yml").write_text(
        "employment:\n  employment_terms:\n    target_titles: [Program Analyst, Data Analyst]\n",
        encoding="utf-8",
    )
    route = respx.get(API).mock(return_value=Response(200, json=_page(1, 1)))
    async with _http() as http:
        got = await UsaJobsAdapter().fetch({"contact_email": EMAIL}, http)
    assert [_query(c)["Keyword"] for c in route.calls] == ["Program Analyst", "Data Analyst"]
    assert len(got) == 5  # the second search returned the same announcements


@respx.mock
async def test_the_request_budget_is_capped_across_titles():
    titles = ", ".join(f"Title{i}" for i in range(5))
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_dir().joinpath("profile.yml").write_text(
        f"employment:\n  employment_terms:\n    target_titles: [{titles}]\n", encoding="utf-8")
    route = respx.get(API).mock(return_value=Response(200, json=_page(1, 10, ITEMS[:1])))
    async with _http() as http:
        got = await UsaJobsAdapter().fetch({"contact_email": EMAIL, "max_pages": 10}, http)
    assert route.call_count == 12 and "limit of requests" in got.partial_error


# ---------------------------------------------------------------- missing key, errors, secrecy
@respx.mock
@pytest.mark.parametrize("which", ["key", "email"])
async def test_a_missing_key_or_email_is_a_plain_partial_batch_and_asks_nothing(monkeypatch, which):
    route = respx.get(API).mock(return_value=Response(200, json=_page(1, 1)))
    cfg = dict(CFG)
    if which == "key":
        monkeypatch.delenv("USAJOBS_API_KEY", raising=False)
    else:
        cfg.pop("contact_email")
    async with _http() as http:
        got = await UsaJobsAdapter().fetch(cfg, http)
    assert list(got) == [] and got.partial_error == NEEDS_KEY and route.call_count == 0
    assert is_key_problem(got.partial_error) and KEY_PROBLEM_PHRASE in NEEDS_KEY


@pytest.mark.parametrize("status", [429, 500, 502])
@respx.mock
async def test_429_and_5xx_are_partial_and_keep_what_was_collected(status):
    route = respx.get(API).mock(side_effect=[
        Response(200, json=_page(1, 3, ITEMS[:2])),
        Response(status, json={"message": "provider words"}),
    ])
    async with _http() as http:
        got = await UsaJobsAdapter().fetch({**CFG, "max_pages": 3}, http)
    assert [p.external_id for p in got] == ["800000001", "800000002"]
    assert got.partial_error and "provider words" not in got.partial_error
    assert route.call_count == 2  # never retried, never asked for page 3
    assert "—" not in got.partial_error


@respx.mock
async def test_a_429_on_the_first_page_is_an_empty_partial_batch():
    respx.get(API).mock(return_value=Response(429))
    async with _http() as http:
        got = await UsaJobsAdapter().fetch(CFG, http)
    assert list(got) == [] and "slow down" in got.partial_error


@pytest.mark.parametrize(
    ("status", "needle"),
    [(401, KEY_PROBLEM_PHRASE), (403, KEY_PROBLEM_PHRASE), (400, "rejected the search")],
)
@respx.mock
async def test_a_refusal_becomes_an_authored_adapter_error(status, needle):
    respx.get(API).mock(return_value=Response(status, json={"message": "provider words"}))
    async with _http() as http:
        with pytest.raises(AdapterError) as ei:
            await UsaJobsAdapter().fetch(CFG, http)
    text = str(ei.value)
    assert needle in text and "provider words" not in text and KEY not in text
    assert is_key_problem(text) is (status in (401, 403))


@respx.mock
async def test_an_unreadable_reply_is_an_adapter_error():
    respx.get(API).mock(return_value=Response(200, json={"unexpected": True}))
    async with _http() as http:
        with pytest.raises(AdapterError, match="could not read"):
            await UsaJobsAdapter().fetch(CFG, http)


@respx.mock
async def test_the_key_never_reaches_a_log_or_an_error(caplog):
    caplog.set_level(logging.DEBUG)
    respx.get(API).mock(return_value=Response(401))
    with structlog.testing.capture_logs() as logs:
        async with _http() as http:
            with pytest.raises(AdapterError) as ei:
                await UsaJobsAdapter().fetch(CFG, http)
    assert KEY not in str(ei.value) and KEY not in json.dumps(logs, default=str)
    assert KEY not in caplog.text


@respx.mock
async def test_robots_txt_is_checked_and_a_disallow_skips_the_request():
    respx.get("https://data.usajobs.gov/robots.txt").mock(
        return_value=Response(200, text="User-agent: *\nDisallow: /api\n"))
    route = respx.get(API).mock(return_value=Response(200, json=_page(1, 1)))
    async with _http(robots=True) as http:
        with pytest.raises(AdapterError, match="robots.txt"):
            await UsaJobsAdapter().fetch(CFG, http)
    assert route.call_count == 0


def test_config_validation():
    a = UsaJobsAdapter()
    a.validate_config({})
    a.validate_config({**CFG, "location": "Remote", "remote_only": True, "max_pages": 4,
                       "results_per_page": 500, "date_posted_days": 60, "api_key_env": "MY_KEY"})
    for bad in (
        {"api_key": "x"}, {"api_key_env": "not a name"}, {"contact_email": "nope"},
        {"keyword": 5}, {"remote_only": "yes"}, {"max_pages": 0}, {"max_pages": 11},
        {"results_per_page": 501}, {"date_posted_days": 0}, {"date_posted_days": 61},
    ):
        with pytest.raises(SourceConfigError):
            a.validate_config(bad)
    assert get_adapter("usajobs") is not None


# ------------------------------------------------- the run: D-SRC-13 drop, partial, no failure
async def _source(sm, **cfg) -> Source:
    async with sm() as s:
        src = Source(track="employment", kind="usajobs", name="Analysts on USAJobs",
                     config={**CFG, **cfg}, enabled=True, cadence_hours=24)
        s.add(src)
        await s.commit()
        await s.refresh(src)
        return src


def _profile(titles: str = "[Program Analyst, Management Analyst, Data Analyst]") -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_dir().joinpath("profile.yml").write_text(
        "employment:\n  employment_terms:\n    target_titles: " + titles + "\n"
        "  geo_rules:\n    remote_country_allowlist: [US]\n", encoding="utf-8")


@respx.mock
async def test_roles_go_through_the_area_and_title_drop_before_they_are_stored(sm):
    _profile()
    respx.get(API).mock(return_value=Response(200, json=_page(1, 1)))
    src = await _source(sm)
    async with _http() as http:
        out = await run_source(sm, src.id, http=http)
    assert out.status == "ok", out.error
    c = out.counts
    assert c["fetched"] == 5 and c["dropped_outside_area"] == 1  # Frankfurt, Germany
    assert c["dropped_unrelated_title"] == 1  # Forestry Technician
    assert c["new"] == 3
    async with sm() as s:
        opps = (await s.execute(select(Opportunity))).scalars().all()
        assert sorted(o.title for o in opps) == [
            "Data Analyst (Remote)", "Management Analyst", "Management and Program Analyst"]
        remote = next(o for o in opps if o.title.startswith("Data Analyst"))
        assert remote.remote_type == "remote" and remote.source_id == src.id
        hybrid = next(o for o in opps if o.title.startswith("Management and"))
        assert hybrid.remote_type == "hybrid" and hybrid.comp_min == 85000
        assert hybrid.state == "TX" and hybrid.url == "https://www.usajobs.gov/job/800000001"


@respx.mock
async def test_a_run_without_a_key_says_so_in_the_run_record_and_is_not_a_failure(sm, monkeypatch):
    monkeypatch.delenv("USAJOBS_API_KEY", raising=False)
    route = respx.get(API).mock(return_value=Response(200, json=_page(1, 1)))
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
    respx.get(API).mock(side_effect=[Response(200, json=_page(1, 3, ITEMS[:2])), Response(429)])
    src = await _source(sm, max_pages=3)
    async with _http() as http:
        out = await run_source(sm, src.id, http=http)
    assert out.status == "partial" and "slow down" in out.error and out.counts["new"] == 2
    async with sm() as s:
        assert (await s.get(Source, src.id)).consecutive_failures == 0


@respx.mock
async def test_a_rejected_key_is_a_failure_that_names_the_fix(sm):
    respx.get(API).mock(return_value=Response(401))
    src = await _source(sm)
    async with _http() as http:
        out = await run_source(sm, src.id, http=http)
    assert out.status == "partial" and KEY_PROBLEM_PHRASE in out.error
    async with sm() as s:
        assert (await s.get(Source, src.id)).consecutive_failures == 1
