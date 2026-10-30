"""Recruitee company board adapter (B5, D-SRC-15). The fixture is a scrubbed capture of one real
public board's `/api/offers/` reply (2026-10-29: company name, mailbox addresses and careers
domain replaced, descriptions shortened); nothing here touches the network. Under test: field
mapping, the description join, multi-place locations, the remote flags, pay, 404 and 429 and bad
shapes, config validation, link recognition (host and custom-domain markup), and that roles go
through the D-SRC-13 area and title drop."""

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
from kestrel.ingest.adapters import get_adapter
from kestrel.ingest.adapters import recruitee as recruitee_module
from kestrel.ingest.adapters.recruitee import RecruiteeAdapter
from kestrel.ingest.base import AdapterError, PostingBatch, SourceConfigError
from kestrel.ingest.http import HttpClient
from kestrel.ingest.runner import run_source

_FIXTURES = Path(__file__).parent / "fixtures"
_BOARD = json.loads((_FIXTURES / "recruitee_offers.json").read_text(encoding="utf-8"))
_URL = "https://northwind.recruitee.com/api/offers/"


@pytest.fixture(autouse=True)
def _no_rate_wait(monkeypatch):
    monkeypatch.setattr(recruitee_module, "_RATE", 0)


def _board(**changes) -> dict:
    return {**copy.deepcopy(_BOARD), **changes}


def _http() -> HttpClient:
    return HttpClient(
        user_agent=get_settings().ingest.user_agent, respect_robots=False, max_attempts=1
    )


async def _fetch(config: dict | None = None):
    async with _http() as client:
        return await RecruiteeAdapter().fetch({"slug": "northwind", **(config or {})}, client)


@respx.mock
async def test_recruitee_field_mapping():
    route = respx.get(_URL).mock(return_value=Response(200, json=_BOARD))
    got = await _fetch()

    assert isinstance(got, PostingBatch) and got.partial_error is None
    assert route.call_count == 1  # the whole board, descriptions included, is one request
    assert [p.external_id for p in got] == ["2800490", "2798695", "2797591"]
    p = got[0]
    assert p.title == "Data Scientist Marketing"
    assert p.url == "https://careers.northwind.example/o/data-scientist-marketing-3"
    assert p.apply_url == "https://careers.northwind.example/o/data-scientist-marketing-3/c/new"
    assert p.org_name == "Northwind"
    assert p.location_raw == "Amsterdam, Noord-Holland, Netherlands"
    assert p.employment_type == "fulltime_permanent"
    assert p.date_posted == "2026-10-28 13:21:58 UTC"
    assert p.is_remote is None and p.remote_hint == "Hybrid"  # hybrid true, remote false
    assert p.comp_min is None and p.comp_max is None and p.comp_interval is None
    assert p.description_md is None
    assert p.raw["id"] == 2800490


@respx.mock
async def test_recruitee_body_is_description_then_requirements():
    offer = {**_BOARD["offers"][0], "description": "<p>About</p>", "requirements": "<p>Need</p>"}
    respx.get(_URL).mock(return_value=Response(200, json={"offers": [offer]}))
    (p,) = await _fetch()
    assert p.description_html == "<p>About</p>\n<p>Need</p>"
    blank = {**offer, "description": "  ", "requirements": None}
    respx.get(_URL).mock(return_value=Response(200, json={"offers": [blank]}))
    (p,) = await _fetch()
    assert p.description_html is None


@respx.mock
async def test_recruitee_every_place_a_role_is_open_is_in_the_location():
    respx.get(_URL).mock(return_value=Response(200, json=_BOARD))
    got = await _fetch()
    assert got[1].location_raw == "Sofia, Sofia (stolitsa), Bulgaria; İstanbul, İstanbul, Türkiye"
    assert got[2].location_raw.count(";") == 2


@respx.mock
async def test_recruitee_remote_flags_and_pay_and_name_override():
    offer = {
        **_BOARD["offers"][0], "remote": True, "hybrid": False, "on_site": False,
        "salary": {"min": "90,000", "max": 110000, "period": "year", "currency": "USD"},
    }
    respx.get(_URL).mock(return_value=Response(200, json={"offers": [offer]}))
    (p,) = await _fetch({"company_name": "Northwind Inc"})
    assert p.is_remote is True and p.remote_hint == "Remote"
    assert (p.comp_min, p.comp_max, p.comp_currency, p.comp_interval) == (
        90000.0, 110000.0, "USD", "year")
    assert p.org_name == "Northwind Inc"
    plain = {**offer, "remote": False, "hybrid": False, "on_site": False,
             "salary": {"min": None, "max": None, "period": "year", "currency": "USD"}}
    respx.get(_URL).mock(return_value=Response(200, json={"offers": [plain]}))
    (p,) = await _fetch()
    assert p.is_remote is None and p.remote_hint is None  # never guessed as on-site
    assert p.comp_currency is None and p.comp_interval is None  # no amount, no currency


@respx.mock
async def test_recruitee_unpublished_and_idless_offers_are_skipped_and_url_falls_back():
    first = {**_BOARD["offers"][0], "careers_url": None}
    offers = [first, {**_BOARD["offers"][1], "status": "draft"}, {"title": "No id"}, "junk"]
    respx.get(_URL).mock(return_value=Response(200, json={"offers": offers}))
    got = await _fetch()
    assert [p.external_id for p in got] == ["2800490"]
    assert got[0].url == "https://northwind.recruitee.com/o/data-scientist-marketing-3"


@respx.mock
async def test_recruitee_empty_board_is_an_empty_run():
    respx.get(_URL).mock(return_value=Response(200, json={"offers": []}))
    got = await _fetch()
    assert list(got) == [] and got.partial_error is None


@respx.mock
async def test_recruitee_unknown_board_is_an_adapter_error():
    respx.get(_URL).mock(return_value=Response(404, json={"error": "Not Found"}))
    with pytest.raises(AdapterError, match="board not found: northwind"):
        await _fetch()


@respx.mock
async def test_recruitee_429_is_a_partial_run_not_a_failure():
    respx.get(_URL).mock(return_value=Response(429))
    got = await _fetch()
    assert list(got) == [] and "rate limiting" in got.partial_error
    assert "—" not in got.partial_error


@respx.mock
@pytest.mark.parametrize("reply", [
    Response(200, json=[1, 2]), Response(200, json={"offers": "x"}), Response(200, text="<html>"),
    Response(500),
])
async def test_recruitee_bad_shapes_and_server_errors_raise_adapter_error(reply):
    respx.get(_URL).mock(return_value=reply)
    with pytest.raises(AdapterError):
        await _fetch()


def test_recruitee_validate_config_errors():
    a = RecruiteeAdapter()
    a.validate_config({"slug": "northwind"})
    a.validate_config({"slug": "north-wind-2", "company_name": "Northwind"})
    for bad in ({}, {"slug": ""}, {"slug": "a.b"}, {"slug": "-x"}, {"slug": 5}, {"slug": "a/b"}):
        with pytest.raises(SourceConfigError):
            a.validate_config(bad)


def test_recruitee_is_registered():
    assert get_adapter("recruitee") is not None


def test_board_discovery_recognizes_a_recruitee_link():
    for url in (
        "https://northwind.recruitee.com/",
        "https://Northwind.recruitee.com/o/data-scientist-marketing-3",
        "http://northwind.recruitee.com/api/offers/",
    ):
        ref = bd.board_from_url(url)
        assert ref is not None and (ref.kind, ref.config) == ("recruitee", {"slug": "northwind"})
    ref = bd.board_from_url("https://Northwind.recruitee.com/o/x")
    assert ref.key == ("recruitee", "northwind")
    for url in (
        "https://www.recruitee.com/", "https://careers.recruitee.com/", "https://recruitee.com/",
        "https://a.b.recruitee.com/", "https://northwind.recruitee.com.evil.x/",
        "https://example.com/northwind.recruitee.com/",
    ):
        assert bd.board_from_url(url) is None


def test_a_careers_page_on_its_own_domain_is_recognized_by_its_markup():
    page = (
        '<script>fetch("https://northwind.recruitee.com/api/offers/" + slug)</script>'
        '<a href="//Northwind.recruitee.com/o/analyst">Analyst</a>'
    )
    ref = bd.board_from_markup(page, "https://careers.northwind.example/")
    assert ref is not None and (ref.kind, ref.config) == ("recruitee", {"slug": "northwind"})
    assert bd.board_from_markup('<a href="https://www.recruitee.com/o/x">x</a>') is None
    assert bd.board_from_markup("<html>nothing here</html>") is None
    assert bd.board_from_markup(None) is None and bd.board_from_markup("") is None


# ------------------------------------------------- the run: D-SRC-13 area and title drop
def _profile() -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_dir().joinpath("profile.yml").write_text(
        "employment:\n  employment_terms:\n    target_titles: [Program Analyst, Data Analyst]\n"
        "  geo_rules:\n    remote_country_allowlist: [US]\n", encoding="utf-8")


@respx.mock
async def test_roles_go_through_the_area_and_title_drop_before_they_are_stored(sm):
    _profile()
    base = _BOARD["offers"][0]

    def offer(i, title, where, state, country):
        return {**base, "id": i, "title": title, "location": f"{where}, {state}, {country}",
                "locations": [], "slug": f"role-{i}", "careers_url": f"https://x.example/o/{i}"}

    offers = [
        offer(1, "Program Analyst", "Chicago", "Illinois", "United States"),
        offer(2, "Data Analyst", "Amsterdam", "Noord-Holland", "Netherlands"),
        offer(3, "Forestry Technician", "Chicago", "Illinois", "United States"),
    ]
    respx.get(_URL).mock(return_value=Response(200, json={"offers": offers}))
    async with sm() as s:
        src = Source(track="employment", kind="recruitee", name="Northwind jobs (Recruitee)",
                     config={"slug": "northwind"}, enabled=True, cadence_hours=24)
        s.add(src)
        await s.commit()
        await s.refresh(src)
    async with _http() as http:
        out = await run_source(sm, src.id, http=http)
    assert out.status == "ok", out.error
    assert out.counts["fetched"] == 3
    assert out.counts["dropped_outside_area"] == 1  # Amsterdam, Netherlands
    assert out.counts["dropped_unrelated_title"] == 1  # Forestry Technician
    async with sm() as s:
        titles = [o.title for o in (await s.execute(select(Opportunity))).scalars().all()]
    assert titles == ["Program Analyst"]
