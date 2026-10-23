"""RemoteRocketship paid jobs API, DISCOVERY ONLY (SPEC.md 5.1 and Appendix A, D-SRC-14).

`POST https://www.remoterocketship.com/api/openclaw/jobs/` with `Authorization: Bearer <key>` and
`{"filters": {..., "page": N, "itemsPerPage": <=50}, "includeJobDescription": false}`. The trailing
slash is required (without it the server answers 308).

This adapter keeps ONE field of each job: the employer's own posting `url`. Titles, descriptions,
company data and the reply itself are never stored or logged: the provider's terms allow caching for
24 hours only and prohibit archiving, so the reply lives in memory for this call. The runner hands
the links to `kestrel.ingest.employer_links`, which reads each posting from the employer (a found
board through board discovery, or the employer's public page).

Rules that bind this module:
  - The key comes from `.env` through `api_key_env` (default REMOTEROCKETSHIP_API_KEY), never from
    the config row, and appears in no log line, exception text or message.
  - The provider's own `message` and `hint` are never shown. Each failure is said in Kestrel's words
    (D-SEC-2); the body is read only to tell a missing key from a rejected one.
  - A run makes at most `max_pages` requests (default 2, 50 jobs each), far inside the provider's
    500 requests or 3,000 jobs per UTC day. 429 and 403 are never retried (each retry spends quota);
    a 429 keeps the links already collected (none, on the first page) and makes the run partial,
    never a failure of the source.
  - The robots check is skipped for this one authenticated API call only: `Disallow: /api` is a
    crawler rule and this API is published for programmatic use with the user's own key (D-SRC-14).
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import structlog

from kestrel.ingest.base import AdapterError, RawPosting, SourceConfigError
from kestrel.pipeline.profile import load_profile

log = structlog.get_logger(__name__)

API_URL = "https://www.remoterocketship.com/api/openclaw/jobs/"
DEFAULT_KEY_ENV = "REMOTEROCKETSHIP_API_KEY"
DEFAULT_MAX_PAGES = 2
MAX_PAGES_LIMIT = 5
MAX_PER_PAGE = 50
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_RATE = 1.0
_NEEDS_SUBSCRIPTION = (
    "RemoteRocketship needs an active paid subscription with API access. Kestrel stays free; "
    "this source uses your own subscription"
)


class LinkBatch(list):
    """The employer links one fetch collected, as RawPostings holding only `url`, plus
    `partial_error`: why the fetch stopped early after collecting some (a 429). Still a plain list
    to every consumer; the runner reads `partial_error` (duck-typed) to record a partial run."""

    partial_error: str | None

    def __init__(self) -> None:
        super().__init__()
        self.partial_error = None


MAX_PROFILE_FILTERS = 10


def _profile_filters() -> dict:
    """Filters from the employment profile when the source sets none: the target titles as
    `jobTitleFilters` and the onsite metros as `locationFilters`. Filters the user writes in the
    source always replace these. Nothing in the profile means no filter (a plain query)."""
    try:
        prof = load_profile("employment")
    except (OSError, ValueError, AttributeError):
        return {}
    if prof is None:
        return {}
    terms = getattr(prof, "employment_terms", None)
    raw_titles = getattr(terms, "target_titles", None) or []
    titles = [str(t).strip() for t in raw_titles if str(t).strip()]
    geo = getattr(prof, "geo_rules", None)
    places = []
    for m in getattr(geo, "onsite_metros", None) or []:
        name = str(getattr(m, "name", None) or (m.get("name") if isinstance(m, dict) else "") or "")
        if name.strip():
            places.append(name.strip())
    out: dict = {}
    if titles:
        out["jobTitleFilters"] = titles[:MAX_PROFILE_FILTERS]
    if places:
        out["locationFilters"] = places[:MAX_PROFILE_FILTERS]
    return out


def _key(config: dict) -> str:
    from kestrel.config import get_secret

    name = config.get("api_key_env") or DEFAULT_KEY_ENV
    key = get_secret(name)
    if not key:
        raise SourceConfigError(
            f"RemoteRocketship needs your own API key ({name}). Save it in the RemoteRocketship "
            f"card on the Sources page. {_NEEDS_SUBSCRIPTION}."
        )
    return key


def _problem(status: int, body: Any) -> str:
    """Kestrel's own sentence for an error status. `body` is read only to tell a missing key from
    a rejected one; none of its text is returned."""
    if status == 401:
        said = str(body.get("message", "")).lower() if isinstance(body, dict) else ""
        if "missing" in said:
            return "RemoteRocketship did not receive an API key. Check the key name in the source"
        return (
            "RemoteRocketship did not accept the API key. "
            "Save it again in the RemoteRocketship card on Sources"
        )
    if status == 403:
        return _NEEDS_SUBSCRIPTION
    if status == 429:
        return "RemoteRocketship's daily limit was reached; try again after 00:00 UTC"
    if status in (400, 413):
        return "RemoteRocketship rejected the request. Check the source's filters"
    if status >= 500:
        return "RemoteRocketship had a problem on their side; try again later"
    return f"RemoteRocketship answered with an unexpected status ({status})"


def _int_in(config: dict, name: str, default: int, low: int, high: int) -> int:
    value = config.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise SourceConfigError(f"'{name}' must be a whole number from {low} to {high}")
    return value


class RemoteRocketshipAdapter:
    kind = "remoterocketship"
    # The runner sends this adapter's links to employer_links.resolve instead of storing them.
    discovery_only = True

    def validate_config(self, config: dict) -> None:
        if "api_key" in config:
            raise SourceConfigError(
                "Never put the key in the source. Save it in Kestrel's .env file and, if you "
                "use a name other than REMOTEROCKETSHIP_API_KEY, give that name as 'api_key_env'"
            )
        env = config.get("api_key_env", DEFAULT_KEY_ENV)
        if not isinstance(env, str) or not _ENV_NAME.match(env):
            raise SourceConfigError("'api_key_env' must be an environment variable name")
        _int_in(config, "max_pages", DEFAULT_MAX_PAGES, 1, MAX_PAGES_LIMIT)
        _int_in(config, "items_per_page", MAX_PER_PAGE, 1, MAX_PER_PAGE)
        filters = config.get("filters", {})
        if not isinstance(filters, dict):
            raise SourceConfigError("'filters' must be an object of RemoteRocketship filters")

    async def fetch(self, config: dict, client: Any) -> LinkBatch:
        self.validate_config(config)
        key = _key(config)
        max_pages = _int_in(config, "max_pages", DEFAULT_MAX_PAGES, 1, MAX_PAGES_LIMIT)
        per_page = _int_in(config, "items_per_page", MAX_PER_PAGE, 1, MAX_PER_PAGE)
        filters = dict(config.get("filters") or {}) or _profile_filters()
        headers = {"Authorization": f"Bearer {key}"}
        batch = LinkBatch()
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            body = {
                "filters": {**filters, "page": page, "itemsPerPage": per_page},
                "includeJobDescription": False,
            }
            try:
                # retries=1: one attempt. A 429 or 403 retried would spend more of the daily quota.
                r = await client.request(
                    "POST", API_URL, json=body, headers=headers, rate_per_sec=_RATE, retries=1,
                )
            except httpx.HTTPStatusError as exc:  # the client raises this after a 429/403/503
                r = exc.response
            except httpx.HTTPError as exc:
                log.warning("remoterocketship_unreachable", error=type(exc).__name__)
                raise AdapterError("Could not reach RemoteRocketship; try again later") from exc
            if r.status_code != 200:
                try:
                    detail = r.json()
                except ValueError:
                    detail = None
                message = _problem(r.status_code, detail)
                log.warning("remoterocketship_error", status=r.status_code, page=page)
                if r.status_code == 429:
                    # The daily quota is spent: what is already collected (maybe nothing) is
                    # returned and the run is partial. Not a failure of the source, so it never
                    # counts toward the auto-disable streak.
                    batch.partial_error = message
                    return batch
                raise AdapterError(message)
            try:
                data = r.json()
                jobs = data["jobOpenings"]
                if not isinstance(jobs, list):
                    raise TypeError("jobOpenings")
            except (ValueError, KeyError, TypeError) as exc:
                raise AdapterError(
                    "RemoteRocketship sent a reply Kestrel could not read; the format may have "
                    "changed"
                ) from exc
            for job in jobs:
                url = job.get("url") if isinstance(job, dict) else None
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    if url not in seen:
                        seen.add(url)
                        batch.append(RawPosting(external_id=url, url=url))
            pagination = data.get("pagination")
            if not (isinstance(pagination, dict) and pagination.get("hasNextPage") is True):
                break
        return batch
