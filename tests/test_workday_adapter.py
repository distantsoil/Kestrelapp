"""Workday adapter: POST list + per-job GET detail, respx-mocked, never hitting a live endpoint.

Covers the Appendix A gotchas: the list is a POST, the description comes from a separate detail
GET, date_posted uses the detail `startDate` (not the human `postedOn`), and a single failed
detail skips only that job rather than aborting the board.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import respx
from httpx import Response

from kestrel.config import get_settings
from kestrel.ingest.adapters.workday import WorkdayAdapter
from kestrel.ingest.base import SourceConfigError
from kestrel.ingest.http import HttpClient

_FIX = Path(__file__).parent / "fixtures"
_LIST = json.loads((_FIX / "workday_list.json").read_text())
_DETAIL = json.loads((_FIX / "workday_detail.json").read_text())

_CONFIG = {"tenant": "nvidia", "dc": "wd5", "site": "NVIDIAExternalCareerSite", "max_results": 40}
_BASE = "https://nvidia.wd5.myworkdayjobs.com"
_CXS = f"{_BASE}/wday/cxs/nvidia/NVIDIAExternalCareerSite"
_LIST_URL = f"{_CXS}/jobs"


def _detail_url(external_path: str) -> str:
    # Mirror the adapter: externalPath already starts with "/job/", so no extra "/job" is prepended.
    path = external_path if external_path.startswith("/job") else f"/job{external_path}"
    return f"{_CXS}{path}"


def _detail_for(item: dict) -> dict:
    """A copy of the detail fixture whose requisition id matches the list bullet for that job."""
    detail = json.loads(json.dumps(_DETAIL))
    detail["jobPostingInfo"]["jobRequisitionId"] = item["bulletFields"][0]
    return detail


async def _run() -> list:
    s = get_settings().ingest
    async with HttpClient(user_agent=s.user_agent, respect_robots=False) as client:
        return await WorkdayAdapter().fetch(_CONFIG, client)


@respx.mock
async def test_workday_posts_list_then_fetches_each_detail():
    list_route = respx.post(_LIST_URL).mock(return_value=Response(200, json=_LIST))
    for item in _LIST["jobPostings"]:
        respx.get(_detail_url(item["externalPath"])).mock(
            return_value=Response(200, json=_detail_for(item))
        )

    postings = await _run()

    # The list endpoint was reached via POST, and both details were fetched.
    assert list_route.called
    assert list_route.calls.last.request.method == "POST"
    assert len(postings) == 2

    p0 = postings[0]
    # Detail was fetched: the HTML description is present (the list has none).
    assert p0.description_html is not None
    assert "<p>" in p0.description_html
    # date_posted uses the detail startDate, NOT the human "Posted Today" list string.
    assert p0.date_posted == "2026-07-15"
    assert "Posted" not in (p0.date_posted or "")
    # external_id came from the requisition id in the detail payload.
    assert p0.external_id == "JR123"
    assert postings[1].external_id == "JR456"
    # List-sourced and detail-sourced fields both map through.
    assert p0.title == _LIST["jobPostings"][0]["title"]
    assert p0.location_raw == "Santa Clara, CA"
    assert p0.employment_type == "Full time"
    assert p0.org_name == "NVIDIA"
    # Human-facing URL, not the cxs API URL.
    expected_path = _LIST["jobPostings"][0]["externalPath"]
    assert p0.url == f"{_BASE}/NVIDIAExternalCareerSite{expected_path}"


@respx.mock
async def test_workday_single_detail_failure_skips_only_that_job():
    respx.post(_LIST_URL).mock(return_value=Response(200, json=_LIST))
    items = _LIST["jobPostings"]
    # First detail 500s (skipped); second succeeds. The board must still return the good one.
    respx.get(_detail_url(items[0]["externalPath"])).mock(return_value=Response(500))
    respx.get(_detail_url(items[1]["externalPath"])).mock(
        return_value=Response(200, json=_detail_for(items[1]))
    )

    postings = await _run()

    assert len(postings) == 1
    assert postings[0].external_id == "JR456"


def test_workday_validate_config_requires_tenant_dc_site():
    adapter = WorkdayAdapter()
    with pytest.raises(SourceConfigError):
        adapter.validate_config({})
    with pytest.raises(SourceConfigError):
        adapter.validate_config({"tenant": "nvidia", "dc": "wd5"})  # missing site
    with pytest.raises(SourceConfigError):
        adapter.validate_config({"tenant": "nvidia", "site": "X"})  # missing dc
    # A complete config is accepted.
    adapter.validate_config(_CONFIG)


# ---- D-SEC-10: the host labels and the site are checked whole, at config load and at fetch time
_BAD_TENANTS = ["x@10.0.0.1/", "x@127.0.0.1:443/#", "a.b", "a/b", "a b", "", "-a", "a\n",
                "x" * 64, "evil.example.com#"]


@pytest.mark.parametrize("tenant", _BAD_TENANTS)
def test_workday_refuses_a_tenant_that_could_point_at_another_host(tenant):
    with pytest.raises(SourceConfigError):
        WorkdayAdapter().validate_config({**_CONFIG, "tenant": tenant})


@pytest.mark.parametrize("dc", ["wd5.evil.example", "wd5@10.0.0.1", "wd5/", "wd5:443", "wd 5"])
def test_workday_refuses_a_data_center_that_could_point_at_another_host(dc):
    with pytest.raises(SourceConfigError):
        WorkdayAdapter().validate_config({**_CONFIG, "dc": dc})


@pytest.mark.parametrize("site", ["a/../b", "a?x=1", "a#b", "a@b", "../x", "a b", "a\n"])
def test_workday_refuses_a_site_that_is_not_one_path_segment(site):
    with pytest.raises(SourceConfigError):
        WorkdayAdapter().validate_config({**_CONFIG, "site": site})


@respx.mock
async def test_workday_fetch_refuses_before_any_request_is_made():
    route = respx.route().mock(return_value=Response(200, json={}))
    for tenant in ("x@10.0.0.1/", "x@127.0.0.1:443/#"):
        with pytest.raises(SourceConfigError):
            await WorkdayAdapter().fetch({**_CONFIG, "tenant": tenant}, client=None)
    assert route.call_count == 0
