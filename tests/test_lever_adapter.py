"""Lever adapter maps the real payload shape correctly (Appendix A gotchas), respx-mocked.

Key Lever traps exercised: the title lives in `text` (not `title`), location is
`categories.location`, `createdAt` is epoch milliseconds, `salaryRange` may be null, and an
unknown slug (404) must degrade the run to `partial` via AdapterError rather than crash.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import respx
from httpx import Response

from kestrel.config import get_settings
from kestrel.ingest.adapters.lever import LeverAdapter
from kestrel.ingest.base import AdapterError, SourceConfigError
from kestrel.ingest.http import HttpClient

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "lever.json").read_text())


async def _fetch(company: str = "acme"):
    s = get_settings().ingest
    async with HttpClient(user_agent=s.user_agent, respect_robots=False) as client:
        return await LeverAdapter().fetch({"company": company}, client)


@respx.mock
async def test_lever_adapter_field_mapping():
    respx.get("https://api.lever.co/v0/postings/acme").mock(
        return_value=Response(200, json=_FIXTURE)  # bare JSON array, not a {"jobs": ...} object
    )
    postings = await _fetch()

    assert len(postings) == 2
    cto = postings[0]

    # title comes from `text`, NOT `title` — the canonical Lever bug
    assert cto.title == "Fractional Chief Technology Officer"
    # location from categories.location
    assert cto.location_raw == "Boston, MA"
    # org_name falls back to the config slug (Lever has no company_name field)
    assert cto.org_name == "acme"
    assert cto.external_id == "a1b2c3d4-1111-4a2b-9c3d-000000000001"
    assert cto.url == "https://jobs.lever.co/acme/a1b2c3d4-1111-4a2b-9c3d-000000000001"
    assert cto.apply_url.endswith("/apply")
    assert cto.employment_type == "Part-time"

    # createdAt (epoch ms) -> ISO 8601 UTC string
    expected_iso = datetime.fromtimestamp(1753000000000 / 1000, tz=UTC).isoformat()
    assert cto.date_posted == expected_iso
    assert cto.date_posted.startswith("2025-07-20T")  # sanity: 1753000000000 ms is 2025-07-20 UTC

    # descriptionPlain preferred as markdown; description_html left None when plain present
    assert cto.description_md.startswith("Lead technology strategy")
    assert cto.description_html is None

    # salaryRange populated -> comp_* mapped, interval normalized best-effort
    assert cto.comp_min == 180000.0
    assert cto.comp_max == 240000.0
    assert cto.comp_currency == "USD"
    assert cto.comp_interval == "year"  # "per-year-salary" -> "year"

    # workplaceType "hybrid" -> remote_hint, is_remote stays None (only exact "remote" sets True)
    assert cto.remote_hint == "hybrid"
    assert cto.is_remote is None


@respx.mock
async def test_lever_adapter_null_salary_and_remote():
    respx.get("https://api.lever.co/v0/postings/acme").mock(
        return_value=Response(200, json=_FIXTURE)
    )
    postings = await _fetch()
    sales = postings[1]

    # salaryRange null -> all comp fields stay None
    assert sales.comp_min is None
    assert sales.comp_max is None
    assert sales.comp_currency is None
    assert sales.comp_interval is None

    # workplaceType exactly "remote" -> is_remote True
    assert sales.remote_hint == "remote"
    assert sales.is_remote is True
    assert sales.location_raw == "Remote - US"


@respx.mock
async def test_lever_unknown_slug_raises_adapter_error():
    # An unknown slug returns 404; the run must become `partial`, never crash.
    respx.get("https://api.lever.co/v0/postings/nope").mock(return_value=Response(404))
    with pytest.raises(AdapterError, match="Lever slug not found: nope"):
        await _fetch("nope")


def test_lever_validate_config_requires_company():
    with pytest.raises(SourceConfigError):
        LeverAdapter().validate_config({})
