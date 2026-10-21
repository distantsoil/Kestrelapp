"""RemoteRocketship, discovery only (#38, D-SRC-14). The paid API is respx-mocked from scrubbed
captures; nothing here touches the network. The design under test: only each job's employer link
survives, recognized boards take the D-SRC-7 discovery path, other links are read from the
employer's public page with robots.txt checked, unreadable links are counted, the key never
appears anywhere, and the provider's own error text never reaches a page."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import respx
import structlog
from httpx import Response
from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.models import Opportunity, ScrapeRun, Source
from kestrel.ingest import board_discovery as bd
from kestrel.ingest.adapters import get_adapter
from kestrel.ingest.adapters.remoterocketship import RemoteRocketshipAdapter
from kestrel.ingest.base import AdapterError, SourceConfigError
from kestrel.ingest.http import HttpClient
from kestrel.ingest.runner import run_source

FIX = Path(__file__).parent / "fixtures"
JOBS = json.loads((FIX / "remoterocketship_jobs.json").read_text())
ERR401 = json.loads((FIX / "remoterocketship_401.json").read_text())
API = "https://www.remoterocketship.com/api/openclaw/jobs/"
KEY = "rr-secret-key-0123456789"
URLS = [j["url"] for j in JOBS["jobOpenings"]]
EMPLOYER_URL = "https://careers.fabrikam.example.com/jobs/3001"
LDJSON = """<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting","title":"Platform Engineer",
 "description":"<p>Build and run the platform.</p>","datePosted":"2026-10-15",
 "employmentType":"FULL_TIME","hiringOrganization":{"@type":"Organization","name":"Fabrikam"},
 "jobLocationType":"TELECOMMUTE",
 "jobLocation":{"@type":"Place","address":{"addressLocality":"Austin","addressRegion":"TX",
 "addressCountry":"US"}}}
</script></head><body>x</body></html>"""


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("REMOTEROCKETSHIP_API_KEY", KEY)


def _http(robots: bool = False) -> HttpClient:
    return HttpClient(user_agent=get_settings().ingest.user_agent, respect_robots=robots)


def _page(n: int, has_next: bool) -> dict:
    return {**JOBS, "pagination": {**JOBS["pagination"], "page": n, "hasNextPage": has_next}}


# ------------------------------------------------------------------------------ the adapter
@respx.mock
async def test_posts_to_the_slashed_endpoint_and_keeps_only_employer_links():
    route = respx.post(API).mock(return_value=Response(200, json=_page(1, False)))
    async with _http() as http:
        got = await RemoteRocketshipAdapter().fetch({"max_pages": 2}, http)
    assert [p.url for p in got] == URLS
    sent = route.calls[0].request
    assert sent.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(sent.content)
    assert body["includeJobDescription"] is False
    assert body["filters"]["page"] == 1 and body["filters"]["itemsPerPage"] == 50
    assert route.call_count == 1  # hasNextPage was false
    for p in got:  # nothing but the link survives the adapter
        assert (p.title, p.org_name, p.description_md, p.description_html, p.raw) == (
            None, None, None, None, {}
        )


@respx.mock
async def test_follows_pages_up_to_the_cap_and_no_further():
    route = respx.post(API).mock(return_value=Response(200, json=_page(1, True)))
    async with _http() as http:
        await RemoteRocketshipAdapter().fetch({"max_pages": 2, "items_per_page": 20}, http)
    assert route.call_count == 2
    pages = [json.loads(c.request.content)["filters"]["page"] for c in route.calls]
    assert pages == [1, 2]
    assert json.loads(route.calls[0].request.content)["filters"]["itemsPerPage"] == 20


@respx.mock
async def test_user_filters_are_passed_through_alongside_paging():
    route = respx.post(API).mock(return_value=Response(200, json=_page(1, False)))
    cfg = {"filters": {"titles": ["Data Engineer"]}}
    async with _http() as http:
        await RemoteRocketshipAdapter().fetch(cfg, http)
    assert json.loads(route.calls[0].request.content)["filters"]["titles"] == ["Data Engineer"]


async def test_no_key_is_a_plain_config_error_not_a_crash(monkeypatch):
    monkeypatch.delenv("REMOTEROCKETSHIP_API_KEY", raising=False)
    async with _http() as http:
        with pytest.raises(SourceConfigError) as ei:
            await RemoteRocketshipAdapter().fetch({}, http)
    assert "REMOTEROCKETSHIP_API_KEY" in str(ei.value) and "subscription" in str(ei.value)


def test_config_validation():
    a = RemoteRocketshipAdapter()
    a.validate_config({})
    a.validate_config({"max_pages": 3, "items_per_page": 10, "api_key_env": "MY_RR_KEY"})
    for bad in (
        {"api_key": "x"},  # a key never lives in a config row
        {"max_pages": 0}, {"max_pages": 99}, {"items_per_page": 51}, {"items_per_page": 0},
        {"api_key_env": "not a name"}, {"filters": "titles"},
    ):
        with pytest.raises(SourceConfigError):
            a.validate_config(bad)
    assert get_adapter("remoterocketship") is not None


@pytest.mark.parametrize(
    ("status", "needle"),
    [(401, "did not receive"), (403, "subscription"), (400, "rejected the request"),
     (413, "rejected the request"), (500, "problem on their side")],
)
@respx.mock
async def test_error_statuses_become_authored_messages(status, needle):
    respx.post(API).mock(return_value=Response(status, json=ERR401))
    async with _http() as http:
        with pytest.raises(AdapterError) as ei:
            await RemoteRocketshipAdapter().fetch({}, http)
    text = str(ei.value)
    assert needle in text
    for leak in (ERR401["message"], ERR401["hint"], ERR401["code"], KEY):
        assert leak not in text
    assert "—" not in text


@respx.mock
async def test_an_invalid_key_is_told_apart_from_a_missing_one():
    respx.post(API).mock(return_value=Response(401, json={**ERR401, "message": "Invalid API key"}))
    async with _http() as http:
        with pytest.raises(AdapterError) as ei:
            await RemoteRocketshipAdapter().fetch({}, http)
    assert "did not accept" in str(ei.value)


@respx.mock
async def test_a_429_on_the_first_page_is_an_empty_partial_batch_not_a_failure():
    route = respx.post(API).mock(
        return_value=Response(429, json={"message": "Daily request limit reached"}))
    async with _http() as http:
        got = await RemoteRocketshipAdapter().fetch({}, http)
    assert list(got) == [] and "daily limit" in got.partial_error
    assert route.call_count == 1  # never retried
    assert "Daily request limit" not in got.partial_error


@respx.mock
async def test_a_429_after_a_good_page_keeps_the_links():
    respx.post(API).mock(side_effect=[
        Response(200, json=_page(1, True)),
        Response(429, json={"message": "Daily request limit reached"}),
    ])
    async with _http() as http:
        got = await RemoteRocketshipAdapter().fetch({"max_pages": 3}, http)
    assert [p.url for p in got] == URLS and "daily limit" in got.partial_error


@respx.mock
async def test_the_key_never_reaches_a_log_or_an_error(caplog):
    caplog.set_level(logging.DEBUG)
    respx.post(API).mock(return_value=Response(401, json=ERR401))
    with structlog.testing.capture_logs() as logs:
        async with _http() as http:
            with pytest.raises(AdapterError) as ei:
                await RemoteRocketshipAdapter().fetch({}, http)
    assert KEY not in str(ei.value)
    assert KEY not in json.dumps(logs, default=str)
    assert KEY not in caplog.text


# ------------------------------------------------------- discovery of the links, then storage
async def _source(sm, **cfg) -> Source:
    async with sm() as s:
        src = Source(track="employment", kind="remoterocketship", name="RemoteRocketship",
                     config=cfg, enabled=True, cadence_hours=24)
        s.add(src)
        await s.commit()
        await s.refresh(src)
        return src


def _employer_mocks(robots_txt: str = "User-agent: *\nDisallow:\n", page: str = LDJSON):
    respx.get("https://careers.fabrikam.example.com/robots.txt").mock(
        return_value=Response(200, text=robots_txt))
    return respx.get(EMPLOYER_URL).mock(
        return_value=Response(200, text=page, headers={"content-type": "text/html"}))


@respx.mock
async def test_a_run_discovers_known_boards_and_reads_the_unknown_employer_page(sm):
    respx.post(API).mock(return_value=Response(200, json=_page(1, False)))
    page = _employer_mocks()
    src = await _source(sm)
    async with _http(robots=True) as http:
        out = await run_source(sm, src.id, http=http)
    assert out.status == "ok", out.error
    assert page.call_count == 1
    c = out.counts
    assert (c["links"], c["boards_found"], c["pages_read"], c["unreadable"]) == (3, 2, 1, 0)
    async with sm() as s:
        found = (await s.execute(select(Source).where(Source.origin == bd.DISCOVERED))).scalars()
        assert sorted(f.kind for f in found) == ["ashby", "workday"]
        (opp,) = (await s.execute(select(Opportunity))).scalars().all()
        assert opp.title == "Platform Engineer" and opp.url == EMPLOYER_URL
        assert opp.source_id == src.id


@respx.mock
async def test_nothing_from_the_reply_is_stored_anywhere(sm):
    respx.post(API).mock(return_value=Response(200, json=_page(1, False)))
    _employer_mocks()
    src = await _source(sm)
    async with _http(robots=True) as http:
        await run_source(sm, src.id, http=http)
    job = JOBS["jobOpenings"][0]
    forbidden = [job["roleTitle"], job["company"]["name"], job["company"]["homePageURL"],
                 job["jobDescriptionSummary"], str(job["id"]), "jobOpenings", "roleTitle"]
    async with sm() as s:
        dump = json.dumps(
            [r.__dict__.get("counts") for r in (await s.execute(select(ScrapeRun))).scalars()]
            + [(o.title, o.description_md, o.raw if hasattr(o, "raw") else None)
               for o in (await s.execute(select(Opportunity))).scalars()]
            + [(x.name, x.config) for x in (await s.execute(select(Source))).scalars()],
            default=str,
        )
    for needle in forbidden:
        assert needle not in dump, needle


@respx.mock
async def test_a_robots_disallow_drops_the_link_and_counts_it(sm):
    respx.post(API).mock(return_value=Response(200, json=_page(1, False)))
    page = _employer_mocks(robots_txt="User-agent: *\nDisallow: /jobs/\n")
    src = await _source(sm)
    async with _http(robots=True) as http:
        out = await run_source(sm, src.id, http=http)
    assert page.call_count == 0  # never fetched
    assert (out.counts["unreadable"], out.counts["pages_read"]) == (1, 0)
    assert out.status == "ok"
    async with sm() as s:
        assert (await s.execute(select(Opportunity))).scalars().all() == []


@respx.mock
async def test_a_page_without_posting_data_is_unreadable(sm):
    respx.post(API).mock(return_value=Response(200, json=_page(1, False)))
    _employer_mocks(page="<html><body>Careers at Fabrikam</body></html>")
    src = await _source(sm)
    async with _http(robots=True) as http:
        out = await run_source(sm, src.id, http=http)
    assert out.counts["unreadable"] == 1 and out.counts["pages_read"] == 0


@respx.mock
async def test_a_redirecting_employer_link_is_not_followed(sm):
    respx.post(API).mock(return_value=Response(200, json=_page(1, False)))
    respx.get("https://careers.fabrikam.example.com/robots.txt").mock(
        return_value=Response(200, text="User-agent: *\nDisallow:\n"))
    other = respx.get("https://elsewhere.example.org/x").mock(return_value=Response(200))
    respx.get(EMPLOYER_URL).mock(
        return_value=Response(302, headers={"location": "https://elsewhere.example.org/x"}))
    src = await _source(sm)
    async with _http(robots=True) as http:
        out = await run_source(sm, src.id, http=http)
    assert other.call_count == 0 and out.counts["unreadable"] == 1


@respx.mock
async def test_a_429_run_is_partial_not_failed_and_does_not_count_a_failure(sm):
    respx.post(API).mock(side_effect=[
        Response(200, json=_page(1, True)),
        Response(429, json={"message": "Daily request limit reached"}),
    ])
    _employer_mocks()
    src = await _source(sm, max_pages=2)
    async with _http(robots=True) as http:
        out = await run_source(sm, src.id, http=http)
    assert out.status == "partial" and "daily limit" in out.error
    assert out.counts["boards_found"] == 2
    async with sm() as s:
        assert (await s.get(Source, src.id)).consecutive_failures == 0


@respx.mock
async def test_a_bad_key_run_is_partial_with_the_authored_message_only(sm):
    respx.post(API).mock(return_value=Response(401, json=ERR401))
    src = await _source(sm)
    async with _http(robots=True) as http:
        out = await run_source(sm, src.id, http=http)
    assert out.status == "partial" and ERR401["hint"] not in out.error
    assert KEY not in out.error


@respx.mock
async def test_unreadable_links_are_capped_and_counted(sm, monkeypatch):
    from kestrel.ingest import employer_links

    monkeypatch.setattr(employer_links, "_PAGE_RATE", 1000.0)  # the test must not wait 20 s
    many = {**JOBS, "jobOpenings": [{"url": f"https://careers.fabrikam.example.com/jobs/{i}"}
                                    for i in range(30)]}
    respx.post(API).mock(return_value=Response(200, json=many))
    respx.get("https://careers.fabrikam.example.com/robots.txt").mock(
        return_value=Response(200, text="User-agent: *\nDisallow:\n"))
    respx.get(url__regex=r"https://careers\.fabrikam\.example\.com/jobs/\d+").mock(
        return_value=Response(404))
    src = await _source(sm)
    async with _http(robots=True) as http:
        out = await run_source(sm, src.id, http=http)
    assert out.counts["unreadable"] == 30  # 20 tried and unreadable, 10 over the cap
    assert employer_links.MAX_PAGES_PER_RUN == 20


@respx.mock
async def test_repeated_daily_limit_runs_never_disable_the_source(sm):
    respx.post(API).mock(return_value=Response(429, json={"message": "Daily request limit"}))
    src = await _source(sm)
    for _ in range(4):
        async with _http(robots=True) as http:
            out = await run_source(sm, src.id, http=http)
        assert out.status == "partial" and "daily limit" in out.error
    async with sm() as s:
        got = await s.get(Source, src.id)
        assert got.enabled and got.consecutive_failures == 0


@respx.mock
async def test_a_resolve_that_raises_stores_nothing_from_the_link_batch(sm, monkeypatch):
    from kestrel.ingest import employer_links

    respx.post(API).mock(return_value=Response(200, json=_page(1, False)))

    async def boom(*a, **kw):
        raise RuntimeError("resolver blew up")

    monkeypatch.setattr(employer_links, "resolve", boom)
    src = await _source(sm)
    async with _http(robots=True) as http:
        out = await run_source(sm, src.id, http=http)
    assert out.status == "partial" and "resolver blew up" in out.error
    async with sm() as s:
        assert (await s.execute(select(Opportunity))).scalars().all() == []


def test_deeply_nested_json_ld_is_unreadable_not_a_crash():
    from kestrel.ingest.employer_page import parse_job_posting

    for depth in (900, 5000, 200_000):
        html = ('<script type="application/ld+json">' + "[" * depth + "]" * depth + "</script>")
        assert parse_job_posting(html, "https://x.example.com/j") is None
    nested = '{"a":' * 900 + "1" + "}" * 900
    html = f'<script type="application/ld+json">{nested}</script>'
    assert parse_job_posting(html, "https://x.example.com/j") is None


@respx.mock
async def test_the_providers_own_site_is_never_fetched(sm):
    own = ["https://www.remoterocketship.com/us/company/x/jobs/1",
           "https://remoterocketship.com/job/2", "https://jobs.remoterocketship.com/3"]
    many = {**JOBS, "jobOpenings": [{"url": u} for u in own] + [{"url": EMPLOYER_URL}]}
    respx.post(API).mock(return_value=Response(200, json=many))
    page = _employer_mocks()
    theirs = respx.get(url__regex=r"https://([a-z]+\.)?remoterocketship\.com/.*").mock(
        return_value=Response(200, text=LDJSON))
    src = await _source(sm)
    async with _http(robots=True) as http:
        out = await run_source(sm, src.id, http=http)
    assert theirs.call_count == 0 and page.call_count == 1
    assert out.counts["unreadable"] == 3 and out.counts["pages_read"] == 1


@respx.mock
async def test_no_filters_means_the_profiles_titles_and_places_user_filters_win(monkeypatch):
    from types import SimpleNamespace as NS

    from kestrel.ingest.adapters import remoterocketship as rr

    prof = NS(
        employment_terms=NS(target_titles=["Data Engineer", "Analytics Engineer"]),
        geo_rules=NS(onsite_metros=[NS(name="Austin"), NS(name="Denver")]),
    )
    monkeypatch.setattr(rr, "load_profile", lambda track: prof if track == "employment" else None)
    route = respx.post(API).mock(return_value=Response(200, json=_page(1, False)))
    async with _http() as http:
        await RemoteRocketshipAdapter().fetch({}, http)
        await RemoteRocketshipAdapter().fetch({"filters": {"jobTitleFilters": ["Chef"]}}, http)
    first = json.loads(route.calls[0].request.content)["filters"]
    assert first["jobTitleFilters"] == ["Data Engineer", "Analytics Engineer"]
    assert first["locationFilters"] == ["Austin", "Denver"]
    second = json.loads(route.calls[1].request.content)["filters"]
    assert second["jobTitleFilters"] == ["Chef"] and "locationFilters" not in second


@respx.mock
async def test_no_profile_means_no_default_filters(monkeypatch):
    from kestrel.ingest.adapters import remoterocketship as rr

    monkeypatch.setattr(rr, "load_profile", lambda track: None)
    route = respx.post(API).mock(return_value=Response(200, json=_page(1, False)))
    async with _http() as http:
        await RemoteRocketshipAdapter().fetch({}, http)
    assert set(json.loads(route.calls[0].request.content)["filters"]) == {"page", "itemsPerPage"}


# --------------------------------------------------------------------------------- help copy
def test_help_documents_remoterocketship_without_em_dashes():
    import re

    from fastapi.testclient import TestClient

    from kestrel.db import migrate
    from kestrel.web.app import build_app

    migrate.run_migrations()
    with TestClient(build_app(worker=False)) as c:
        page = c.get("/help")
    assert page.status_code == 200
    m = re.search(r'<h3 id="remoterocketship".*?(?=<h3>Region</h3>)', page.text, re.S)
    assert m, "the RemoteRocketship help section did not render"
    section = m.group(0)
    for needle in ("own RemoteRocketship subscription", "REMOTEROCKETSHIP_API_KEY", ".env",
                   "Only the link to each employer", "500 requests or 3,000 jobs per UTC day",
                   "Needs an active paid", "Daily limit"):
        assert needle.lower() in section.lower() or needle in section, needle
    assert "—" not in section and "–" not in section
    # the catalog row for the kind also renders in the sources table above it
    assert "remoterocketship" in page.text
