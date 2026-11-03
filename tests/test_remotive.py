"""Remotive adapter tests. No network."""

from __future__ import annotations

import httpx
import pytest
import respx

from kestrel.ingest.adapters.remotive import RemotiveAdapter


@pytest.mark.anyio
async def test_remotive_maps_jobs_correctly():
    adapter = RemotiveAdapter()
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

    with respx.mock(assert_all_called=False) as router:
        router.get("https://remotive.com/api/remote-jobs?limit=50").mock(
            return_value=httpx.Response(200, json=fixture)
        )
        async with httpx.AsyncClient() as client:
            postings = await adapter.run(client)

    assert len(postings) == 2
    p0 = postings[0]
    assert p0.title == "Senior Developer"
    assert p0.org_name == "Example Corp"
    assert p0.location_raw == "Anywhere"
    assert p0.is_remote is True
    assert p0.description_html == "Build things."
    assert p0.date_posted == "2026-11-01T12:00:00Z"
    assert p0.url == "https://remotive.com/jobs/12345"

    p1 = postings[1]
    assert p1.title == "Product Manager"
    assert p1.description_html is None


@pytest.mark.anyio
async def test_remotive_handles_429():
    from kestrel.ingest.base import AdapterError

    adapter = RemotiveAdapter()
    with respx.mock(assert_all_called=False) as router:
        router.get("https://remotive.com/api/remote-jobs?limit=50").mock(
            return_value=httpx.Response(429)
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(AdapterError, match="rate limit"):
                await adapter.run(client)


@pytest.mark.anyio
async def test_remotive_handles_empty_jobs():
    adapter = RemotiveAdapter()
    fixture = {"jobs": []}
    with respx.mock(assert_all_called=False) as router:
        router.get("https://remotive.com/api/remote-jobs?limit=50").mock(
            return_value=httpx.Response(200, json=fixture)
        )
        async with httpx.AsyncClient() as client:
            postings = await adapter.run(client)
    assert postings == []
