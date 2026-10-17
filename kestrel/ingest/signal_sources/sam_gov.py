"""SAM.gov Opportunities API signal source (SPEC.md 5.2, SPEC.md 5.3). Produces POSTINGS (public-
sector RFPs), not triggers: the runner builds a RawPosting from each item and normalizes it onto
the consulting track directly, without P10 classification.

GET https://api.sam.gov/opportunities/v2/search — documented shape (SPEC.md Appendix A /
REFERENCES.md); this adapter is coded against the docs and is NOT verified live, because no API
key was available at build time. SAM requires a free API key (constraint 7's "politely" reading:
a public government API credential is not "behind a login" scraping). `validate_config` therefore
treats a missing key as a structural config error so the runner can DISABLE this source cleanly
(hard rule: uncertainty escalates, it never rejects the whole batch) rather than fail the run.

SAM requires a bounded `postedFrom`/`postedTo` date window in MM/dd/yyyy, and takes one `ncode`
(NAICS code) per call, so the configured NAICS list is looped. Response body is
`{"opportunitiesData": [...]}`; every field is read defensively since the live shape is unverified.
The `description` field is sometimes a URL to a separate document rather than inline text; this
adapter never fetches it (constraint 7: only the pages explicitly listed, politely) and falls back
to the title.

The API key travels in the `X-Api-Key` header (the api.data.gov gateway accepts it there), never
in the query string: an error string carries the request URL into ScrapeRun.error, notifications,
and logs, and a credential must never ride along."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import structlog

from kestrel.ingest.signal_base import AdapterError, RawSignal, SourceConfigError

log = structlog.get_logger()

_SEARCH_URL = "https://api.sam.gov/opportunities/v2/search"
_DEFAULT_NAICS = ["541512", "541611", "541519"]
_DEFAULT_LOOKBACK_DAYS = 30
_DEFAULT_LIMIT = 100
_DEFAULT_RATE = 2.0
_DATE_FMT = "%m/%d/%Y"


class SamGovSignalSource:
    kind = "sam_gov"

    def validate_config(self, config: dict) -> None:
        if not _resolve_api_key(config):
            raise SourceConfigError(
                "SAM.gov requires a free API key from sam.gov; "
                "save it as SAM_GOV_API_KEY in Kestrel's .env or set config.api_key"
            )

    async def fetch(self, config: dict, client: Any) -> list[RawSignal]:
        self.validate_config(config)
        api_key = _resolve_api_key(config)
        naics_codes = config.get("naics") or _DEFAULT_NAICS
        limit = int(config.get("limit", _DEFAULT_LIMIT))
        rate = float(config.get("rate_per_sec", _DEFAULT_RATE))
        lookback_days = int(config.get("lookback_days", _DEFAULT_LOOKBACK_DAYS))
        ptype = config.get("ptype")

        today = datetime.now(UTC).date()
        posted_from = (today - timedelta(days=lookback_days)).strftime(_DATE_FMT)
        posted_to = today.strftime(_DATE_FMT)

        headers = {"X-Api-Key": api_key}
        signals = SignalBatch()
        seen_ids: set[str] = set()
        errors: list[str] = []
        attempted = 0
        for ncode in naics_codes:
            if not isinstance(ncode, str) or not ncode:
                continue
            params = {
                "postedFrom": posted_from,
                "postedTo": posted_to,
                "ncode": ncode,
                "limit": limit,
            }
            if ptype:
                params["ptype"] = ptype
            url = f"{_SEARCH_URL}?{urlencode(params)}"
            attempted += 1
            try:
                data = await client.get_json(url, rate_per_sec=rate, headers=headers)
            except Exception as exc:  # one bad NAICS code never sinks the whole source
                log.warning("sam_gov_query_error", ncode=ncode, error=str(exc))
                errors.append(f"ncode {ncode!r}: {exc}")
                continue
            for item in _opportunities(data):
                signal = _to_signal(item)
                if signal is None or signal.external_id in seen_ids:
                    continue
                seen_ids.add(signal.external_id)
                signals.append(signal)
        if errors and attempted == len(errors):
            # Every query failed: nothing was collected, so the run is a failure, not a quiet zero.
            raise AdapterError("every SAM.gov query failed: " + "; ".join(errors))
        signals.fetch_errors = errors  # a failed query makes the run partial, never lost
        return signals


class SignalBatch(list):
    """The signals one fetch collected, plus `fetch_errors`: the queries that failed along the way.
    Still a plain list to every consumer; the signal runner reads `fetch_errors` (duck-typed, via
    getattr) to record a `partial` run instead of discarding what was collected."""

    fetch_errors: list[str]

    def __init__(self) -> None:
        super().__init__()
        self.fetch_errors = []


def _resolve_api_key(config: dict) -> str | None:
    key = config.get("api_key")
    if isinstance(key, str) and key:
        return key
    env_var = config.get("api_key_env", "SAM_GOV_API_KEY")
    from kestrel.config import get_secret

    env_key = get_secret(env_var)
    return env_key if env_key else None


def _opportunities(data: object) -> list[dict]:
    if isinstance(data, dict):
        items = data.get("opportunitiesData")
        if isinstance(items, list):
            return [i for i in items if isinstance(i, dict)]
    return []


def _to_signal(item: dict) -> RawSignal | None:
    external_id = item.get("noticeId")
    if not isinstance(external_id, str) or not external_id:
        return None
    title = item.get("title") if isinstance(item.get("title"), str) else None
    org_name = _first_str(
        item.get("fullParentPathName"), item.get("organizationName"), item.get("office")
    )
    source_url = item.get("uiLink") if isinstance(item.get("uiLink"), str) else None
    published_date = item.get("postedDate") if isinstance(item.get("postedDate"), str) else None
    naics = item.get("naicsCode") if isinstance(item.get("naicsCode"), str) else None

    description = item.get("description")
    if isinstance(description, str) and description and not _looks_like_url(description):
        text = description
    else:
        text = title

    return RawSignal(
        signal_kind="posting",
        org_name=org_name,
        source_url=source_url,
        external_id=external_id,
        title=title,
        text=text,
        published_date=published_date,
        location_raw=_location_raw(item.get("placeOfPerformance")),
        naics=naics,
        raw=item,
    )


def _first_str(*values: object) -> str | None:
    for v in values:
        if isinstance(v, str) and v:
            return v
    return None


def _looks_like_url(text: str) -> bool:
    return text.strip().lower().startswith(("http://", "https://"))


def _location_raw(place: object) -> str | None:
    if not isinstance(place, dict):
        return None
    city = place.get("city")
    state = place.get("state")
    country = place.get("country")
    parts: list[str] = []
    if isinstance(city, dict):
        city_name = city.get("name")
        if isinstance(city_name, str) and city_name:
            parts.append(city_name)
    if isinstance(state, dict):
        state_val = state.get("code") or state.get("name")
        if isinstance(state_val, str) and state_val:
            parts.append(state_val)
    if isinstance(country, dict):
        country_val = country.get("code") or country.get("name")
        if isinstance(country_val, str) and country_val:
            parts.append(country_val)
    return ", ".join(parts) or None
