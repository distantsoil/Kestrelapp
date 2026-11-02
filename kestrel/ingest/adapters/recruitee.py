"""Recruitee company job board adapter (SPEC.md Appendix A, verified live 2026-10-29).

GET https://{slug}.recruitee.com/api/offers/ -> 200 `{"offers": [...]}`, no auth. The whole board
is one response and every offer carries its full HTML description, so there is no detail request.
An unknown slug answers 404 `{"error": "Not Found"}`.

GOTCHAS this adapter handles:
  - Custom careers domains are common (careers.example.com), but the API stays on
    `{slug}.recruitee.com`. The slug is the only thing the adapter needs; a posting's public link
    is its `careers_url` (which is on the custom domain when there is one).
  - `description` and `requirements` are separate HTML fields; the body is both, in that order.
  - `location` is one display string ("Amsterdam, Noord-Holland, Netherlands"); an offer open in
    several places also has `locations[]` (each with `city`, `state`, `country`). All of them are
    joined with "; " so the area rule sees every place the role is open.
  - Remote is three booleans: `remote`, `hybrid` and `on_site`. Only an explicit `remote` or
    `hybrid` sets the hint; all three false leaves it unknown (never guessed as on-site).
  - Only `status` "published" offers are kept (the public list holds nothing else, but a draft
    would not be public).
  - `salary` is `{min, max, period, currency}` with nulls when the employer shows none.
  - `{slug}.recruitee.com/robots.txt` redirects to the careers domain's own file; the shared client
    follows it and respects what it says.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from kestrel.errors import ROBOTS_SENTENCE
from kestrel.ingest.base import AdapterError, PostingBatch, RawPosting, SourceConfigError

_BASE = "https://{slug}.recruitee.com/api/offers/"
_RATE = 1.0  # requests/second; one request per scan
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")
_INTERVALS = {"hour": "hour", "day": "day", "month": "month", "year": "year"}
_REMOTE = "Remote"
_HYBRID = "Hybrid"


class RecruiteeAdapter:
    kind = "recruitee"

    def validate_config(self, config: dict) -> None:
        slug = config.get("slug")
        if not isinstance(slug, str) or not _SLUG.match(slug):
            raise SourceConfigError(
                "recruitee source requires a 'slug' (the name in {slug}.recruitee.com: letters, "
                "digits and '-')"
            )

    async def fetch(self, config: dict, client: Any) -> list[RawPosting]:
        self.validate_config(config)
        slug = config["slug"]
        batch = PostingBatch()
        try:
            data = await client.get_json(
                _BASE.format(slug=slug), rate_per_sec=_RATE, check_robots=True
            )
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 429:
                batch.partial_error = (
                    f"Recruitee is rate limiting requests for {slug}. Kestrel will try again "
                    "next run."
                )
                return batch
            if code == 404:
                raise AdapterError(f"Recruitee board not found: {slug}") from exc
            raise AdapterError(f"Recruitee fetch failed for {slug}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise AdapterError(f"Recruitee fetch failed for {slug}: {exc}") from exc
        except PermissionError as exc:
            raise AdapterError(ROBOTS_SENTENCE) from exc
        except ValueError as exc:
            raise AdapterError(f"Recruitee reply for {slug} was not valid JSON") from exc
        offers = data.get("offers") if isinstance(data, dict) else None
        if not isinstance(offers, list):
            raise AdapterError(f"Recruitee reply for {slug} had an unexpected shape")
        for offer in offers:
            if isinstance(offer, dict) and offer.get("status", "published") == "published":
                posting = self._to_posting(offer, config)
                if posting is not None:
                    batch.append(posting)
        return batch

    def _to_posting(self, o: dict, config: dict) -> RawPosting | None:
        if o.get("id") in (None, ""):
            return None
        slug = config["slug"]
        offer_slug = _strip(o.get("slug"))
        url = _strip(o.get("careers_url")) or (
            f"https://{slug}.recruitee.com/o/{offer_slug}" if offer_slug else None
        )
        hint = None
        if o.get("remote") is True:
            hint = _REMOTE
        elif o.get("hybrid") is True:
            hint = _HYBRID
        salary = o.get("salary") if isinstance(o.get("salary"), dict) else {}
        comp_min, comp_max = _number(salary.get("min")), _number(salary.get("max"))
        has_comp = comp_min is not None or comp_max is not None
        return RawPosting(
            external_id=str(o["id"]),
            url=url,
            apply_url=_strip(o.get("careers_apply_url")),
            title=_strip(o.get("title")),
            org_name=_strip(config.get("company_name")) or _strip(o.get("company_name")) or slug,
            location_raw=_locations(o),
            description_html=_description(o),
            employment_type=_strip(o.get("employment_type_code")),
            is_remote=True if o.get("remote") is True else None,
            remote_hint=hint,
            comp_min=comp_min,
            comp_max=comp_max,
            comp_currency=_strip(salary.get("currency")) if has_comp else None,
            comp_interval=_INTERVALS.get(str(salary.get("period") or "").lower())
            if has_comp
            else None,
            date_posted=_strip(o.get("published_at")) or _strip(o.get("created_at")),
            raw=o,
        )


def _description(o: dict) -> str | None:
    parts = [
        v.strip()
        for k in ("description", "requirements")
        if isinstance(v := o.get(k), str) and v.strip()
    ]
    return "\n".join(parts) or None


def _locations(o: dict) -> str | None:
    labels: list[str] = []
    rows = o.get("locations")
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        parts = [_strip(row.get(k)) for k in ("city", "state", "country")]
        label = ", ".join(p for p in parts if p) or _strip(row.get("name"))
        if label and label not in labels:
            labels.append(label)
    if not labels and (single := _strip(o.get("location"))):
        labels.append(single)
    return "; ".join(labels) or None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", ""))
        except ValueError:
            return None
    return None


def _strip(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None
