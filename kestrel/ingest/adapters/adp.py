"""ADP Workforce Now career-center adapter (SPEC.md Appendix A, verified live 2026-10-21).

List:   GET https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/
            job-requisitions?cid={cid}&ccId={ccId}&lang=en_US&locale=en_US&$top=20&$skip=1
        -> 200, {"jobRequisitions": [...], "meta": {"startSequence", "totalNumber", ...}}, no auth.
Detail: GET .../job-requisitions/{itemID}?cid=...  -> the same object plus `requisitionDescription`.

GOTCHAS this adapter handles:
  - Pages are capped at 20 whatever `$top` says, and `$skip` is 1-BASED (1, 21, 41, ...).
    `$skip=0` returns a short page, so it is never sent. Paging stops at `meta.totalNumber`, on
    an empty page, or at config `max_results` (default 200).
  - The title is `requisitionTitle` and carries a trailing space on some rows; it is trimmed.
  - `requisitionLocations` repeats entries and carries blank ones (`nameCode: {}`, empty address
    strings). Blanks are dropped, duplicates merged, and the labels joined with "; ".
  - The LIST has no description; each posting's HTML body comes from the detail. A failing detail
    skips only that posting; a 429 anywhere makes the whole run partial.
  - The payload names no employer, so org_name is the config `company_name`, else the `cid`.
  - Nothing in the payload carries pay or a remote flag: comp_* stay None, and remote is inferred
    only from a location label containing "Remote" (otherwise None, never False).
  - The public posting link needs `ExternalJobID` (customFieldGroup.stringFields); without it the
    link falls back to the career-center page itself.
  - workforcenow.adp.com publishes no robots.txt (it redirects to the sign-in page), so there is
    no rule to follow; requests stay at 1/second and carry the Kestrel User-Agent.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from kestrel.ingest.base import AdapterError, RawPosting, SourceConfigError

_BASE = (
    "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/"
    "job-requisitions"
)
_PAGE_URL = "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
_PAGE = 20  # the server caps a page at 20
_DEFAULT_MAX_RESULTS = 200
_MAX_PAGES = 100  # a bound if a server keeps answering rows without a usable total
_DEFAULT_CC_ID = "19000101_000001"
_DEFAULT_LANG = "en_US"
_RATE = 1.0  # requests/second; one detail request per posting
_CID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_CC_ID = re.compile(r"^[A-Za-z0-9_]{1,40}$")
_LANG = re.compile(r"^[a-z]{2}_[A-Z]{2}$")


class AdpAdapter:
    kind = "adp"

    def validate_config(self, config: dict) -> None:
        cid = config.get("cid")
        if not isinstance(cid, str) or not _CID.match(cid):
            raise SourceConfigError(
                "adp source requires a 'cid' (the employer UUID in the career-center link: "
                "workforcenow.adp.com/.../recruitment.html?cid={cid})"
            )
        cc_id = config.get("cc_id")
        if cc_id is not None and not (isinstance(cc_id, str) and _CC_ID.match(cc_id)):
            raise SourceConfigError("adp 'cc_id' must be the ccId from the career-center link")
        lang = config.get("lang")
        if lang is not None and not (isinstance(lang, str) and _LANG.match(lang)):
            raise SourceConfigError("adp 'lang' must look like en_US")
        max_results = config.get("max_results")
        if max_results is not None and (
            isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1
        ):
            raise SourceConfigError("adp 'max_results' must be a positive whole number")

    async def fetch(self, config: dict, client: Any) -> list[RawPosting]:
        self.validate_config(config)
        cid = config["cid"]
        cc_id = config.get("cc_id") or _DEFAULT_CC_ID
        lang = config.get("lang") or _DEFAULT_LANG
        max_results = config.get("max_results") or _DEFAULT_MAX_RESULTS
        ident = f"cid={cid}&ccId={cc_id}&lang={lang}&locale={lang}"

        items = await self._fetch_list(ident, cid, max_results, client)
        postings: list[RawPosting] = []
        for item in items:
            detail = await self._fetch_detail(ident, cid, str(item["itemID"]), client)
            if detail is None:
                continue  # a single failing DETAIL skips only this posting
            postings.append(self._to_posting(item, detail, config, cc_id, lang))
        return postings

    async def _fetch_list(self, ident: str, cid: str, max_results: int, client: Any) -> list[dict]:
        collected: list[dict] = []
        skip = 1  # 1-based
        for _ in range(_MAX_PAGES):
            if len(collected) >= max_results:
                break
            url = f"{_BASE}?{ident}&$top={_PAGE}&$skip={skip}"
            try:
                data = await client.get_json(url, rate_per_sec=_RATE)
            except httpx.HTTPError as exc:
                raise _failure("list", cid, exc) from exc
            except ValueError as exc:
                raise AdapterError(f"ADP list for {cid} was not valid JSON") from exc
            rows = data.get("jobRequisitions") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                raise AdapterError(f"ADP list for {cid} had an unexpected shape")
            page = [r for r in rows if isinstance(r, dict) and r.get("itemID")]
            if not rows:
                break
            collected.extend(page)
            skip += _PAGE
            meta = data.get("meta")
            total = meta.get("totalNumber") if isinstance(meta, dict) else None
            if isinstance(total, int) and not isinstance(total, bool) and skip > total:
                break
        return collected[:max_results]

    async def _fetch_detail(self, ident: str, cid: str, item_id: str, client: Any) -> dict | None:
        """The parsed detail, or None on a non-429 failure so the caller skips the posting."""
        try:
            detail = await client.get_json(f"{_BASE}/{item_id}?{ident}", rate_per_sec=_RATE)
        except httpx.HTTPError as exc:
            if _is_429(exc):
                raise _failure("detail", cid, exc) from exc
            return None
        except ValueError:
            return None
        return detail if isinstance(detail, dict) else {}

    def _to_posting(
        self, item: dict, detail: dict, config: dict, cc_id: str, lang: str
    ) -> RawPosting:
        locations = _locations(
            item.get("requisitionLocations") or detail.get("requisitionLocations")
        )
        remote = any("remote" in loc.lower() for loc in locations)
        ext_job_id = _external_job_id(item) or _external_job_id(detail)
        return RawPosting(
            external_id=str(item["itemID"]),
            url=_posting_url(config["cid"], cc_id, lang, ext_job_id),
            title=_strip(item.get("requisitionTitle")) or _strip(detail.get("requisitionTitle")),
            org_name=_strip(config.get("company_name")) or config["cid"],
            location_raw="; ".join(locations) or None,
            description_html=_strip(detail.get("requisitionDescription")),
            employment_type=_level(item.get("workLevelCode") or detail.get("workLevelCode")),
            is_remote=True if remote else None,
            remote_hint="Remote" if remote else None,
            date_posted=_strip(item.get("postDate")) or _strip(detail.get("postDate")),
            raw={**item, **detail},
        )


def _is_429(exc: httpx.HTTPError) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429


def _failure(part: str, cid: str, exc: httpx.HTTPError) -> AdapterError:
    if _is_429(exc):
        return AdapterError(f"ADP rate limit reached while reading {part} for {cid}")
    return AdapterError(f"ADP {part} fetch failed for {cid}: {exc}")


def _locations(entries: Any) -> list[str]:
    """Distinct non-blank location labels, first-seen order."""
    out: list[str] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("nameCode")
        label = _strip(name.get("shortName")) if isinstance(name, dict) else None
        if not label:
            address = entry.get("address") if isinstance(entry.get("address"), dict) else {}
            sub = address.get("countrySubdivisionLevel1")
            state = _strip(sub.get("codeValue")) if isinstance(sub, dict) else None
            label = ", ".join(p for p in (_strip(address.get("cityName")), state) if p) or None
        if label and label not in out:
            out.append(label)
    return out


def _external_job_id(source: dict) -> str | None:
    group = source.get("customFieldGroup")
    for field in (group.get("stringFields") if isinstance(group, dict) else None) or []:
        name = field.get("nameCode") if isinstance(field, dict) else None
        if isinstance(name, dict) and name.get("codeValue") == "ExternalJobID":
            return _strip(field.get("stringValue"))
    return None


def _posting_url(cid: str, cc_id: str, lang: str, ext_job_id: str | None) -> str:
    job = f"&jobId={ext_job_id}" if ext_job_id else ""
    return f"{_PAGE_URL}?cid={cid}&ccId={cc_id}{job}&lang={lang}"


def _level(value: Any) -> str | None:
    return _strip(value.get("shortName")) if isinstance(value, dict) else None


def _strip(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None
