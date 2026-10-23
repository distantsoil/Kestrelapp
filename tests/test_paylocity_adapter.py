"""Paylocity Recruiting adapter maps the real page data correctly (SPEC.md Appendix A).

Traps exercised: the list is JSON assigned to `window.pageData` inside the careers page, the list's
`Description` is only a snippet so the body comes from each posting page's ld+json, the posting
page's `datePosted` has a wrong UTC offset so the list's `PublishedDate` wins, an unknown board
answers 200 with no page data, a list HTTP error, a 429 or an unreadable page degrades the run to
`partial` via AdapterError, and one failing detail skips only that posting. Tests never touch a
live endpoint.
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
from kestrel.ingest.adapters import paylocity as paylocity_module
from kestrel.ingest.adapters.paylocity import PaylocityAdapter
from kestrel.ingest.base import AdapterError, SourceConfigError
from kestrel.ingest.http import HttpClient


@pytest.fixture(autouse=True)
def _no_rate_wait(monkeypatch):
    """The adapter paces itself at 1 request/second; tests must not sleep through that."""
    monkeypatch.setattr(paylocity_module, "_RATE", 0)


_FIXTURES = Path(__file__).parent / "fixtures"
_LIST = (_FIXTURES / "paylocity_list.html").read_text(encoding="utf-8")
_DETAIL = (_FIXTURES / "paylocity_detail.html").read_text(encoding="utf-8")

_BOARD = "8a1f3c52-6d47-4e0b-9b21-7c3e5d9a2f10"
_LIST_URL = f"https://recruiting.paylocity.com/recruiting/jobs/All/{_BOARD}"
_DETAILS = "https://recruiting.paylocity.com/Recruiting/Jobs/Details"


def _page(**changes) -> str:
    """The fixture page with its page data changed (a copy; the file is never touched)."""
    marker = "window.pageData = "
    head, rest = _LIST.split(marker)
    data, end = json.JSONDecoder().raw_decode(rest)
    data.update(changes)
    return head + marker + json.dumps(data) + rest[end:]


def _jobs() -> list[dict]:
    rest = _LIST.split("window.pageData = ")[1]
    return json.JSONDecoder().raw_decode(rest)[0]["Jobs"]


def _mock_fixture():
    respx.get(_LIST_URL).mock(return_value=Response(200, text=_LIST))
    respx.get(f"{_DETAILS}/9100001").mock(return_value=Response(200, text=_DETAIL))
    respx.get(f"{_DETAILS}/9100002").mock(return_value=Response(200, text=_DETAIL))


async def _fetch(config: dict | None = None):
    s = get_settings().ingest
    cfg = {"board_id": _BOARD, **(config or {})}
    async with HttpClient(user_agent=s.user_agent, respect_robots=False, max_attempts=1) as client:
        return await PaylocityAdapter().fetch(cfg, client)


@respx.mock
async def test_paylocity_field_mapping():
    _mock_fixture()
    postings = await _fetch()

    assert [p.external_id for p in postings] == ["9100001", "9100002"]
    desk, billing = postings
    assert desk.title == "Clinic Front Desk Associate"
    assert desk.url == f"{_DETAILS}/9100001"
    assert desk.org_name == "Northwind Health"  # ModuleTitle
    assert desk.location_raw == "Springfield, IL"
    # The list's own PublishedDate wins; the posting page's datePosted has a shifted offset.
    assert desk.date_posted == "2026-10-05T14:44:15-05:00"
    # The body is the posting page's HTML, not the list's ~110 character snippet.
    assert "Verify insurance details" in desk.description_html
    assert desk.description_md is None
    assert desk.is_remote is False  # IsRemote is an explicit boolean in the payload
    assert desk.comp_min is None and desk.employment_type is None
    assert billing.location_raw == "Peoria, IL"


@respx.mock
async def test_paylocity_company_name_in_config_wins():
    _mock_fixture()
    postings = await _fetch({"company_name": "Northwind Inc"})
    assert {p.org_name for p in postings} == {"Northwind Inc"}


@respx.mock
async def test_paylocity_remote_hidden_location_and_internal_postings():
    jobs = _jobs()
    jobs[0]["IsRemote"] = True
    jobs[1]["ShouldDisplayLocation"] = False  # the employer hides the place: unknown, not guessed
    internal = {**jobs[0], "JobId": 9100003, "IsInternal": True}
    respx.get(_LIST_URL).mock(return_value=Response(200, text=_page(Jobs=[*jobs, internal])))
    respx.get(f"{_DETAILS}/9100001").mock(return_value=Response(200, text=_DETAIL))
    respx.get(f"{_DETAILS}/9100002").mock(return_value=Response(200, text=_DETAIL))
    postings = await _fetch()
    assert [p.external_id for p in postings] == ["9100001", "9100002"]  # internal-only is skipped
    assert postings[0].is_remote is True and postings[0].remote_hint == "Remote"
    assert postings[1].location_raw is None


@respx.mock
async def test_paylocity_max_results_caps_the_details_fetched():
    respx.get(_LIST_URL).mock(return_value=Response(200, text=_LIST))
    one = respx.get(f"{_DETAILS}/9100001").mock(return_value=Response(200, text=_DETAIL))
    two = respx.get(f"{_DETAILS}/9100002").mock(return_value=Response(200, text=_DETAIL))
    postings = await _fetch({"max_results": 1})
    assert [p.external_id for p in postings] == ["9100001"]
    assert one.called and not two.called


@respx.mock
async def test_paylocity_whole_board_is_one_request_no_paging():
    route = respx.get(_LIST_URL).mock(return_value=Response(200, text=_LIST))
    respx.get(url__regex=rf"{_DETAILS}/.+").mock(return_value=Response(200, text=_DETAIL))
    await _fetch()
    assert route.call_count == 1 and not route.calls[0].request.url.query


@respx.mock
async def test_paylocity_empty_board_is_an_empty_run():
    respx.get(_LIST_URL).mock(return_value=Response(200, text=_page(Jobs=[])))
    assert await _fetch() == []


@respx.mock
async def test_paylocity_unknown_board_has_no_page_data():
    # An unknown id lands on a "Job Not Found" page that answers 200 with no page data.
    respx.get(_LIST_URL).mock(
        return_value=Response(200, text="<html><title>Job Not Found</title></html>")
    )
    with pytest.raises(AdapterError, match="no job list"):
        await _fetch()


@respx.mock
async def test_paylocity_unreadable_page_data_raises_adapter_error():
    respx.get(_LIST_URL).mock(
        return_value=Response(200, text="<script>window.pageData = {not json</script>")
    )
    with pytest.raises(AdapterError, match="unreadable"):
        await _fetch()
    respx.get(_LIST_URL).mock(
        return_value=Response(200, text='<script>window.pageData = {"Jobs": "none"};</script>')
    )
    with pytest.raises(AdapterError, match="unexpected"):
        await _fetch()


@respx.mock
async def test_paylocity_detail_error_skips_only_that_posting():
    respx.get(_LIST_URL).mock(return_value=Response(200, text=_LIST))
    respx.get(f"{_DETAILS}/9100001").mock(return_value=Response(500))
    respx.get(f"{_DETAILS}/9100002").mock(return_value=Response(200, text=_DETAIL))
    postings = await _fetch()
    assert [p.external_id for p in postings] == ["9100002"]


@respx.mock
async def test_paylocity_detail_without_ld_json_keeps_the_posting_without_a_body():
    respx.get(_LIST_URL).mock(return_value=Response(200, text=_LIST))
    respx.get(url__regex=rf"{_DETAILS}/.+").mock(
        return_value=Response(200, text="<html><body>no schema here</body></html>")
    )
    postings = await _fetch()
    assert len(postings) == 2 and all(p.description_html is None for p in postings)


@respx.mock
async def test_paylocity_rows_without_a_job_id_are_skipped():
    rows = [{"JobTitle": "No id"}, "junk", _jobs()[0]]
    respx.get(_LIST_URL).mock(return_value=Response(200, text=_page(Jobs=rows)))
    respx.get(f"{_DETAILS}/9100001").mock(return_value=Response(200, text=_DETAIL))
    assert [p.external_id for p in await _fetch()] == ["9100001"]


@respx.mock
async def test_paylocity_list_error_raises_adapter_error():
    respx.get(_LIST_URL).mock(return_value=Response(500))
    with pytest.raises(AdapterError, match="Paylocity list fetch failed"):
        await _fetch()


@respx.mock
async def test_paylocity_429_is_a_partial_run_not_a_crash():
    respx.get(_LIST_URL).mock(return_value=Response(429))
    with pytest.raises(AdapterError, match="rate limit"):
        await _fetch()


@respx.mock
async def test_paylocity_429_on_a_detail_is_also_partial():
    respx.get(_LIST_URL).mock(return_value=Response(200, text=_LIST))
    respx.get(f"{_DETAILS}/9100001").mock(return_value=Response(429))
    with pytest.raises(AdapterError, match="rate limit"):
        await _fetch()


def test_paylocity_validate_config_errors():
    adapter = PaylocityAdapter()
    for bad in (
        {},
        {"board_id": ""},
        {"board_id": 7},
        {"board_id": "not-a-guid"},
        {"board_id": _BOARD, "max_results": 0},
        {"board_id": _BOARD, "max_results": "5"},
    ):
        with pytest.raises(SourceConfigError):
            adapter.validate_config(bad)
    adapter.validate_config({"board_id": _BOARD})
    adapter.validate_config({"board_id": _BOARD.upper(), "company_name": "N", "max_results": 3})


def test_paylocity_is_registered():
    assert get_adapter("paylocity") is not None


def test_board_discovery_recognizes_a_paylocity_board_link():
    for url in (
        f"https://recruiting.paylocity.com/recruiting/jobs/All/{_BOARD}",
        f"https://recruiting.paylocity.com/Recruiting/Jobs/All/{_BOARD.upper()}/Northwind-Health",
        f"https://recruiting.paylocity.com/recruiting/jobs/All/{_BOARD}?source=x",
    ):
        ref = bd.board_from_url(url)
        assert ref is not None and (ref.kind, ref.config) == ("paylocity", {"board_id": _BOARD})
    # A posting link names no board, so it is not one; neither is a look-alike host.
    assert bd.board_from_url(f"{_DETAILS}/9100001") is None
    assert bd.board_from_url("https://recruiting.paylocity.com/recruiting/jobs/All/abc") is None
    lookalike = f"https://recruiting.paylocity.com.evil.x/recruiting/jobs/All/{_BOARD}"
    assert bd.board_from_url(lookalike) is None
