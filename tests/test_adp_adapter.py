"""ADP Workforce Now career-center adapter maps the real payload shape (SPEC.md Appendix A).

Traps exercised: pages cap at 20 and `$skip` is 1-based, the title is `requisitionTitle` (with a
trailing space on some rows), `requisitionLocations` repeats and carries blank entries, the list
has no description (the detail does), a list HTTP error, a 429 or a payload of the wrong shape
degrades the run to `partial` via AdapterError, and one failing detail skips only that posting.
Tests never touch a live endpoint.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import respx
from httpx import Request, Response

from kestrel.config import get_settings
from kestrel.ingest import board_discovery as bd
from kestrel.ingest.adapters import adp as adp_module
from kestrel.ingest.adapters import get_adapter
from kestrel.ingest.adapters.adp import AdpAdapter
from kestrel.ingest.base import AdapterError, SourceConfigError
from kestrel.ingest.http import HttpClient


@pytest.fixture(autouse=True)
def _no_rate_wait(monkeypatch):
    """The adapter paces itself at 1 request/second; tests must not sleep through that."""
    monkeypatch.setattr(adp_module, "_RATE", 0)


_FIXTURES = Path(__file__).parent / "fixtures"
_LIST = json.loads((_FIXTURES / "adp_list.json").read_text(encoding="utf-8"))
_DETAIL = json.loads((_FIXTURES / "adp_detail.json").read_text(encoding="utf-8"))

_CID = "5d2f7a90-3c41-4e8b-9a16-0b7e4c2d8f11"
_BASE = "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1"
_LIST_URL = f"{_BASE}/job-requisitions"
_ITEM1, _ITEM2, _ITEM3 = (j["itemID"] for j in _LIST["jobRequisitions"])


def _page_server(rows: list[dict], total: int | None = None, seen: list | None = None):
    """A fake career center: 20 rows a page, `$skip` 1-based, like the live one."""

    def handler(request: Request) -> Response:
        params = request.url.params
        skip = int(params["$skip"])
        if seen is not None:
            seen.append((skip, params["$top"], params["cid"]))
        page = rows[skip - 1 : skip - 1 + 20]
        meta = {"startSequence": skip, "totalNumber": len(rows) if total is None else total}
        return Response(200, json={"jobRequisitions": page, "meta": meta, "links": []})

    return handler


def _mock_fixture():
    respx.get(_LIST_URL).mock(side_effect=_page_server(_LIST["jobRequisitions"]))
    respx.get(f"{_LIST_URL}/{_ITEM2}").mock(return_value=Response(200, json=_DETAIL))
    # The first posting has no `workLevelCode` in the list, and none in its detail either.
    bare = {k: v for k, v in _DETAIL.items() if k != "workLevelCode"}
    respx.get(f"{_LIST_URL}/{_ITEM1}").mock(return_value=Response(200, json=bare))
    respx.get(f"{_LIST_URL}/{_ITEM3}").mock(return_value=Response(200, json=_DETAIL))


async def _fetch(config: dict | None = None):
    s = get_settings().ingest
    cfg = {"cid": _CID, **(config or {})}
    async with HttpClient(user_agent=s.user_agent, respect_robots=False, max_attempts=1) as client:
        return await AdpAdapter().fetch(cfg, client)


@respx.mock
async def test_adp_field_mapping():
    _mock_fixture()
    postings = await _fetch()

    assert [p.external_id for p in postings] == [_ITEM1, _ITEM2, _ITEM3]
    clinic, billing, access = postings
    assert clinic.title == "Clinic Operations Specialist"
    assert billing.title == "Billing Data Analyst"  # trailing space trimmed
    assert billing.date_posted == "2026-10-16T15:06:00.000-04:00"
    assert billing.employment_type == "Full Time"
    assert clinic.employment_type is None  # `workLevelCode` absent: unknown, not guessed
    assert access.employment_type == "Part Time"
    assert billing.org_name == _CID  # the payload names no employer; the config may
    # Blank location entries are dropped and the repeated Peoria entry is merged.
    assert billing.location_raw == "Springfield, IL, US; Peoria, IL, US"
    assert clinic.location_raw == "Springfield, IL, US"
    assert billing.url == (
        "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
        f"?cid={_CID}&ccId=19000101_000001&jobId=500102&lang=en_US"
    )
    # The description exists only because the detail was read.
    assert "Reconcile weekly billing extracts" in billing.description_html
    assert billing.description_md is None
    assert billing.is_remote is None and billing.comp_min is None


@respx.mock
async def test_adp_company_name_in_config_wins():
    _mock_fixture()
    postings = await _fetch({"company_name": "Northwind Health"})
    assert {p.org_name for p in postings} == {"Northwind Health"}


@respx.mock
async def test_adp_remote_location_is_flagged():
    rows = copy.deepcopy(_LIST["jobRequisitions"][:1])
    rows[0]["requisitionLocations"][0]["nameCode"]["shortName"] = " Remote - IL, IL, US"
    respx.get(_LIST_URL).mock(side_effect=_page_server(rows))
    respx.get(f"{_LIST_URL}/{_ITEM1}").mock(return_value=Response(200, json=_DETAIL))
    (p,) = await _fetch()
    assert p.is_remote is True and p.remote_hint == "Remote"


@respx.mock
async def test_adp_pages_by_twenty_with_a_one_based_skip():
    rows = []
    for n in range(45):
        row = copy.deepcopy(_LIST["jobRequisitions"][0])
        row["itemID"] = f"9100000009{n:03d}_1"
        rows.append(row)
    seen: list = []
    respx.get(_LIST_URL).mock(side_effect=_page_server(rows, seen=seen))
    respx.get(url__regex=rf"{_LIST_URL}/.+").mock(return_value=Response(200, json=_DETAIL))
    postings = await _fetch({"max_results": 500})
    assert [(s, t) for s, t, _ in seen] == [(1, "20"), (21, "20"), (41, "20")]
    assert {c for _, _, c in seen} == {_CID}
    assert len(postings) == 45


@respx.mock
async def test_adp_max_results_stops_paging_and_detail_reads():
    rows = [
        {**copy.deepcopy(_LIST["jobRequisitions"][0]), "itemID": f"9100000009{n:03d}_1"}
        for n in range(45)
    ]
    seen: list = []
    respx.get(_LIST_URL).mock(side_effect=_page_server(rows, seen=seen))
    details = respx.get(url__regex=rf"{_LIST_URL}/.+").mock(
        return_value=Response(200, json=_DETAIL)
    )
    postings = await _fetch({"max_results": 5})
    assert len(postings) == 5 and details.call_count == 5
    assert [s for s, _, _ in seen] == [1]


@respx.mock
async def test_adp_empty_career_center_is_an_empty_run():
    respx.get(_LIST_URL).mock(side_effect=_page_server([]))
    assert await _fetch() == []


@respx.mock
async def test_adp_detail_error_skips_only_that_posting():
    respx.get(_LIST_URL).mock(side_effect=_page_server(_LIST["jobRequisitions"]))
    respx.get(f"{_LIST_URL}/{_ITEM1}").mock(return_value=Response(200, json=_DETAIL))
    respx.get(f"{_LIST_URL}/{_ITEM2}").mock(return_value=Response(500))
    respx.get(f"{_LIST_URL}/{_ITEM3}").mock(return_value=Response(200, json=_DETAIL))
    postings = await _fetch()
    assert [p.external_id for p in postings] == [_ITEM1, _ITEM3]


@respx.mock
async def test_adp_rows_without_an_item_id_are_skipped():
    rows = [{"requisitionTitle": "No id"}, "junk", copy.deepcopy(_LIST["jobRequisitions"][0])]
    respx.get(_LIST_URL).mock(side_effect=_page_server(rows))
    respx.get(f"{_LIST_URL}/{_ITEM1}").mock(return_value=Response(200, json=_DETAIL))
    assert [p.external_id for p in await _fetch()] == [_ITEM1]


@respx.mock
async def test_adp_list_error_raises_adapter_error():
    respx.get(_LIST_URL).mock(return_value=Response(500))
    with pytest.raises(AdapterError, match="ADP list fetch failed"):
        await _fetch()


@respx.mock
async def test_adp_429_is_a_partial_run_not_a_crash():
    respx.get(_LIST_URL).mock(return_value=Response(429))
    with pytest.raises(AdapterError, match="rate limit"):
        await _fetch()


@respx.mock
async def test_adp_429_on_a_detail_is_also_partial():
    respx.get(_LIST_URL).mock(side_effect=_page_server(_LIST["jobRequisitions"]))
    respx.get(f"{_LIST_URL}/{_ITEM1}").mock(return_value=Response(429))
    with pytest.raises(AdapterError, match="rate limit"):
        await _fetch()


@respx.mock
@pytest.mark.parametrize("body", [[], {"rows": []}, "text", {"jobRequisitions": "none"}])
async def test_adp_unexpected_shape_raises_adapter_error(body):
    respx.get(_LIST_URL).mock(return_value=Response(200, content=json.dumps(body)))
    with pytest.raises(AdapterError, match="unexpected"):
        await _fetch()


@respx.mock
async def test_adp_non_json_body_raises_adapter_error():
    respx.get(_LIST_URL).mock(return_value=Response(200, text="<html>sign in</html>"))
    with pytest.raises(AdapterError, match="not valid JSON"):
        await _fetch()


def test_adp_validate_config_errors():
    adapter = AdpAdapter()
    for bad in (
        {},
        {"cid": ""},
        {"cid": 7},
        {"cid": "not-a-uuid"},
        {"cid": _CID, "cc_id": "bad id"},
        {"cid": _CID, "lang": "english"},
        {"cid": _CID, "max_results": 0},
        {"cid": _CID, "max_results": "5"},
    ):
        with pytest.raises(SourceConfigError):
            adapter.validate_config(bad)
    adapter.validate_config({"cid": _CID})
    adapter.validate_config(
        {"cid": _CID.upper(), "cc_id": "19000101_000001", "lang": "en_CA", "max_results": 3}
    )


def test_adp_is_registered():
    assert get_adapter("adp") is not None


def test_board_discovery_recognizes_an_adp_career_center_link():
    url = (
        "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
        f"?cid={_CID}&ccId=19000101_000001&jobId=500102&lang=en_US"
    )
    ref = bd.board_from_url(url)
    assert ref is not None and ref.kind == "adp"
    assert ref.config == {"cid": _CID, "cc_id": "19000101_000001", "lang": "en_US"}
    # The same employer in another case is the same board.
    upper = bd.board_from_url(url.replace(_CID, _CID.upper()))
    assert upper is not None and upper.key == ref.key
    # No cid, a malformed cid, another path or a look-alike host name no board.
    base = "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
    assert bd.board_from_url(base) is None
    assert bd.board_from_url(f"{base}?cid=abc") is None
    assert bd.board_from_url(f"https://workforcenow.adp.com/login?cid={_CID}") is None
    lookalike = url.replace("workforcenow.adp.com", "workforcenow.adp.com.evil.x")
    assert bd.board_from_url(lookalike) is None
