"""Company boards found in the job searches (SPEC.md 5.1, D-SRC-7). Greenhouse, Lever, Ashby,
Workday and SmartRecruiters cannot be searched across companies: each company has its own board.
An Indeed or Google posting often links to one, so a job search run reads its postings' links,
and each company board it recognizes becomes a source for that company, on or off with the search
that found it. The operator never types a board name, and the board, the most trusted copy of a
posting (dedup ranks it first), is read directly from then on.

A link names the board exactly, unlike a guess from the company name (board_lookup.py), so a
found board starts on. A board already in the sources, in any state, is never added again: one
the operator turned off stays off. At most MAX_DISCOVERED found boards per track (hard rule 7)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Source
from kestrel.ingest.base import RawPosting
from kestrel.ingest.http import HttpClient

log = structlog.get_logger()

DISCOVERED = "discovered"  # Source.origin for a board found in a search
MAX_DISCOVERED = 40
MAX_SHORT_LINKS = 20  # grnh.se links resolved per run (one polite request each)
SHORT_LINK_HOST = "grnh.se"  # Greenhouse's link shortener
CADENCE_HOURS = 24

PLATFORMS = {
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "ashby": "Ashby",
    "workday": "Workday",
    "smartrecruiters": "SmartRecruiters",
    "rippling": "Rippling",
    "adp": "ADP Workforce Now",
    "paylocity": "Paylocity",
    "workable": "Workable",
}
# The config key that names the company, per kind, and whether the platform treats it as case
# sensitive (the Ashby and SmartRecruiters adapters pass it through unchanged).
_KEY = {
    "greenhouse": "token", "lever": "company", "ashby": "token", "smartrecruiters": "company",
    "rippling": "slug", "adp": "cid",
    "paylocity": "board_id",
    "workable": "slug",
}
_CASE_SENSITIVE = {"ashby", "smartrecruiters"}  # adp cids are UUIDs: compared lowercased

_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_LOCALE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2})?$")
_WORKDAY_HOST = re.compile(r"^([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
_CC_ID = re.compile(r"^[A-Za-z0-9_]{1,40}$")
_ADP_LANG = re.compile(r"^[a-z]{2}_[A-Z]{2}$")
_NOT_A_COMPANY = {"embed", "jobs", "job", "apply", "careers", "api", "v1", "posting-api"}


@dataclass(frozen=True)
class BoardRef:
    kind: str
    config: dict

    @property
    def key(self) -> tuple:
        """Identity for "already watched": the company name as the platform compares it."""
        if self.kind == "workday":
            c = self.config
            return ("workday", c["tenant"].lower(), c["dc"].lower(), c["site"].lower())
        value = self.config[_KEY[self.kind]]
        return (self.kind, value if self.kind in _CASE_SENSITIVE else value.lower())


def _slug(value: str | None) -> str | None:
    if value and _SLUG.match(value) and value.lower() not in _NOT_A_COMPANY:
        return value
    return None


def board_from_url(url: str | None) -> BoardRef | None:
    """The company board a posting link points at, or None. Hosts match exactly: a board name in
    the path of some other site, or a look-alike host, is not a board."""
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    segs = [s for s in parts.path.split("/") if s]
    first = segs[0] if segs else None

    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io"):
        if first == "embed":
            token = _slug((parse_qs(parts.query).get("for") or [None])[0])
        else:
            token = _slug(first)
        return BoardRef("greenhouse", {"token": token}) if token else None
    if host == "jobs.lever.co":
        company = _slug(first)
        return BoardRef("lever", {"company": company}) if company else None
    if host == "jobs.ashbyhq.com":
        token = _slug(first)
        return BoardRef("ashby", {"token": token}) if token else None
    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com"):
        company = _slug(first)
        return BoardRef("smartrecruiters", {"company": company}) if company else None
    if host == "ats.rippling.com":
        slug = _slug(first)
        return BoardRef("rippling", {"slug": slug}) if slug else None
    if host == "apply.workable.com":
        slug = _slug(first)
        return BoardRef("workable", {"slug": slug}) if slug else None
    if host == "recruiting.paylocity.com":
        if len(segs) >= 3 and segs[0].lower() == "recruiting" and segs[1].lower() == "jobs":
            if segs[2].lower() == "all" and len(segs) >= 4 and _UUID.match(segs[3]):
                return BoardRef("paylocity", {"board_id": segs[3].lower()})
        return None
    if host == "workforcenow.adp.com" and parts.path.lower().endswith("/recruitment.html"):
        query = parse_qs(parts.query)
        cid = (query.get("cid") or [""])[0]
        if not _UUID.match(cid):
            return None
        config = {"cid": cid.lower()}
        for key, name, rx in (("ccId", "cc_id", _CC_ID), ("lang", "lang", _ADP_LANG)):
            value = (query.get(key) or [""])[0]
            if rx.match(value):
                config[name] = value
        return BoardRef("adp", config)
    m = _WORKDAY_HOST.match(host)
    if m:
        rest = segs[1:] if first and _LOCALE.match(first) else segs
        site = _slug(rest[0]) if rest else None
        if site:
            return BoardRef("workday", {"tenant": m.group(1), "dc": m.group(2), "site": site})
    return None


def _is_short_link(url: str | None) -> bool:
    if not url:
        return False
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and (parts.hostname or "").lower() == SHORT_LINK_HOST


async def resolve_short_link(url: str, http: HttpClient) -> BoardRef | None:
    """The board a grnh.se short link points at: one request with redirects off, then the
    Location header read like any other posting link. Any failure is None, never an error."""
    try:
        r = await http.request(
            "GET", url, follow_redirects=False, retries=1, public_only=True
        )
    except Exception as exc:  # network, robots, rate: a link that cannot be read is skipped
        log.debug("board_discovery.short_link_failed", error=type(exc).__name__)
        return None
    location = r.headers.get("location") if 300 <= r.status_code < 400 else None
    return board_from_url(location)


async def _known_keys(session: AsyncSession, track: str) -> set[tuple]:
    rows = (
        await session.execute(
            select(Source.kind, Source.config).where(
                Source.track == track, Source.kind.in_(tuple(PLATFORMS))
            )
        )
    ).all()
    keys: set[tuple] = set()
    for kind, config in rows:
        try:
            keys.add(BoardRef(kind, dict(config or {})).key)
        except (KeyError, AttributeError):
            continue  # a hand-made source missing its company name: nothing to match
    return keys


async def discover_boards(
    session: AsyncSession, postings: list[RawPosting], *, search: Source,
    http: HttpClient | None = None,
) -> int:
    """Add a source for each new company board these postings link to. Flushes, never commits;
    the caller owns the transaction. Returns how many were added. With an `http` client, a
    grnh.se short link is followed one hop (at most MAX_SHORT_LINKS a run) to find its board."""
    already = (
        await session.execute(
            select(func.count()).select_from(Source).where(
                Source.track == search.track, Source.origin == DISCOVERED
            )
        )
    ).scalar_one()
    room = MAX_DISCOVERED - already
    known = await _known_keys(session, search.track)
    added = skipped = 0
    short_left = MAX_SHORT_LINKS
    tried: set[str] = set()
    for p in postings:
        ref = board_from_url(p.apply_url) or board_from_url(p.url)
        if ref is None and http is not None and short_left > 0:
            link = next((u for u in (p.apply_url, p.url) if _is_short_link(u)), None)
            if link and link not in tried:
                tried.add(link)
                short_left -= 1
                ref = await resolve_short_link(link, http)
        if ref is None or ref.key in known:
            continue
        known.add(ref.key)
        if added >= room:
            skipped += 1
            continue
        company = (p.org_name or "").strip() or next(iter(ref.config.values()))
        config = dict(ref.config)
        if ref.kind == "workday":
            config["company"] = company
        elif ref.kind in ("ashby", "rippling", "adp", "paylocity", "workable"):
            config["company_name"] = company
        session.add(Source(
            track=search.track, kind=ref.kind, name=f"{company} jobs ({PLATFORMS[ref.kind]})",
            config=config, enabled=bool(search.enabled), cadence_hours=CADENCE_HOURS,
            origin=DISCOVERED,
        ))
        added += 1
    if skipped:
        log.info("board_discovery.cap_reached", track=search.track, left_out=skipped)
    await session.flush()
    return added
