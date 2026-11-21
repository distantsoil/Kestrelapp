"""JobSpy adapter (SPEC.md 5.1). JobSpy is SYNCHRONOUS/requests-based, so the scrape runs in
asyncio.to_thread() to keep the event loop free. The Indeed parameter-conflict rule and the
refusal of LinkedIn (D-SRC-5) are enforced at CONFIG LOAD (validate_config), refusing with a
clear message rather than being discovered at scan time."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from kestrel.ingest.base import (
    AdapterError,
    Notice,
    NoticeKind,
    PostingBatch,
    RawPosting,
    SourceConfigError,
    hedged_refusal,
    rate_limited,
    server_error,
)

log = structlog.get_logger()

# Parameters JobSpy's scrape_jobs accepts; anything else in config is ignored.
_JOBSPY_PARAMS = {
    "site_name",
    "search_term",
    "google_search_term",
    "location",
    "distance",
    "results_wanted",
    "hours_old",
    "country_indeed",
    "description_format",
    "job_type",
    "is_remote",
    "easy_apply",
    "enforce_annual_salary",
    "offset",
    "verbose",
    "fetch_description",
}

_INTERVAL = {
    "yearly": "year",
    "annual": "year",
    "monthly": "month",
    "weekly": "week",
    "daily": "day",
    "hourly": "hour",
}

_INDEED_CONFLICT = (
    "Indeed permits only ONE of: hours_old, (job_type and/or is_remote), or easy_apply per search "
    "(SPEC.md 5.1). Split this into separate sources."
)


LINKEDIN_REFUSED = (
    "LinkedIn is not searched: LinkedIn does not allow automated access (D-SRC-5). Paste a "
    "LinkedIn posting by hand with Add a role I found elsewhere."
)


# A board's name in a stored sentence (a partial run's reason).
_BOARD_NAMES = {"glassdoor": "Glassdoor", "indeed": "Indeed", "zip_recruiter": "ZipRecruiter",
                "google": "Google", "bayt": "Bayt", "naukri": "Naukri", "bdjobs": "BDJobs"}

# Kestrel's default boards. JobSpy reads a missing or None `site_name` as EVERY site it knows,
# LinkedIn included, so Kestrel never passes one: a source with no boards named asks these
# (release round review, finding 2). LinkedIn is never among them (D-SRC-5).
DEFAULT_SITES: tuple[str, ...] = ("indeed", "glassdoor")

# Set in a source's config when LinkedIn was its only board and was taken out: the source stays
# off, and the Sources row says why and offers the default boards, until the operator picks them.
NO_BOARDS_KEY = "no_boards"
NO_BOARDS_REFUSED = (
    "This search named only LinkedIn, which Kestrel does not search, so it has no job boards "
    "left. Choose Use Kestrel's default boards (Indeed and Glassdoor) to turn it back on."
)


def _raw_sites(config: dict) -> list[str]:
    sites = config.get("site_name") or []
    if isinstance(sites, str):
        sites = [sites]
    return [str(s).strip().lower() for s in sites if str(s).strip()]


def _sites(config: dict) -> list[str]:
    """The explicit board list sent to JobSpy: never empty, never None, never LinkedIn."""
    sites = [s for s in _raw_sites(config) if s != "linkedin"]
    return sites or list(DEFAULT_SITES)


# Boards whose JobSpy scraper reads the pay period AND the currency from structured fields of the
# board's own reply (checked in python-jobspy 1.3.0: indeed/util.get_compensation, unitOfWork and
# currencyCode; glassdoor _parse_salary, payPeriod and payCurrency; ziprecruiter _parse_salary,
# interval and currency). Every other board has them filled in: Bayt reads a bare "$" as USD and
# sets every period to monthly, Naukri sets every salary to yearly and a stipend to INR, BDJobs
# sets monthly BDT. Those stay assumed unless proven and added here (round 6, D-SCORE-31).
_STATED_PAY_BOARDS = frozenset({"indeed", "glassdoor", "zip_recruiter"})
def _board_notice(board: str, exc: BaseException) -> Notice:
    """The notice for one board's failed call, from the shared sentences. The kind comes from
    the HTTP status on the exception's response, never its text: a 429 is rate limiting, a 5xx
    a problem on the site's side. JobSpy mostly logs a bad status and returns no rows, and what
    it raises carries no status, so anything else is the hedged sentence (rate limiting or
    unavailable) under the general kind, because its cause is not proven."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429:
        return rate_limited(board)
    if isinstance(status, int) and 500 <= status < 600:
        return server_error(board)
    return Notice(NoticeKind.STOPPED, hedged_refusal(board))


class JobSpyAdapter:
    kind = "jobspy"

    def validate_config(self, config: dict) -> None:
        if not config.get("search_term") and not config.get("google_search_term"):
            raise SourceConfigError(
                "jobspy source requires a 'search_term' or 'google_search_term'"
            )
        if config.get(NO_BOARDS_KEY):
            raise SourceConfigError(NO_BOARDS_REFUSED)
        if "linkedin" in _raw_sites(config):
            raise SourceConfigError(LINKEDIN_REFUSED)
        sites = _sites(config)
        if "indeed" in sites:
            groups = [
                config.get("hours_old") is not None,
                config.get("job_type") is not None or config.get("is_remote") is not None,
                bool(config.get("easy_apply")),
            ]
            if sum(1 for present in groups if present) > 1:
                raise SourceConfigError(_INDEED_CONFLICT)

    def _params(self, config: dict) -> dict:
        params = {k: v for k, v in config.items() if k in _JOBSPY_PARAMS}
        params["site_name"] = _sites(config)  # always explicit: None would mean every site
        params.setdefault("description_format", "markdown")
        return params

    def _calls(self, config: dict) -> list[dict]:
        """The scrape_jobs calls for one search. JobSpy 1.3.0 fetches a Glassdoor posting's body
        only when asked (its default is off), so without the flag every Glassdoor role arrived
        with no description and could not be scored (issue 196). The flag applies to every
        board in a call, and other boards that honor it (ZipRecruiter, Bayt, BDJobs, HelloWork,
        Naukri) fetch one more page per posting, so Glassdoor gets a call of its own with the
        flag and the other boards keep theirs without it (D-WORKER-10 amended). Glassdoor sends
        the bodies in one request per 25 postings. A source that sets the flag itself is sent
        as one call, exactly as it asks."""
        params = self._params(config)
        sites = params["site_name"]
        if "fetch_description" in params or "glassdoor" not in sites:
            return [params]
        glassdoor = {**params, "site_name": ["glassdoor"], "fetch_description": True}
        others = [s for s in sites if s != "glassdoor"]
        return ([{**params, "site_name": others}] if others else []) + [glassdoor]

    async def fetch(self, config: dict, client: Any = None) -> list[RawPosting]:
        self.validate_config(config)
        try:
            import jobspy
        except ImportError as exc:  # a broken install fails the source cleanly, as on the base
            log.warning("jobspy_scrape_failed", error_type=type(exc).__name__, error=str(exc))
            raise AdapterError(f"jobspy scrape failed: {exc}") from exc

        out = PostingBatch()
        failed: list[tuple[list[str], Exception]] = []
        calls = self._calls(config)
        for params in calls:
            try:
                df = await asyncio.to_thread(jobspy.scrape_jobs, **params)
            except Exception as exc:  # noqa: BLE001 - JobSpy raises any type per board
                # A board's 429 etc.: logged with context and recovered as a partial run (or the
                # source's AdapterError when every call failed), never an aborted batch.
                # The class name left the stored sentence (D-SEC-4); keep it for the log.
                log.warning("jobspy_scrape_failed", error_type=type(exc).__name__,
                            error=str(exc), sites=params["site_name"])
                failed.append((params["site_name"], exc))
                continue
            if df is not None and len(df):
                out.extend(self._row_to_posting(row) for _, row in df.iterrows())
        if failed and len(failed) == len(calls):
            exc = failed[0][1]
            raise AdapterError(f"jobspy scrape failed: {exc}") from exc
        for sites, exc in failed:
            # One board's call failed and the other's did not: what was found is kept and the
            # run is partial (a notice), never a failed source.
            out.add(_board_notice(" and ".join(_BOARD_NAMES.get(s, s) for s in sites), exc))
        return out

    def _row_to_posting(self, row: Any) -> RawPosting:
        interval = _INTERVAL.get(str(_v(row, "interval") or "").lower())
        # The period and currency count as stated only when JobSpy read them from a structured
        # field of a board on the allowlist below, never from the description (round 6).
        assumed = (str(_v(row, "salary_source") or "").lower() == "description"
                   or str(_v(row, "site") or "").lower() not in _STATED_PAY_BOARDS)
        desc = _v(row, "description")
        fmt = _v(row, "description_format")
        return RawPosting(
            external_id=_str(_v(row, "id") or _v(row, "job_url")),
            url=_str(_v(row, "job_url")),
            apply_url=_str(_v(row, "job_url_direct")),
            title=_str(_v(row, "title")),
            org_name=_str(_v(row, "company")),
            location_raw=_str(_v(row, "location")),
            description_md=_str(desc) if fmt != "html" else None,
            description_html=_str(desc) if fmt == "html" else None,
            employment_type=_str(_v(row, "job_type")),
            is_remote=_bool(_v(row, "is_remote")),
            comp_min=_num(_v(row, "min_amount")),
            comp_max=_num(_v(row, "max_amount")),
            comp_currency=_str(_v(row, "currency")),
            comp_interval=interval,
            # Pay JobSpy read from the description has its period guessed from the size of the
            # figure and USD filled in (jobspy util.extract_salary), and boards off the allowlist
            # fill them in too: neither was stated (#208).
            comp_interval_assumed=assumed,
            comp_currency_assumed=assumed,
            # Glassdoor's pay is its p10-p90 estimate (jobspy glassdoor _parse_salary), never
            # a figure the employer posted.
            comp_is_estimate=str(_v(row, "site") or "").lower() == "glassdoor",
            date_posted=_str(_v(row, "date_posted")),
            # Indeed exposes these three via JobSpy (SPEC.md 5.4).
            size_band=_str(_v(row, "company_num_employees") or _v(row, "company_employees_label")),
            raw={"site": _str(_v(row, "site"))},
        )


def _v(row: Any, key: str) -> Any:
    try:
        val = row[key] if key in row else None
    except (KeyError, TypeError):
        return None
    try:
        import pandas as pd

        if pd.isna(val):
            return None
    except (TypeError, ValueError, ImportError):
        pass
    return val


def _str(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def _num(val: Any) -> float | None:
    try:
        return float(val) if val is not None and str(val) != "" else None
    except (TypeError, ValueError):
        return None


def _bool(val: Any) -> bool | None:
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("true", "1", "yes")
