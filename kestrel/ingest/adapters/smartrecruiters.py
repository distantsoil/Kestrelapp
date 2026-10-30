"""SmartRecruiters postings adapter (SPEC.md Appendix A, verified live 2026-07-30).

List:   GET https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100&offset=0
        -> 200, an object {"offset", "limit", "totalFound", "content":[...]}. Verified against
        company=Visa. Paginated with `offset`; capped at config `max_results` (default 200).
Detail: GET https://api.smartrecruiters.com/v1/companies/{company}/postings/{id}
        -> the description, which the LIST does NOT carry.

GOTCHAS this adapter handles:
  - **The job title is `name`, NOT `title`** — the canonical SmartRecruiters integration bug.
  - The LIST has NO description; each posting's body must be fetched from the detail endpoint.
    The detail's `jobAd.sections` holds `companyDescription`, `jobDescription`, `qualifications`,
    and `additionalInformation`, each an object with `title` and `text` (HTML). description_html
    is assembled from jobDescription + qualifications + additionalInformation (in that order,
    newline-joined, missing sections skipped). companyDescription is deliberately excluded.
  - **Company identifiers are CASE SENSITIVE** — the config `company` is passed through unchanged;
    it is never lowercased.
  - `location` is an OBJECT ({"city", "region", "country"}); location_raw joins the present parts
    into "City, Region, Country".
  - `typeOfEmployment` may be an object ({"id", "label"}) or a bare value; employment_type takes
    its `label` when it is an object.
  - On a LIST HTTP error the run degrades to `partial` via AdapterError (never aborts the batch);
    a single failing DETAIL skips only that one posting.
"""

from __future__ import annotations

from typing import Any

import httpx

from kestrel.ingest.base import AdapterError, RawPosting, SourceConfigError

_BASE = "https://api.smartrecruiters.com/v1/companies"
_PAGE = 100  # SmartRecruiters caps the postings page at 100.
_DEFAULT_MAX_RESULTS = 200


class SmartRecruitersAdapter:
    kind = "smartrecruiters"

    def validate_config(self, config: dict) -> None:
        if not config.get("company"):
            raise SourceConfigError(
                "smartrecruiters source requires a 'company' (the SmartRecruiters identifier, "
                "which is CASE SENSITIVE)"
            )

    async def fetch(self, config: dict, client: Any) -> list[RawPosting]:
        self.validate_config(config)
        company = config["company"]  # CASE SENSITIVE — pass through unchanged
        max_results = _positive_int(config.get("max_results"), _DEFAULT_MAX_RESULTS)

        items = await self._fetch_list(company, max_results, client)

        postings: list[RawPosting] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            posting_id = item.get("id")
            if posting_id in (None, ""):
                continue
            detail = await self._fetch_detail(company, str(posting_id), client)
            if detail is None:
                continue  # a single failing DETAIL skips only this posting
            postings.append(self._to_posting(item, detail, company))
        return postings

    async def _fetch_list(self, company: str, max_results: int, client: Any) -> list[dict]:
        collected: list[dict] = []
        offset = 0
        while len(collected) < max_results:
            url = f"{_BASE}/{company}/postings?limit={_PAGE}&offset={offset}"
            try:
                data = await client.get_json(url)
            except httpx.HTTPError as exc:
                raise AdapterError(
                    f"SmartRecruiters list fetch failed for {company}: {exc}"
                ) from exc
            if not isinstance(data, dict):
                break
            content = data.get("content")
            page = [c for c in content if isinstance(c, dict)] if isinstance(content, list) else []
            if not page:
                break
            collected.extend(page)
            offset += _PAGE
            total = data.get("totalFound")
            if isinstance(total, int) and offset >= total:
                break
        return collected[:max_results]

    async def _fetch_detail(self, company: str, posting_id: str, client: Any) -> dict | None:
        """Return the parsed detail body, or None on an HTTP error so the caller skips it.

        A successful-but-empty response becomes {} (not None), so the posting is still emitted
        with no description; None is reserved strictly for a fetch failure."""
        url = f"{_BASE}/{company}/postings/{posting_id}"
        try:
            detail = await client.get_json(url)
        except httpx.HTTPError:
            return None
        return detail if isinstance(detail, dict) else {}

    def _to_posting(self, item: dict, detail: dict, company: str) -> RawPosting:
        return RawPosting(
            external_id=_str(item.get("id")),
            url=_posting_url(detail, item),
            apply_url=_str(detail.get("applyUrl")) or _str(item.get("applyUrl")),
            title=_str(item.get("name")),  # NOT `title` — the SmartRecruiters gotcha
            org_name=_org_name(item.get("company"), company),
            location_raw=_location(item.get("location")),
            # The location object's own remote flags, when the posting carries them, so a remote
            # role is never read as on-site at its city (D-RULES-9 review).
            remote_hint=_remote_hint(item.get("location")),
            description_html=_assemble_description(detail),
            employment_type=_employment_type(item.get("typeOfEmployment")),
            date_posted=_str(item.get("releasedDate")),
            raw={**item, **detail},  # merged list + detail
        )


def _assemble_description(detail: dict) -> str | None:
    """Concatenate jobDescription + qualifications + additionalInformation section texts."""
    job_ad = detail.get("jobAd") if isinstance(detail.get("jobAd"), dict) else {}
    sections = job_ad.get("sections") if isinstance(job_ad.get("sections"), dict) else {}
    parts: list[str] = []
    for key in ("jobDescription", "qualifications", "additionalInformation"):
        section = sections.get(key)
        if isinstance(section, dict):
            text = section.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text)
    return "\n".join(parts) or None


def _remote_hint(location: Any) -> str | None:
    if isinstance(location, dict) and (location.get("remote") is True
                                       or location.get("fullyRemote") is True):
        return "Remote"
    return None


def _location(location: Any) -> str | None:
    """SmartRecruiters `location` is an object; join present city/region/country parts."""
    if isinstance(location, dict):
        parts = [location.get("city"), location.get("region"), location.get("country")]
        joined = ", ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
        return joined or None
    return _str(location)


def _employment_type(value: Any) -> str | None:
    if isinstance(value, dict):
        return _str(value.get("label"))
    return _str(value)


def _org_name(company_field: Any, fallback: str) -> str | None:
    if isinstance(company_field, dict):
        return _str(company_field.get("name")) or fallback
    return _str(company_field) or fallback


def _posting_url(detail: dict, item: dict) -> str | None:
    for source in (detail, item):
        for key in ("applyUrl", "postingUrl"):
            value = _str(source.get(key))
            if value:
                return value
    return None


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if value > 0 else default


def _str(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None
