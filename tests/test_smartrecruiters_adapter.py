"""SmartRecruiters adapter maps the real payload shape correctly (Appendix A gotchas), respx-mocked.

Key SmartRecruiters traps exercised: the title lives in `name` (not `title`), the LIST carries no
description so each body is assembled from the DETAIL endpoint's `jobAd.sections`, `location` is an
object joined into "City, Region, Country", the company identifier is case sensitive, a LIST HTTP
error degrades the run to `partial` via AdapterError, and a single failing DETAIL skips only that
posting. Tests never touch a live endpoint.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import respx
from httpx import Response

from kestrel.config import get_settings
from kestrel.ingest.adapters.smartrecruiters import SmartRecruitersAdapter
from kestrel.ingest.base import AdapterError, SourceConfigError
from kestrel.ingest.http import HttpClient

_FIXTURES = Path(__file__).parent / "fixtures"
_LIST = json.loads((_FIXTURES / "smartrecruiters_list.json").read_text())
_DETAIL = json.loads((_FIXTURES / "smartrecruiters_detail.json").read_text())

_COMPANY = "AcmeCorp"  # mixed case on purpose: the identifier is CASE SENSITIVE
_BASE = f"https://api.smartrecruiters.com/v1/companies/{_COMPANY}/postings"


async def _fetch(company: str = _COMPANY, config: dict | None = None):
    s = get_settings().ingest
    cfg = {"company": company, **(config or {})}
    async with HttpClient(user_agent=s.user_agent, respect_robots=False) as client:
        return await SmartRecruitersAdapter().fetch(cfg, client)


@respx.mock
async def test_smartrecruiters_adapter_field_mapping():
    respx.get(_BASE).mock(return_value=Response(200, json=_LIST))
    respx.get(f"{_BASE}/743999900000001").mock(return_value=Response(200, json=_DETAIL))
    respx.get(f"{_BASE}/743999900000002").mock(return_value=Response(200, json=_DETAIL))

    postings = await _fetch()

    assert len(postings) == 2
    vp = postings[0]

    # title comes from `name`, NOT `title` — the canonical SmartRecruiters bug
    assert vp.title == "Vice President of Information Technology"
    # location is an OBJECT; parts joined "City, Region, Country"
    assert vp.location_raw == "Austin, TX, us"
    assert vp.external_id == "743999900000001"
    # org_name from the list company object's name (falls back to the config slug otherwise)
    assert vp.org_name == "Acme Corporation"
    # typeOfEmployment is an object -> its label
    assert vp.employment_type == "Permanent"
    assert vp.date_posted == "2026-07-20T09:00:00.000Z"
    # url from the detail's applyUrl
    assert vp.url == "https://jobs.smartrecruiters.com/AcmeCorp/743999900000001/apply"

    # description_html assembled from DETAIL sections: jobDescription + qualifications
    assert vp.description_html == "<p>Role details.</p>\n<p>Reqs.</p>"
    # companyDescription is deliberately excluded from the assembled body
    assert "boring, reliable" not in vp.description_html
    # the LIST carries no description; the body only exists because the DETAIL was fetched
    assert vp.description_md is None


@respx.mock
async def test_smartrecruiters_detail_error_skips_only_that_posting():
    # A single failing DETAIL must skip just that posting, never abort the run.
    respx.get(_BASE).mock(return_value=Response(200, json=_LIST))
    respx.get(f"{_BASE}/743999900000001").mock(return_value=Response(200, json=_DETAIL))
    respx.get(f"{_BASE}/743999900000002").mock(return_value=Response(404))

    postings = await _fetch()

    assert len(postings) == 1
    assert postings[0].external_id == "743999900000001"


@respx.mock
async def test_smartrecruiters_list_error_raises_adapter_error():
    # A LIST HTTP error degrades the run to `partial` via AdapterError rather than crashing.
    respx.get(_BASE).mock(return_value=Response(500))
    with pytest.raises(AdapterError, match="SmartRecruiters list fetch failed"):
        await _fetch()


def test_smartrecruiters_validate_config_requires_company():
    with pytest.raises(SourceConfigError):
        SmartRecruitersAdapter().validate_config({})
