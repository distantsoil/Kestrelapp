"""USAJobs search board (SPEC.md 5.1 and Appendix A, D-SRC-15). The reference for keyed search
boards: Adzuna and the others copy this shape.

`GET https://data.usajobs.gov/api/search` with two headers the operator's own free key supplies:
`Authorization-Key` (the key) and `User-Agent` (the email address the key was requested with,
which USAJobs requires in place of a product name). One request per search term and page.

What the operator provides: an API key (saved in the app, stored in `.env` like an online-model
key, never in the config row) and the email. The email is not a secret; it rides in the source's
`contact_email` (the way the SEC source carries its contact in `user_agent`).

Rules that bind this module:
  - The key comes from `.env` through `api_key_env` (default USAJOBS_API_KEY) and appears in no log
    line, exception text or message.
  - No key or email is a `partial` run with a plain sentence, never a failure and never an abort.
    A 429 or 5xx keeps the pages already collected and is also `partial`. A rejected key (401 or
    403) is an AdapterError in Kestrel's own words. The provider's body is never shown.
  - Etiquette: 2 requests a second at most (their limit is not published; far under any typical
    one), at most MAX_REQUESTS requests a run, robots.txt checked, 429 never retried.
  - Search terms: the source's `keyword`, else the employment profile's target titles (the same
    titles the generated JobSpy searches use), one search each, up to MAX_TITLES.
  - Remote and telework stay unknown unless the API says: `RemoteIndicator` true is remote,
    `TeleworkEligible` true is hybrid, both explicitly false is on-site, anything else is unknown.
"""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from kestrel.errors import ROBOTS_SENTENCE
from kestrel.ingest.base import AdapterError, PostingBatch, RawPosting, SourceConfigError
from kestrel.pipeline.profile import load_profile

log = structlog.get_logger(__name__)

API_URL = "https://data.usajobs.gov/api/search"
DEFAULT_KEY_ENV = "USAJOBS_API_KEY"
KEY_SIGNUP_URL = "https://developer.usajobs.gov/apirequest/"
DEFAULT_MAX_PAGES = 3
MAX_PAGES_LIMIT = 10
DEFAULT_PER_PAGE = 100
MAX_PER_PAGE = 500
DEFAULT_DAYS = 7
MAX_DAYS = 60
MAX_TITLES = 5
MAX_REQUESTS = 12  # across all search terms in one run
_RATE = 2.0
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MAX_EMAIL_LENGTH = 254  # the longest address SMTP allows (RFC 5321)


def looks_like_email(value: str) -> bool:
    """True for text shaped like name@host.tld: no whitespace, exactly one "@", a name before it,
    and a dot inside the host with a character on each side. Plain string operations, so the time
    is linear in the length; the regular expression it replaces backtracked polynomially on a
    long hostile string (code scanning, "Polynomial regular expression"). The length cap also
    stops a pasted megabyte from being scanned at all."""
    if not value or len(value) > MAX_EMAIL_LENGTH or "".join(value.split()) != value:
        return False
    name, _, host = value.partition("@")
    return bool(name) and "@" not in host and "." in host[1:-1]


NEEDS_KEY = (
    "USAJobs needs your API key and email. Add them on the Sources page under Sources that "
    "need your own key"
)
KEY_REJECTED = (
    "USAJobs did not accept your API key and email. Save them again on the Sources page "
    "under Sources that need your own key"
)
# Both sentences above hold this phrase; the Sources card reads it to say "Needs you".
KEY_PROBLEM_PHRASE = "API key and email"

_INTERVAL = {"PA": "year", "PH": "hour", "PM": "month", "PW": "week", "PD": "day"}
_NO_PLACE = {"", "remote", "anywhere", "united states", "usa", "us", "u.s."}


def is_key_problem(error: str | None) -> bool:
    """True when a run's error text is the missing-key or rejected-key sentence."""
    return bool(error) and KEY_PROBLEM_PHRASE in str(error)


def key_env(config: dict) -> str:
    return config.get("api_key_env") or DEFAULT_KEY_ENV


# ------------------------------------------------------------------------------ the search terms
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
    s = str(val).strip()
    return s or None


def _num(val: Any) -> float | None:
    try:
        n = float(str(val).replace(",", "")) if val not in (None, "") else None
    except ValueError:
        return None
    return n if n and n > 0 else None  # 0 means "not stated" (volunteer or fee-basis roles)


def _as_html(val: Any) -> str:
    """A description field as HTML: USAJobs sends text, HTML, or a list of either."""
    parts = val if isinstance(val, list) else [val]
    out = []
    for part in parts:
        t = _text(part)
        if not t:
            continue
        out.append(t if "<" in t else f"<p>{html.escape(t).replace(chr(10), '<br>')}</p>")
    return "".join(out)


def _description(desc: dict, details: dict) -> str | None:
    close = _text(desc.get("ApplicationCloseDate"))
    sections = [
        ("Summary", details.get("JobSummary")),
        ("Duties", details.get("MajorDuties")),
        ("Qualifications", desc.get("QualificationSummary")),
        ("Requirements", details.get("Requirements")),
        ("Education", details.get("Education")),
        ("Benefits", details.get("Benefits")),
    ]
    body = "".join(
        f"<h3>{label}</h3>{h}" for label, v in sections if (h := _as_html(v))
    )
    if close:
        body = f"<p>Closing date: {html.escape(close[:10])}</p>" + body
    return body or None


def _location(desc: dict) -> str | None:
    """One place when the posting has one; else the display text USAJobs supplies (for example
    "Multiple Locations"), so the D-SRC-13 area check never reads a joined list as one place."""
    places = [p for p in (desc.get("PositionLocation") or []) if isinstance(p, dict)]
    names = [n for n in (_text(p.get("LocationName")) for p in places) if n]
    if len(names) == 1:
        return names[0]
    return _text(desc.get("PositionLocationDisplay")) or ("; ".join(names[:3]) or None)


def _remote(details: dict) -> tuple[bool | None, str | None]:
    """(is_remote, hint). Unknown stays None; only an explicit answer sets either."""
    remote, telework = details.get("RemoteIndicator"), details.get("TeleworkEligible")
    if remote is True:
        return True, "Remote"
    if telework is True:
        return None, "Hybrid"
    if remote is False and telework is False:
        return False, "On-site"
    return None, None


def _pay(desc: dict) -> tuple[float | None, float | None, str | None, str | None]:
    for row in desc.get("PositionRemuneration") or []:
        if not isinstance(row, dict):
            continue
        interval = _INTERVAL.get(str(row.get("RateIntervalCode") or "").upper())
        lo, hi = _num(row.get("MinimumRange")), _num(row.get("MaximumRange"))
        if interval and (lo or hi):
            return lo, hi or lo, "USD", interval
    return None, None, None, None


def _schedule(desc: dict) -> str | None:
    names = [
        _text(x.get("Name")) for x in (desc.get("PositionSchedule") or []) if isinstance(x, dict)
    ]
    return ", ".join(n for n in names if n) or None


def to_posting(item: dict) -> RawPosting | None:
    """One `SearchResultItems` entry as a RawPosting; None when it has no title and no link."""
    desc = item.get("MatchedObjectDescriptor") if isinstance(item, dict) else None
    if not isinstance(desc, dict):
        return None
    details = (desc.get("UserArea") or {}).get("Details") or {}
    title = _text(desc.get("PositionTitle"))
    url = _text(desc.get("PositionURI"))
    if not title and not url:
        return None
    applies = [u for u in (desc.get("ApplyURI") or []) if isinstance(u, str) and u.strip()]
    is_remote, hint = _remote(details)
    lo, hi, cur, interval = _pay(desc)
    return RawPosting(
        external_id=_text(item.get("MatchedObjectId")) or _text(desc.get("PositionID")) or url,
        url=url,
        apply_url=applies[0].strip() if applies else url,
        title=title,
        org_name=_text(desc.get("OrganizationName")) or _text(desc.get("DepartmentName")),
        location_raw=_location(desc),
        description_html=_description(desc, details),
        employment_type=_schedule(desc),
        is_remote=is_remote,
        remote_hint=hint,
        comp_min=lo, comp_max=hi, comp_currency=cur, comp_interval=interval,
        date_posted=_text(desc.get("PublicationStartDate")),
        raw={
            "position_id": _text(desc.get("PositionID")),
            "department": _text(desc.get("DepartmentName")),
            "closes": _text(desc.get("ApplicationCloseDate")),
        },
    )


# ------------------------------------------------------------------------------ the adapter
def _problem(status: int) -> str:
    """Kestrel's own sentence for an error status; the provider's body is never read or shown."""
    if status in (401, 403):
        return KEY_REJECTED
    if status == 429:
        return "USAJobs asked Kestrel to slow down; the rest is fetched on the next check"
    if status in (400, 404, 413):
        return "USAJobs rejected the search. Check the search words and the place"
    if status >= 500:
        return "USAJobs had a problem on their side; the rest is fetched on the next check"
    return f"USAJobs answered with an unexpected status ({status})"


class UsaJobsAdapter:
    kind = "usajobs"

    def validate_config(self, config: dict) -> None:
        if "api_key" in config:
            raise SourceConfigError(
                "Never put the key in the source. Save it on the Sources page under Sources that "
                "need your own key"
            )
        env = config.get("api_key_env", DEFAULT_KEY_ENV)
        if not isinstance(env, str) or not _ENV_NAME.match(env):
            raise SourceConfigError("'api_key_env' must be an environment variable name")
        for name in ("keyword", "location", "contact_email"):
            if config.get(name) is not None and not isinstance(config[name], str):
                raise SourceConfigError(f"'{name}' must be text")
        email = (config.get("contact_email") or "").strip()
        if email and not looks_like_email(email):
            raise SourceConfigError("'contact_email' must be an email address")
        if "remote_only" in config and not isinstance(config["remote_only"], bool):
            raise SourceConfigError("'remote_only' must be true or false")
        _int_in(config, "max_pages", DEFAULT_MAX_PAGES, 1, MAX_PAGES_LIMIT)
        _int_in(config, "results_per_page", DEFAULT_PER_PAGE, 1, MAX_PER_PAGE)
        _int_in(config, "date_posted_days", DEFAULT_DAYS, 1, MAX_DAYS)

    def _params(self, config: dict, term: str | None) -> dict[str, str]:
        params = {
            "ResultsPerPage": str(config.get("results_per_page", DEFAULT_PER_PAGE)),
            "DatePosted": str(config.get("date_posted_days", DEFAULT_DAYS)),
            "WhoMayApply": "Public",
            "Fields": "Full",
        }
        if term:
            params["Keyword"] = term
        place = (config.get("location") or "").strip()
        if config.get("remote_only") or place.lower() == "remote":
            params["RemoteIndicator"] = "True"
        elif place.lower() not in _NO_PLACE:
            params["LocationName"] = place
        return params

    async def fetch(self, config: dict, client: Any) -> PostingBatch:
        self.validate_config(config)
        from kestrel.config import get_secret

        batch = PostingBatch()
        key = get_secret(key_env(config))
        email = (config.get("contact_email") or "").strip()
        if not key or not email:
            batch.partial_error = NEEDS_KEY
            log.info("usajobs_needs_key", key_saved=bool(key), email_saved=bool(email))
            return batch
        headers = {"Authorization-Key": key, "User-Agent": email}
        keyword = (config.get("keyword") or "").strip()
        terms: list[str | None] = [keyword] if keyword else list(_profile_titles()) or [None]
        max_pages = _int_in(config, "max_pages", DEFAULT_MAX_PAGES, 1, MAX_PAGES_LIMIT)
        seen: set[str] = set()
        requests = 0
        for term in terms:
            params = self._params(config, term)
            for page in range(1, max_pages + 1):
                if requests >= MAX_REQUESTS:
                    batch.partial_error = batch.partial_error or (
                        "USAJobs: stopped at the limit of requests per check; the rest is "
                        "fetched on the next check"
                    )
                    return batch
                requests += 1
                data = await self._get(client, headers, {**params, "Page": str(page)}, batch)
                if data is None:  # a 429 or 5xx: keep what is collected, run is partial
                    return batch
                result = data["SearchResult"]
                items = result.get("SearchResultItems") or []
                for item in items:
                    posting = to_posting(item)
                    if posting is None or posting.external_id in seen:
                        continue
                    seen.add(posting.external_id or "")
                    batch.append(posting)
                pages = (result.get("UserArea") or {}).get("NumberOfPages")
                try:
                    last = int(pages)
                except (TypeError, ValueError):
                    last = page if len(items) < int(params["ResultsPerPage"]) else page + 1
                if not items or page >= last:
                    break
        return batch

    async def _get(
        self, client: Any, headers: dict, params: dict, batch: PostingBatch
    ) -> dict | None:
        try:
            # retries=1: one attempt. A 429 retried would only be asked to slow down again.
            r = await client.request(
                "GET", f"{API_URL}?{urlencode(params)}", headers=headers, rate_per_sec=_RATE,
                retries=1, check_robots=True,
            )
        except httpx.HTTPStatusError as exc:  # the client raises this after a 429/403/503
            r = exc.response
        except PermissionError as exc:
            raise AdapterError(ROBOTS_SENTENCE) from exc
        except httpx.HTTPError as exc:
            log.warning("usajobs_unreachable", error=type(exc).__name__)
            raise AdapterError("Could not reach USAJobs; try again later") from exc
        if r.status_code != 200:
            log.warning("usajobs_error", status=r.status_code)
            message = _problem(r.status_code)
            if r.status_code == 429 or r.status_code >= 500:
                batch.partial_error = message
                return None
            raise AdapterError(message)
        try:
            data = r.json()
            if not isinstance(data["SearchResult"], dict):
                raise TypeError("SearchResult")
        except (ValueError, KeyError, TypeError) as exc:
            raise AdapterError(
                "USAJobs sent a reply Kestrel could not read; the format may have changed"
            ) from exc
        return data
