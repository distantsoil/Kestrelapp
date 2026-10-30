"""Remote job boards (B3, D-SRC-15): Himalayas (JSON API) and We Work Remotely (RSS), both keyless.
The payloads are captured shapes (tests/fixtures/himalayas_search.json and
weworkremotely_feed.rss, trimmed, no people; the last items of each are edited copies for the
expired, non-US and worldwide cases) served by respx; nothing here touches the network. Under
test: mapping (apply link, attribution fields, pay periods, expiry, region), paging and caps,
429/403/5xx as partial runs, robots.txt, and the D-SRC-13 drop in a real run."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from kestrel import paths
from kestrel.config import get_settings
from kestrel.db.models import Opportunity, Source
from kestrel.ingest.adapters import get_adapter
from kestrel.ingest.adapters.himalayas import HimalayasAdapter
from kestrel.ingest.adapters.himalayas import to_posting as him_posting
from kestrel.ingest.adapters.weworkremotely import WeWorkRemotelyAdapter
from kestrel.ingest.adapters.weworkremotely import to_posting as wwr_posting
from kestrel.ingest.base import AdapterError, SourceConfigError
from kestrel.ingest.http import HttpClient
from kestrel.ingest.runner import run_source

FIX = Path(__file__).parent / "fixtures"
HIM = json.loads((FIX / "himalayas_search.json").read_text(encoding="utf-8"))
HIM_EMPTY = json.loads((FIX / "himalayas_empty.json").read_text(encoding="utf-8"))
RSS = (FIX / "weworkremotely_feed.rss").read_text(encoding="utf-8")
JOBS = HIM["jobs"]
HIM_API = "https://himalayas.app/jobs/api/search"
WWR_FEED = "https://weworkremotely.com/remote-jobs.rss"
NOW = 1_793_400_000.0  # shortly after the capture; the edited "archived" job expired in 2023


def _http(robots: bool = False) -> HttpClient:
    return HttpClient(user_agent=get_settings().ingest.user_agent, respect_robots=robots)


def _page(jobs: list | None = None) -> dict:
    return {**HIM, "jobs": JOBS if jobs is None else jobs}


def _query(call) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(str(call.request.url)).query).items()}


def _profile(titles: str = "[Program Analyst, Data Analyst, Operations Analyst]") -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_dir().joinpath("profile.yml").write_text(
        "employment:\n  employment_terms:\n    target_titles: " + titles + "\n"
        "  geo_rules:\n    remote_country_allowlist: [US]\n", encoding="utf-8")


# ================================================================================== Himalayas
def test_himalayas_maps_the_fields_and_keeps_the_terms():
    by_title = {j["title"]: him_posting(j, now=NOW) for j in JOBS}
    p = by_title["Software Quality Engineer/Analyst"]
    assert p.url == p.external_id and p.url.startswith("https://himalayas.app/companies/")
    # Terms clause 113(a): Apply always uses the API's applicationLink.
    assert p.apply_url.startswith("https://himalayas.app/companies/") and "utm_source=api" in (
        p.apply_url)
    assert p.org_name == "Solventum" and p.location_raw == "United States"
    assert p.is_remote is True and p.remote_hint == "Remote"
    assert (p.comp_min, p.comp_max, p.comp_currency, p.comp_interval) == (
        106000.0, 145750.0, "USD", "year")
    assert p.date_posted == "2026-10-29" and p.employment_type == "Full Time"
    assert p.raw["via"] == "Himalayas" and p.raw["via_url"] == "https://himalayas.app"
    assert '<a href="https://himalayas.app/companies/solventum">' in p.description_html


def test_himalayas_pay_periods_and_missing_pay():
    by_title = {j["title"]: him_posting(j, now=NOW) for j in JOBS}
    hourly = by_title["AI/ Supply Chain Intelligence Intern"]
    assert (hourly.comp_min, hourly.comp_max, hourly.comp_interval) == (24.37, 38.71, "hour")
    monthly = by_title["Data Analyst, Worldwide"]
    assert (monthly.comp_min, monthly.comp_max, monthly.comp_currency,
            monthly.comp_interval) == (4000.0, 5000.0, "EUR", "month")
    nothing = by_title["Data Analyst"]
    assert (nothing.comp_min, nothing.comp_currency, nothing.comp_interval) == (None, None, None)
    no_currency = dict(JOBS[1], currency=None)
    assert him_posting(no_currency, now=NOW).comp_min is None  # a salary with no currency
    fortnight = dict(JOBS[1], salaryPeriod="fortnightly", minSalary=2000, maxSalary=3000)
    f = him_posting(fortnight, now=NOW)
    assert (f.comp_min, f.comp_max, f.comp_interval) == (1000.0, 1500.0, "week")


def test_himalayas_locations_and_expiry():
    by_title = {j["title"]: him_posting(j, now=NOW) for j in JOBS}
    assert by_title["Sales Systems Administrator (Berlin)"].location_raw == "Germany"
    assert by_title["Data Analyst, Worldwide"].location_raw == "Remote (worldwide)"
    assert by_title["Archived Sales Systems Administrator"] is None  # past its expiryDate
    assert him_posting({}) is None and him_posting("x") is None
    assert him_posting({"title": "Clerk", "guid": "https://himalayas.app/x"}).title == "Clerk"


@respx.mock
async def test_himalayas_sends_the_documented_search_and_follows_pages():
    full = [dict(JOBS[0], guid=f"https://himalayas.app/companies/c/jobs/j{i}") for i in range(20)]
    route = respx.get(HIM_API).mock(side_effect=[
        Response(200, json=_page(full)), Response(200, json=_page(JOBS[:3])),
        Response(200, json=_page(JOBS))])
    async with _http() as http:
        got = await HimalayasAdapter().fetch({"keyword": "analyst", "max_pages": 3}, http)
    assert [_query(c)["page"] for c in route.calls] == ["1", "2"]  # a short page ends the walk
    assert _query(route.calls[0]) == {"country": "US", "sort": "recent", "page": "1",
                                      "q": "analyst"}
    assert len(got) == 23 and got.partial_error is None
    assert "authorization" not in route.calls[0].request.headers


@respx.mock
async def test_himalayas_page_cap_and_request_budget():
    full = [dict(JOBS[0], guid=f"https://himalayas.app/companies/c/jobs/j{i}") for i in range(20)]
    route = respx.get(HIM_API).mock(return_value=Response(200, json=_page(full)))
    async with _http() as http:
        await HimalayasAdapter().fetch({"keyword": "x", "max_pages": 2}, http)
    assert route.call_count == 2
    titles = ", ".join(f"T{i}" for i in range(5))
    _profile(f"[{titles}]")
    before = route.call_count
    async with _http() as http:
        got = await HimalayasAdapter().fetch({"max_pages": 10}, http)
    assert route.call_count - before == 12 and "limit of requests" in got.partial_error


@respx.mock
async def test_himalayas_without_a_keyword_uses_the_profile_titles_then_the_whole_feed():
    _profile("[Program Analyst, Data Analyst]")
    route = respx.get(HIM_API).mock(return_value=Response(200, json=_page(JOBS[:2])))
    async with _http() as http:
        await HimalayasAdapter().fetch({}, http)
    assert [_query(c)["q"] for c in route.calls] == ["Program Analyst", "Data Analyst"]
    _profile("[]")
    before = len(route.calls)
    async with _http() as http:
        await HimalayasAdapter().fetch({}, http)
    assert "q" not in _query(route.calls[before])


@pytest.mark.parametrize("status", [403, 429, 500, 503])
@respx.mock
async def test_himalayas_429_and_5xx_are_partial_and_keep_what_was_collected(status):
    full = [dict(JOBS[0], guid=f"https://himalayas.app/companies/c/jobs/j{i}") for i in range(20)]
    route = respx.get(HIM_API).mock(side_effect=[
        Response(200, json=_page(full)), Response(status, json={"error": "provider words"})])
    async with _http() as http:
        got = await HimalayasAdapter().fetch({"keyword": "x", "max_pages": 3}, http)
    assert len(got) == 20 and got.partial_error and "provider words" not in got.partial_error
    assert route.call_count == 2 and "—" not in got.partial_error


@respx.mock
async def test_himalayas_errors_unreadable_and_robots():
    respx.get(HIM_API).mock(return_value=Response(400, json={}))
    async with _http() as http:
        with pytest.raises(AdapterError, match="rejected the search"):
            await HimalayasAdapter().fetch({"keyword": "x"}, http)
    respx.get(HIM_API).mock(return_value=Response(200, json={"nope": 1}))
    async with _http() as http:
        with pytest.raises(AdapterError, match="could not read"):
            await HimalayasAdapter().fetch({"keyword": "x"}, http)


@respx.mock
async def test_himalayas_checks_robots_txt_and_a_disallow_skips_the_request():
    respx.get("https://himalayas.app/robots.txt").mock(
        return_value=Response(200, text="User-agent: *\nDisallow: /jobs/api\n"))
    route = respx.get(HIM_API).mock(return_value=Response(200, json=_page()))
    async with _http(robots=True) as http:
        with pytest.raises(AdapterError, match="robots.txt"):
            await HimalayasAdapter().fetch({"keyword": "x"}, http)
    assert route.call_count == 0


def test_himalayas_config_validation():
    a = HimalayasAdapter()
    a.validate_config({})
    a.validate_config({"keyword": "x", "max_pages": 10})
    for bad in ({"keyword": 3}, {"max_pages": 0}, {"max_pages": 11}, {"max_pages": True}):
        with pytest.raises(SourceConfigError):
            a.validate_config(bad)
    assert get_adapter("himalayas") is not None


@respx.mock
async def test_himalayas_run_drops_outside_area_and_unrelated_titles_and_expired(sm):
    _profile()
    respx.get(HIM_API).mock(return_value=Response(200, json=_page()))
    async with sm() as s:
        src = Source(track="employment", kind="himalayas", name="Analyst on Himalayas",
                     config={"keyword": "analyst"}, enabled=True, cadence_hours=24)
        s.add(src)
        await s.commit()
        await s.refresh(src)
    async with _http() as http:
        out = await run_source(sm, src.id, http=http)
    assert out.status == "ok", out.error
    c = out.counts
    assert c["fetched"] == 7  # the archived one was dropped by the adapter
    assert c["dropped_outside_area"] == 1  # Germany
    async with sm() as s:
        titles = {o.title for o in (await s.execute(select(Opportunity))).scalars()}
    assert "Sales Systems Administrator (Berlin)" not in titles and "Data Analyst" in titles
    assert not any("Archived" in t for t in titles)


# ============================================================================ We Work Remotely
def _items():
    from xml.etree import ElementTree

    return ElementTree.fromstring(RSS).findall("./channel/item")


def test_wwr_maps_the_fields():
    by_org = {}
    for item in _items():
        p = wwr_posting(item)
        if p is not None:
            by_org[p.org_name] = p
    mc = by_org["McGraw Hill LLC."]
    assert mc.title == "Customer Success Representative, Inside"
    assert mc.url == mc.apply_url == mc.external_id
    assert mc.url.startswith("https://weworkremotely.com/remote-jobs/")
    assert mc.location_raw == "Remote (North America)" and mc.is_remote is True
    assert mc.employment_type == "Full-Time" and mc.date_posted == "2026-10-29"
    assert "Headquarters" in mc.description_html and "<p>" in mc.description_html
    assert mc.raw["via"] == "We Work Remotely" and mc.raw["category"] == "Customer Support"
    assert by_org["Native Camp"].employment_type == "Contract"
    assert by_org["Native Camp"].location_raw == "Remote (worldwide)"
    assert by_org["Prairie Works"].location_raw == "United States"
    # The first colon splits company from title; a title keeps its own colons.
    from xml.etree import ElementTree

    one = wwr_posting(ElementTree.fromstring(
        "<item><title>Acme: Lead: Platform</title>"
        "<link>https://weworkremotely.com/remote-jobs/x</link></item>"))
    assert (one.org_name, one.title) == ("Acme", "Lead: Platform")
    assert wwr_posting(ElementTree.fromstring("<item/>")) is None


def test_wwr_drops_europe_only_and_expired_and_keeps_the_unclear():
    kept = {p.org_name for p in map(wwr_posting, _items()) if p}
    assert "Fjord Labs" not in kept  # Europe Only
    assert "Old Mill" not in kept  # expires_at in 2024
    assert {"McGraw Hill LLC.", "Bullfinch Recruitment", "Prairie Works"} <= kept


@respx.mock
async def test_wwr_fetch_reads_the_feed_once():
    route = respx.get(WWR_FEED).mock(return_value=Response(200, text=RSS))
    async with _http() as http:
        got = await WeWorkRemotelyAdapter().fetch({}, http)
    assert route.call_count == 1 and got.partial_error is None
    assert len(got) == 5 and len({p.external_id for p in got}) == 5


@respx.mock
async def test_wwr_category_feed_url_and_validation():
    url = "https://weworkremotely.com/categories/remote-programming-jobs.rss"
    route = respx.get(url).mock(return_value=Response(200, text=RSS))
    async with _http() as http:
        await WeWorkRemotelyAdapter().fetch({"category": "remote-programming-jobs"}, http)
    assert route.call_count == 1
    a = WeWorkRemotelyAdapter()
    a.validate_config({})
    for bad in ({"category": "../x"}, {"category": 5}, {"category": "programming"}):
        with pytest.raises(SourceConfigError):
            a.validate_config(bad)
    assert get_adapter("weworkremotely") is not None


@pytest.mark.parametrize("status", [403, 429, 500])
@respx.mock
async def test_wwr_blocks_and_5xx_are_partial_runs(status):
    respx.get(WWR_FEED).mock(return_value=Response(status, text="Just a moment..."))
    async with _http() as http:
        got = await WeWorkRemotelyAdapter().fetch({}, http)
    assert list(got) == [] and got.partial_error and "Just a moment" not in got.partial_error
    assert "—" not in got.partial_error


@respx.mock
async def test_wwr_unreadable_doctype_and_robots():
    respx.get(WWR_FEED).mock(return_value=Response(200, text="<html>not a feed"))
    async with _http() as http:
        with pytest.raises(AdapterError, match="could not read"):
            await WeWorkRemotelyAdapter().fetch({}, http)
    bomb = '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel/></rss>'
    respx.get(WWR_FEED).mock(return_value=Response(200, text=bomb))
    async with _http() as http:
        with pytest.raises(AdapterError, match="could not read"):
            await WeWorkRemotelyAdapter().fetch({}, http)
    respx.get("https://weworkremotely.com/robots.txt").mock(
        return_value=Response(200, text="User-agent: *\nDisallow: /remote-jobs.rss\n"))
    respx.get(WWR_FEED).mock(return_value=Response(200, text=RSS))
    async with _http(robots=True) as http:
        with pytest.raises(AdapterError, match="robots.txt"):
            await WeWorkRemotelyAdapter().fetch({}, http)


@respx.mock
async def test_wwr_run_keeps_only_analyst_titles(sm):
    _profile("[Program Analyst, Operations Analyst]")
    respx.get(WWR_FEED).mock(return_value=Response(200, text=RSS))
    async with sm() as s:
        src = Source(track="employment", kind="weworkremotely", name="We Work Remotely",
                     config={}, enabled=True, cadence_hours=24)
        s.add(src)
        await s.commit()
        await s.refresh(src)
    async with _http() as http:
        out = await run_source(sm, src.id, http=http)
    assert out.status == "ok", out.error
    assert out.counts["fetched"] == 5 and out.counts["dropped_unrelated_title"] >= 1
    async with sm() as s:
        opps = (await s.execute(select(Opportunity))).scalars().all()
    assert {o.title for o in opps} == {"Operations Analyst"}
    assert opps[0].source_id == src.id
