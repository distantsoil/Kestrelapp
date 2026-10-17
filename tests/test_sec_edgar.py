"""SEC EDGAR signal source: parse Elasticsearch-shaped full-text-search hits, construct resolvable
Archives document URLs (CIK leading zeros stripped, accession de-dashed), clean display names,
dedup by _id, and reduce a fetched document to bounded text for P10."""

from __future__ import annotations

import pytest

from kestrel.ingest.signal_base import AdapterError, SourceConfigError
from kestrel.ingest.signal_sources.sec_edgar import SecEdgarSignalSource, edgar_user_agent

_REAL_UA = "Kestrel/1.0 (ops@kestrel-test.org)"
_PLACEHOLDER_UA = "Kestrel/1.0 (+https://github.com/kestrel-app; contact: operator@example.com)"

_FTS = {
    "hits": {
        "total": {"value": 3},
        "hits": [
            {
                "_id": "0001193125-24-000111:d1.htm",
                "_source": {
                    "ciks": ["0001193125"],
                    "display_names": ["ACME CORP (CIK 0001193125)"],
                    "file_date": "2024-03-01",
                    "items": ["5.02"],
                },
            },
            {
                "_id": "0000320193-24-000222:aapl.htm",
                "_source": {
                    "ciks": ["0000320193"],
                    "display_names": ["APPLE INC (CIK 0000320193) (AAPL)"],
                    "file_date": "2024-04-01",
                    "items": ["1.05"],
                },
            },
            {  # duplicate of the first hit; must be deduped by _id
                "_id": "0001193125-24-000111:d1.htm",
                "_source": {
                    "ciks": ["0001193125"],
                    "display_names": ["ACME CORP (CIK 0001193125)"],
                    "file_date": "2024-03-01",
                    "items": ["5.02"],
                },
            },
        ],
    }
}

_DOC = (
    "<html><body><p>Item 5.02.</p>"
    "<p>Jane Roe resigned as Chief Information Officer.</p></body></html>"
)


class _FakeHttp:
    def __init__(self, user_agent: str | None = _REAL_UA) -> None:
        self.text_calls: list[str] = []
        self.json_calls: list[str] = []
        self.sent_ua: list[str | None] = []
        self.user_agent = user_agent  # what a real HttpClient exposes (settings.ingest.user_agent)

    async def get_json(self, url, **kw):
        self.json_calls.append(url)
        self.sent_ua.append((kw.get("headers") or {}).get("User-Agent"))
        return _FTS

    async def get_text(self, url, **kw):
        self.text_calls.append(url)
        return _DOC


class _FailOnce(_FakeHttp):
    async def get_json(self, url, **kw):
        if not self.json_calls:
            self.json_calls.append(url)
            raise RuntimeError("503 from https://efts.sec.gov/LATEST/search-index")
        return await super().get_json(url, **kw)


def test_validate_config_requires_queries():
    src = SecEdgarSignalSource()
    with pytest.raises(SourceConfigError):
        src.validate_config({})
    src.validate_config({"queries": ['"chief information officer"']})  # ok


# ------------------------------------------------------------ hard rule 7: a REAL contact address
def test_placeholder_user_agent_is_a_config_error_not_a_fabricated_contact():
    with pytest.raises(SourceConfigError) as exc:
        edgar_user_agent({}, _FakeHttp(user_agent=_PLACEHOLDER_UA))
    assert "ingest.user_agent" in str(exc.value)
    with pytest.raises(SourceConfigError):
        edgar_user_agent({}, _FakeHttp(user_agent="Kestrel/1.0 (no contact here)"))
    with pytest.raises(SourceConfigError):
        edgar_user_agent({}, _FakeHttp(user_agent=None))


def test_effective_user_agent_is_the_clients_unless_the_source_overrides():
    http = _FakeHttp(user_agent=_REAL_UA)
    assert edgar_user_agent({}, http) == _REAL_UA
    assert edgar_user_agent({"user_agent": "Ops (ops@other.org)"}, http) == "Ops (ops@other.org)"
    # a source override is held to the same rule
    with pytest.raises(SourceConfigError):
        edgar_user_agent({"user_agent": "Kestrel (x@example.com)"}, http)


async def test_fetch_sends_the_configured_user_agent_and_refuses_a_placeholder():
    src = SecEdgarSignalSource()
    http = _FakeHttp(user_agent=_REAL_UA)
    await src.fetch({"queries": ["q"], "fetch_documents": False}, http)
    assert http.sent_ua == [_REAL_UA]
    with pytest.raises(SourceConfigError):
        await src.fetch({"queries": ["q"]}, _FakeHttp(user_agent=_PLACEHOLDER_UA))


async def test_one_failed_query_keeps_the_rest_and_records_the_error():
    src = SecEdgarSignalSource()
    http = _FailOnce()
    signals = await src.fetch({"queries": ["bad", "good"], "fetch_documents": False}, http)
    assert len(signals) == 2  # the second query's hits were collected, not discarded
    assert len(signals.fetch_errors) == 1 and "'bad'" in signals.fetch_errors[0]


async def test_every_query_failing_is_an_adapter_error():
    class _AlwaysFail(_FakeHttp):
        async def get_json(self, url, **kw):
            raise RuntimeError("503")

    src = SecEdgarSignalSource()
    with pytest.raises(AdapterError):
        await src.fetch({"queries": ["a", "b"], "fetch_documents": False}, _AlwaysFail())


async def test_parses_hits_and_builds_document_urls():
    src = SecEdgarSignalSource()
    http = _FakeHttp()
    signals = await src.fetch(
        {"queries": ['"chief information officer"'], "fetch_documents": False}, http
    )
    assert len(signals) == 2  # the duplicate _id was removed
    acme, apple = signals[0], signals[1]
    assert acme.org_name == "ACME CORP"
    assert acme.item == "5.02"
    assert acme.external_id == "0001193125-24-000111:d1.htm"
    assert acme.source_url == (
        "https://www.sec.gov/Archives/edgar/data/1193125/000119312524000111/d1.htm"
    )
    assert apple.org_name == "APPLE INC"
    assert apple.source_url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019324000222/aapl.htm"
    )
    assert http.text_calls == []  # fetch_documents off -> no document requests


async def test_fetch_documents_reduces_to_bounded_text():
    src = SecEdgarSignalSource()
    http = _FakeHttp()
    signals = await src.fetch(
        {"queries": ['"chief information officer"'], "fetch_documents": True}, http
    )
    assert len(http.text_calls) == 2  # one document per unique hit
    assert "resigned as Chief Information Officer" in signals[0].text
    assert "<" not in signals[0].text  # HTML stripped
