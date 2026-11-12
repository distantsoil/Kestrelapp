"""4 Day Week (public API v2, keyless). The payload is a trimmed capture with invented employers
(tests/fixtures/fourdayweek_page1.json) served by respx; nothing here touches the network. Under
test: mapping (Markdown description, cents to pay, location, remote flag, credit fields), expiry,
filters and paging, 429/5xx as partial runs, robots.txt, config validation, and a real run that
does not duplicate a posting it already holds."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from httpx import Response
from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.models import Opportunity, Source
from kestrel.ingest.adapters import get_adapter
from kestrel.ingest.adapters.fourdayweek import FourDayWeekAdapter, to_posting
from kestrel.ingest.base import AdapterError, SourceConfigError
from kestrel.ingest.http import HttpClient
from kestrel.ingest.runner import run_source

FIX = Path(__file__).parent / "fixtures"
PAGE = json.loads((FIX / "fourdayweek_page1.json").read_text(encoding="utf-8"))
JOBS = PAGE["data"]
API = "https://4dayweek.io/api/v2/jobs"
NOW = 1_794_000_000.0  # 2026-11-06, shortly before the captured postings


def _http(robots: bool = False) -> HttpClient:
    return HttpClient(user_agent=get_settings().ingest.user_agent, respect_robots=robots)


def _page(jobs: list, has_more: bool = False) -> dict:
    return {**PAGE, "data": jobs, "has_more": has_more}


def _job(i: int) -> dict:
    return dict(JOBS[0], id=f"id-{i}", slug=f"role-{i}", url=f"https://4dayweek.io/job/role-{i}")


def _query(call) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(str(call.request.url)).query).items()}


def test_maps_the_fields_including_pay_in_dollars():
    by_title = {j["title"]: to_posting(j, now=NOW) for j in JOBS}
    p = by_title["Property Paralegal"]
    assert p.external_id == "00000000-0000-4000-8000-000000000001"
    assert p.url == p.apply_url == "https://4dayweek.io/job/" + JOBS[0]["slug"]
    assert p.org_name == "Northwind Studio" and p.org_url == "https://northwind-studio.example.org/"
    assert p.location_raw == "Leeds, England, United Kingdom"
    assert p.is_remote is False and p.remote_hint == "hybrid"
    assert p.description_md.startswith("### **About the team**")
    assert p.date_posted == "2026-11-07"
    assert (p.comp_min, p.comp_max, p.comp_currency, p.comp_interval) == (None,) * 4
    assert p.raw["via"] == "4 Day Week" and p.raw["via_url"] == "https://4dayweek.io"
    assert p.raw["schedule_type"] == "4_day_week" and p.raw["hours_per_week_min"] == 32
    fin = by_title["Finance Lead"]
    assert fin.is_remote is True and fin.location_raw == "United States"
    assert fin.employment_type == "permanent"
    assert (fin.comp_min, fin.comp_max, fin.comp_currency, fin.comp_interval) == (
        78000.0, 150000.0, "USD", "year")


def test_malformed_and_expired_entries_are_skipped():
    assert to_posting({}) is None and to_posting("nope") is None
    assert to_posting({"title": "No id"}) is None
    expired = dict(JOBS[0], expires_at="2026-01-01T00:00:00Z")
    assert to_posting(expired, now=NOW) is None
    live = dict(JOBS[0], expires_at="2027-01-01T00:00:00Z")
    assert to_posting(live, now=NOW) is not None
    bare = to_posting({"slug": "x-1", "title": "Bare"}, now=NOW)
    assert bare.url == "https://4dayweek.io/job/x-1" and bare.comp_min is None


@respx.mock
async def test_sends_the_documented_request_with_filters_and_follows_pages():
    jobs = [_job(i) for i in range(6)]
    route = respx.get(API).mock(side_effect=[
        Response(200, json=_page(jobs[:3], True)),
        Response(200, json=_page(jobs[3:], False)),
    ])
    cfg = {"schedule": "4_day_week,9_day_fortnight", "keyword": "data analyst", "count": 3,
           "max_pages": 5}
    async with _http() as http:
        got = await FourDayWeekAdapter().fetch(cfg, http)
    assert [p.external_id for p in got] == [f"id-{i}" for i in range(6)]
    assert got.partial_error is None and route.call_count == 2
    first = _query(route.calls[0])
    assert first == {"page": "1", "limit": "3", "schedule": "4_day_week,9_day_fortnight",
                     "q": "data analyst"}
    assert _query(route.calls[1])["page"] == "2"


@respx.mock
async def test_page_cap_limits_requests_and_duplicates_collapse():
    route = respx.get(API).mock(side_effect=[
        Response(200, json=_page([_job(1), _job(2)], True)),
        Response(200, json=_page([_job(2), _job(3)], True)),
        Response(200, json=_page([_job(4)], False)),
    ])
    async with _http() as http:
        got = await FourDayWeekAdapter().fetch({"max_pages": 2}, http)
    assert [p.external_id for p in got] == ["id-1", "id-2", "id-3"] and route.call_count == 2


@pytest.mark.parametrize("status", [403, 429, 500])
@respx.mock
async def test_429_and_5xx_are_partial(status):
    route = respx.get(API).mock(side_effect=[
        Response(200, json=_page([_job(1)], True)),
        Response(status, json={}),
    ])
    async with _http() as http:
        got = await FourDayWeekAdapter().fetch({"max_pages": 5}, http)
    assert len(got) == 1 and got.partial_error and "4 Day Week" in got.partial_error
    assert route.call_count == 2


@respx.mock
async def test_a_dropped_connection_on_page_two_keeps_page_one_as_a_partial_run():
    respx.get(API).mock(side_effect=[
        Response(200, json=_page([_job(1)], True)),
        httpx.ConnectError("boom"),
    ])
    async with _http() as http:
        got = await FourDayWeekAdapter().fetch({"max_pages": 3}, http)
    assert [p.external_id for p in got] == ["id-1"]
    assert got.partial_error and "4 Day Week" in got.partial_error


@respx.mock
async def test_unreachable_on_the_first_page_is_a_failed_run():
    respx.get(API).mock(side_effect=httpx.ConnectError("boom"))
    async with _http() as http:
        with pytest.raises(AdapterError):
            await FourDayWeekAdapter().fetch({}, http)


def test_weekly_pay_keeps_its_week_interval_instead_of_becoming_a_yearly_figure():
    job = dict(JOBS[0], salary_min=120000, salary_max=200000, salary_currency="USD",
               salary_period="week")
    p = to_posting(job, now=NOW)
    assert (p.comp_min, p.comp_max, p.comp_interval) == (1200.0, 2000.0, "week")


@respx.mock
async def test_rejects_unreadable_and_bad_requests():
    respx.get(API).mock(return_value=Response(400, json={}))
    async with _http() as http:
        with pytest.raises(AdapterError, match="rejected the request"):
            await FourDayWeekAdapter().fetch({}, http)
    respx.get(API).mock(return_value=Response(200, json={"nope": 1}))
    async with _http() as http:
        with pytest.raises(AdapterError, match="could not read"):
            await FourDayWeekAdapter().fetch({}, http)
    respx.get(API).mock(return_value=Response(200, text="<html>not json</html>"))
    async with _http() as http:
        with pytest.raises(AdapterError, match="could not read"):
            await FourDayWeekAdapter().fetch({}, http)


def test_config_validation():
    a = FourDayWeekAdapter()
    a.validate_config({})
    a.validate_config({"count": 100, "max_pages": 5, "schedule": "4_day_week",
                       "category": "engineering,sales", "keyword": "analyst"})
    for bad in ({"max_pages": 0}, {"max_pages": 6}, {"max_pages": True}, {"count": 0},
                {"count": 101}, {"count": "100"}, {"schedule": "4 day week"},
                {"schedule": "a&page=9"}, {"category": "../x"}, {"level": ""},
                {"work_arrangement": "Remote"}, {"keyword": "x" * 101}, {"keyword": 5}):
        with pytest.raises(SourceConfigError):
            a.validate_config(bad)
    assert get_adapter("fourdayweek") is not None


@respx.mock
async def test_a_bad_filter_is_refused_at_fetch_before_any_request():
    route = respx.get(API).mock(return_value=Response(200, json=PAGE))
    async with _http() as http:
        with pytest.raises(SourceConfigError):
            await FourDayWeekAdapter().fetch({"schedule": "x&limit=1000"}, http)
    assert route.call_count == 0


@respx.mock
async def test_checks_robots_txt_and_a_disallow_skips_the_request():
    respx.get("https://4dayweek.io/robots.txt").mock(
        return_value=Response(200, text="User-agent: *\nDisallow: /api/\n"))
    route = respx.get(API).mock(return_value=Response(200, json=PAGE))
    async with _http(robots=True) as http:
        with pytest.raises(AdapterError, match="robots.txt"):
            await FourDayWeekAdapter().fetch({}, http)
    assert route.call_count == 0


@respx.mock
async def test_the_published_robots_rules_allow_the_v2_path():
    respx.get("https://4dayweek.io/robots.txt").mock(return_value=Response(
        200, text="User-agent: *\nAllow: /api/v1\nAllow: /api/v2\nDisallow: /api/\n"))
    route = respx.get(API).mock(return_value=Response(200, json=PAGE))
    async with _http(robots=True) as http:
        got = await FourDayWeekAdapter().fetch({}, http)
    assert len(got) == 3 and route.call_count == 1


@respx.mock
async def test_run_fetches_all_and_a_second_run_does_not_duplicate(sm):
    respx.get(API).mock(return_value=Response(200, json=PAGE))
    async with sm() as s:
        src = Source(track="employment", kind="fourdayweek", name="4 Day Week",
                     config={}, enabled=True, cadence_hours=24)
        s.add(src)
        await s.commit()
        await s.refresh(src)
    async with _http() as http:
        out = await run_source(sm, src.id, http=http)
        again = await run_source(sm, src.id, http=http)
    assert out.status == "ok", out.error
    assert out.counts["fetched"] == 3 and again.status == "ok"
    async with sm() as s:
        opps = (await s.execute(select(Opportunity))).scalars().all()
    assert {o.title for o in opps} == {"Property Paralegal", "Communications Lead", "Finance Lead"}
    assert len(opps) == 3 and opps[0].source_id == src.id


def test_an_odd_field_in_one_record_does_not_lose_the_record_or_the_batch():
    sentinel = dict(JOBS[0], id="s1", slug="s1", url="https://4dayweek.io/job/s1",
                    expires_at="0001-01-01T00:00:00")
    huge = dict(JOBS[0], id="s2", slug="s2", url="https://4dayweek.io/job/s2",
                salary_min=10**400, salary_max=float("inf"), salary_period="year")
    got = [to_posting(j, now=NOW) for j in (sentinel, huge)]
    assert all(p is not None for p in got)  # the odd field is dropped, the role is kept
    assert got[1].comp_min is None and got[1].comp_max is None


@respx.mock
async def test_one_record_that_cannot_be_converted_is_skipped_and_the_rest_are_kept(monkeypatch):
    from kestrel.ingest.adapters import fourdayweek as fd

    real = fd.to_posting

    def flaky(job, now=None):
        if job.get("id") == "id-2":
            raise OverflowError("boom")
        return real(job, now)

    monkeypatch.setattr(fd, "to_posting", flaky)
    respx.get(API).mock(return_value=Response(200, json=_page([_job(1), _job(2), _job(3)])))
    async with _http() as http:
        got = await FourDayWeekAdapter().fetch({}, http)
    assert [p.external_id for p in got] == ["id-1", "id-3"]


@pytest.mark.parametrize("second", [
    Response(404, json={}), Response(400, json={}),
    Response(200, text="<html>challenge</html>"), Response(200, json={"nope": 1}),
])
@respx.mock
async def test_a_later_page_that_fails_any_way_keeps_the_earlier_pages_as_a_partial_run(second):
    respx.get(API).mock(side_effect=[Response(200, json=_page([_job(1)], True)), second])
    async with _http() as http:
        got = await FourDayWeekAdapter().fetch({"max_pages": 3}, http)
    assert [p.external_id for p in got] == ["id-1"]
    assert got.partial_error and "4 Day Week" in got.partial_error


@respx.mock
async def test_the_first_page_failing_with_a_bad_request_is_still_a_failed_run():
    respx.get(API).mock(return_value=Response(404, json={}))
    async with _http() as http:
        with pytest.raises(AdapterError):
            await FourDayWeekAdapter().fetch({}, http)


@pytest.mark.parametrize("period", ["fortnight", "biweekly", "per hour", "1"])
def test_an_unknown_pay_period_drops_the_pay_instead_of_calling_it_yearly(period):
    job = dict(JOBS[0], salary_min=500000, salary_max=700000, salary_currency="USD",
               salary_period=period)
    p = to_posting(job, now=NOW)
    assert (p.comp_min, p.comp_max, p.comp_interval, p.comp_currency) == (None,) * 4


@pytest.mark.parametrize("spelling,want", [("annual", "year"), ("Hourly", "hour"),
                                           ("weekly", "week"), ("monthly", "month")])
def test_common_spellings_of_a_pay_period_are_understood(spelling, want):
    job = dict(JOBS[0], salary_min=500000, salary_max=700000, salary_currency="USD",
               salary_period=spelling)
    assert to_posting(job, now=NOW).comp_interval == want
