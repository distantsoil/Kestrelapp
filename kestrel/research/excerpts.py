"""The shared excerpt-acquisition layer (SPEC-MODULES 2.1). Every downstream piece (P5 research, P6
contacts) consumes an ExcerptSet: a numbered list of {index, url, kind, text} the model
synthesizes and NEVER browses. Every returned claim resolves to one of these indices.

Five source tiers, tried in order, each best-effort (a tier that fails or is unconfigured is
skipped): 1) direct fetch of conventional company pages; 2) the ATS board itself (here, the org's
already-ingested open roles — a company posting five IT roles is a signal); 3) SEC EDGAR for public
companies; 4) optional SearXNG; 5) OPERATOR PASTE — the guaranteed path that needs no network.
Only public pages, politely (constraint 7): the shared HttpClient sends a descriptive User-Agent and
rate-limits; the EDGAR tier runs only when that configured UA carries a real contact address
(settings.ingest.user_agent), never with a fabricated placeholder."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.config import Settings
from kestrel.db.models import Opportunity, Org
from kestrel.ingest.http import has_contact_address

log = structlog.get_logger()

_CONVENTIONAL = ["", "/about", "/about-us", "/company", "/careers", "/team", "/leadership",
                 "/news", "/blog", "/investors"]
_MAX_EXCERPT_CHARS = 6000
_MAX_COMPANY_PAGES = 6
_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_STRIP_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_EDGAR_FTS = "https://efts.sec.gov/LATEST/search-index"
_EDGAR_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"


@dataclass
class Excerpt:
    index: int  # 1-based, matching the prompt block "[1] url\n excerpt"
    url: str | None
    kind: str  # company_page | ats | edgar | searx | operator_paste
    text: str


@dataclass
class ExcerptSet:
    excerpts: list[Excerpt] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.excerpts)

    def texts(self) -> list[str]:
        return [e.text for e in self.excerpts]

    def resolve(self, index: object):
        from kestrel.research.validation import resolve_index

        return resolve_index(index, self.excerpts)

    def prompt_block(self) -> str:
        return "\n\n".join(f"[{e.index}] {e.url or e.kind}\n{e.text}" for e in self.excerpts)

    def source_records(self) -> list[dict]:
        return [{"index": e.index, "url": e.url, "kind": e.kind} for e in self.excerpts]


def _clean_html(html: str) -> str:
    text = _TAG_RE.sub(" ", html)
    text = _STRIP_RE.sub(" ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#160;", " ")
    return _WS_RE.sub(" ", text).strip()


class _Builder:
    def __init__(self) -> None:
        self._excerpts: list[Excerpt] = []
        self._seen: set[str] = set()

    def add(self, *, url: str | None, kind: str, text: str) -> None:
        text = (text or "").strip()[:_MAX_EXCERPT_CHARS]
        if not text or text in self._seen:
            return
        self._seen.add(text)
        self._excerpts.append(Excerpt(index=len(self._excerpts) + 1, url=url, kind=kind, text=text))

    def build(self) -> ExcerptSet:
        return ExcerptSet(excerpts=self._excerpts)


async def _company_pages(builder: _Builder, http: Any, org: Org) -> None:
    # Ingestion sets org.domain (resolve_org), not org.url, so the domain is the usual entry point.
    site = org.url or org.domain
    if http is None or not site:
        return
    base = site if urlparse(site).scheme else f"https://{site}"
    added = 0
    for path in _CONVENTIONAL:
        if added >= _MAX_COMPANY_PAGES:
            break
        url = urljoin(base, path) if path else base
        try:
            html = await http.get_text(url, check_robots=True, public_only=True)
        except Exception as exc:  # one dead path never sinks acquisition
            log.debug("research_page_skip", url=url, error=str(exc))
            continue
        text = _clean_html(html)
        if len(text) > 200:
            builder.add(url=url, kind="company_page", text=text)
            added += 1


async def _ats_open_roles(builder: _Builder, session: AsyncSession, org: Org) -> None:
    titles = (
        await session.execute(
            select(Opportunity.title).where(
                Opportunity.org_id == org.id, Opportunity.title.is_not(None)
            ).limit(25)
        )
    ).scalars().all()
    if titles:
        listing = "; ".join(t for t in titles if t)
        builder.add(url=None, kind="ats",
                    text=f"Open roles currently posted by {org.name}: {listing}.")


async def _edgar(builder: _Builder, http: Any, org: Org) -> None:
    if http is None or not org.name:
        return
    ua = getattr(http, "user_agent", None)
    if not has_contact_address(ua):
        # SEC requires a real contact in the UA (hard rule 7); a placeholder is never sent. The
        # tier is skipped like any other unconfigured tier; doctor warns about ingest.user_agent.
        log.debug("research_edgar_skip", org=org.name, error="ingest.user_agent has no contact")
        return
    headers = {"User-Agent": ua}
    url = f"{_EDGAR_FTS}?q={quote_plus(chr(34) + org.name + chr(34))}&forms=8-K,10-K"
    try:
        data = await http.get_json(url, rate_per_sec=5.0, headers=headers)
    except Exception as exc:
        log.debug("research_edgar_skip", org=org.name, error=str(exc))
        return
    hits = (((data or {}).get("hits") or {}).get("hits") or []) if isinstance(data, dict) else []
    for hit in hits[:2]:
        src = hit.get("_source") or {}
        _id = hit.get("_id")
        ciks = src.get("ciks") or []
        if not isinstance(_id, str) or ":" not in _id or not ciks:
            continue
        adsh, filename = _id.split(":", 1)
        try:
            cik = int(str(ciks[0]))
        except (TypeError, ValueError):
            continue
        doc = f"{_EDGAR_ARCHIVES}/{cik}/{adsh.replace('-', '')}/{filename}"
        try:
            html = await http.get_text(doc, rate_per_sec=5.0, headers=headers)
        except Exception as exc:  # one unreachable filing never sinks acquisition
            log.debug("research_edgar_doc_skip", url=doc, error=str(exc))
            continue
        builder.add(url=doc, kind="edgar", text=_clean_html(html))


async def _searx(builder: _Builder, http: Any, org: Org, settings: Settings) -> None:
    base = settings.research.searx_url
    if http is None or not base or not org.name:
        return
    url = f"{base.rstrip('/')}/search?q={quote_plus(org.name)}&format=json"
    try:
        data = await http.get_json(url)
    except Exception as exc:
        log.debug("research_searx_skip", error=str(exc))
        return
    for r in (data.get("results") or [])[:3] if isinstance(data, dict) else []:
        content = r.get("content") or r.get("title")
        if content:
            builder.add(url=r.get("url"), kind="searx", text=str(content))


def _operator_paste(builder: _Builder, operator_paste: str | None) -> None:
    if not operator_paste:
        return
    # Split on blank lines so distinct pasted notes become distinct, individually-citable sources.
    for chunk in re.split(r"\n\s*\n", operator_paste.strip()):
        builder.add(url=None, kind="operator_paste", text=chunk)


async def acquire_excerpts(
    session: AsyncSession,
    http: Any,
    org: Org,
    *,
    settings: Settings,
    operator_paste: str | None = None,
) -> ExcerptSet:
    """Acquire excerpts across the five tiers. `http` may be None (fully offline): only the ATS
    (DB-derived open roles) and OPERATOR PASTE tiers run, and operator paste alone suffices for the
    guaranteed, network-free path."""
    builder = _Builder()
    await _company_pages(builder, http, org)
    await _ats_open_roles(builder, session, org)
    await _edgar(builder, http, org)
    await _searx(builder, http, org, settings)
    _operator_paste(builder, operator_paste)
    return builder.build()
