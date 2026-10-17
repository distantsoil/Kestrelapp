"""Workday CXS adapter (SPEC.md Appendix A, verified live 2026-07-30 against
`nvidia.wd5.myworkdayjobs.com`, site `NVIDIAExternalCareerSite`).

Workday is the odd one out among the ATS adapters:

  - The job LIST is a **POST**, not a GET:
      POST https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
      body {"limit": 20, "offset": 0, "searchText": "", "appliedFacets": {}}
      -> {"total": <int>, "jobPostings": [...]}
  - `limit` caps at 20, so the board is paginated with `offset` (0, 20, 40, ...) up to the
    reported `total`, bounded by a config `max_results` so a 2000-job board does not run forever.
  - The LIST carries NO description. Each posting's detail is a separate GET:
      GET https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job{externalPath}
    (externalPath already begins with "/"), returning `jobPostingInfo` with the HTML
    `jobDescription`, `startDate`, `timeType`, `jobRequisitionId`, and often `hiringOrganization`.
  - `postedOn` in the LIST is a HUMAN string ("Posted Today"), never a date; date_posted comes
    from the detail's `startDate`.
  - `tenant`, the data-center segment `dc` (wd1/wd3/wd5/...), and `site` all vary per employer and
    are required config, never derived.

Failure model (hard rule: uncertainty escalates, it never aborts the batch): a LIST failure raises
AdapterError (the run becomes `partial`); a single DETAIL failure skips that one job and the rest
of the board still returns.
"""

from __future__ import annotations

from typing import Any

import httpx

from kestrel.ingest.base import AdapterError, RawPosting, SourceConfigError

_PAGE_LIMIT = 20  # Workday hard-caps the list `limit` at 20.
_DEFAULT_MAX_RESULTS = 200
_RATE_PER_SEC = 1.0  # Workday is rate-limited to ~1 request/sec (list and detail alike).
# Appendix A: Workday tenants often 403 a non-browser User-Agent, so send a browser one. Per-request
# headers override the client's descriptive Kestrel UA for Workday requests only.
_HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}
_REQUIRED = ("tenant", "dc", "site")


class WorkdayAdapter:
    kind = "workday"

    def validate_config(self, config: dict) -> None:
        missing = [k for k in _REQUIRED if not config.get(k)]
        if missing:
            raise SourceConfigError(
                "workday source requires " + ", ".join(f"'{k}'" for k in missing)
            )

    async def fetch(self, config: dict, client: Any) -> list[RawPosting]:
        self.validate_config(config)
        tenant = str(config["tenant"])
        dc = str(config["dc"])
        site = str(config["site"])
        company = _str(config.get("company"))
        max_results = int(config.get("max_results") or _DEFAULT_MAX_RESULTS)

        base = f"https://{tenant}.{dc}.myworkdayjobs.com"
        cxs = f"{base}/wday/cxs/{tenant}/{site}"

        listings = await self._fetch_list(client, f"{cxs}/jobs", max_results)

        postings: list[RawPosting] = []
        for item in listings:
            posting = await self._fetch_one(
                client, base=base, cxs=cxs, site=site, item=item, company=company or tenant
            )
            if posting is not None:
                postings.append(posting)
        return postings

    async def _fetch_list(self, client: Any, list_url: str, max_results: int) -> list[dict]:
        """Paginate the POST list endpoint, stopping at `total`, an empty page, or max_results."""
        collected: list[dict] = []
        offset = 0
        while len(collected) < max_results:
            body = {"limit": _PAGE_LIMIT, "offset": offset, "searchText": "", "appliedFacets": {}}
            try:
                data = await client.post_json(
                    list_url, json=body, rate_per_sec=_RATE_PER_SEC, headers=_HEADERS
                )
            except httpx.HTTPError as exc:
                raise AdapterError(f"workday list fetch failed at offset {offset}: {exc}") from exc
            page = data.get("jobPostings", []) if isinstance(data, dict) else []
            page = [p for p in page if isinstance(p, dict)]
            if not page:
                break
            collected.extend(page)
            offset += len(page)
            total = data.get("total") if isinstance(data, dict) else None
            if isinstance(total, int) and offset >= total:
                break
            if len(page) < _PAGE_LIMIT:  # a short page means the board is exhausted.
                break
        return collected[:max_results]

    async def _fetch_one(
        self,
        client: Any,
        *,
        base: str,
        cxs: str,
        site: str,
        item: dict,
        company: str,
    ) -> RawPosting | None:
        external_path = _str(item.get("externalPath")) or ""
        # The real list externalPath already begins with "/job/..."; only prepend "/job" when it
        # does not, so the detail path is never "/job/job/..." (a 404 that would skip every job).
        detail_path = external_path if external_path.startswith("/job") else f"/job{external_path}"
        detail_url = f"{cxs}{detail_path}"
        try:
            detail = await client.get_json(detail_url, rate_per_sec=_RATE_PER_SEC, headers=_HEADERS)
        except httpx.HTTPError:
            return None  # skip just this job; the rest of the board still returns.

        info = detail.get("jobPostingInfo") if isinstance(detail, dict) else None
        if not isinstance(info, dict):
            info = {}

        org = info.get("hiringOrganization")
        if not isinstance(org, dict) and isinstance(detail, dict):
            org = detail.get("hiringOrganization")
        org_name = _str(org.get("name")) if isinstance(org, dict) else None

        bullets = item.get("bulletFields")
        bullet_id = None
        if isinstance(bullets, list) and bullets:
            bullet_id = _str(bullets[0])

        return RawPosting(
            external_id=_str(info.get("jobRequisitionId")) or bullet_id,
            url=f"{base}/{site}{external_path}",  # human-facing URL, not the cxs API URL.
            title=_str(item.get("title")),
            org_name=org_name or company,
            location_raw=_str(item.get("locationsText")),
            description_html=_str(info.get("jobDescription")),
            employment_type=_str(info.get("timeType")),
            # `postedOn` is a human string ("Posted Today"); the real date is the detail startDate.
            date_posted=_str(info.get("startDate")),
            raw={"list": item, "detail": detail},
        )


def _str(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None
