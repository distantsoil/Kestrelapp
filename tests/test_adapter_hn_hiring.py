"""Hacker News "Who is hiring?" (official API, keyless). The payloads are a shaped copy with
invented companies, people and ids (tests/fixtures/hn_hiring.json) served by respx; nothing here
touches the network. Under test: thread discovery by title (not the "Who wants to be hired?"
thread), mapping of the Company | Role | Location line, skipped deleted and dead comments, a bad id
refused at config load and at fetch, caps, 429/5xx as partial runs, only `.json` URLs requested,
and a real run that does not duplicate a posting it already holds."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from httpx import Response
from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.models import Opportunity, Source
from kestrel.ingest.adapters import get_adapter
from kestrel.ingest.adapters.hn_hiring import HnHiringAdapter, to_posting, valid_id
from kestrel.ingest.base import AdapterError, SourceConfigError
from kestrel.ingest.http import HttpClient
from kestrel.ingest.runner import run_source

FIX = json.loads((Path(__file__).parent / "fixtures" / "hn_hiring.json").read_text("utf-8"))
BASE = "https://hacker-news.firebaseio.com/v0"
ITEMS = FIX["items"]


def _http() -> HttpClient:
    return HttpClient(user_agent=get_settings().ingest.user_agent, respect_robots=True)


def _serve(items: dict | None = None, user: dict | None = None):
    items = ITEMS if items is None else items
    respx.get(f"{BASE}/user/whoishiring.json").mock(
        return_value=Response(200, json=user or FIX["user"]))
    return respx.get(url__regex=rf"{BASE}/item/(\d+)\.json").mock(
        side_effect=lambda req: Response(
            200, text=json.dumps(items.get(req.url.path.rsplit("/", 1)[1].removesuffix(".json")))))


def test_maps_the_header_line_and_keeps_the_whole_comment():
    one = to_posting(ITEMS["900101"])
    assert one.external_id == "900101"
    assert one.org_name == "Northwind Labs" and one.title == "Senior Data Engineer"
    assert one.is_remote is True and one.location_raw == "Remote (US)"
    assert one.employment_type == "Full time"
    assert one.apply_url == "https://jobs.northwind.example/roles/data-engineer"
    assert one.url == "https://news.ycombinator.com/item?id=900101"
    assert "hiring a Senior Data Engineer" in one.description_html
    assert "jobs@northwind.example" in one.description_html
    assert one.date_posted == "2026-11-02"
    assert one.raw["via"] == "Hacker News"
    two = to_posting(ITEMS["900102"])
    assert (two.org_name, two.title, two.is_remote) == (
        "Contoso Works", "Product Designer", False)
    assert two.location_raw == "London, UK" and two.employment_type == "Full time"
    assert two.apply_url == two.url  # no link in the comment: the thread item is the link


def test_free_text_and_hybrid_headers_are_kept_not_dropped():
    free = to_posting(ITEMS["900105"])
    assert free.org_name is None and free.is_remote is None
    assert free.title.startswith("Fabrikam Plumbing is looking for a Licensed Plumber")
    hyb = to_posting(ITEMS["900106"])
    assert hyb.org_name == "Tailspin Co" and hyb.title == "Staff Engineer, Platform & Tools"
    assert hyb.employment_type == "Contract"
    assert hyb.remote_hint == "Hybrid" and hyb.is_remote is None


def _comment(header: str) -> dict:
    return {"id": 910001, "type": "comment", "text": header + "<p>Details follow.",
            "time": 1793649000}


def test_a_header_with_location_style_and_type_segments_keeps_the_title_to_one_segment():
    one = to_posting(_comment("Acme | Senior Engineer | New York, NY | ONSITE | Full-time"))
    assert one.org_name == "Acme" and one.title == "Senior Engineer"
    assert one.location_raw == "New York, NY" and one.is_remote is False
    assert one.employment_type == "Full-time"


def test_a_url_in_parentheses_is_not_part_of_the_company_name():
    one = to_posting(_comment("Acme (https://acme.example) | Platform Engineer | Remote (US)"))
    assert one.org_name == "Acme" and one.title == "Platform Engineer" and one.is_remote is True


def test_no_remote_is_not_remote_and_an_odd_header_is_still_kept():
    one = to_posting(_comment("Acme | Designer | NYC (no remote)"))
    assert one.is_remote is False and one.location_raw == "NYC (no remote)"
    assert one.title == "Designer"
    odd = to_posting(_comment("Acme | Remote"))
    assert odd is not None and odd.title == "Acme | Remote"


def test_deleted_dead_empty_and_non_comments_are_skipped():
    assert to_posting(ITEMS["900103"]) is None and to_posting(ITEMS["900104"]) is None
    assert to_posting(ITEMS["900002"]) is None  # a story
    assert to_posting({"id": 5, "type": "comment", "text": "  "}) is None
    assert to_posting({"id": "../x", "type": "comment", "text": "A | B"}) is None
    assert to_posting("nope") is None and to_posting({}) is None


@respx.mock
async def test_finds_the_hiring_thread_not_the_job_seeker_thread():
    _serve()
    async with _http() as http:
        got = await HnHiringAdapter().fetch({}, http)
    assert [p.external_id for p in got] == ["900101", "900102", "900105", "900106"]
    assert got.partial_error is None
    requested = {str(c.request.url) for c in respx.calls}
    assert f"{BASE}/item/900201.json" not in requested  # the "Who wants to be hired?" comments


@respx.mock
async def test_only_json_urls_are_requested_which_is_all_robots_txt_allows():
    _serve()
    async with _http() as http:
        await HnHiringAdapter().fetch({}, http)
    urls = [str(c.request.url) for c in respx.calls]
    assert urls and all(u.startswith(BASE + "/") and u.endswith(".json") for u in urls)
    assert not any("robots.txt" in u or "ycombinator.com" in u for u in urls)


@respx.mock
async def test_an_explicit_thread_id_skips_discovery_and_max_comments_caps_fetches():
    _serve()
    async with _http() as http:
        got = await HnHiringAdapter().fetch({"thread_id": 900002, "max_comments": 2}, http)
    assert [p.external_id for p in got] == ["900101", "900102"]
    urls = [str(c.request.url) for c in respx.calls]
    assert f"{BASE}/user/whoishiring.json" not in urls and len(urls) == 3


@respx.mock
async def test_a_hostile_comment_id_is_never_put_in_a_url():
    items = {**ITEMS, "900002": {**ITEMS["900002"], "kids": ["../../user/x", 900101, True, 1.5]}}
    _serve(items)
    async with _http() as http:
        got = await HnHiringAdapter().fetch({}, http)
    assert [p.external_id for p in got] == ["900101"]
    assert all("/../" not in str(c.request.url) for c in respx.calls)


@pytest.mark.parametrize("status", [403, 429, 500])
@respx.mock
async def test_429_and_5xx_are_partial_and_keep_what_was_read(status):
    respx.get(f"{BASE}/user/whoishiring.json").mock(return_value=Response(200, json=FIX["user"]))
    seen = {"n": 0}

    def reply(req):
        key = req.url.path.rsplit("/", 1)[1].removesuffix(".json")
        if key in ("900101", "900102") and seen["n"] >= 1:
            return Response(status, json={})
        if key in ("900101", "900102"):
            seen["n"] += 1
        return Response(200, text=json.dumps(ITEMS.get(key)))

    respx.get(url__regex=rf"{BASE}/item/(\d+)\.json").mock(side_effect=reply)
    async with _http() as http:
        got = await HnHiringAdapter().fetch({}, http)
    assert [p.external_id for p in got] == ["900101"]
    assert got.partial_error and "Hacker News" in got.partial_error


@respx.mock
async def test_a_dropped_connection_after_some_postings_is_partial_not_failed():
    respx.get(f"{BASE}/user/whoishiring.json").mock(return_value=Response(200, json=FIX["user"]))

    def reply(req):
        key = req.url.path.rsplit("/", 1)[1].removesuffix(".json")
        if key == "900102":
            raise httpx.ConnectError("boom")
        return Response(200, text=json.dumps(ITEMS.get(key)))

    respx.get(url__regex=rf"{BASE}/item/(\d+)\.json").mock(side_effect=reply)
    async with _http() as http:
        got = await HnHiringAdapter().fetch({}, http)
    assert [p.external_id for p in got] == ["900101"]
    assert got.partial_error and "Hacker News" in got.partial_error


@respx.mock
async def test_nothing_collected_and_unreachable_is_still_a_failed_run():
    respx.get(f"{BASE}/user/whoishiring.json").mock(side_effect=httpx.ConnectError("boom"))
    async with _http() as http:
        with pytest.raises(AdapterError):
            await HnHiringAdapter().fetch({}, http)


@respx.mock
async def test_rejects_unreadable_replies_and_a_missing_thread():
    respx.get(f"{BASE}/user/whoishiring.json").mock(return_value=Response(200, text="null"))
    async with _http() as http:
        with pytest.raises(AdapterError, match="could not read"):
            await HnHiringAdapter().fetch({}, http)
    respx.get(f"{BASE}/user/whoishiring.json").mock(return_value=Response(200, text="<html>"))
    async with _http() as http:
        with pytest.raises(AdapterError, match="could not read"):
            await HnHiringAdapter().fetch({}, http)
    respx.get(f"{BASE}/item/900002.json").mock(return_value=Response(200, text="null"))
    async with _http() as http:
        with pytest.raises(AdapterError, match="not found"):
            await HnHiringAdapter().fetch({"thread_id": 900002}, http)


@respx.mock
async def test_no_hiring_thread_yet_is_a_partial_run_not_a_failure():
    _serve(user={**FIX["user"], "submitted": [900003]})
    async with _http() as http:
        got = await HnHiringAdapter().fetch({}, http)
    assert len(got) == 0 and got.partial_error


def test_config_validation_and_id_check():
    a = HnHiringAdapter()
    a.validate_config({})
    a.validate_config({"max_comments": 500, "thread_id": 50338728})
    a.validate_config({"thread_id": "50338728"})
    for bad in ({"max_comments": 0}, {"max_comments": 501}, {"max_comments": True},
                {"max_comments": "5"}, {"thread_id": "../x"}, {"thread_id": "12 34"},
                {"thread_id": True}, {"thread_id": 1.5}, {"thread_id": "1" * 13},
                {"thread_id": ""}):
        with pytest.raises(SourceConfigError):
            a.validate_config(bad)
    assert valid_id("123") == "123" and valid_id("12/3") is None and valid_id(None) is None
    assert get_adapter("hn_hiring") is not None


@respx.mock
async def test_a_bad_thread_id_is_refused_at_fetch_before_any_request():
    route = _serve()
    async with _http() as http:
        with pytest.raises(SourceConfigError):
            await HnHiringAdapter().fetch({"thread_id": "9/../9"}, http)
    assert route.call_count == 0 and respx.calls.call_count == 0


@respx.mock
async def test_run_fetches_all_and_a_second_run_does_not_duplicate(sm):
    _serve()
    async with sm() as s:
        src = Source(track="employment", kind="hn_hiring", name="Hacker News",
                     config={}, enabled=True, cadence_hours=24)
        s.add(src)
        await s.commit()
        await s.refresh(src)
    async with _http() as http:
        out = await run_source(sm, src.id, http=http)
        again = await run_source(sm, src.id, http=http)
    assert out.status == "ok", out.error
    assert out.counts["fetched"] == 4 and again.status == "ok"
    async with sm() as s:
        opps = (await s.execute(select(Opportunity))).scalars().all()
    assert len(opps) == 4 and opps[0].source_id == src.id


@pytest.mark.parametrize("header,title", [
    ("Acme | Remote Senior Backend Engineer | US | Full-time", "Remote Senior Backend Engineer"),
    ("Acme | San Francisco, CA | Senior Backend Engineer | Onsite", "Senior Backend Engineer"),
    ("Acme | Onsite Backend Engineer | Austin, TX", "Onsite Backend Engineer"),
    ("Acme | Hybrid Backend Engineer | London", "Hybrid Backend Engineer"),
    ("Acme | Equity Research Analyst | New York", "Equity Research Analyst"),
    ("Acme | Visa Operations Lead | Dublin", "Visa Operations Lead"),
    ("Acme | Remote Sensing Engineer | Boulder, CO", "Remote Sensing Engineer"),
])
def test_a_role_that_starts_with_a_work_style_word_or_follows_the_location_is_the_title(
    header, title
):
    one = to_posting(_comment(header))
    assert one.title == title


def test_a_city_is_never_the_title_when_a_later_segment_names_the_role():
    one = to_posting(_comment("Acme | San Francisco, CA | Senior Backend Engineer | Onsite"))
    assert one.location_raw == "San Francisco, CA" and one.is_remote is False


def test_the_smart_filter_keeps_the_postings_whose_headers_used_to_read_as_a_city():
    from kestrel.ingest import smart_filter as sf

    ctx = sf.FilterContext(
        floor_rank=-1, tokens=sf.profile_tokens(["Backend Engineer", "Software Engineer"]))
    for header in ("Acme | Remote Senior Backend Engineer | US | Full-time",
                   "Acme | San Francisco, CA | Senior Backend Engineer | Onsite",
                   "Acme | Onsite Backend Engineer | Austin, TX",
                   "Acme | Hybrid Backend Engineer | London"):
        assert sf.keep(to_posting(_comment(header)).title, ctx), header


def test_a_hostile_time_does_not_lose_the_comment():
    for bad in (1e20, float("inf"), 10**30, -5, "yesterday"):
        one = to_posting({**_comment("Acme | Engineer | NYC"), "time": bad})
        assert one is not None and one.date_posted is None


@respx.mock
async def test_a_story_whose_kids_is_not_a_list_is_an_empty_thread_not_a_crash():
    items = {**ITEMS, "900002": {**ITEMS["900002"], "kids": 7}}
    _serve(items)
    async with _http() as http:
        got = await HnHiringAdapter().fetch({}, http)
    assert len(got) == 0


@pytest.mark.parametrize("bad", [Response(401, json={"error": "denied"}),
                                 Response(404, text="nope"), Response(200, text="<html>")])
@respx.mock
async def test_a_later_item_that_fails_any_way_keeps_the_comments_already_read(bad):
    respx.get(f"{BASE}/user/whoishiring.json").mock(return_value=Response(200, json=FIX["user"]))

    def reply(req):
        key = req.url.path.rsplit("/", 1)[1].removesuffix(".json")
        if key == "900102":
            return bad
        return Response(200, text=json.dumps(ITEMS.get(key)))

    respx.get(url__regex=rf"{BASE}/item/(\d+)\.json").mock(side_effect=reply)
    async with _http() as http:
        got = await HnHiringAdapter().fetch({}, http)
    assert [p.external_id for p in got] == ["900101"]
    assert got.partial_error and "Hacker News" in got.partial_error
