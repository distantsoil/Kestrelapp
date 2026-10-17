"""SAM.gov signal source: missing-key config disables the source cleanly (SourceConfigError), and
opportunitiesData items parse into posting RawSignals with defensive field handling. Coded against
the documented response shape (SPEC.md Appendix A); no live SAM.gov call was available at build
time, so this fixture is a best-effort model of the real payload."""

from __future__ import annotations

import pytest
import respx
from httpx import Response

from kestrel.ingest.http import HttpClient
from kestrel.ingest.signal_base import AdapterError, SourceConfigError
from kestrel.ingest.signal_sources.sam_gov import SamGovSignalSource

_OPPS = {
    "opportunitiesData": [
        {
            "noticeId": "abc123",
            "title": "IT Modernization Advisory Services",
            "fullParentPathName": "DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY",
            "uiLink": "https://sam.gov/opp/abc123/view",
            "description": "Advisory support for legacy system modernization.",
            "postedDate": "2026-07-01",
            "naicsCode": "541512",
            "placeOfPerformance": {
                "city": {"name": "Fort Belvoir"},
                "state": {"code": "VA"},
                "country": {"code": "USA"},
            },
        },
        {
            "noticeId": "def456",
            "title": "Cybersecurity Assessment Services",
            "organizationName": "GENERAL SERVICES ADMINISTRATION",
            "uiLink": "https://sam.gov/opp/def456/view",
            # description is a URL to a separate doc, not inline text -> must fall back to title
            "description": "https://sam.gov/opp/def456/description.pdf",
            "postedDate": "2026-07-05",
            "naicsCode": "541519",
        },
    ]
}


class _FakeHttp:
    def __init__(self) -> None:
        self.json_calls: list[str] = []
        self.headers: list[dict] = []

    async def get_json(self, url, **kw):
        self.json_calls.append(url)
        self.headers.append(kw.get("headers") or {})
        return _OPPS


class _FailOnce(_FakeHttp):
    """The first NAICS query fails; the rest succeed."""

    async def get_json(self, url, **kw):
        if not self.json_calls:
            self.json_calls.append(url)
            raise RuntimeError("503 from https://api.sam.gov/opportunities/v2/search")
        return await super().get_json(url, **kw)


class _AlwaysFail(_FakeHttp):
    async def get_json(self, url, **kw):
        self.json_calls.append(url)
        raise RuntimeError("503 from https://api.sam.gov/opportunities/v2/search")


def test_validate_config_requires_api_key(monkeypatch):
    monkeypatch.delenv("SAM_GOV_API_KEY", raising=False)
    src = SamGovSignalSource()
    with pytest.raises(SourceConfigError):
        src.validate_config({})


def test_validate_config_passes_with_explicit_key():
    src = SamGovSignalSource()
    src.validate_config({"api_key": "x"})  # ok, no raise


def test_validate_config_passes_with_env_key(monkeypatch):
    monkeypatch.setenv("SAM_GOV_API_KEY", "envkey")
    src = SamGovSignalSource()
    src.validate_config({})  # ok, no raise


async def test_fetch_parses_opportunities_into_postings():
    src = SamGovSignalSource()
    http = _FakeHttp()
    signals = await src.fetch({"api_key": "x", "naics": ["541512"]}, http)
    assert len(signals) == 2

    first, second = signals[0], signals[1]

    assert first.signal_kind == "posting"
    assert first.external_id == "abc123"
    assert first.title == "IT Modernization Advisory Services"
    assert first.source_url == "https://sam.gov/opp/abc123/view"
    assert first.naics == "541512"
    assert first.org_name == "DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY"
    assert first.text == "Advisory support for legacy system modernization."
    assert first.location_raw == "Fort Belvoir, VA, USA"

    assert second.signal_kind == "posting"
    assert second.external_id == "def456"
    assert second.title == "Cybersecurity Assessment Services"
    assert second.source_url == "https://sam.gov/opp/def456/view"
    assert second.naics == "541519"
    # description looked like a URL -> falls back to title, never fetched
    assert second.text == "Cybersecurity Assessment Services"

    assert len(http.json_calls) == 1  # one NAICS code configured -> one call


async def test_api_key_travels_in_the_header_never_the_url():
    src = SamGovSignalSource()
    http = _FakeHttp()
    await src.fetch({"api_key": "SECRET123", "naics": ["541512"]}, http)
    assert "SECRET123" not in http.json_calls[0]
    assert "api_key" not in http.json_calls[0]
    assert http.headers[0].get("X-Api-Key") == "SECRET123"


@respx.mock
async def test_403_error_never_contains_the_api_key(monkeypatch):
    """The end-to-end string that would land in ScrapeRun.error/notifications on a real 403."""
    import kestrel.ingest.http as http_mod

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(http_mod.asyncio, "sleep", _no_sleep)
    respx.get("https://api.sam.gov/opportunities/v2/search").mock(
        return_value=Response(403, json={"error": {"code": "API_KEY_INVALID"}})
    )
    src = SamGovSignalSource()
    async with HttpClient(user_agent="Kestrel/1.0 (test)", respect_robots=False) as http:
        with pytest.raises(AdapterError) as exc:
            await src.fetch({"api_key": "SECRET123", "naics": ["541512"]}, http)
    assert "SECRET123" not in str(exc.value)
    assert "403" in str(exc.value)


async def test_one_failed_naics_query_keeps_the_rest_and_records_the_error():
    src = SamGovSignalSource()
    http = _FailOnce()
    signals = await src.fetch({"api_key": "x", "naics": ["541512", "541611"]}, http)
    assert len(signals) == 2  # the second query's items were collected, not discarded
    assert len(signals.fetch_errors) == 1 and "541512" in signals.fetch_errors[0]


async def test_every_query_failing_is_an_adapter_error():
    src = SamGovSignalSource()
    with pytest.raises(AdapterError):
        await src.fetch({"api_key": "x", "naics": ["541512", "541611"]}, _AlwaysFail())


async def test_fetch_skips_items_without_notice_id():
    src = SamGovSignalSource()
    http = _FakeHttp()
    http.get_json = lambda url, **kw: _with_missing_id()  # type: ignore[method-assign]
    signals = await src.fetch({"api_key": "x", "naics": ["541512"]}, http)
    assert signals == []


async def _with_missing_id():
    return {"opportunitiesData": [{"title": "no id here"}]}
