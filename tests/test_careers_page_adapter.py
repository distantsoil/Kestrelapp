"""Careers-page reader (B4, D-SRC-15): schema.org JobPosting JSON-LD on an employer's own page.
The fixtures under tests/fixtures/careers/ are: detail_phenom.html, a scrubbed capture of one real
public job page (2026-10-29, employer and domain replaced, description shortened); the rest are
built from schema.org's documented JobPosting shape (list, @graph, stub, links-only, JavaScript
shell) because no live public page was found that shows each case. Nothing here touches the
network. Under test: JSON-LD found with a real HTML parser (lists, @graph, ItemList, bad blocks),
field mapping (remote, pay, dates, expiry), the detail-page follow and its cap, the JavaScript
message, delegation to a known hiring system, robots/404/429, config validation, the Sources link
check, and the D-SRC-13 area and title drop."""

from __future__ import annotations

import json
from html import unescape
from pathlib import Path

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from kestrel import paths
from kestrel.config import get_settings
from kestrel.db.models import Opportunity, Source
from kestrel.ingest import jobposting as jp
from kestrel.ingest.adapters import careers_page as cp
from kestrel.ingest.adapters import get_adapter
from kestrel.ingest.adapters.careers_page import JS_ONLY, CareersPageAdapter, job_links
from kestrel.ingest.base import AdapterError, PostingBatch, SourceConfigError
from kestrel.ingest.employer_page import parse_job_posting
from kestrel.ingest.http import HttpClient
from kestrel.ingest.runner import run_source

_FIX = Path(__file__).parent / "fixtures" / "careers"
_FIXTURES = Path(__file__).parent / "fixtures"


def _page(name: str) -> str:
    return (_FIX / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _public_and_fast(monkeypatch):
    from kestrel.ingest import http as ingest_http

    async def _resolve(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(ingest_http, "_resolve", _resolve)
    monkeypatch.setattr(cp, "_RATE", 0)


def _http() -> HttpClient:
    return HttpClient(
        user_agent=get_settings().ingest.user_agent, respect_robots=True, max_attempts=1
    )


def _robots(host: str, body: str = "", status: int = 404):
    respx.get(f"https://{host}/robots.txt").mock(return_value=Response(status, text=body))


async def _fetch(url: str, **config):
    async with _http() as client:
        return await CareersPageAdapter().fetch({"url": url, **config}, client)


# --------------------------------------------------------------------------- the parser
def test_json_ld_is_found_with_a_parser_in_lists_graphs_and_item_lists_and_bad_blocks_skipped():
    graph = jp.job_postings(_page("graph.html"), "https://jobs.fabrikam.example/")
    assert [o["title"] for o in graph] == [
        "Registered Nurse", "Billing Specialist", "Pharmacy Technician"
    ]  # @graph members, an @type list, and an ItemList item; the broken block is skipped
    listing = jp.job_postings(_page("list_jsonld.html"), "https://careers.harborpoint.example/")
    assert len(listing) == 4  # a top-level JSON array
    assert jp.job_postings(_page("js_only.html"), "https://talent.contoso.example/") == []


def test_the_parser_ignores_other_scripts_and_comments_and_survives_broken_markup():
    html = (
        '<script>var a = {"@type":"JobPosting","title":"Not data","description":"x"}</script>'
        '<!-- <script type="application/ld+json">{"@type":"JobPosting"}</script> -->'
        '<SCRIPT TYPE="application/ld+json; charset=utf-8">'
        '{"@type":"JobPosting","title":"Real","description":"<p>Body</p>"}</SCRIPT>'
        "<div <<< unclosed"
    )
    assert [o["title"] for o in jp.job_postings(html, "https://x.example/")] == ["Real"]


def test_a_deeply_nested_block_is_skipped_not_fatal():
    deep = "[" * 5000 + "]" * 5000
    html = f'<script type="application/ld+json">{deep}</script>'
    assert jp.job_postings(html, "https://x.example/") == []


def test_posting_mapping_remote_pay_dates_and_expiry():
    posts = {
        o["identifier"]["value"]: o
        for o in jp.job_postings(_page("list_jsonld.html"), "https://careers.harborpoint.example/")
        if "identifier" in o
    }
    url = "https://careers.harborpoint.example/"
    a = jp.to_raw_posting(posts["HP-101"], url)
    assert a.external_id == "HP-101" and a.title == "Data Analyst"
    assert a.url == "https://careers.harborpoint.example/jobs/hp-101"
    assert a.org_name == "Harbor Point Analytics"
    assert a.org_url == "https://careers.harborpoint.example"
    assert a.location_raw == "Denver, CO, US" and a.is_remote is None
    assert (a.comp_min, a.comp_max, a.comp_currency, a.comp_interval) == (
        85000.0, 105000.0, "USD", "year")
    assert a.date_posted == "2026-10-01" and a.employment_type == "FULL_TIME"
    assert a.description_html.startswith("<p>Data Analyst at Harbor Point")
    r = jp.to_raw_posting(posts["HP-102"], url)  # TELECOMMUTE, no jobLocation, hourly value
    assert r.is_remote is True and r.remote_hint == "Remote" and r.location_raw == "USA"
    assert (r.comp_min, r.comp_max, r.comp_interval) == (32.0, 32.0, "hour")
    assert jp.to_raw_posting(posts["HP-103"], url) is None  # validThrough 2025-01-01: expired


def test_weekly_pay_is_stated_per_year_and_unknown_units_leave_the_interval_unknown():
    assert jp._salary({"currency": "USD", "value": {"minValue": 1000, "unitText": "WEEK"}}) == (
        52000.0, None, "USD", "year")
    assert jp._salary({"value": {"value": "50,000", "unitText": "FORTNIGHT"}}) == (
        50000.0, 50000.0, None, None)
    assert jp._salary({"value": {"unitText": "YEAR"}}) == (None, None, None, None)
    assert jp._salary("lots") == (None, None, None, None)


def test_a_posting_needs_a_title_and_a_description_and_an_id_is_always_present():
    assert jp.to_raw_posting({"title": "T"}, "https://x.example/") is None
    assert jp.to_raw_posting({"description": "D"}, "https://x.example/") is None
    p = jp.to_raw_posting({"title": "T", "description": "D"}, "https://x.example/list")
    assert p.external_id == "https://x.example/list#T|" and p.url == "https://x.example/list"
    q = jp.to_raw_posting({"title": "T", "description": "D", "url": "/job/9"}, "https://x.example/")
    assert q.external_id == "https://x.example/job/9"


def test_the_one_posting_reader_uses_the_same_parser():
    p = parse_job_posting(_page("detail_phenom.html"), "https://careers.northwind.example/j")
    assert p is not None and p.title == "Senior Data Scientist"


# ------------------------------------------------------------------------ the adapter
@respx.mock
async def test_a_list_page_with_json_ld_is_one_request_and_expired_roles_are_left_out():
    _robots("careers.harborpoint.example")
    page = respx.get("https://careers.harborpoint.example/jobs").mock(
        return_value=Response(200, text=_page("list_jsonld.html")))
    stub = respx.get("https://careers.harborpoint.example/jobs/hp-104").mock(
        return_value=Response(200, text=_page("detail_stub.html")))
    got = await _fetch("https://careers.harborpoint.example/jobs")
    assert isinstance(got, PostingBatch) and got.partial_error is None
    assert page.call_count == 1 and stub.call_count == 1  # the stub's own page, one level
    assert [p.external_id for p in got] == ["HP-101", "HP-102", "HP-104"]  # HP-103 expired
    assert got[2].is_remote is True and got[2].title == "Stub Role"


@respx.mock
async def test_a_graph_page_yields_every_posting_and_the_company_name_fills_a_missing_org():
    _robots("jobs.fabrikam.example")
    respx.get("https://jobs.fabrikam.example/").mock(
        return_value=Response(200, text=_page("graph.html")))
    got = await _fetch("https://jobs.fabrikam.example/", company_name="Fabrikam Co")
    titles = ["Registered Nurse", "Billing Specialist", "Pharmacy Technician"]
    assert [p.title for p in got] == titles
    assert {p.org_name for p in got} == {"Harbor Point Analytics", "Fabrikam Health"}


@respx.mock
async def test_a_links_only_list_page_follows_job_pages_one_level_and_skips_the_rest():
    _robots("careers.northwind.example")
    base = "https://careers.northwind.example/us/en"
    listing = respx.get(f"{base}/search-jobs").mock(
        return_value=Response(200, text=_page("list_links.html").replace(
            "/us/en/search-results", "/us/en/search-jobs")))
    detail = respx.get(f"{base}/job/1202337/Senior-Data-Scientist").mock(
        return_value=Response(200, text=_page("detail_phenom.html")))
    other = respx.get(f"{base}/job/1211928/Program-Manager").mock(
        return_value=Response(200, text="<html>no data</html>"))
    gone = respx.get(f"{base}/job/1999999/Gone-Role").mock(return_value=Response(404))
    got = await _fetch(f"{base}/search-jobs")
    assert [p.title for p in got] == ["Senior Data Scientist"]
    assert listing.call_count == 1 and detail.call_count == 1  # the #apply twin is the same page
    assert other.call_count == 1 and gone.call_count == 1
    assert got.partial_error is None
    p = got[0]
    assert p.external_id == "1202337" and p.org_name == "Northwind"
    assert p.location_raw == "Sunnyvale, California, United States of America"
    assert p.date_posted == "2026-09-07" and unescape(p.description_html).startswith(
        "Senior Data Scientist<p")


@respx.mock
async def test_the_detail_page_cap_is_enforced_and_said_in_the_run():
    _robots("careers.example.org")
    total = cp.MAX_DETAIL_PAGES + 7
    links = "".join(f'<a href="/jobs/{i}/role-{i}">r{i}</a>' for i in range(total))
    respx.get("https://careers.example.org/jobs").mock(
        return_value=Response(200, text=f"<html><body>{links}</body></html>"))
    detail = respx.get(url__regex=r"https://careers\.example\.org/jobs/\d+/role-\d+").mock(
        return_value=Response(200, text=_page("detail_stub.html")))
    got = await _fetch("https://careers.example.org/jobs")
    assert detail.call_count == cp.MAX_DETAIL_PAGES
    assert got.partial_error == (
        f"This page links to {cp.MAX_DETAIL_PAGES + 7} job pages. Kestrel reads at most "
        f"{cp.MAX_DETAIL_PAGES} a run, so 7 were left for later."
    )
    assert len(got) == 1  # every detail page is the same stub posting, so one id


@respx.mock
async def test_a_page_that_builds_its_list_in_the_browser_is_told_plainly_never_a_silent_zero():
    _robots("talent.contoso.example")
    respx.get("https://talent.contoso.example/").mock(
        return_value=Response(200, text=_page("js_only.html")))
    got = await _fetch("https://talent.contoso.example/")
    assert list(got) == [] and got.partial_error == JS_ONLY
    assert JS_ONLY == "This page lists its jobs with JavaScript, which Kestrel cannot read yet."


@respx.mock
async def test_a_known_hiring_system_link_uses_its_own_adapter_and_fetches_no_page():
    board = json.loads((_FIXTURES / "recruitee_offers.json").read_text(encoding="utf-8"))
    respx.get("https://northwind.recruitee.com/robots.txt").mock(return_value=Response(404))
    api = respx.get("https://northwind.recruitee.com/api/offers/").mock(
        return_value=Response(200, json=board))
    page = respx.get("https://northwind.recruitee.com/").mock(return_value=Response(200, text=""))
    got = await _fetch("https://northwind.recruitee.com/", company_name="Northwind Co")
    assert api.call_count == 1 and page.call_count == 0 and len(got) == 3
    assert {p.org_name for p in got} == {"Northwind Co"}


@respx.mock
async def test_a_page_whose_markup_names_a_hiring_system_is_read_by_that_adapter():
    board = json.loads((_FIXTURES / "recruitee_offers.json").read_text(encoding="utf-8"))
    _robots("careers.northwind.example")
    respx.get("https://northwind.recruitee.com/robots.txt").mock(return_value=Response(404))
    respx.get("https://careers.northwind.example/").mock(return_value=Response(
        200, text='<script src="https://northwind.recruitee.com/widget/x.js"></script>'))
    api = respx.get("https://northwind.recruitee.com/api/offers/").mock(
        return_value=Response(200, json=board))
    got = await _fetch("https://careers.northwind.example/")
    assert api.call_count == 1 and len(got) == 3


@respx.mock
async def test_robots_not_found_rate_limit_and_server_errors_are_reported_not_hidden():
    _robots("closed.example", "User-agent: *\nDisallow: /\n", status=200)
    with pytest.raises(AdapterError, match="robots.txt"):
        await _fetch("https://closed.example/careers")
    _robots("gone.example")
    respx.get("https://gone.example/careers").mock(return_value=Response(404))
    with pytest.raises(AdapterError, match="not found"):
        await _fetch("https://gone.example/careers")
    _robots("busy.example")
    respx.get("https://busy.example/careers").mock(return_value=Response(429))
    got = await _fetch("https://busy.example/careers")
    assert list(got) == [] and "rate limiting" in got.partial_error
    _robots("down.example")
    respx.get("https://down.example/careers").mock(return_value=Response(500))
    with pytest.raises(AdapterError, match="500"):
        await _fetch("https://down.example/careers")


@respx.mock
async def test_a_link_into_a_private_network_is_refused(monkeypatch):
    from kestrel.ingest import http as ingest_http

    async def _private(host, port):
        return ["10.0.0.5"]

    monkeypatch.setattr(ingest_http, "_resolve", _private)
    with pytest.raises(AdapterError, match="cannot read that link"):
        await _fetch("https://intranet.example/careers")


def test_validate_config_and_registration():
    adapter = get_adapter("careers_page")
    assert isinstance(adapter, CareersPageAdapter)
    adapter.validate_config({"url": "https://careers.example.com/jobs"})
    for bad in ({}, {"url": ""}, {"url": "ftp://x.example/"}, {"url": "https://localhost"},
                {"url": 5}):
        with pytest.raises(SourceConfigError):
            adapter.validate_config(bad)


def test_job_links_keep_job_pages_and_drop_categories_files_other_sites_and_the_page_itself():
    html = _page("list_links.html") + (
        '<a href="/files/brochure.pdf">pdf</a><a href="/jobs/view/7.pdf">x</a>'
        '<a href="https://other.example/job/5/x">other</a>'
        '<a href="/apply?jobid=44">q</a><a href="/jobs">self</a><a href="/jobs/">self</a>'
    )
    got = job_links(html, "https://careers.northwind.example/jobs")
    assert got == [
        "https://careers.northwind.example/us/en/job/1202337/Senior-Data-Scientist",
        "https://careers.northwind.example/us/en/job/1211928/Program-Manager",
        "https://careers.northwind.example/us/en/job/1999999/Gone-Role",
        "https://careers.northwind.example/apply?jobid=44",
    ]


# ------------------------------------------------- the run: D-SRC-13 area and title drop
def _profile() -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_dir().joinpath("profile.yml").write_text(
        "employment:\n  employment_terms:\n    target_titles: [Data Analyst, Support Engineer]\n"
        "  geo_rules:\n    remote_country_allowlist: [US]\n", encoding="utf-8")


@respx.mock
async def test_roles_go_through_the_area_and_title_drop_before_they_are_stored(sm):
    _profile()
    _robots("careers.harborpoint.example")
    respx.get("https://careers.harborpoint.example/jobs").mock(
        return_value=Response(200, text=_page("list_jsonld.html")))
    respx.get("https://careers.harborpoint.example/jobs/hp-104").mock(
        return_value=Response(200, text=_page("detail_stub.html")))
    async with sm() as s:
        src = Source(track="employment", kind="careers_page", name="Harbor Point",
                     config={"url": "https://careers.harborpoint.example/jobs"}, enabled=True,
                     cadence_hours=24)
        s.add(src)
        await s.commit()
        await s.refresh(src)
    async with _http() as http:
        out = await run_source(sm, src.id, http=http)
    assert out.status == "ok", out.error
    assert out.counts["fetched"] == 3
    assert out.counts["dropped_unrelated_title"] == 1  # "Stub Role" is not a target title
    async with sm() as s:
        titles = sorted(o.title for o in (await s.execute(select(Opportunity))).scalars().all())
    assert titles == ["Data Analyst", "Support Engineer"]


@respx.mock
async def test_a_javascript_page_makes_a_partial_run_that_says_why(sm):
    _robots("talent.contoso.example")
    respx.get("https://talent.contoso.example/").mock(
        return_value=Response(200, text=_page("js_only.html")))
    async with sm() as s:
        src = Source(track="employment", kind="careers_page", name="Contoso",
                     config={"url": "https://talent.contoso.example/"}, enabled=True,
                     cadence_hours=24)
        s.add(src)
        await s.commit()
        await s.refresh(src)
    async with _http() as http:
        out = await run_source(sm, src.id, http=http)
    assert out.status == "partial" and JS_ONLY in (out.error or "")
