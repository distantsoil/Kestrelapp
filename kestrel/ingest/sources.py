"""Source configuration: load the shipped sources from config/sources.yml and upsert them into the
`source` table (by name; never overwriting operator edits). The scheduler and scan operate on DB
rows so last_run_at, consecutive_failures, and enabled persist. Since D-SRC-3 the file ships only
SAM.gov; the old examples are retired here."""

from __future__ import annotations

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel import paths
from kestrel.db.enums import TRACKS
from kestrel.db.models import Source


def load_source_specs() -> list[dict]:
    path = paths.config_file("sources.yml")
    if path is None:
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    specs: list[dict] = []
    for track in TRACKS:
        for s in data.get(track, []) or []:
            specs.append(
                {
                    "track": track,
                    "kind": s["kind"],
                    "name": s["name"],
                    "config": s.get("config", {}) or {},
                    "enabled": bool(s.get("enabled", False)),
                    "cadence_hours": int(s.get("cadence_hours", 24)),
                }
            )
    return specs


# The example sources shipped until D-SRC-3, written for the fictional example person. Job searches
# and the SEC watch are now generated from the profile, so these are retired: never seeded again
# (an install seeded before D-SRC-3 still has them in its own config/sources.yml), and deleted at
# startup when the operator never touched them.
RETIRED_EXAMPLE_NAMES = frozenset({
    "Indeed: Operations Analyst, Columbus OH",
    "Greenhouse — example board",
    "Lever — example board",
    "Fractional / interim CIO-CTO",
    "SEC EDGAR — 8-K trigger events",
    "PE portfolio — new additions",
    "Company newsroom / layoff RSS",
})
# Shipped sources renamed in D-SRC-3 (old name -> new name); the row keeps its state.
RENAMED_SHIPPED = {"SAM.gov — IT services RFPs": "SAM.gov federal IT contracts"}


async def retire_example_sources(session: AsyncSession) -> list[str]:
    """Delete the retired example rows the operator never used, and rename the renamed ones. A row
    is deleted only when it is still `shipped` (never edited), disabled, has never run, and has no
    scrape run or opportunity pointing at it; anything the operator enabled, edited or ran stays.
    Returns the deleted names. The caller commits."""
    from kestrel.db.models import Opportunity, ScrapeRun

    deleted: list[str] = []
    rows = (
        await session.execute(
            select(Source).where(Source.origin == "shipped")
        )
    ).scalars().all()
    names = {r.name for r in rows}
    for row in rows:
        if row.name in RENAMED_SHIPPED and RENAMED_SHIPPED[row.name] not in names:
            row.name = RENAMED_SHIPPED[row.name]
            continue
        if row.name not in RETIRED_EXAMPLE_NAMES or row.enabled or row.last_run_at is not None:
            continue
        used = (
            await session.execute(select(ScrapeRun.id).where(ScrapeRun.source_id == row.id))
        ).first() or (
            await session.execute(select(Opportunity.id).where(Opportunity.source_id == row.id))
        ).first()
        if used:
            continue
        await session.delete(row)
        deleted.append(row.name)
    return deleted


async def sync_sources(session: AsyncSession) -> int:
    """Create any source from sources.yml not already present (matched by name). Returns the number
    created. Existing rows are left untouched so operator changes survive a reseed. A retired
    example name is never created, even from an older seeded copy of sources.yml."""
    created = 0
    for spec in load_source_specs():
        if spec["name"] in RETIRED_EXAMPLE_NAMES or spec["name"] in RENAMED_SHIPPED:
            continue
        exists = (
            await session.execute(select(Source).where(Source.name == spec["name"]))
        ).scalar_one_or_none()
        if exists is None:
            session.add(
                Source(
                    track=spec["track"],
                    kind=spec["kind"],
                    name=spec["name"],
                    config=spec["config"],
                    enabled=spec["enabled"],
                    cadence_hours=spec["cadence_hours"],
                    origin="shipped",
                )
            )
            created += 1
    await session.commit()
    return created


LINKEDIN_NOTICE_KIND = "linkedin_removed"
LINKEDIN_NOTICE_TITLE = "LinkedIn searching was removed"
LINKEDIN_NOTICE_BODY = (
    "LinkedIn searching was removed: LinkedIn does not allow automated access. Paste a LinkedIn "
    "posting by hand with Add a role I found elsewhere."
)


async def retire_linkedin_sources(session: AsyncSession) -> list[str]:
    """D-SRC-5: take LinkedIn out of every job-board source already in the database. A source
    that named only LinkedIn is switched off; one that also names other boards keeps those and
    loses LinkedIn. Idempotent (a second start finds nothing), and the one-time notification is
    added only when something changed. Returns the affected source names. The caller commits."""
    from kestrel.ingest.adapters.jobspy import NO_BOARDS_KEY

    rows = (await session.execute(select(Source).where(Source.kind == "jobspy"))).scalars().all()
    changed: list[str] = []
    for row in rows:
        config = dict(row.config or {})
        raw = config.get("site_name") or []
        sites = [raw] if isinstance(raw, str) else list(raw)
        kept = [s for s in sites if str(s).strip().lower() != "linkedin"]
        if len(kept) == len(sites):
            continue
        config["site_name"] = kept
        if not kept:
            # No boards left. An empty list would now mean Kestrel's default boards, which the
            # operator never chose for this search: it stays off, marked, and its row offers
            # the default boards (release round review, finding 2).
            config[NO_BOARDS_KEY] = True
            row.enabled = False
        row.config = config  # a new dict, so the JSON column registers the change
        changed.append(row.name)
    if changed:
        from kestrel.db.models import Notification

        session.add(Notification(
            kind=LINKEDIN_NOTICE_KIND, severity="info", title=LINKEDIN_NOTICE_TITLE,
            body=LINKEDIN_NOTICE_BODY, delivered_channels=[],
        ))
    return changed
