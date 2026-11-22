"""Manual single-opportunity ingest (SPEC-INTERFACES 3, 5). The browser extension's capture and the
MCP ingest_opportunity tool both land here: a URL plus the readable text of a page the operator is
ALREADY looking at, turned into one opportunity that enters the pipeline exactly like a scraped one
(normalized, deduped, embed-enqueued, stage=new so Tier 0 sweeps it).

This reads a page the operator handed over; it never crawls, navigates, or fetches on its own (hard
rule 1 / SPEC 5). The `manual` source is disabled, so the scheduler never runs it unattended."""

from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Opportunity, Source, Task
from kestrel.ingest.base import RawPosting
from kestrel.ingest.normalize import normalize_and_store


async def _manual_source(session: AsyncSession, track: str) -> Source:
    name = f"Manual capture ({track})"
    src = (
        await session.execute(select(Source).where(Source.name == name, Source.kind == "manual"))
    ).scalar_one_or_none()
    if src is None:
        src = Source(track=track, kind="manual", name=name, config={}, enabled=False)
        session.add(src)
        await session.flush()
    return src


def _title_from(text: str | None, url: str | None) -> str | None:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return url


# The one place captured content is cut to size (D-EXT-777, D-SEC-20): every caller (the capture
# route the browser extension posts to, the MCP ingest tool, the applied-roles import) passes
# through manual_ingest, so the limits hold whichever door the text came in by. Overlong values are
# cut, never refused; a value that is not text is dropped.
TEXT_MAX = 40000
URL_MAX = 2048
NAME_MAX = 500


def cut(value: object, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) else None


async def manual_ingest(
    session: AsyncSession,
    *,
    url: str | None = None,
    text: str | None = None,
    title: str | None = None,
    org_name: str | None = None,
    track: str = "employment",
) -> Opportunity | None:
    """Create (or re-see) one opportunity from a captured page and enqueue its embed. Returns the
    opportunity, or None if there is neither a url nor text to work with."""
    url, text = cut(url, URL_MAX), cut(text, TEXT_MAX)
    title, org_name = cut(title, NAME_MAX), cut(org_name, NAME_MAX)
    if not (url or text):
        return None
    if track not in ("employment", "consulting"):
        track = "employment"
    source = await _manual_source(session, track)
    external_id = url or "manual:" + hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]  # noqa: S324  content id, not a security hash
    raw = RawPosting(
        external_id=external_id,
        url=url,
        title=title or _title_from(text, url),
        org_name=org_name,
        description_md=text,
    )
    res = await normalize_and_store(session, raw, source=source)
    if res.enqueue_embed:
        session.add(Task(type="embed", payload={"opportunity_id": res.opportunity.id}))
    await session.commit()
    return res.opportunity
