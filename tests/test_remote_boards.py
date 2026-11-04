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
from kestrel.ingest.adapters.arbeitnow import ArbeitnowAdapter
from kestrel.ingest.adapters.arbeitnow import to_posting as an_posting
from kestrel.ingest.adapters.himalayas import HimalayasAdapter
from kestrel.ingest.adapters.himalayas import to_posting as him_posting
from kestrel.ingest.adapters.jobicy import JobicyAdapter
from kestrel.ingest.adapters.jobicy import to_posting as jc_posting
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

AN = json.loads((FIX / "arbeitnow_page1.json").read_text(encoding="utf-8"))
AN_JOBS = AN["data"]
AN_API = "https://www.arbeitnow.com/api/job-board-api"

JC = json.loads((FIX / "jobicy_page1.json").read_text(encoding="utf-8"))
JC_JOBS = JC["jobs"]
JC_API = "https://jobicy.com/api/v2/remote-jobs"


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


# ============================================================================ Arbeitnow
def _an_full(prefix: str = "j", n: int = 300) -> list[dict]:
    return [dict(AN_JOBS[0], slug=f"{prefix}{i}",
                 url=f"https://www.arbeitnow.com/jobs/{prefix}{i}") for i in range(n)]


def _an_page(jobs: list, next_url: str | None) -> dict:
    return {**AN, "data": jobs, "links": {"next": next_url}}


def test_arbeitnow_maps_the_fields_and_keeps_the_link():
    by_title = {}
    for j in AN_JOBS:
        p = an_posting(j)
        if p is not None:
            by_title[p.title] = p
    da = by_title["Data Analyst"]
    assert da.external_id == "data-analyst-northwind"
    assert da.url.startswith("https://www.arbeitnow.com/jobs/")
    assert da.org_name == "Northwind Labs" and da.location_raw == "Berlin"
    assert da.is_remote is True and da.employment_type == "Vollzeit"
    assert da.date_posted == "2026-10-30"
    assert da.raw["via"] == "Arbeitnow" and da.raw["tags"] == ["SQL", "Python"]
    sw = by_title["Software Engineer"]
    assert sw.is_remote is False and sw.employment_type == "Vollzeit, Unbefristet"
    clerk = by_title["Office Clerk"]  # no slug: the link is the id
    assert clerk.external_id == clerk.url
    assert clerk.external_id.startswith("https://www.arbeitnow.com/jobs/")
    assert an_posting({}) is None and an_posting("x") is None


@respx.mock
async def test_arbeitnow_follows_pages_until_the_last_page():
    route = respx.get(AN_API).mock(side_effect=[
        Response(200, json=_an_page(_an_full("a"), AN_API + "?page=2")),
        Response(200, json=_an_page(_an_full("b", 3), None))])
    async with _http() as http:
        got = await ArbeitnowAdapter().fetch({}, http)
    assert [_query(c)["page"] for c in route.calls] == ["1", "2"]
    assert len(got) == 303 and got.partial_error is None
    assert "authorization" not in route.calls[0].request.headers


@respx.mock
async def test_arbeitnow_page_cap_limits_requests():
    route = respx.get(AN_API).mock(side_effect=[
        Response(200, json=_an_page(_an_full("a"), AN_API + "?page=2")),
        Response(200, json=_an_page(_an_full("b"), AN_API + "?page=3"))])
    async with _http() as http:
        got = await ArbeitnowAdapter().fetch({"max_pages": 2}, http)
    assert route.call_count == 2 and len(got) == 600


@pytest.mark.parametrize("status", [403, 429, 500])
@respx.mock
async def test_arbeitnow_429_and_5xx_are_partial(status):
    route = respx.get(AN_API).mock(side_effect=[
        Response(200, json=_an_page(_an_full("a"), AN_API + "?page=2")),
        Response(status, json={"error": "provider words"})])
    async with _http() as http:
        got = await ArbeitnowAdapter().fetch({"max_pages": 3}, http)
    assert len(got) == 300 and got.partial_error and "provider words" not in got.partial_error
    assert route.call_count == 2 and "—" not in got.partial_error


@respx.mock
async def test_arbeitnow_rejects_unreadable_and_bad_requests():
    respx.get(AN_API).mock(return_value=Response(400, json={}))
    async with _http() as http:
        with pytest.raises(AdapterError, match="rejected the request"):
            await ArbeitnowAdapter().fetch({}, http)
    respx.get(AN_API).mock(return_value=Response(200, json={"nope": 1}))
    async with _http() as http:
        with pytest.raises(AdapterError, match="could not read"):
            await ArbeitnowAdapter().fetch({}, http)


def test_arbeitnow_config_validation():
    a = ArbeitnowAdapter()
    a.validate_config({})
    a.validate_config({"max_pages": 5})
    for bad in ({"max_pages": 0}, {"max_pages": 6}, {"max_pages": True}, {"max_pages": "2"}):
        with pytest.raises(SourceConfigError):
            a.validate_config(bad)
    assert get_adapter("arbeitnow") is not None


@respx.mock
async def test_arbeitnow_checks_robots_txt_and_a_disallow_skips_the_request():
    respx.get("https://www.arbeitnow.com/robots.txt").mock(
        return_value=Response(200, text="User-agent: *\nDisallow: /api\n"))
    route = respx.get(AN_API).mock(return_value=Response(200, json=AN))
    async with _http(robots=True) as http:
        with pytest.raises(AdapterError, match="robots.txt"):
            await ArbeitnowAdapter().fetch({}, http)
    assert route.call_count == 0


@respx.mock
async def test_arbeitnow_run_fetches_all_and_marks_ok(sm):
    _profile("[Data Analyst, Software Engineer, Office Clerk]")
    respx.get(AN_API).mock(return_value=Response(200, json=AN))
    async with sm() as s:
        src = Source(track="employment", kind="arbeitnow", name="Arbeitnow",
                     config={}, enabled=True, cadence_hours=24)
        s.add(src)
        await s.commit()
        await s.refresh(src)
    async with _http() as http:
        out = await run_source(sm, src.id, http=http)
    assert out.status == "ok", out.error
    assert out.counts["fetched"] == 3
    async with sm() as s:
        opps = (await s.execute(select(Opportunity))).scalars().all()
    assert {o.title for o in opps} == {
        "Data Analyst", "Software Engineer", "Office Clerk"}
    assert opps[0].source_id == src.id


# ================================================================================== Jobicy
def test_jobicy_maps_the_fields_including_pay():
    by_title = {j["jobTitle"]: jc_posting(j) for j in JC_JOBS}
    da = by_title["Data Analyst"]
    assert da.external_id == "900001"
    assert da.url == da.apply_url == "https://jobicy.com/jobs/900001-data-analyst-4"
    assert da.org_name == "Northwind Labs" and da.location_raw == "USA"
    assert da.is_remote is True and da.employment_type == "Full-Time"
    assert (da.comp_min, da.comp_max, da.comp_currency, da.comp_interval) == (
        90000.0, 110000.0, "USD", "year")
    assert da.date_posted == "2026-11-03"
    assert da.raw["via"] == "Jobicy" and da.raw["via_url"] == "https://jobicy.com"
    sw = by_title["Software Engineer"]
    assert (sw.comp_min, sw.comp_max, sw.comp_currency, sw.comp_interval) == (
        60.0, 80.0, "USD", "hour")
    clerk = by_title["Office Clerk"]  # no salary: pay fields are absent
    assert (clerk.comp_min, clerk.comp_max, clerk.comp_currency, clerk.comp_interval) == (
        None, None, None, None)


def _jc_page(jobs: list, next_cursor: str | None, has_more: bool) -> dict:
    return {**JC, "jobs": jobs, "nextCursor": next_cursor, "hasMore": has_more}


def _jc_job(prefix: str, i: int) -> dict:
    return dict(JC_JOBS[0], id=f"j{prefix}{i}", url=f"https://jobicy.com/jobs/{prefix}{i}")


def test_jobicy_pay_is_optional_and_malformed_entries_are_skipped():
    no_pay = {"jobTitle": "No Pay", "url": "https://jobicy.com/jobs/x",
              "id": 1, "pubDate": "2026-11-01T00:00:00+00:00"}
    p = jc_posting(no_pay)
    assert p is not None and (p.comp_min, p.comp_max, p.comp_interval) == (None, None, None)
    assert jc_posting({}) is None and jc_posting("not a dict") is None


@respx.mock
async def test_jobicy_follows_cursors_until_has_more_is_false():
    jobs = [_jc_job("a", i) for i in range(10)]
    page1 = jobs[:5]
    page2 = jobs[5:]
    route = respx.get(JC_API).mock(side_effect=[
        Response(200, json=_jc_page(page1, "cursor-2", True)),
        Response(200, json=_jc_page(page2, None, False)),
    ])
    async with _http() as http:
        got = await JobicyAdapter().fetch({"max_pages": 5}, http)
    assert [p.external_id for p in got] == [f"ja{i}" for i in range(10)]
    assert got.partial_error is None
    assert route.call_count == 2
    # The second call carried the cursor from the first reply.
    assert "cursor=cursor-2" in str(route.calls[1].request.url)


@respx.mock
async def test_jobicy_page_cap_limits_requests():
    p1 = [_jc_job("b", i) for i in range(10)]
    p2 = [_jc_job("b", 10 + i) for i in range(10)]
    route = respx.get(JC_API).mock(side_effect=[
        Response(200, json=_jc_page(p1, "c1", True)),
        Response(200, json=_jc_page(p2, "c2", True)),
        Response(200, json=_jc_page([_jc_job("b", 20)], None, False)),
    ])
    async with _http() as http:
        got = await JobicyAdapter().fetch({"max_pages": 2}, http)
    assert len(got) == 20 and route.call_count == 2


@pytest.mark.parametrize("status", [403, 429, 500])
@respx.mock
async def test_jobicy_429_and_5xx_are_partial(status):
    route = respx.get(JC_API).mock(side_effect=[
        Response(200, json=_jc_page([_jc_job("c", 0)], "c", True)),
        Response(status, json={}),
    ])
    async with _http() as http:
        got = await JobicyAdapter().fetch({"max_pages": 5}, http)
    assert len(got) == 1 and got.partial_error and "Jobicy" in got.partial_error
    assert route.call_count == 2


@respx.mock
async def test_jobicy_rejects_unreadable_and_bad_requests():
    respx.get(JC_API).mock(return_value=Response(400, json={}))
    async with _http() as http:
        with pytest.raises(AdapterError, match="rejected the request"):
            await JobicyAdapter().fetch({}, http)
    respx.get(JC_API).mock(return_value=Response(200, json={"nope": 1}))
    async with _http() as http:
        with pytest.raises(AdapterError, match="could not read"):
            await JobicyAdapter().fetch({}, http)


def test_jobicy_config_validation():
    a = JobicyAdapter()
    a.validate_config({})
    a.validate_config({"count": 500, "max_pages": 5})
    for bad in ({"max_pages": 0}, {"max_pages": 6}, {"max_pages": True},
                {"count": 0}, {"count": 501}, {"count": "100"}):
        with pytest.raises(SourceConfigError):
            a.validate_config(bad)
    assert get_adapter("jobicy") is not None


@respx.mock
async def test_jobicy_checks_robots_txt_and_a_disallow_skips_the_request():
    respx.get("https://jobicy.com/robots.txt").mock(
        return_value=Response(200, text="User-agent: *\nDisallow: /api\n"))
    route = respx.get(JC_API).mock(return_value=Response(200, json=JC))
    async with _http(robots=True) as http:
        with pytest.raises(AdapterError, match="robots.txt"):
            await JobicyAdapter().fetch({}, http)
    assert route.call_count == 0


@respx.mock
async def test_jobicy_run_fetches_all_and_marks_ok(sm):
    _profile("[Data Analyst, Software Engineer, Office Clerk]")
    respx.get(JC_API).mock(return_value=Response(200, json=JC))
    async with sm() as s:
        src = Source(track="employment", kind="jobicy", name="Jobicy",
                     config={}, enabled=True, cadence_hours=24)
        s.add(src)
        await s.commit()
        await s.refresh(src)
    async with _http() as http:
        out = await run_source(sm, src.id, http=http)
    assert out.status == "ok", out.error
    assert out.counts["fetched"] == 3
    async with sm() as s:
        opps = (await s.execute(select(Opportunity))).scalars().all()
    assert {o.title for o in opps} == {
        "Data Analyst", "Software Engineer", "Office Clerk"}
    assert opps[0].source_id == src.id
