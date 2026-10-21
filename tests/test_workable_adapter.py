"""Workable board adapter maps the maintainer-captured payload (SPEC.md Appendix A), respx-mocked.

The fixtures were captured 2026-10-21 from a real public board and scrubbed. Traps exercised: the
list is a POST with body `{}`; the location text is built from city/region/country with empty
parts dropped; the list has no description, so each posting's detail is read and its description,
requirements and benefits are joined; internal and unpublished results are skipped; pagination is
unverified, so the `nextPage` case below is a CONSTRUCTED payload, not a captured one; a list HTTP
error, a 429 or a payload of the wrong shape degrades the run to `partial` via AdapterError. Tests
never touch a live endpoint.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import respx
from httpx import Response

from kestrel.config import get_settings
from kestrel.ingest import board_discovery as bd
from kestrel.ingest.adapters import get_adapter
from kestrel.ingest.adapters import workable as workable_module
from kestrel.ingest.adapters.workable import WorkableAdapter
from kestrel.ingest.base import AdapterError, SourceConfigError
from kestrel.ingest.http import HttpClient


@pytest.fixture(autouse=True)
def _no_rate_wait(monkeypatch):
    """The adapter paces itself at 1 request/second; tests must not sleep through that."""
    monkeypatch.setattr(workable_module, "_RATE", 0)


_FIXTURES = Path(__file__).parent / "fixtures"
_LIST = json.loads((_FIXTURES / "workable_list.json").read_text())
_DETAIL = json.loads((_FIXTURES / "workable_detail.json").read_text())

_LIST_URL = "https://apply.workable.com/api/v3/accounts/northwind/jobs"
_DETAIL_URL = "https://apply.workable.com/api/v2/accounts/northwind/jobs"
_CODES = [r["shortcode"] for r in _LIST["results"]]


def _detail(code: str, **over):
    row = next(r for r in _LIST["results"] if r["shortcode"] == code)
    return {**row, "description": "<p>Body</p>", "requirements": "", "benefits": "", **over}


def _mock_details(**per_code):
    for code in _CODES:
        respx.get(f"{_DETAIL_URL}/{code}").mock(
            return_value=Response(200, json=_detail(code, **per_code.get(code, {})))
        )


async def _fetch(config: dict | None = None):
    s = get_settings().ingest
    cfg = {"slug": "northwind", **(config or {})}
    async with HttpClient(user_agent=s.user_agent, respect_robots=False, max_attempts=1) as client:
        return await WorkableAdapter().fetch(cfg, client)


@respx.mock
async def test_workable_field_mapping():
    list_route = respx.post(_LIST_URL).mock(return_value=Response(200, json=_LIST))
    for code in _CODES:
        body = _DETAIL if code == "E5F6A7B8C9" else _detail(code)
        respx.get(f"{_DETAIL_URL}/{code}").mock(return_value=Response(200, json=body))
    postings = await _fetch()

    assert [p.external_id for p in postings] == _CODES
    assert json.loads(list_route.calls[0].request.content) == {}
    p = next(p for p in postings if p.external_id == "E5F6A7B8C9")
    assert p.title == "Open-Source Engineer - EMEA Remote"
    assert p.url == "https://apply.workable.com/northwind/j/E5F6A7B8C9/"
    assert p.apply_url is None
    assert p.org_name == "northwind"  # the payload names no employer; the slug is the fallback
    assert p.location_raw == "Paris, Île-de-France, France"
    assert p.is_remote is True and p.remote_hint == "Remote"
    assert p.date_posted == "2026-05-29T00:00:00.000Z"
    assert p.employment_type == "full"
    assert p.comp_min is None and p.comp_max is None
    # description then requirements, newline-joined; the empty benefits section is skipped.
    assert p.description_html is not None
    assert "You will improve our open-source libraries" in p.description_html
    assert p.description_html.endswith(_DETAIL["requirements"])
    assert p.description_html.index("About the Role") < p.description_html.index(
        "Please include a short note"
    )
    assert p.description_md is None


@respx.mock
async def test_workable_location_drops_empty_parts():
    respx.post(_LIST_URL).mock(return_value=Response(200, json=_LIST))
    _mock_details()
    postings = await _fetch()
    by_code = {p.external_id: p for p in postings}
    # City "" and region null: only the country is left.
    assert by_code["A1B2C3D4E5"].location_raw == "France"
    assert by_code["B2C3D4E5F6"].location_raw == "United States"
    assert by_code["D4E5F6A7B8"].location_raw == "New York, New York, United States"


@respx.mock
async def test_workable_benefits_are_joined_when_present():
    respx.post(_LIST_URL).mock(return_value=Response(200, json={"results": [_LIST["results"][0]]}))
    respx.get(f"{_DETAIL_URL}/A1B2C3D4E5").mock(
        return_value=Response(
            200,
            json=_detail(
                "A1B2C3D4E5",
                description="<p>D</p>",
                requirements="<p>R</p>",
                benefits="<p>B</p>",
            ),
        )
    )
    (posting,) = await _fetch()
    assert posting.description_html == "<p>D</p>\n<p>R</p>\n<p>B</p>"


@respx.mock
async def test_workable_company_name_in_config_wins():
    respx.post(_LIST_URL).mock(return_value=Response(200, json=_LIST))
    _mock_details()
    postings = await _fetch({"company_name": "Northwind Inc"})
    assert {p.org_name for p in postings} == {"Northwind Inc"}


@respx.mock
async def test_workable_skips_internal_and_unpublished_results():
    rows = [
        _LIST["results"][0],
        {**_LIST["results"][1], "isInternal": True},
        {**_LIST["results"][2], "state": "closed"},
        {**_LIST["results"][3], "state": None},
    ]
    respx.post(_LIST_URL).mock(return_value=Response(200, json={"results": rows, "total": 4}))
    one = respx.get(f"{_DETAIL_URL}/A1B2C3D4E5").mock(
        return_value=Response(200, json=_detail("A1B2C3D4E5"))
    )
    other = respx.get(url__regex=rf"{_DETAIL_URL}/(B2|C3|D4).*").mock(
        return_value=Response(200, json=_DETAIL)
    )
    postings = await _fetch()
    assert [p.external_id for p in postings] == ["A1B2C3D4E5"]
    assert one.called and not other.called


@respx.mock
async def test_workable_max_results_caps_the_details_fetched():
    respx.post(_LIST_URL).mock(return_value=Response(200, json=_LIST))
    one = respx.get(f"{_DETAIL_URL}/A1B2C3D4E5").mock(
        return_value=Response(200, json=_detail("A1B2C3D4E5"))
    )
    other = respx.get(f"{_DETAIL_URL}/B2C3D4E5F6").mock(
        return_value=Response(200, json=_detail("B2C3D4E5F6"))
    )
    postings = await _fetch({"max_results": 1})
    assert [p.external_id for p in postings] == ["A1B2C3D4E5"]
    assert one.called and not other.called


@respx.mock
async def test_workable_empty_board_is_an_empty_run_not_an_error():
    respx.post(_LIST_URL).mock(return_value=Response(200, json={"results": [], "total": 0}))
    assert await _fetch() == []


@respx.mock
async def test_workable_single_page_board_sends_one_list_request():
    route = respx.post(_LIST_URL).mock(return_value=Response(200, json=_LIST))
    _mock_details()
    await _fetch()
    assert route.call_count == 1  # the captured board had total == len(results), no token


@respx.mock
async def test_workable_follows_a_next_page_token():
    """CONSTRUCTED pagination case: the captured board never paged, so this payload is inline."""
    first, second = _LIST["results"][:1], _LIST["results"][1:2]
    route = respx.post(_LIST_URL).mock(
        side_effect=[
            Response(200, json={"results": first, "total": 2, "nextPage": "tok-2"}),
            Response(200, json={"results": second, "total": 2}),
        ]
    )
    _mock_details()
    postings = await _fetch()
    assert [p.external_id for p in postings] == ["A1B2C3D4E5", "B2C3D4E5F6"]
    assert route.call_count == 2
    assert json.loads(route.calls[0].request.content) == {}
    assert json.loads(route.calls[1].request.content) == {"token": "tok-2"}


@respx.mock
async def test_workable_page_loop_is_capped():
    """CONSTRUCTED: a server that always returns a token cannot loop the adapter forever."""
    route = respx.post(_LIST_URL).mock(
        return_value=Response(
            200, json={"results": _LIST["results"][:1], "total": 999, "nextPage": "again"}
        )
    )
    _mock_details()
    await _fetch()
    assert route.call_count == workable_module._MAX_PAGES


@respx.mock
async def test_workable_total_larger_than_collected_without_token_warns_not_raises():
    respx.post(_LIST_URL).mock(
        return_value=Response(200, json={"results": _LIST["results"][:2], "total": 50})
    )
    _mock_details()
    postings = await _fetch()  # returns what it has
    assert len(postings) == 2


@respx.mock
async def test_workable_detail_error_skips_only_that_posting():
    respx.post(_LIST_URL).mock(return_value=Response(200, json=_LIST))
    _mock_details()
    respx.get(f"{_DETAIL_URL}/B2C3D4E5F6").mock(return_value=Response(404))
    postings = await _fetch()
    assert "B2C3D4E5F6" not in [p.external_id for p in postings]
    assert len(postings) == len(_CODES) - 1


@respx.mock
async def test_workable_rows_without_a_shortcode_are_skipped():
    rows = ["junk", None, {"title": "No code", "state": "published"}, _LIST["results"][0]]
    respx.post(_LIST_URL).mock(return_value=Response(200, json={"results": rows, "total": 4}))
    _mock_details()
    postings = await _fetch()
    assert [p.external_id for p in postings] == ["A1B2C3D4E5"]


@respx.mock
async def test_workable_list_error_raises_adapter_error():
    respx.post(_LIST_URL).mock(return_value=Response(404))
    with pytest.raises(AdapterError, match="Workable list fetch failed"):
        await _fetch()


@respx.mock
async def test_workable_429_is_a_partial_run_not_a_crash():
    respx.post(_LIST_URL).mock(return_value=Response(429))
    with pytest.raises(AdapterError, match="rate limit"):
        await _fetch()


@respx.mock
async def test_workable_429_on_a_detail_is_also_partial():
    respx.post(_LIST_URL).mock(return_value=Response(200, json=_LIST))
    respx.get(f"{_DETAIL_URL}/A1B2C3D4E5").mock(return_value=Response(429))
    with pytest.raises(AdapterError, match="rate limit"):
        await _fetch()


@respx.mock
@pytest.mark.parametrize(
    "body",
    [[], {"jobs": []}, {"results": "x"}, {"results": {"a": 1}}, 12, "null"],
)
async def test_workable_unexpected_shape_raises_adapter_error(body):
    respx.post(_LIST_URL).mock(return_value=Response(200, content=json.dumps(body)))
    with pytest.raises(AdapterError, match="unexpected"):
        await _fetch()


@respx.mock
async def test_workable_non_json_body_raises_adapter_error():
    respx.post(_LIST_URL).mock(return_value=Response(200, text="<html>not json</html>"))
    with pytest.raises(AdapterError, match="not valid JSON"):
        await _fetch()


def test_workable_validate_config_errors():
    adapter = WorkableAdapter()
    for bad in ({}, {"slug": ""}, {"slug": 7}, {"slug": "a b"}, {"slug": "../x"},
                {"slug": "northwind", "max_results": 0}, {"slug": "northwind", "max_results": "9"}):
        with pytest.raises(SourceConfigError):
            adapter.validate_config(bad)
    adapter.validate_config({"slug": "northwind"})
    adapter.validate_config({"slug": "north-wind_1", "max_results": 5, "company_name": "N"})


def test_workable_is_registered():
    assert get_adapter("workable") is not None


def test_board_discovery_recognizes_workable_board_and_posting_links():
    for url in (
        "https://apply.workable.com/northwind/",
        "https://apply.workable.com/northwind",
        "https://apply.workable.com/northwind/j/A1B2C3D4E5/",
        "https://apply.workable.com/northwind/j/A1B2C3D4E5/apply/?x=1",
    ):
        ref = bd.board_from_url(url)
        assert ref is not None, url
        assert (ref.kind, ref.config) == ("workable", {"slug": "northwind"}), url
    assert bd.board_from_url("https://apply.workable.com/") is None
    assert bd.board_from_url("https://apply.workable.com.evil.example/northwind/") is None
    # The API path on the same host is not a board.
    assert bd.board_from_url("https://apply.workable.com/api/v3/accounts/x/jobs") is None
