"""Read one posting from an employer's own public page (SPEC.md 5.1, D-SRC-14).

Only structured data the employer publishes for search engines is read: a schema.org `JobPosting`
object in a `<script type="application/ld+json">` block (the same data Workday and Paylocity
postings carry). A page without one yields None; nothing is scraped from the visible page and no
model is involved. Pure functions, no network."""

from __future__ import annotations

import json
import re
from typing import Any

from kestrel.ingest.base import RawPosting

_LD_BLOCK = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.I | re.S
)
MAX_PAGE_BYTES = 2_000_000


def _walk(node: Any):
    """Every dict inside a JSON-LD value (lists and @graph included)."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            if isinstance(v, (dict, list)):
                yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _is_job(obj: dict) -> bool:
    kind = obj.get("@type")
    return kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind)


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _place(loc: Any) -> str | None:
    places = loc if isinstance(loc, list) else [loc]
    parts: list[str] = []
    for p in places:
        addr = p.get("address") if isinstance(p, dict) else None
        if isinstance(addr, dict):
            bits = [_text(addr.get(k)) for k in ("addressLocality", "addressRegion",
                                                 "addressCountry")]
            line = ", ".join(b for b in bits if b)
            if line and line not in parts:
                parts.append(line)
    return "; ".join(parts) or None


def parse_job_posting(html: str, url: str) -> RawPosting | None:
    """The posting an employer page publishes, or None when it publishes none Kestrel can use
    (no JobPosting block, or one missing its title or description)."""
    if not html or len(html) > MAX_PAGE_BYTES:
        return None
    for block in _LD_BLOCK.findall(html):
        try:
            data = json.loads(block.strip())
            objs = list(_walk(data))
        except (ValueError, RecursionError):  # malformed, or nested deeper than Python allows
            continue
        for obj in objs:
            if not _is_job(obj):
                continue
            title, body = _text(obj.get("title")), _text(obj.get("description"))
            if not title or not body:
                continue
            org = obj.get("hiringOrganization")
            employment = obj.get("employmentType")
            if isinstance(employment, list):
                employment = employment[0] if employment else None
            posted = _text(obj.get("datePosted"))
            return RawPosting(
                external_id=url,
                url=url,
                apply_url=url,
                title=title,
                org_name=_text(org.get("name")) if isinstance(org, dict) else _text(org),
                location_raw=_place(obj.get("jobLocation")),
                description_html=body,
                employment_type=_text(employment),
                is_remote=True if obj.get("jobLocationType") == "TELECOMMUTE" else None,
                remote_hint="Remote" if obj.get("jobLocationType") == "TELECOMMUTE" else None,
                date_posted=posted[:10] if posted else None,
            )
    return None
