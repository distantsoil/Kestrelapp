"""Greenhouse adapter maps the real payload shape correctly (Appendix A gotchas), respx-mocked."""

from __future__ import annotations

import json
from pathlib import Path

import respx
from httpx import Response

from kestrel.config import get_settings
from kestrel.ingest.adapters.greenhouse import GreenhouseAdapter
from kestrel.ingest.http import HttpClient

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "greenhouse.json").read_text())


@respx.mock
async def test_greenhouse_adapter_field_mapping():
    respx.get("https://boards-api.greenhouse.io/v1/boards/meridian/jobs").mock(
        return_value=Response(200, json=_FIXTURE)
    )
    s = get_settings().ingest
    async with HttpClient(user_agent=s.user_agent, respect_robots=False) as client:
        postings = await GreenhouseAdapter().fetch({"token": "meridian"}, client)

    assert len(postings) == 2
    vp = postings[0]
    assert vp.title == "Vice President of Information Technology"
    assert vp.org_name == "Meridian Health Partners"
    assert vp.location_raw == "Austin, TX"  # location is an OBJECT {"name": ...}
    assert vp.external_id == "5501001"
    assert vp.remote_hint == "Hybrid"
    assert vp.sector == "healthcare"
    # content is HTML-ESCAPED HTML; the adapter passes it through, normalize unescapes it
    assert "&lt;p&gt;" in vp.description_html
    assert vp.description_md is None


def test_greenhouse_validate_config_requires_token():
    import pytest

    from kestrel.ingest.base import SourceConfigError

    with pytest.raises(SourceConfigError):
        GreenhouseAdapter().validate_config({})
