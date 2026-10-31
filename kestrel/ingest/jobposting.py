"""schema.org JobPosting data in a page, read with a real HTML parser (B4, D-SRC-15).

A careers page that wants search engines to list its roles embeds each one as JSON-LD:
`<script type="application/ld+json">` holding a `JobPosting`, alone, in a list, in an `@graph`
container, or inside an `ItemList`. This module finds those blocks with `html.parser` (never a
regular expression over the page), parses them with `json`, and maps every JobPosting to a
`RawPosting`. It also lists the links a page offers, so a list page that only links to its job
pages can be followed one level. Pure functions, no network, no model."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

from kestrel.ingest.base import RawPosting

MAX_NODES = 5000  # JSON-LD dicts examined per page; a page with more is not a careers list
_INTERVALS = {"HOUR": "hour", "DAY": "day", "MONTH": "month", "YEAR": "year"}
_WEEKS_PER_YEAR = 52


class _PageScan(HTMLParser):
    """The JSON-LD script bodies and the link targets of one page, in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.links: list[str] = []
        self._ld: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script" and values.get("type", "").split(";")[0].strip().lower() == (
            "application/ld+json"
        ):
            self._ld = []
        elif tag == "a" and values.get("href"):
            self.links.append(values["href"].strip())

    def handle_data(self, data: str) -> None:
        if self._ld is not None:
            self._ld.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._ld is not None:
            self.blocks.append("".join(self._ld))
            self._ld = None


def _scan(html: str) -> _PageScan:
    scan = _PageScan()
    try:
        scan.feed(html or "")
        scan.close()
    except (AssertionError, ValueError):  # very broken markup: keep what was read
        pass
    return scan


def ld_json_blocks(html: str) -> list[str]:
    """The text of every `application/ld+json` script on the page."""
    return _scan(html).blocks


def page_links(html: str, base_url: str) -> list[str]:
    """Absolute http(s) link targets on the page, fragment removed, in order, no repeats."""
    seen: dict[str, None] = {}
    for href in _scan(html).links:
        if href.lower().startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urldefrag(urljoin(base_url, href))[0]
        if urlsplit(absolute).scheme in ("http", "https"):
            seen.setdefault(absolute)
    return list(seen)


def _walk(node: Any, budget: list[int]):
    """Every dict inside a JSON-LD value (lists, @graph and nested items included)."""
    if budget[0] <= 0:
        return
    if isinstance(node, dict):
        budget[0] -= 1
        yield node
        for v in node.values():
            if isinstance(v, (dict, list)):
                yield from _walk(v, budget)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, budget)


def _is_job(obj: dict) -> bool:
    kind = obj.get("@type")
    return kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind)


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _name(value: Any) -> str | None:
    """A schema.org text-or-thing: a string, or an object carrying a `name`."""
    if isinstance(value, dict):
        return _text(value.get("name"))
    return _text(value)


def _place(loc: Any) -> str | None:
    places = loc if isinstance(loc, list) else [loc]
    parts: list[str] = []
    for p in places:
        addr = p.get("address") if isinstance(p, dict) else None
        line = None
        if isinstance(addr, dict):
            bits = [
                _text(addr.get("addressLocality")),
                _text(addr.get("addressRegion")),
                _name(addr.get("addressCountry")),
            ]
            line = ", ".join(b for b in bits if b)
        elif isinstance(addr, str):
            line = _text(addr)
        if line and line not in parts:
            parts.append(line)
    return "; ".join(parts) or None


def _applicant_places(value: Any) -> str | None:
    items = value if isinstance(value, list) else [value]
    names = [n for n in (_name(i) for i in items) if n]
    return "; ".join(dict.fromkeys(names)) or None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _salary(base: Any) -> tuple[float | None, float | None, str | None, str | None]:
    """(min, max, currency, interval) from a MonetaryAmount. A weekly figure is stated per year
    (x52); a unit Kestrel has no word for leaves the interval unknown."""
    if isinstance(base, list):
        base = next((b for b in base if isinstance(b, dict)), None)
    if not isinstance(base, dict):
        return None, None, None, None
    value = base.get("value")
    holder = value if isinstance(value, dict) else base
    low = _number(holder.get("minValue"))
    high = _number(holder.get("maxValue"))
    exact = _number(holder.get("value")) if isinstance(value, dict) else _number(value)
    if low is None and high is None and exact is not None:
        low = high = exact
    if low is None and high is None:
        return None, None, None, None
    currency = _text(base.get("currency")) or _text(holder.get("currency"))
    unit = str(holder.get("unitText") or base.get("unitText") or "").strip().upper()
    if unit == "WEEK":
        low = low * _WEEKS_PER_YEAR if low is not None else None
        high = high * _WEEKS_PER_YEAR if high is not None else None
        unit = "YEAR"
    return low, high, currency, _INTERVALS.get(unit)


def _identifier(obj: dict) -> str | None:
    ident = obj.get("identifier")
    if isinstance(ident, dict):
        ident = ident.get("value")
    if isinstance(ident, (int, float)) and not isinstance(ident, bool):
        ident = str(ident)
    return _text(ident)


def _date(value: Any) -> date | None:
    text = _text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def is_expired(obj: dict, today: date | None = None) -> bool:
    """True when `validThrough` is a date before today (a role past its closing date)."""
    through = _date(obj.get("validThrough"))
    return through is not None and through < (today or datetime.now(UTC).date())


def job_postings(html: str, page_url: str) -> list[dict]:
    """Every JobPosting object the page embeds, in document order. A malformed block is skipped."""
    found: list[dict] = []
    budget = [MAX_NODES]
    for block in ld_json_blocks(html):
        try:
            data = json.loads(block.strip())
            objs = list(_walk(data, budget))
        except (ValueError, RecursionError):  # malformed, or nested deeper than Python allows
            continue
        found.extend(o for o in objs if _is_job(o))
    return found


def to_raw_posting(
    obj: dict, page_url: str, *, org_fallback: str | None = None, today: date | None = None
) -> RawPosting | None:
    """The RawPosting for one JobPosting, or None when it has no title or no description, or is
    past its `validThrough` date (an expired role is not an opening)."""
    title, body = _text(obj.get("title")) or _text(obj.get("name")), _text(obj.get("description"))
    if not title or not body or is_expired(obj, today):
        return None
    url = _text(obj.get("url"))
    url = urldefrag(urljoin(page_url, url))[0] if url else page_url
    remote = obj.get("jobLocationType") == "TELECOMMUTE"
    place = _place(obj.get("jobLocation"))
    if remote and not place:
        place = _applicant_places(obj.get("applicantLocationRequirements"))
    employment = obj.get("employmentType")
    if isinstance(employment, list):
        employment = employment[0] if employment else None
    low, high, currency, interval = _salary(obj.get("baseSalary"))
    posted = _text(obj.get("datePosted"))
    org = obj.get("hiringOrganization")
    org_site = _text(org.get("sameAs") or org.get("url")) if isinstance(org, dict) else None
    ident = _identifier(obj)
    return RawPosting(
        external_id=ident or (url if url != page_url else f"{page_url}#{title}|{place or ''}"),
        url=url,
        apply_url=url,
        title=title,
        org_name=_name(org) or org_fallback,
        org_url=org_site,
        location_raw=place,
        description_html=body,
        employment_type=_text(employment),
        is_remote=True if remote else None,
        remote_hint="Remote" if remote else None,
        comp_min=low,
        comp_max=high,
        comp_currency=currency if low is not None or high is not None else None,
        comp_interval=interval,
        date_posted=posted[:10] if posted else None,
        raw={"validThrough": _text(obj.get("validThrough")), "identifier": ident},
    )
