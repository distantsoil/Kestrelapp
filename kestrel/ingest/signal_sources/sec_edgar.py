"""SEC EDGAR full-text search signal source (SPEC.md 5.3, REFERENCES.md, verified live 2026-07-30).

GET https://efts.sec.gov/LATEST/search-index?q={query}&forms=8-K returns an Elasticsearch-shaped
body. Each hit's `_id` is "{accession-with-dashes}:{filename}"; the document URL is
https://www.sec.gov/Archives/edgar/data/{cik}/{accession-no-dashes}/{filename}. 8-K Item 5.02 covers
officer departures; Item 1.05 covers material cybersecurity incidents (mandatory since 2023).

LEGAL / POLITE (constraint 7): public filings only. A DESCRIPTIVE User-Agent with a REAL contact
address is MANDATORY (SEC blocks requests without one). The effective UA is the source config's
`user_agent` when set, else the shared HttpClient's configured one (settings.ingest.user_agent); a
placeholder (example.com, or no `@`) is a SourceConfigError so the runner disables the source with
a clear message rather than sending a fabricated contact. Capped at 10 req/sec, backing off on 403
(client-handled). This adapter never logs in and never fetches anything but public archive
documents."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus

import structlog

from kestrel.ingest.http import has_contact_address
from kestrel.ingest.signal_base import AdapterError, RawSignal, SourceConfigError
from kestrel.ingest.signal_sources.sam_gov import SignalBatch

log = structlog.get_logger()

_FTS = "https://efts.sec.gov/LATEST/search-index"
_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_MULTINL_RE = re.compile(r"\n\s*\n\s*\n+")
# EDGAR's 10/sec ceiling is PER IP across sec.gov, but the queries hit efts.sec.gov and the
# documents hit www.sec.gov, which the per-host rate limiter buckets separately. Cap each host at
# 5/sec so the combined worst case stays within the ceiling.
_DEFAULT_RATE = 5.0
_MAX_DOC_CHARS = 8000  # enough opening + item text for P10; bounds the fetch


def edgar_user_agent(config: dict | None, client: Any) -> str:
    """The User-Agent EDGAR requests carry: the source config's `user_agent` if set, else the
    HttpClient's configured one. Raises SourceConfigError when the result has no real contact
    address, because SEC requires one and a fabricated placeholder violates hard rule 7."""
    ua = (config or {}).get("user_agent") or getattr(client, "user_agent", None)
    if not has_contact_address(ua):
        raise SourceConfigError(
            "SEC EDGAR requires a descriptive User-Agent with a real contact address; "
            f"the effective one is {ua!r}. Set ingest.user_agent in settings.yml to "
            "'Kestrel/1.0 (your.name@your-domain.com)' or set this source's user_agent"
        )
    return ua


class SecEdgarSignalSource:
    kind = "sec_edgar"

    def validate_config(self, config: dict) -> None:
        queries = config.get("queries")
        if not isinstance(queries, list) or not any(isinstance(q, str) and q for q in queries):
            raise SourceConfigError(
                "sec_edgar source requires a non-empty 'queries' list of 8-K search phrases"
            )

    async def fetch(self, config: dict, client: Any) -> list[RawSignal]:
        self.validate_config(config)
        forms = ",".join(config.get("forms") or ["8-K"])
        max_results = int(config.get("max_results", 10))
        fetch_documents = bool(config.get("fetch_documents", True))
        rate = float(config.get("rate_per_sec", _DEFAULT_RATE))
        # Sent per-request so a source-level override wins over the client's default UA.
        headers = {"User-Agent": edgar_user_agent(config, client)}

        signals = SignalBatch()
        seen_ids: set[str] = set()
        errors: list[str] = []
        attempted = 0
        for query in config["queries"]:
            if not isinstance(query, str) or not query:
                continue
            url = f"{_FTS}?q={quote_plus(query)}&forms={quote_plus(forms)}"
            attempted += 1
            try:
                data = await client.get_json(url, rate_per_sec=rate, headers=headers)
            except Exception as exc:  # one bad query never sinks the whole source
                log.warning("sec_edgar_query_error", query=query, error=str(exc))
                errors.append(f"query {query!r}: {exc}")
                continue
            for hit in _hits(data)[:max_results]:
                signal = self._to_signal(hit, query, forms)
                if signal is None or signal.external_id in seen_ids:
                    continue
                seen_ids.add(signal.external_id)
                if fetch_documents and signal.source_url:
                    signal.text = (
                        await self._document_text(client, signal, rate, headers) or signal.text
                    )
                signals.append(signal)
        if errors and attempted == len(errors):
            # Every query failed: nothing was collected, so the run is a failure, not a quiet zero.
            raise AdapterError("every EDGAR query failed: " + "; ".join(errors))
        signals.fetch_errors = errors  # a failed query makes the run partial, never lost
        return signals

    def _to_signal(self, hit: dict, query: str, forms: str) -> RawSignal | None:
        _id = hit.get("_id")
        src = hit.get("_source") or {}
        if not isinstance(_id, str) or ":" not in _id or not isinstance(src, dict):
            return None
        adsh_raw, filename = _id.split(":", 1)
        adsh = src.get("adsh") or adsh_raw
        ciks = src.get("ciks") or []
        cik = _first_cik(ciks)
        if cik is None or not filename:
            return None
        doc_url = f"{_ARCHIVES}/{cik}/{adsh.replace('-', '')}/{filename}"
        org_name = _clean_display_name(src.get("display_names"))
        items = src.get("items") or []
        item = items[0] if items else None
        file_date = src.get("file_date")
        # Metadata text is the fallback signal (used when a document fetch is off or fails).
        meta_text = (
            f"SEC 8-K filing by {org_name or 'unknown company'} on {file_date or 'unknown date'}. "
            f"Items: {', '.join(items) if items else 'n/a'}. Matched search phrase: {query}."
        )
        return RawSignal(
            signal_kind="trigger",
            org_name=org_name,
            source_url=doc_url,
            external_id=_id,
            title=f"8-K: {org_name or 'company'}",
            text=meta_text,
            published_date=file_date,
            form=src.get("root_form") or src.get("form") or forms,
            item=item,
            raw={"hit": hit},
        )

    async def _document_text(
        self, client: Any, signal: RawSignal, rate: float, headers: dict | None
    ) -> str | None:
        """Fetch the primary filing document and reduce it to bounded plain text for P10. A fetch
        failure is non-fatal: the metadata text remains the signal (never sink the hit)."""
        try:
            html = await client.get_text(signal.source_url, rate_per_sec=rate, headers=headers)
        except Exception:
            return None
        return _html_to_text(html)[:_MAX_DOC_CHARS] or None


def _hits(data: object) -> list[dict]:
    if isinstance(data, dict):
        inner = data.get("hits")
        if isinstance(inner, dict) and isinstance(inner.get("hits"), list):
            return [h for h in inner["hits"] if isinstance(h, dict)]
    return []


def _first_cik(ciks: object) -> int | None:
    if isinstance(ciks, list):
        for c in ciks:
            try:
                return int(str(c))  # archives path uses the CIK with leading zeros stripped
            except (TypeError, ValueError):
                continue
    return None


def _clean_display_name(display_names: object) -> str | None:
    if not isinstance(display_names, list) or not display_names:
        return None
    name = str(display_names[0])
    return re.split(r"\s+\(CIK", name)[0].strip() or None


def _html_to_text(html: str) -> str:
    text = _TAG_RE.sub(" ", html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#160;", " ")
    text = _WS_RE.sub(" ", text)
    return _MULTINL_RE.sub("\n\n", text).strip()
