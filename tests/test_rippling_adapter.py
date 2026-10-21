"""Rippling board adapter maps the real payload shape (SPEC.md Appendix A), respx-mocked.

Traps exercised: the title is `name` and carries stray spaces; a posting open in several places is
one list row per location with the same `uuid` (merged, not duplicated); the list has no
description or date, so each posting's detail is read; a list HTTP error, a 429 or a payload of the
wrong shape degrades the run to `partial` via AdapterError; one failing detail skips only that
posting. Tests never touch a live endpoint.
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
from kestrel.ingest.adapters.rippling import RipplingAdapter
from kestrel.ingest.base import AdapterError, SourceConfigError
from kestrel.ingest.http import HttpClient

_FIXTURES = Path(__file__).parent / "fixtures"
_LIST = json.loads((_FIXTURES / "rippling_list.json").read_text())
_DETAIL = json.loads((_FIXTURES / "rippling_detail.json").read_text())

_BASE = "https://api.rippling.com/platform/api/ats/v1/board/northwind/jobs"
_U1 = "3f6c1a52-7d0e-4b8a-9c21-5a0d2e7b1001"
_U2 = "3f6c1a52-7d0e-4b8a-9c21-5a0d2e7b1002"
_U3 = "3f6c1a52-7d0e-4b8a-9c21-5a0d2e7b1003"


def _detail(uuid: str, **over):
    return {**_DETAIL, "uuid": uuid, **over}


def _mock_all():
    respx.get(_BASE).mock(return_value=Response(200, json=_LIST))
    respx.get(f"{_BASE}/{_U1}").mock(return_value=Response(200, json=_detail(_U1)))
    respx.get(f"{_BASE}/{_U2}").mock(
        return_value=Response(
            200,
            json=_detail(
                _U2,
                name=" Support Engineer ",
                workLocations=["Raleigh, NC", "Remote (Dallas, Texas, US)"],
                employmentType={"label": "TEMP", "id": "Temporary / Intern"},
            ),
        )
    )
    respx.get(f"{_BASE}/{_U3}").mock(return_value=Response(200, json=_detail(_U3)))


async def _fetch(config: dict | None = None):
    s = get_settings().ingest
    cfg = {"slug": "northwind", **(config or {})}
    async with HttpClient(user_agent=s.user_agent, respect_robots=False, max_attempts=1) as client:
        return await RipplingAdapter().fetch(cfg, client)


@respx.mock
async def test_rippling_field_mapping():
    _mock_all()
    postings = await _fetch()

    # Four list rows, but the two rows for one posting merge: three postings.
    assert [p.external_id for p in postings] == [_U1, _U2, _U3]
    ae = postings[0]
    assert ae.title == "Account Executive, Mid-Market (Austin)"  # `name`, stray spaces trimmed
    assert ae.url == f"https://ats.rippling.com/northwind/jobs/{_U1}"
    assert ae.apply_url is None
    assert ae.org_name == "Northwind Labs"
    assert ae.location_raw == "TX"
    assert ae.employment_type == "Salaried, full-time"
    assert ae.date_posted == "2026-10-06T16:36:07.947000-07:00"
    # The role body is kept; the employer boilerplate is not.
    assert "Own a book of mid-market accounts" in ae.description_html
    assert "scheduling software" not in ae.description_html
    assert ae.description_md is None
    # Nothing in the payload says remote, so it stays unknown rather than false.
    assert ae.is_remote is None and ae.remote_hint is None
    assert ae.comp_min is None and ae.comp_max is None


@respx.mock
async def test_rippling_merges_a_posting_open_in_several_places():
    _mock_all()
    _, support, _ = await _fetch()
    assert support.location_raw == "Raleigh, NC; Remote (Dallas, Texas, US)"
    assert support.employment_type == "Temporary / Intern"
    assert support.is_remote is True
    assert support.remote_hint == "Remote"


@respx.mock
async def test_rippling_company_name_in_config_wins():
    _mock_all()
    postings = await _fetch({"company_name": "Northwind Inc"})
    assert {p.org_name for p in postings} == {"Northwind Inc"}


@respx.mock
async def test_rippling_max_results_caps_the_details_fetched():
    respx.get(_BASE).mock(return_value=Response(200, json=_LIST))
    one = respx.get(f"{_BASE}/{_U1}").mock(return_value=Response(200, json=_detail(_U1)))
    other = respx.get(f"{_BASE}/{_U2}").mock(return_value=Response(200, json=_detail(_U2)))
    postings = await _fetch({"max_results": 1})
    assert [p.external_id for p in postings] == [_U1]
    assert one.called and not other.called


@respx.mock
async def test_rippling_whole_board_is_one_response_no_paging():
    route = respx.get(_BASE).mock(return_value=Response(200, json=_LIST))
    respx.get(url__regex=rf"{_BASE}/.+").mock(return_value=Response(200, json=_DETAIL))
    await _fetch()
    assert route.call_count == 1  # no page parameter is ever sent
    assert "page" not in str(route.calls[0].request.url)


@respx.mock
async def test_rippling_empty_board_is_an_empty_run_not_an_error():
    respx.get(_BASE).mock(return_value=Response(200, json=[]))
    assert await _fetch() == []


@respx.mock
async def test_rippling_detail_error_skips_only_that_posting():
    respx.get(_BASE).mock(return_value=Response(200, json=_LIST))
    respx.get(f"{_BASE}/{_U1}").mock(return_value=Response(200, json=_detail(_U1)))
    respx.get(f"{_BASE}/{_U2}").mock(return_value=Response(404))
    respx.get(f"{_BASE}/{_U3}").mock(return_value=Response(200, json=_detail(_U3)))
    postings = await _fetch()
    assert [p.external_id for p in postings] == [_U1, _U3]


@respx.mock
async def test_rippling_rows_without_an_id_are_skipped():
    respx.get(_BASE).mock(
        return_value=Response(200, json=[{"name": "No id"}, "junk", None, _LIST[0]])
    )
    respx.get(f"{_BASE}/{_U1}").mock(return_value=Response(200, json=_detail(_U1)))
    postings = await _fetch()
    assert [p.external_id for p in postings] == [_U1]


@respx.mock
async def test_rippling_list_error_raises_adapter_error():
    respx.get(_BASE).mock(return_value=Response(404))
    with pytest.raises(AdapterError, match="Rippling list fetch failed"):
        await _fetch()


@respx.mock
async def test_rippling_429_is_a_partial_run_not_a_crash():
    respx.get(_BASE).mock(return_value=Response(429))
    with pytest.raises(AdapterError, match="rate limit"):
        await _fetch()


@respx.mock
async def test_rippling_429_on_a_detail_is_also_partial():
    respx.get(_BASE).mock(return_value=Response(200, json=_LIST))
    respx.get(f"{_BASE}/{_U1}").mock(return_value=Response(429))
    with pytest.raises(AdapterError, match="rate limit"):
        await _fetch()


@respx.mock
@pytest.mark.parametrize(
    "body",
    [{"jobs": []}, "<html>maintenance</html>", 12, "null"],
)
async def test_rippling_unexpected_shape_raises_adapter_error(body):
    respx.get(_BASE).mock(return_value=Response(200, content=json.dumps(body)))
    with pytest.raises(AdapterError, match="unexpected"):
        await _fetch()


@respx.mock
async def test_rippling_non_json_body_raises_adapter_error():
    respx.get(_BASE).mock(return_value=Response(200, text="<html>not json</html>"))
    with pytest.raises(AdapterError, match="not valid JSON"):
        await _fetch()


def test_rippling_validate_config_errors():
    adapter = RipplingAdapter()
    for bad in ({}, {"slug": ""}, {"slug": 7}, {"slug": "a b"}, {"slug": "../x"},
                {"slug": "northwind", "max_results": 0}, {"slug": "northwind", "max_results": "9"}):
        with pytest.raises(SourceConfigError):
            adapter.validate_config(bad)
    adapter.validate_config({"slug": "northwind"})
    adapter.validate_config({"slug": "north-wind_1", "max_results": 5, "company_name": "N"})


def test_rippling_is_registered():
    assert get_adapter("rippling") is not None


def test_board_discovery_recognizes_a_rippling_posting_link():
    ref = bd.board_from_url(f"https://ats.rippling.com/northwind/jobs/{_U1}")
    assert ref is not None and (ref.kind, ref.config) == ("rippling", {"slug": "northwind"})
    # A board root, a locale-free path and a query string name the same board.
    assert bd.board_from_url("https://ats.rippling.com/northwind/jobs?x=1").config == {
        "slug": "northwind"
    }
    assert bd.board_from_url("https://ats.rippling.com/") is None
    assert bd.board_from_url("https://ats.rippling.com.evil.example/northwind/jobs/1") is None
    # The API host is not a posting page.
    assert bd.board_from_url("https://api.rippling.com/platform/api/ats/v1/board/x/jobs") is None
