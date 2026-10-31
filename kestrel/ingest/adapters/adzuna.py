"""Adzuna search board (SPEC.md 5.1 and Appendix A, D-SRC-15, step B2). Copies the USAJobs adapter.

`GET https://api.adzuna.com/v1/api/jobs/us/search/{page}` with the operator's own free `app_id` and
`app_key` as query parameters (Adzuna takes no header auth). The country is fixed to `us` (Region
is US only, D-UI-109). One request per search term and page.

What the operator provides: the two keys, saved in the app and stored in `.env` like an online-model
key (never in the config row, a log or an error). Adzuna's terms (developer.adzuna.com, Terms of
Service) allow "Personal job search" and ask that each displayed advert carry the word "Adzuna"
linked to Adzuna: every role here links to Adzuna's own redirect page, and the source is named after
Adzuna, so the posting tab reads "Posted on <search> on Adzuna" with that link.

Rules that bind this module:
  - Both keys come from `.env` (ADZUNA_APP_ID_KEY, ADZUNA_APP_KEY; the names end in _KEY because
    that is what the key store accepts). A missing key is a `partial` run with a plain sentence,
    never a failure and never an abort. A 429 or 5xx keeps the pages already collected and is also
    `partial`. A rejected key (401 or 403) is an AdapterError in Kestrel's own words. The
    provider's body is never shown, and the URL (which holds the keys) is never logged or shown.
  - Budget: the free tier allows about 250 calls a day, so a run makes at most MAX_REQUESTS (8)
    requests in all: `max_pages` (default 2, at most 5) pages of 50 per search term, one search
    per term, up to MAX_TITLES terms. 429 is never retried.
  - The description is a SNIPPET (Adzuna's docs: "we currently only provide a snipped of the job
    description"). The body ends with one plain sentence saying so and `raw.description_partial` is
    true, so the scorer and the reader both know the text is the start of the posting only.
  - A salary Adzuna PREDICTED (`salary_is_predicted` = 1) is not a posted salary: it is kept in
    `raw` and never becomes `comp_min` / `comp_max`, so the comp corpus holds stated pay only.
  - Robots: api.adzuna.com/robots.txt disallows everything for crawlers. This is the published
    API, called with the user's own key for their personal job search, which Adzuna's terms allow,
    so the robots check is skipped for this one call (the RemoteRocketship precedent, D-SRC-14).
"""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from kestrel.ingest.base import AdapterError, PostingBatch, RawPosting, SourceConfigError
from kestrel.pipeline.profile import load_profile

log = structlog.get_logger(__name__)

API_URL = "https://api.adzuna.com/v1/api/jobs/us/search"
ID_ENV = "ADZUNA_APP_ID_KEY"
KEY_ENV = "ADZUNA_APP_KEY"
KEY_SIGNUP_URL = "https://developer.adzuna.com/signup"
DEFAULT_MAX_PAGES = 2
MAX_PAGES_LIMIT = 5
DEFAULT_PER_PAGE = 50
MAX_PER_PAGE = 50
DEFAULT_DAYS = 7
MAX_DAYS = 60
MAX_TITLES = 5
MAX_REQUESTS = 8  # across all search terms in one run (the free tier is about 250 calls a day)
_RATE = 1.0
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

NEEDS_KEY = (
    "Adzuna needs your app ID and app key. Add them on the Sources page under Sources that "
    "need your own key"
)
KEY_REJECTED = (
    "Adzuna did not accept your app ID and app key. Save them again on the Sources page "
    "under Sources that need your own key"
)
# Both sentences above hold this phrase; the Sources card reads it to say "Needs you".
KEY_PROBLEM_PHRASE = "app ID and app key"
SNIPPET_NOTE = (
    "Adzuna supplies only the start of this posting. Open the original posting for the full text."
)

_NO_PLACE = {"", "remote", "anywhere", "united states", "usa", "us", "u.s."}


def is_key_problem(error: str | None) -> bool:
    """True when a run's error text is the missing-key or rejected-key sentence."""
    return bool(error) and KEY_PROBLEM_PHRASE in str(error)


def _profile_titles() -> list[str]:
    """The target titles from the employment profile, when the source names no keyword."""
    try:
        prof = load_profile("employment")
    except (OSError, ValueError, AttributeError):
        return []
    terms = getattr(prof, "employment_terms", None) if prof is not None else None
    raw = getattr(terms, "target_titles", None) or []
    titles = [" ".join(str(t).split()) for t in raw if str(t).strip()]
    return titles[:MAX_TITLES]


def _int_in(config: dict, name: str, default: int, low: int, high: int) -> int:
    value = config.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise SourceConfigError(f"'{name}' must be a whole number from {low} to {high}")
    return value


# ------------------------------------------------------------------------------ field mapping
def _text(val: Any) -> str | None:
    if val is None:
        return None
    s = " ".join(str(val).split())
    return s or None


def _num(val: Any) -> float | None:
    try:
        n = float(str(val).replace(",", "")) if val not in (None, "") else None
    except ValueError:
        return None
    return n if n and n > 0 else None


def _predicted(job: dict) -> bool:
    """`salary_is_predicted` is 0 or 1, as a number or a string depending on the reply."""
    return str(job.get("salary_is_predicted") or "0").strip().lower() in ("1", "true")


def _location(job: dict) -> str | None:
    loc = job.get("location") if isinstance(job.get("location"), dict) else {}
    name = _text(loc.get("display_name"))
    if name:
        return name
    area = [a for a in (_text(x) for x in (loc.get("area") or [])) if a]
    area = [a for a in area if a.upper() not in ("US", "USA")]
    if len(area) >= 2:  # Adzuna's area runs country, state, county, city: "City, State"
        return f"{area[-1]}, {area[0]}"
    return area[0] if area else None


def _employment(job: dict) -> str | None:
    time = {"full_time": "Full-time", "part_time": "Part-time"}.get(
        str(job.get("contract_time") or ""))
    kind = {"contract": "Contract", "permanent": "Permanent"}.get(
        str(job.get("contract_type") or ""))
    return ", ".join(x for x in (time, kind) if x) or None


def to_posting(job: dict) -> RawPosting | None:
    """One `results` entry as a RawPosting; None when it has no title and no link."""
    if not isinstance(job, dict):
        return None
    title, url = _text(job.get("title")), _text(job.get("redirect_url"))
    if not title and not url:
        return None
    company = job.get("company") if isinstance(job.get("company"), dict) else {}
    category = job.get("category") if isinstance(job.get("category"), dict) else {}
    snippet = _text(job.get("description"))
    body = f"<p>{html.escape(snippet)}</p>" if snippet else ""
    body += f"<p><em>{SNIPPET_NOTE}</em></p>"
    lo, hi = _num(job.get("salary_min")), _num(job.get("salary_max"))
    predicted = _predicted(job)
    # A predicted salary is Adzuna's model, not the employer's number: it stays in `raw` only.
    pay_lo, pay_hi = (None, None) if predicted else (lo or hi, hi or lo)
    return RawPosting(
        external_id=_text(job.get("id")) or url,
        url=url,
        apply_url=url,
        title=title,
        org_name=_text(company.get("display_name")),
        location_raw=_location(job),
        description_html=body,
        employment_type=_employment(job),
        comp_min=pay_lo, comp_max=pay_hi,
        comp_currency="USD" if pay_lo is not None else None,
        comp_interval="year" if pay_lo is not None else None,
        date_posted=_text(job.get("created")),
        raw={
            "description_partial": True,
            "category": _text(category.get("label")),
            "salary_predicted": predicted,
            **({"predicted_min": lo, "predicted_max": hi} if predicted and (lo or hi) else {}),
        },
    )


# ------------------------------------------------------------------------------ the adapter
def _problem(status: int) -> str:
    """Kestrel's own sentence for an error status; the provider's body is never read or shown."""
    if status in (401, 403):
        return KEY_REJECTED
    if status == 429:
        return "Adzuna asked Kestrel to slow down; the rest is fetched on the next check"
    if status in (400, 404, 413):
        return "Adzuna rejected the search. Check the search words and the place"
    if status >= 500:
        return "Adzuna had a problem on their side; the rest is fetched on the next check"
    return f"Adzuna answered with an unexpected status ({status})"


class AdzunaAdapter:
    kind = "adzuna"

    def validate_config(self, config: dict) -> None:
        for forbidden in ("app_id", "app_key", "api_key"):
            if forbidden in config:
                raise SourceConfigError(
                    "Never put a key in the source. Save them on the Sources page under Sources "
                    "that need your own key"
                )
        for name in ("app_id_env", "app_key_env"):
            env = config.get(name)
            if env is not None and (not isinstance(env, str) or not _ENV_NAME.match(env)):
                raise SourceConfigError(f"'{name}' must be an environment variable name")
        for name in ("keyword", "location"):
            if config.get(name) is not None and not isinstance(config[name], str):
                raise SourceConfigError(f"'{name}' must be text")
        _int_in(config, "max_pages", DEFAULT_MAX_PAGES, 1, MAX_PAGES_LIMIT)
        _int_in(config, "results_per_page", DEFAULT_PER_PAGE, 1, MAX_PER_PAGE)
        _int_in(config, "date_posted_days", DEFAULT_DAYS, 1, MAX_DAYS)

    @staticmethod
    def _params(config: dict, term: str | None) -> dict[str, str]:
        place = (config.get("location") or "").strip()
        what = term or ""
        if place.lower() == "remote":
            what = f"{what} remote".strip()  # Adzuna has no remote filter: the word is the filter
        params = {
            "results_per_page": str(config.get("results_per_page", DEFAULT_PER_PAGE)),
            "max_days_old": str(config.get("date_posted_days", DEFAULT_DAYS)),
            "sort_by": "date",
            "content-type": "application/json",
        }
        if what:
            params["what"] = what
        if place.lower() not in _NO_PLACE:
            params["where"] = place
        return params

    async def fetch(self, config: dict, client: Any) -> PostingBatch:
        self.validate_config(config)
        from kestrel.config import get_secret

        batch = PostingBatch()
        app_id = get_secret(config.get("app_id_env") or ID_ENV)
        app_key = get_secret(config.get("app_key_env") or KEY_ENV)
        if not app_id or not app_key:
            batch.partial_error = NEEDS_KEY
            log.info("adzuna_needs_key", id_saved=bool(app_id), key_saved=bool(app_key))
            return batch
        keyword = (config.get("keyword") or "").strip()
        terms: list[str | None] = [keyword] if keyword else list(_profile_titles()) or [None]
        max_pages = _int_in(config, "max_pages", DEFAULT_MAX_PAGES, 1, MAX_PAGES_LIMIT)
        seen: set[str] = set()
        requests = 0
        for term in terms:
            params = self._params(config, term)
            per_page = int(params["results_per_page"])
            for page in range(1, max_pages + 1):
                if requests >= MAX_REQUESTS:
                    batch.partial_error = batch.partial_error or (
                        "Adzuna: stopped at the limit of requests per check; the rest is "
                        "fetched on the next check"
                    )
                    return batch
                requests += 1
                query = {"app_id": app_id, "app_key": app_key, **params}
                data = await self._get(client, page, query, batch)
                if data is None:  # a 429 or 5xx: keep what is collected, run is partial
                    return batch
                items = data["results"]
                for item in items:
                    posting = to_posting(item)
                    if posting is None or posting.external_id in seen:
                        continue
                    seen.add(posting.external_id or "")
                    batch.append(posting)
                total = data.get("count")
                last = -(-int(total) // per_page) if isinstance(total, int) and total >= 0 else None
                if not items or len(items) < per_page or (last is not None and page >= last):
                    break
        return batch

    async def _get(self, client: Any, page: int, query: dict, batch: PostingBatch) -> dict | None:
        url = f"{API_URL}/{page}?{urlencode(query)}"
        try:
            # retries=1: one attempt. A 429 retried would only be asked to slow down again.
            r = await client.request("GET", url, rate_per_sec=_RATE, retries=1)
        except httpx.HTTPStatusError as exc:  # the client raises this after a 429/403/503
            r = exc.response
        except httpx.HTTPError as exc:
            log.warning("adzuna_unreachable", error=type(exc).__name__)
            raise AdapterError("Could not reach Adzuna; try again later") from exc
        if r.status_code != 200:
            log.warning("adzuna_error", status=r.status_code)
            message = _problem(r.status_code)
            if r.status_code == 429 or r.status_code >= 500:
                batch.partial_error = message
                return None
            raise AdapterError(message)
        try:
            data = r.json()
            if not isinstance(data["results"], list):
                raise TypeError("results")
        except (ValueError, KeyError, TypeError) as exc:
            raise AdapterError(
                "Adzuna sent a reply Kestrel could not read; the format may have changed"
            ) from exc
        return data
