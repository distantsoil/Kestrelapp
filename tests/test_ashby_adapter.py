"""Ashby adapter maps the real payload shape correctly (Appendix A gotchas), respx-mocked."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import respx
from httpx import Response

from kestrel.config import get_settings
from kestrel.ingest.adapters.ashby import AshbyAdapter
from kestrel.ingest.base import SourceConfigError
from kestrel.ingest.http import HttpClient

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "ashby.json").read_text())


@respx.mock
async def test_ashby_field_mapping():
    # Token is case sensitive; the mocked URL uses the exact "Ashby" casing.
    respx.get("https://api.ashbyhq.com/posting-api/job-board/Ashby").mock(
        return_value=Response(200, json=_FIXTURE)
    )
    s = get_settings().ingest
    async with HttpClient(user_agent=s.user_agent, respect_robots=False) as client:
        postings = await AshbyAdapter().fetch({"token": "Ashby"}, client)

    # Three fixture jobs, but the isListed:false one is filtered out.
    assert len(postings) == 2

    cto, staff = postings

    # Core field mapping.
    assert cto.title == "Chief Technology Officer"
    assert cto.external_id == "e1a7c9f0-1111-4a22-9b33-000000000001"
    assert cto.org_name == "Ashby"  # no company_name in payload -> falls back to token
    assert cto.url == "https://jobs.ashbyhq.com/Ashby/e1a7c9f0-1111-4a22-9b33-000000000001"
    assert cto.apply_url.endswith("/application")
    assert cto.location_raw == "San Francisco, CA"  # plain string
    assert cto.employment_type == "FullTime"
    assert cto.date_posted == "2026-07-22T15:00:00.000Z"

    # descriptionPlain is preferred -> description_md; description_html left None.
    assert cto.description_md.startswith("Lead engineering")
    assert cto.description_html is None

    # isRemote -> is_remote; workplaceType -> remote_hint.
    assert cto.is_remote is True
    assert cto.remote_hint == "Hybrid"
    assert staff.remote_hint == "Remote"

    # Compensation present (Salary component) is mapped best-effort.
    assert cto.comp_min == 240000
    assert cto.comp_max == 300000
    assert cto.comp_currency == "USD"
    assert cto.comp_interval == "year"

    # Compensation ABSENT -> comp_* stay None (unknown, never zero).
    assert staff.comp_min is None
    assert staff.comp_max is None
    assert staff.comp_currency is None
    assert staff.comp_interval is None


@respx.mock
async def test_ashby_unlisted_job_is_filtered_out():
    respx.get("https://api.ashbyhq.com/posting-api/job-board/Ashby").mock(
        return_value=Response(200, json=_FIXTURE)
    )
    s = get_settings().ingest
    async with HttpClient(user_agent=s.user_agent, respect_robots=False) as client:
        postings = await AshbyAdapter().fetch({"token": "Ashby"}, client)

    titles = {p.title for p in postings}
    assert "Confidential Executive Search" not in titles  # isListed: false


def test_ashby_org_name_uses_config_company_when_given():
    posting = AshbyAdapter()._to_posting(
        _FIXTURE["jobs"][0], {"token": "Ashby", "company_name": "Ashby Inc"}, "Ashby"
    )
    assert posting.org_name == "Ashby Inc"


def test_ashby_validate_config_requires_token():
    with pytest.raises(SourceConfigError):
        AshbyAdapter().validate_config({})
    with pytest.raises(SourceConfigError):
        AshbyAdapter().validate_config({"token": ""})
