"""Personio job board adapter (B5, D-SRC-15). `personio_feed.xml` is the real XML feed of
Personio's own public demo board (verified 2026-10-29; four positions, placeholder text) plus one
CONSTRUCTED position with an empty `<jobDescriptions/>`, the common real-world case, and
`personio_job.html` is a job page built from the shape of a real page's schema.org JobPosting with
invented values. Nothing here touches the network. Under test: field mapping, the empty-description
job-page read and its cap, the page's country replacing the bare office name, 429 on the list and on
a page (partial, keeps what was collected), a failing page, unknown boards, config validation, link
recognition and that roles go through the D-SRC-13 area and title drop."""

from __future__ import annotations

from pathlib import Path

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from kestrel import paths
from kestrel.config import get_settings
from kestrel.db.models import Opportunity, Source
from kestrel.ingest import board_discovery as bd
from kestrel.ingest.adapters import get_adapter
from kestrel.ingest.adapters import personio as personio_module
from kestrel.ingest.adapters.personio import PersonioAdapter
from kestrel.ingest.base import AdapterError, PostingBatch, SourceConfigError
from kestrel.ingest.http import HttpClient
from kestrel.ingest.runner import run_source

_FIXTURES = Path(__file__).parent / "fixtures"
_FEED = (_FIXTURES / "personio_feed.xml").read_text(encoding="utf-8")
_PAGE = (_FIXTURES / "personio_job.html").read_text(encoding="utf-8")
_BASE = "https://northwind.jobs.personio.de"
_XML = f"{_BASE}/xml"
_EMPTY_ID = "1822299"


@pytest.fixture(autouse=True)
def _no_rate_wait(monkeypatch):
    monkeypatch.setattr(personio_module, "_RATE", 0)


def _http() -> HttpClient:
    return HttpClient(
        user_agent=get_settings().ingest.user_agent, respect_robots=False, max_attempts=1
    )


async def _fetch(config: dict | None = None):
    async with _http() as client:
        return await PersonioAdapter().fetch({"slug": "northwind", **(config or {})}, client)


def _feed_with_empty_roles(n: int) -> str:
    """The fixture feed with `n` extra positions that have no description."""
    one = _FEED[_FEED.index("<position>\n    <id>1822299"):_FEED.index("</workzag-jobs>")]
    extra = "".join(one.replace(_EMPTY_ID, str(1822300 + i)) for i in range(n))
    return _FEED.replace("</workzag-jobs>", extra + "</workzag-jobs>")


@respx.mock
async def test_personio_field_mapping():
    respx.get(_XML).mock(return_value=Response(200, text=_FEED))
    page = respx.get(f"{_BASE}/job/{_EMPTY_ID}").mock(return_value=Response(200, text=_PAGE))
    got = await _fetch()

    assert isinstance(got, PostingBatch) and got.partial_error is None
    assert [p.external_id for p in got] == ["1822268", "1822269", "1822270", "1822267", _EMPTY_ID]
    assert page.call_count == 1  # only the role with an empty description needs its page
    first = got[0]
    assert first.title == "Bewerberpool"
    assert first.url == f"{_BASE}/job/1822268"
    assert first.org_name == "northwind"  # the feed names no employer
    assert first.location_raw == "München"
    assert first.date_posted == "2017-03-16T16:25:09+00:00"
    assert first.employment_type == "full-time"
    assert first.description_md is None
    # Sections keep their headings; the HTML is the employer's own.
    assert first.description_html.startswith("<h3>Ihre Aufgaben</h3>\n<p ")
    assert "<h3>Ihr Profil</h3>" in first.description_html
    assert "<h3>Warum wir?</h3>" in first.description_html
    assert first.is_remote is None and first.remote_hint is None


@respx.mock
async def test_personio_remote_office_and_pay():
    respx.get(_XML).mock(return_value=Response(200, text=_FEED))
    respx.get(f"{_BASE}/job/{_EMPTY_ID}").mock(return_value=Response(200, text=_PAGE))
    got = await _fetch({"company_name": "Northwind GmbH"})
    remote = next(p for p in got if p.external_id == "1822269")
    assert remote.location_raw == "Remote"
    assert remote.is_remote is True and remote.remote_hint == "Remote"
    assert {p.org_name for p in got} == {"Northwind GmbH"}
    paid = next(p for p in got if p.comp_min is not None)
    assert (paid.comp_min, paid.comp_max, paid.comp_currency, paid.comp_interval) == (
        45000.0, 55000.0, "EUR", "year")
    unpaid = next(p for p in got if p.external_id == "1822268")
    assert unpaid.comp_min is None and unpaid.comp_currency is None


@respx.mock
async def test_personio_an_empty_description_is_filled_from_the_job_page_with_its_country():
    respx.get(_XML).mock(return_value=Response(200, text=_FEED))
    respx.get(f"{_BASE}/job/{_EMPTY_ID}").mock(return_value=Response(200, text=_PAGE))
    got = await _fetch()
    p = got[-1]
    assert p.title == "Operations Analyst"
    assert "keep the daily operations reports accurate" in p.description_html
    # The bare office name "Chicago" becomes a place with a country the area rule can read.
    assert p.location_raw == "Chicago, United States"
    assert p.raw["page"]["datePosted"] == "2026-10-20"
    assert p.date_posted == "2026-10-20T09:30:00+00:00"  # still the feed's createdAt


@respx.mock
async def test_personio_hybrid_office_and_other_domain():
    xml = _FEED.replace("<office>München</office>", "<office>München (hybrid)</office>", 1)
    route = respx.get("https://northwind.jobs.personio.com/xml").mock(
        return_value=Response(200, text=xml)
    )
    respx.get(f"https://northwind.jobs.personio.com/job/{_EMPTY_ID}").mock(
        return_value=Response(200, text=_PAGE)
    )
    got = await _fetch({"tld": "com"})
    assert route.called
    assert got[0].remote_hint == "Hybrid" and got[0].is_remote is None
    assert got[0].url == "https://northwind.jobs.personio.com/job/1822268"


@respx.mock
async def test_personio_job_page_cap_keeps_the_roles_and_says_how_many_lack_a_body():
    respx.get(_XML).mock(return_value=Response(200, text=_feed_with_empty_roles(3)))
    pages = respx.get(url__regex=rf"{_BASE}/job/\d+").mock(return_value=Response(200, text=_PAGE))
    got = await _fetch({"max_results": 2})
    assert pages.call_count == 2
    assert len(got) == 8  # nothing dropped
    bodiless = [p for p in got if p.description_html is None]
    assert len(bodiless) == 2
    assert "2 roles" in got.partial_error and "max_results" in got.partial_error
    assert "—" not in got.partial_error


@respx.mock
async def test_personio_429_on_a_job_page_stops_reading_pages_and_keeps_every_role():
    respx.get(_XML).mock(return_value=Response(200, text=_feed_with_empty_roles(2)))
    pages = respx.get(url__regex=rf"{_BASE}/job/\d+").mock(return_value=Response(429))
    got = await _fetch()
    assert pages.call_count == 1  # the first 429 ends the page reads
    assert len(got) == 7 and sum(p.description_html is None for p in got) == 3
    assert "rate limiting" in got.partial_error and "3 roles" in got.partial_error


@respx.mock
async def test_personio_429_on_the_list_is_a_partial_run_not_a_failure():
    respx.get(_XML).mock(return_value=Response(429))
    got = await _fetch()
    assert list(got) == [] and "rate limiting" in got.partial_error


@respx.mock
async def test_personio_a_failing_job_page_leaves_that_role_without_a_body():
    respx.get(_XML).mock(return_value=Response(200, text=_FEED))
    respx.get(f"{_BASE}/job/{_EMPTY_ID}").mock(return_value=Response(500))
    got = await _fetch()
    assert len(got) == 5 and got[-1].description_html is None and got.partial_error is None
    assert got[-1].location_raw == "Chicago"  # the feed's office name stays


@respx.mock
async def test_personio_a_page_without_ld_json_keeps_the_role_without_a_body():
    respx.get(_XML).mock(return_value=Response(200, text=_FEED))
    respx.get(f"{_BASE}/job/{_EMPTY_ID}").mock(return_value=Response(200, text="<html></html>"))
    got = await _fetch()
    assert got[-1].description_html is None and got[-1].location_raw == "Chicago"


@respx.mock
async def test_personio_empty_board_is_an_empty_run():
    respx.get(_XML).mock(return_value=Response(200, text="<workzag-jobs></workzag-jobs>"))
    got = await _fetch()
    assert list(got) == [] and got.partial_error is None


@respx.mock
async def test_personio_unknown_board_is_an_adapter_error():
    respx.get(_XML).mock(return_value=Response(404, text="<html>Not found</html>"))
    with pytest.raises(AdapterError, match="board not found: northwind"):
        await _fetch()


@respx.mock
@pytest.mark.parametrize("reply", [
    Response(200, text="<html>nope</html>"), Response(200, text="<workzag-jobs><position>"),
    Response(200, text='<!DOCTYPE x [<!ENTITY a "b">]><workzag-jobs/>'), Response(500),
])
async def test_personio_bad_feeds_raise_adapter_error(reply):
    respx.get(_XML).mock(return_value=reply)
    with pytest.raises(AdapterError):
        await _fetch()


def test_personio_validate_config_errors():
    a = PersonioAdapter()
    a.validate_config({"slug": "northwind"})
    a.validate_config({"slug": "north-wind", "tld": "com", "max_results": 10, "company_name": "N"})
    for bad in (
        {}, {"slug": ""}, {"slug": "a.b"}, {"slug": 5}, {"slug": "-x"},
        {"slug": "x", "tld": "org"}, {"slug": "x", "max_results": 0},
        {"slug": "x", "max_results": True},
    ):
        with pytest.raises(SourceConfigError):
            a.validate_config(bad)


def test_personio_is_registered():
    assert get_adapter("personio") is not None


def test_board_discovery_recognizes_a_personio_link():
    ref = bd.board_from_url("https://Northwind.jobs.personio.de/job/1822268?language=en")
    assert ref is not None and (ref.kind, ref.config) == ("personio", {"slug": "northwind"})
    com = bd.board_from_url("https://northwind.jobs.personio.com/")
    assert com is not None and com.config == {"slug": "northwind", "tld": "com"}
    assert ref.key != com.key  # the two domains hold separate accounts
    for url in (
        "https://jobs.personio.de/", "https://www.jobs.personio.de/",
        "https://northwind.jobs.personio.org/", "https://northwind.jobs.personio.de.evil.x/",
        "https://example.com/northwind.jobs.personio.de/",
    ):
        assert bd.board_from_url(url) is None


# ------------------------------------------------- the run: D-SRC-13 area and title drop
def _profile() -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_dir().joinpath("profile.yml").write_text(
        "employment:\n  employment_terms:\n    target_titles: [Operations Analyst, Data Analyst]\n"
        "  geo_rules:\n    remote_country_allowlist: [US]\n", encoding="utf-8")


@respx.mock
async def test_roles_go_through_the_area_and_title_drop_before_they_are_stored(sm):
    _profile()
    xml = _FEED.replace("<name>Bewerberpool</name>", "<name>Data Analyst</name>", 1).replace(
        "<office>München</office>", "<office>Berlin, Germany</office>", 1)
    respx.get(_XML).mock(return_value=Response(200, text=xml))
    respx.get(f"{_BASE}/job/{_EMPTY_ID}").mock(return_value=Response(200, text=_PAGE))
    async with sm() as s:
        src = Source(track="employment", kind="personio", name="Northwind jobs (Personio)",
                     config={"slug": "northwind"}, enabled=True, cadence_hours=24)
        s.add(src)
        await s.commit()
        await s.refresh(src)
    async with _http() as http:
        out = await run_source(sm, src.id, http=http)
    assert out.status == "ok", out.error
    assert out.counts["fetched"] == 5
    assert out.counts["dropped_outside_area"] == 1  # "Berlin, Germany" (the page-less role)
    # Four other roles are not target titles; "Operations Analyst" in Chicago, US stays.
    assert out.counts["dropped_unrelated_title"] == 3
    async with sm() as s:
        titles = [o.title for o in (await s.execute(select(Opportunity))).scalars().all()]
    assert titles == ["Operations Analyst"]
