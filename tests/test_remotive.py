"""Remotive adapter tests. No network."""

from __future__ import annotations

import httpx
import pytest
import respx

from kestrel.config import get_settings
from kestrel.ingest.adapters.remotive import RemotiveAdapter
from kestrel.ingest.base import SourceConfigError
from kestrel.ingest.http import HttpClient

_BASE = "https://remotive.com/api/remote-jobs"


async def _fetch(config: dict | None = None):
    s = get_settings().ingest
    cfg = config or {}
    async with HttpClient(
        user_agent=s.user_agent, respect_robots=False, max_attempts=1
    ) as client:
        return await RemotiveAdapter().fetch(cfg, client)


@respx.mock
async def test_remotive_maps_jobs_correctly():
    fixture = {
        "00-warning": "test",
        "0-legal-notice": "test",
        "job-count": 2,
        "total-job-count": 100,
        "jobs": [
            {
                "id": "12345",
                "url": "https://remotive.com/jobs/12345",
                "title": "Senior Developer",
                "company_name": "Example Corp",
                "candidate_required_location": "Anywhere",
                "salary": "$100k-$150k",
                "description": "<p>Build things.</p>",
                "publication_date": "2026-11-01T12:00:00Z",
            },
            {
                "id": "67890",
                "url": "https://remotive.com/jobs/67890",
                "title": "Product Manager",
                "company_name": "Another Corp",
                "candidate_required_location": "US Only",
                "salary": None,
                "description": None,
                "publication_date": "2026-11-02T08:00:00Z",
            },
        ],
    }
    respx.get(f"{_BASE}?limit=50").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    batch = await _fetch()

    assert len(batch) == 2
    p0 = batch[0]
    assert p0.title == "Senior Developer"
    assert p0.org_name == "Example Corp"
    assert p0.location_raw == "Anywhere"
    assert p0.is_remote is True
    assert p0.description_html == "<p>Build things.</p>"
    assert p0.date_posted == "2026-11-01T12:00:00Z"
    assert p0.url == "https://remotive.com/jobs/12345"
    assert p0.external_id == "12345"

    p1 = batch[1]
    assert p1.title == "Product Manager"
    assert p1.description_html is None
    assert p1.external_id == "67890"


@respx.mock
async def test_remotive_handles_429():
    respx.get(f"{_BASE}?limit=50").mock(return_value=httpx.Response(429))

    batch = await _fetch()

    assert len(batch) == 0
    assert batch.partial_error is not None
    assert "rate limit" in batch.partial_error.lower()


@respx.mock
async def test_remotive_handles_empty_jobs():
    respx.get(f"{_BASE}?limit=50").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )

    batch = await _fetch()

    assert len(batch) == 0
    assert batch.partial_error is None


@respx.mock
async def test_remotive_fallback_external_id():
    """Jobs without an id use the url as the dedup key."""
    fixture = {
        "jobs": [
            {
                "url": "https://remotive.com/jobs/abc",
                "title": "No ID Job",
                "company_name": "Test Corp",
            },
        ],
    }
    respx.get(f"{_BASE}?limit=50").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    batch = await _fetch()

    assert len(batch) == 1
    assert batch[0].external_id == "https://remotive.com/jobs/abc"


@respx.mock
async def test_remotive_deduplicates_by_id():
    fixture = {
        "jobs": [
            {
                "id": "111",
                "url": "https://remotive.com/jobs/111",
                "title": "First",
                "company_name": "Corp A",
            },
            {
                "id": "111",
                "url": "https://remotive.com/jobs/111",
                "title": "Duplicate",
                "company_name": "Corp A",
            },
        ],
    }
    respx.get(f"{_BASE}?limit=50").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    batch = await _fetch()

    assert len(batch) == 1
    assert batch[0].title == "First"


def test_validate_config_rejects_bad_limit():
    adapter = RemotiveAdapter()
    with pytest.raises(SourceConfigError, match="limit"):
        adapter.validate_config({"limit": 0})
    with pytest.raises(SourceConfigError, match="limit"):
        adapter.validate_config({"limit": 101})
    with pytest.raises(SourceConfigError, match="limit"):
        adapter.validate_config({"limit": True})
    # Valid values pass
    adapter.validate_config({"limit": 1})
    adapter.validate_config({"limit": 100})
    adapter.validate_config({})
