"""Teamtailor career site adapter (B5, D-SRC-15). The fixtures are scrubbed captures of one real
public career site's `/jobs.json` and `/jobs.rss` (2026-10-29: employer, domain and street
replaced, descriptions shortened); nothing here touches the network. Under test: both feeds'
field mapping, ISO country codes read as names, the RSS fallback, a site with its feeds off
(partial with a plain sentence, never a failure), 403, 429 and robots.txt, config validation, link
recognition (host and career-site markup), and that roles go through the D-SRC-13 area and title
drop."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from kestrel import paths
from kestrel.config import get_settings
from kestrel.db.models import Opportunity, Source
from kestrel.ingest import board_discovery as bd
from kestrel.ingest import http as ingest_http
from kestrel.ingest.adapters import get_adapter
from kestrel.ingest.adapters import teamtailor as tt_module
from kestrel.ingest.adapters.teamtailor import TeamtailorAdapter, feed_off_message
from kestrel.ingest.base import AdapterError, PostingBatch, SourceConfigError
from kestrel.ingest.http import HttpClient
from kestrel.ingest.runner import run_source

_FIXTURES = Path(__file__).parent / "fixtures"
_JSON = json.loads((_FIXTURES / "teamtailor_jobs.json").read_text(encoding="utf-8"))
_RSS = (_FIXTURES / "teamtailor_jobs.rss").read_text(encoding="utf-8")
_DOMAIN = "careers.northwind.example"
_JSON_URL = f"https://{_DOMAIN}/jobs.json"
_RSS_URL = f"https://{_DOMAIN}/jobs.rss"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No pacing sleeps and no DNS: the career domain is the operator's, so the guarded fetch
    checks that it resolves to a public address."""
    monkeypatch.setattr(tt_module, "_RATE", 0)

    async def _public(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(ingest_http, "_resolve", _public)


def _feed(**changes) -> dict:
    return {**copy.deepcopy(_JSON), **changes}


def _http() -> HttpClient:
    return HttpClient(
        user_agent=get_settings().ingest.user_agent, respect_robots=False, max_attempts=1
    )


async def _fetch(config: dict | None = None):
    async with _http() as client:
        return await TeamtailorAdapter().fetch({"domain": _DOMAIN, **(config or {})}, client)


def _mock_off():
    respx.get(_JSON_URL).mock(return_value=Response(404))
    respx.get(_RSS_URL).mock(return_value=Response(404))


@respx.mock
async def test_teamtailor_json_feed_field_mapping():
    route = respx.get(_JSON_URL).mock(return_value=Response(200, json=_JSON))
    rss = respx.get(_RSS_URL).mock(return_value=Response(200, text=_RSS))
    got = await _fetch()

    assert isinstance(got, PostingBatch) and got.partial_error is None
    assert route.call_count == 1 and not rss.called  # the JSON feed is enough
    assert [p.external_id for p in got] == [i["id"] for i in _JSON["items"]]
    p = got[0]
    assert p.title == "Strategic Partnerships Manager - Chicago"
    assert p.url == "https://careers.northwind.example/jobs/8632755-strategic-partnerships-manager-chicago"
    assert p.org_name == "Northwind"  # hiringOrganization.name
    assert p.location_raw == "Chicago, United States"
    assert p.date_posted == "2026-10-28T18:26:40+01:00"
    assert "Join us to shape the future" in p.description_html and p.description_md is None
    assert p.is_remote is None and p.remote_hint is None  # the JSON feed has no remote flag
    assert p.raw["_jobposting"]["identifier"]["value"] == 8632755
    # An ISO code is read as the country's name: Colombia, never a US state.
    assert got[1].location_raw == "Bogotá, Colombia"
    assert got[2].location_raw.endswith("Sweden")


@respx.mock
async def test_teamtailor_canada_is_not_california_and_remote_type_is_read():
    item = copy.deepcopy(_JSON["items"][0])
    jp = item["_jobposting"]
    jp["jobLocation"] = [
        {"address": {"addressLocality": "Toronto", "addressCountry": "CA"}},
        {"address": {"addressLocality": "Toronto", "addressCountry": "CA"}},
        {"address": {"addressLocality": "Leeds", "addressCountry": "GB"}},
        {"address": {"addressLocality": "Oslo", "addressCountry": "Norway"}},
    ]
    jp["jobLocationType"] = "TELECOMMUTE"
    respx.get(_JSON_URL).mock(return_value=Response(200, json=_feed(items=[item])))
    (p,) = await _fetch({"company_name": "Northwind Inc"})
    assert p.location_raw == "Toronto, Canada; Leeds, United Kingdom; Oslo, Norway"
    assert p.is_remote is True and p.remote_hint == "Remote"
    assert p.org_name == "Northwind Inc"


@respx.mock
async def test_teamtailor_items_without_an_id_are_skipped_and_a_missing_org_uses_the_domain():
    bare = {"id": "abc", "title": "Analyst", "url": "https://x.example/jobs/1"}
    respx.get(_JSON_URL).mock(
        return_value=Response(200, json=_feed(items=[bare, {"title": "no id"}, "junk"]))
    )
    (p,) = await _fetch()
    assert (p.external_id, p.org_name, p.location_raw, p.description_html) == (
        "abc", _DOMAIN, None, None)


@respx.mock
async def test_teamtailor_falls_back_to_the_rss_when_there_is_no_json_feed():
    respx.get(_JSON_URL).mock(return_value=Response(404))
    respx.get(_RSS_URL).mock(return_value=Response(200, text=_RSS))
    got = await _fetch()
    assert got.partial_error is None and len(got) == 2
    p, q = got
    assert p.external_id == "90555eb4-c67c-488b-978d-3efc4c53d2e2"
    assert p.title == "Strategic Partnerships Manager - Chicago"
    assert p.org_name == "Northwind" and p.location_raw == "Chicago, United States"
    assert p.remote_hint == "Hybrid" and p.is_remote is None
    assert p.date_posted == "2026-10-28T18:26:40+01:00"
    assert "Join us to shape the future" in p.description_html
    assert p.raw["department"] == "Partnerships"
    assert q.external_id == "10331f7b-0619-462d-a571-d84b19e6da45" and q.remote_hint == "Hybrid"
    assert q.location_raw == "Bogotá, Colombia"


@respx.mock
async def test_teamtailor_a_200_page_in_place_of_the_json_feed_falls_back_to_the_rss():
    respx.get(_JSON_URL).mock(return_value=Response(200, text="<html>Careers</html>"))
    respx.get(_RSS_URL).mock(return_value=Response(200, text=_RSS))
    got = await _fetch()
    assert len(got) == 2


@respx.mock
async def test_teamtailor_remote_status_remote_sets_the_flag():
    respx.get(_JSON_URL).mock(return_value=Response(404))
    remote = _RSS.replace("<remoteStatus>hybrid", "<remoteStatus>remote", 1)
    respx.get(_RSS_URL).mock(return_value=Response(200, text=remote))
    got = await _fetch()
    assert got[0].is_remote is True and got[0].remote_hint == "Remote"


@respx.mock
async def test_teamtailor_feeds_switched_off_is_a_partial_run_with_a_plain_sentence():
    _mock_off()
    got = await _fetch()
    assert list(got) == [] and got.partial_error == feed_off_message(_DOMAIN)
    assert "turned off" in got.partial_error and "One posting" in got.partial_error
    assert "—" not in got.partial_error


@respx.mock
@pytest.mark.parametrize(
    "status, phrase", [(403, "refuses automated reads"), (429, "rate limiting")]
)
async def test_teamtailor_403_and_429_are_partial_not_failures(status, phrase):
    json_route = respx.get(_JSON_URL).mock(return_value=Response(status))
    rss = respx.get(_RSS_URL).mock(return_value=Response(200, text=_RSS))
    got = await _fetch()
    assert list(got) == [] and phrase in got.partial_error
    assert json_route.called and not rss.called  # a refusal is not retried on the other feed


@respx.mock
async def test_teamtailor_robots_disallow_is_partial_and_nothing_else_is_requested():
    robots = respx.get(f"https://{_DOMAIN}/robots.txt").mock(
        return_value=Response(200, text="User-agent: *\nDisallow: /\n")
    )
    feed = respx.get(_JSON_URL).mock(return_value=Response(200, json=_JSON))
    async with HttpClient(
        user_agent=get_settings().ingest.user_agent, respect_robots=True, max_attempts=1
    ) as client:
        got = await TeamtailorAdapter().fetch({"domain": _DOMAIN}, client)
    assert robots.called and not feed.called
    assert list(got) == [] and "robots.txt" in got.partial_error


@respx.mock
@pytest.mark.parametrize("reply", [
    Response(200, json=[1]), Response(200, json={"items": "x"}), Response(500),
])
async def test_teamtailor_bad_shapes_and_server_errors_raise_adapter_error(reply):
    respx.get(_JSON_URL).mock(return_value=reply)
    respx.get(_RSS_URL).mock(return_value=Response(404))
    with pytest.raises(AdapterError):
        await _fetch()


@respx.mock
async def test_teamtailor_unreadable_rss_raises_adapter_error():
    respx.get(_JSON_URL).mock(return_value=Response(404))
    respx.get(_RSS_URL).mock(return_value=Response(200, text="<rss><channel>"))
    with pytest.raises(AdapterError, match="unreadable"):
        await _fetch()
    respx.get(_RSS_URL).mock(
        return_value=Response(200, text='<!DOCTYPE x [<!ENTITY a "b">]><rss><channel/></rss>')
    )
    with pytest.raises(AdapterError, match="refused"):
        await _fetch()


async def test_teamtailor_a_private_address_is_refused(monkeypatch):
    async def _private(host, port):
        return ["10.0.0.5"]

    monkeypatch.setattr(ingest_http, "_resolve", _private)
    with pytest.raises(AdapterError, match="cannot be read"):
        await _fetch()


def test_teamtailor_validate_config_errors():
    a = TeamtailorAdapter()
    a.validate_config({"domain": "career.example.org"})
    a.validate_config({"domain": "northwind.teamtailor.com", "company_name": "Northwind"})
    for bad in (
        {}, {"domain": ""}, {"domain": 5}, {"domain": "localhost"}, {"domain": "10.0.0.1"},
        {"domain": "https://career.example.org"}, {"domain": "career.example.org/jobs"},
        {"domain": "a b.example.org"}, {"domain": "-x.example.org"},
    ):
        with pytest.raises(SourceConfigError):
            a.validate_config(bad)


def test_teamtailor_is_registered():
    assert get_adapter("teamtailor") is not None


def test_board_discovery_recognizes_a_teamtailor_link():
    for url in (
        "https://northwind.teamtailor.com/jobs",
        "https://Northwind.teamtailor.com/jobs/123-analyst",
        "https://career.teamtailor.com/",
    ):
        ref = bd.board_from_url(url)
        assert ref is not None and ref.kind == "teamtailor"
        assert ref.config == {"domain": url.split("/")[2].lower()}
    assert bd.board_from_url("https://Northwind.teamtailor.com/").key == (
        "teamtailor", "northwind.teamtailor.com")
    for url in (
        "https://www.teamtailor.com/", "https://app.teamtailor.com/", "https://teamtailor.com/",
        "https://a.b.teamtailor.com/", "https://northwind.teamtailor.com.evil.x/",
        "https://example.com/northwind.teamtailor.com/",
    ):
        assert bd.board_from_url(url) is None


def test_a_career_site_on_its_own_domain_is_recognized_by_its_script():
    page = '<script src="//assets-aws.teamtailor-cdn.com/assets/careersite-11761e4a.js"></script>'
    ref = bd.board_from_markup(page, "https://Careers.Northwind.example/jobs")
    assert ref is not None
    assert (ref.kind, ref.config) == ("teamtailor", {"domain": "careers.northwind.example"})
    # Only the script counts (images from the CDN are on many pages), and it needs a real host.
    images = '<img src="//images.teamtailor-cdn.com/images/s3/x/logo.svg">'
    assert bd.board_from_markup(images, "https://careers.northwind.example/") is None
    assert bd.board_from_markup(page, None) is None
    assert bd.board_from_markup(page, "http://10.0.0.1/") is None
    assert bd.board_from_markup(page, "not a url") is None


# ------------------------------------------------- the run: D-SRC-13 area and title drop
def _profile() -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_dir().joinpath("profile.yml").write_text(
        "employment:\n  employment_terms:\n    target_titles: [Program Analyst, Data Analyst]\n"
        "  geo_rules:\n    remote_country_allowlist: [US]\n", encoding="utf-8")


@respx.mock
async def test_roles_go_through_the_area_and_title_drop_before_they_are_stored(sm):
    _profile()

    def item(i, title, city, country):
        it = copy.deepcopy(_JSON["items"][0])
        it.update(id=f"id-{i}", title=title, url=f"https://x.example/jobs/{i}")
        it["_jobposting"]["jobLocation"] = [
            {"address": {"addressLocality": city, "addressCountry": country}}
        ]
        return it

    items = [
        item(1, "Program Analyst", "Chicago", "US"),
        item(2, "Data Analyst", "Stockholm", "SE"),
        item(3, "Forestry Technician", "Chicago", "US"),
    ]
    respx.get(_JSON_URL).mock(return_value=Response(200, json=_feed(items=items)))
    async with sm() as s:
        src = Source(track="employment", kind="teamtailor", name="Northwind jobs (Teamtailor)",
                     config={"domain": _DOMAIN}, enabled=True, cadence_hours=24)
        s.add(src)
        await s.commit()
        await s.refresh(src)
    async with _http() as http:
        out = await run_source(sm, src.id, http=http)
    assert out.status == "ok", out.error
    assert out.counts["fetched"] == 3
    assert out.counts["dropped_outside_area"] == 1  # Stockholm, Sweden
    assert out.counts["dropped_unrelated_title"] == 1  # Forestry Technician
    async with sm() as s:
        titles = [o.title for o in (await s.execute(select(Opportunity))).scalars().all()]
    assert titles == ["Program Analyst"]


@respx.mock
async def test_a_site_with_its_feeds_off_is_a_partial_run_that_counts_no_failure(sm):
    _profile()
    _mock_off()
    async with sm() as s:
        src = Source(track="employment", kind="teamtailor", name="Northwind jobs (Teamtailor)",
                     config={"domain": _DOMAIN}, enabled=True, cadence_hours=24)
        s.add(src)
        await s.commit()
        await s.refresh(src)
    async with _http() as http:
        out = await run_source(sm, src.id, http=http)
    assert out.status == "partial" and out.error == feed_off_message(_DOMAIN)
    async with sm() as s:
        assert (await s.get(Source, src.id)).consecutive_failures == 0
