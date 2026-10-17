"""Ashby job-board adapter (SPEC.md Appendix A, verified live 2026-07-30).

GET https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true
    -> {"jobs": [...], "apiVersion": "..."}.

GOTCHAS this adapter handles:
  - The board `token` is CASE SENSITIVE; it is passed through unchanged (never lowercased).
  - `isListed` filtering: a job is skipped when `isListed` is explicitly false. Unknown/missing
    stays in the batch (hard rule 4: uncertainty escalates, it never rejects).
  - `compensation` only populates when `includeCompensation=true` AND the job's
    `shouldDisplayCompensationOnJobPostings` is true. Absence means UNKNOWN, not zero, so comp_*
    are left None. When present it is a structured object (summaryComponents / compensationTiers);
    numeric min/max are extracted best-effort from a Salary component, else left None. Numbers are
    never invented (hard rule 2: the model selects, it never invents).
  - `descriptionPlain` is preferred (-> description_md); else `descriptionHtml`
    (-> description_html, which normalize unescapes + markdownifies).
  - Ashby has no company_name field, so org_name falls back to the config `token` unless the
    config supplies an explicit company name.
"""

from __future__ import annotations

from typing import Any

from kestrel.ingest.base import RawPosting, SourceConfigError

_BASE = "https://api.ashbyhq.com/posting-api/job-board"

# Ashby expresses a pay period as "<n> <UNIT>" (e.g. "1 YEAR"). Only the canonical RawPosting
# intervals are mapped; anything else (WEEK, NONE for equity, ...) leaves comp_interval None.
_INTERVALS = {"YEAR": "year", "MONTH": "month", "DAY": "day", "HOUR": "hour"}


class AshbyAdapter:
    kind = "ashby"

    def validate_config(self, config: dict) -> None:
        if not config.get("token"):
            raise SourceConfigError("ashby source requires a 'token' (the case-sensitive board id)")

    async def fetch(self, config: dict, client: Any) -> list[RawPosting]:
        self.validate_config(config)
        token = config["token"]  # case sensitive: pass through unchanged
        url = f"{_BASE}/{token}?includeCompensation=true"
        data = await client.get_json(url)
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        return [
            self._to_posting(j, config, token)
            for j in jobs
            if isinstance(j, dict) and j.get("isListed") is not False
        ]

    def _to_posting(self, j: dict, config: dict, token: str) -> RawPosting:
        org_name = config.get("company_name") or config.get("company") or token
        plain = _str(j.get("descriptionPlain"))
        html = None if plain else _str(j.get("descriptionHtml"))
        comp_min, comp_max, comp_currency, comp_interval = _compensation(j.get("compensation"))
        return RawPosting(
            external_id=_str(j.get("id")),
            url=j.get("jobUrl"),
            apply_url=j.get("applyUrl"),
            title=j.get("title"),
            org_name=org_name,
            location_raw=_location(j.get("location")),
            description_html=html,
            description_md=plain,  # preferred when present
            employment_type=_str(j.get("employmentType")),
            is_remote=j.get("isRemote") if isinstance(j.get("isRemote"), bool) else None,
            remote_hint=_str(j.get("workplaceType")),
            comp_min=comp_min,
            comp_max=comp_max,
            comp_currency=comp_currency,
            comp_interval=comp_interval,
            date_posted=_str(j.get("publishedAt")),
            raw=j,
        )


def _location(loc: Any) -> str | None:
    if isinstance(loc, str):
        return loc or None
    if isinstance(loc, dict):
        return _str(loc.get("locationName") or loc.get("name"))
    return None


def _compensation(comp: Any) -> tuple[float | None, float | None, str | None, str | None]:
    """Best-effort numeric min/max/currency/interval from an Ashby compensation object.

    Absence (None / empty) yields all-None: unknown, never zero. Prefers a Salary component over
    equity or bonus; falls back to any numeric component so a partial signal is not discarded.
    """
    if not isinstance(comp, dict):
        return None, None, None, None
    components: list[dict] = []
    summary = comp.get("summaryComponents")
    if isinstance(summary, list):
        components.extend(c for c in summary if isinstance(c, dict))
    for tier in comp.get("compensationTiers") or []:
        if isinstance(tier, dict):
            components.extend(c for c in (tier.get("components") or []) if isinstance(c, dict))

    salary = [c for c in components if str(c.get("compensationType") or "").lower() == "salary"]
    for candidate in salary + components:
        cmin = _num(candidate.get("minValue"))
        cmax = _num(candidate.get("maxValue"))
        if cmin is None and cmax is None:
            continue
        currency = _str(candidate.get("currencyCode"))
        interval = _INTERVALS.get(str(candidate.get("interval") or "").strip().split(" ")[-1])
        return cmin, cmax, currency, interval
    return None, None, None, None


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _str(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None
