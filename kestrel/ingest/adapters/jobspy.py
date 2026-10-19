"""JobSpy adapter (SPEC.md 5.1). JobSpy is SYNCHRONOUS/requests-based, so the scrape runs in
asyncio.to_thread() to keep the event loop free. The Indeed parameter-conflict rule and the
refusal of LinkedIn (D-SRC-5) are enforced at CONFIG LOAD (validate_config), refusing with a
clear message rather than being discovered at scan time."""

from __future__ import annotations

import asyncio
from typing import Any

from kestrel.ingest.base import AdapterError, RawPosting, SourceConfigError

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

    async def fetch(self, config: dict, client: Any = None) -> list[RawPosting]:
        self.validate_config(config)
        try:
            import jobspy

            df = await asyncio.to_thread(jobspy.scrape_jobs, **self._params(config))
        except Exception as exc:  # a board's 429 etc. -> partial run, never abort the batch
            raise AdapterError(f"jobspy scrape failed: {type(exc).__name__}: {exc}") from exc
        if df is None or len(df) == 0:
            return []
        return [self._row_to_posting(row) for _, row in df.iterrows()]

    def _row_to_posting(self, row: Any) -> RawPosting:
        interval = _INTERVAL.get(str(_v(row, "interval") or "").lower())
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
