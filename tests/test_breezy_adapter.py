"""Breezy HR company job board adapter (B5, D-SRC-15). `breezy_list.json` is the real `/json`
reply of Breezy's own public trial board (verified 2026-10-29; three positions of placeholder
data) and `breezy_detail.html` is a real posting page of it (a maps key removed). Nothing here
touches the network. Under test: field mapping, the description read from each posting page and
its cap, the page URL built on the board's own host, 429 on the list and on a page (partial, keeps
what was collected), a failing page, unknown boards, config validation, link recognition and that
roles go through the D-SRC-13 area and title drop."""

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
from kestrel.ingest.adapters import breezy as breezy_module
from kestrel.ingest.adapters import get_adapter
from kestrel.ingest.adapters.breezy import BreezyAdapter
from kestrel.ingest.base import AdapterError, PostingBatch, SourceConfigError
from kestrel.ingest.http import HttpClient
from kestrel.ingest.runner import run_source

_FIXTURES = Path(__file__).parent / "fixtures"
_LIST = json.loads((_FIXTURES / "breezy_list.json").read_text(encoding="utf-8"))
_PAGE = (_FIXTURES / "breezy_detail.html").read_text(encoding="utf-8")
_JSON_URL = "https://breezy.breezy.hr/json"
_P = "https://breezy.breezy.hr/p"
_IDS = [r["friendly_id"] for r in _LIST]


@pytest.fixture(autouse=True)
def _no_rate_wait(monkeypatch):
    monkeypatch.setattr(breezy_module, "_RATE", 0)


def _http() -> HttpClient:
    return HttpClient(
        user_agent=get_settings().ingest.user_agent, respect_robots=False, max_attempts=1
    )


async def _fetch(config: dict | None = None):
    async with _http() as client:
        return await BreezyAdapter().fetch({"slug": "breezy", **(config or {})}, client)


def _mock_pages(text: str = _PAGE):
    return respx.get(url__regex=rf"{_P}/.+").mock(return_value=Response(200, text=text))


def _rows(n: int) -> list[dict]:
    rows = copy.deepcopy(_LIST)
    base = rows[0]
    while len(rows) < n:
        i = len(rows)
        rows.append({**copy.deepcopy(base), "id": f"x{i}", "friendly_id": f"x{i}-role-{i}"})
    return rows[:n]


@respx.mock
async def test_breezy_field_mapping():
    respx.get(_JSON_URL).mock(return_value=Response(200, json=_LIST))
    pages = _mock_pages()
    got = await _fetch()

    assert isinstance(got, PostingBatch) and got.partial_error is None
    assert [p.external_id for p in got] == [r["id"] for r in _LIST]
    assert pages.call_count == 3
    assert [c.request.url.path for c in pages.calls] == [f"/p/{i}" for i in _IDS]
    p = got[0]
    assert p.title == "Employee #12"
    assert p.url == "https://breezy.breezy.hr/p/98323abf2296-employee-12"
    assert p.org_name == "MS Breezy Trial"  # company.name
    assert p.location_raw == "Chaos, Florida, United States"
    assert p.employment_type == "Other"
    assert p.date_posted == "2024-02-15T14:37:22.684Z"
    assert p.description_html == "<p>Roses and Rainbows until 30 days in.</p>"
    assert p.description_md is None
    assert p.is_remote is None and p.remote_hint is None  # is_remote false: never guessed
    assert p.comp_min is None and p.raw["salary"] == "$0.05 – $0.06 / hour"
    assert got[1].location_raw == "New York, New York, United States"
    assert got[1].employment_type == "Contract"


@respx.mock
async def test_breezy_remote_flag_company_name_and_every_place():
    rows = copy.deepcopy(_LIST)[:1]
    rows[0]["location"]["is_remote"] = True
    rows[0]["locations"].append(
        {"country": {"name": "Canada", "id": "CA"}, "state": {"id": "ON", "name": "Ontario"},
         "city": "Toronto", "name": "Toronto, ON"})
    respx.get(_JSON_URL).mock(return_value=Response(200, json=rows))
    _mock_pages()
    (p,) = await _fetch({"company_name": "Northwind"})
    assert p.is_remote is True and p.remote_hint == "Remote"
    assert p.org_name == "Northwind"
    assert p.location_raw == "Chaos, Florida, United States; Toronto, Ontario, Canada"


@respx.mock
async def test_breezy_page_url_is_built_on_the_boards_own_host():
    rows = copy.deepcopy(_LIST)[:1]
    rows[0]["url"] = "https://evil.example/p/98323abf2296-employee-12"
    respx.get(_JSON_URL).mock(return_value=Response(200, json=rows))
    pages = _mock_pages()
    (p,) = await _fetch()
    assert p.url == f"{_P}/{_IDS[0]}"
    assert pages.calls[0].request.url.host == "breezy.breezy.hr"
    rows[0]["friendly_id"] = "../../admin"
    respx.get(_JSON_URL).mock(return_value=Response(200, json=rows))
    assert list(await _fetch()) == []  # an id that is not a plain id is skipped


@respx.mock
async def test_breezy_description_falls_back_to_the_page_ld_json():
    ld = {"@type": "JobPosting", "description": "<p>From the schema.</p>"}
    page = ('<html><head><script type="application/ld+json">' + json.dumps(ld)
            + '</script></head><body><div class="description"> </div></body></html>')
    respx.get(_JSON_URL).mock(return_value=Response(200, json=_LIST[:1]))
    _mock_pages(page)
    (p,) = await _fetch()
    assert p.description_html == "<p>From the schema.</p>"


@respx.mock
async def test_breezy_empty_board_is_an_empty_run():
    respx.get(_JSON_URL).mock(return_value=Response(200, json=[]))
    got = await _fetch()
    assert list(got) == [] and got.partial_error is None


@respx.mock
async def test_breezy_page_cap_keeps_the_roles_and_says_how_many_lack_a_body():
    respx.get(_JSON_URL).mock(return_value=Response(200, json=_rows(5)))
    pages = _mock_pages()
    got = await _fetch({"max_results": 2})
    assert pages.call_count == 2 and len(got) == 5  # nothing dropped
    assert sum(p.description_html is None for p in got) == 3
    assert "5 roles" in got.partial_error and "at most 2" in got.partial_error
    assert "max_results" in got.partial_error and "—" not in got.partial_error
    exact = await _fetch({"max_results": 5})  # at the cap exactly: no partial
    assert exact.partial_error is None


@respx.mock
async def test_breezy_429_on_a_page_stops_reading_pages_and_keeps_every_role():
    respx.get(_JSON_URL).mock(return_value=Response(200, json=_rows(4)))
    pages = _mock_pages()
    pages.mock(side_effect=[Response(200, text=_PAGE), Response(429)])
    got = await _fetch()
    assert pages.call_count == 2  # the 429 ends the page reads
    assert len(got) == 4 and [p.description_html is None for p in got] == [False, True, True, True]
    assert "rate limiting" in got.partial_error and "3 roles" in got.partial_error


@respx.mock
async def test_breezy_429_on_the_list_is_a_partial_run_not_a_failure():
    respx.get(_JSON_URL).mock(return_value=Response(429))
    got = await _fetch()
    assert list(got) == [] and "rate limiting" in got.partial_error


@respx.mock
async def test_breezy_a_failing_page_leaves_only_that_role_without_a_body():
    respx.get(_JSON_URL).mock(return_value=Response(200, json=_LIST))
    respx.get(f"{_P}/{_IDS[0]}").mock(return_value=Response(200, text=_PAGE))
    respx.get(f"{_P}/{_IDS[1]}").mock(return_value=Response(404))
    respx.get(f"{_P}/{_IDS[2]}").mock(return_value=Response(200, text="<html></html>"))
    got = await _fetch()
    assert len(got) == 3 and got.partial_error is None
    assert [p.description_html is None for p in got] == [False, True, True]


@respx.mock
async def test_breezy_unknown_board_is_an_adapter_error():
    respx.get(_JSON_URL).mock(return_value=Response(404, text="<html>Not found</html>"))
    with pytest.raises(AdapterError, match="board not found: breezy"):
        await _fetch()


@respx.mock
@pytest.mark.parametrize("reply", [
    Response(200, json={"jobs": []}), Response(200, text="<html>x</html>"), Response(500),
])
async def test_breezy_bad_shapes_and_server_errors_raise_adapter_error(reply):
    respx.get(_JSON_URL).mock(return_value=reply)
    with pytest.raises(AdapterError):
        await _fetch()


def test_breezy_validate_config_errors():
    a = BreezyAdapter()
    a.validate_config({"slug": "breezy"})
    a.validate_config({"slug": "north-wind", "company_name": "N", "max_results": 10})
    for bad in (
        {}, {"slug": ""}, {"slug": "a.b"}, {"slug": 5}, {"slug": "-x"},
        {"slug": "x", "max_results": 0}, {"slug": "x", "max_results": True},
    ):
        with pytest.raises(SourceConfigError):
            a.validate_config(bad)


def test_breezy_is_registered():
    assert get_adapter("breezy") is not None


def test_board_discovery_recognizes_a_breezy_link():
    for url in (
        "https://breezy.breezy.hr/", "https://Breezy.breezy.hr/p/98323abf2296-employee-12",
        "http://breezy.breezy.hr/json",
    ):
        ref = bd.board_from_url(url)
        assert ref is not None and (ref.kind, ref.config) == ("breezy", {"slug": "breezy"})
    assert bd.board_from_url("https://Breezy.breezy.hr/").key == ("breezy", "breezy")
    for url in (
        "https://www.breezy.hr/", "https://app.breezy.hr/", "https://breezy.hr/",
        "https://a.b.breezy.hr/", "https://breezy.breezy.hr.evil.x/",
        "https://example.com/breezy.breezy.hr/",
    ):
        assert bd.board_from_url(url) is None


# ------------------------------------------------- the run: D-SRC-13 area and title drop
def _profile() -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_dir().joinpath("profile.yml").write_text(
        "employment:\n  employment_terms:\n    target_titles: [Program Analyst, Data Analyst]\n"
        "  geo_rules:\n    remote_country_allowlist: [US]\n", encoding="utf-8")


@respx.mock
async def test_roles_go_through_the_area_and_title_drop_before_they_are_stored(sm):
    _profile()
    rows = _rows(3)
    rows[0]["name"] = "Program Analyst"
    rows[1]["name"] = "Data Analyst"
    rows[1]["location"] = {"city": "Berlin", "country": {"name": "Germany", "id": "DE"},
                           "name": "Berlin"}
    rows[2]["name"] = "Forestry Technician"
    respx.get(_JSON_URL).mock(return_value=Response(200, json=rows))
    _mock_pages()
    async with sm() as s:
        src = Source(track="employment", kind="breezy", name="Breezy jobs (Breezy HR)",
                     config={"slug": "breezy"}, enabled=True, cadence_hours=24)
        s.add(src)
        await s.commit()
        await s.refresh(src)
    async with _http() as http:
        out = await run_source(sm, src.id, http=http)
    assert out.status == "ok", out.error
    assert out.counts["fetched"] == 3
    assert out.counts["dropped_outside_area"] == 1  # Berlin, Germany
    assert out.counts["dropped_unrelated_title"] == 1  # Forestry Technician
    async with sm() as s:
        titles = [o.title for o in (await s.execute(select(Opportunity))).scalars().all()]
    assert titles == ["Program Analyst"]
