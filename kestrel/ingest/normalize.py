"""Map any RawPosting to a canonical opportunity and do the shared work three later phases depend
on (SPEC.md 5.4): resolve/create the org and persist size_band/sector/ownership; parse location and
compensation; classify remote; compute content_hash and assign the dedup group with canonical-source
selection; count description tokens; compute ghost_score; write a comp_observation; and signal that
an `embed` task must be enqueued. All arithmetic and field extraction, no model involved."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from markdownify import markdownify as _md
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.enums import APPLIED_OR_LATER
from kestrel.db.models import CompObservation, Opportunity, Org, Pursuit, Source
from kestrel.ingest.base import RawPosting
from kestrel.ingest.comp import Comp, parse_comp, to_annual
from kestrel.ingest.dedup import (
    content_hash,
    jaccard,
    normalize_desc,
    source_rank,
    title_tokens,
)
from kestrel.ingest.ghost import compute_ghost_score
from kestrel.ingest.location import classify_remote, parse_location
from kestrel.ingest.orgs import infer_org_attrs, resolve_org
from kestrel.ingest.tokens import count_tokens
from kestrel.rules.helpers import seniority_rank

# \d{1,4}, not \d+: a run of digits made a search on pasted text polynomial (CodeQL
# py/polynomial-redos); a search still finds the last four digits of a longer number.
_TEAM_RE = re.compile(
    r"team of|direct reports|manage a team|\d{1,4}[- ]person team|lead a team", re.I
)


@dataclass
class NormalizeResult:
    opportunity: Opportunity
    action: str  # new | reseen_unchanged | reseen_minor | reseen_changed
    enqueue_embed: bool
    requeue_t2: bool
    # True when a new row was hidden on arrival as a re-post of an applied pursuit (SPEC.md 5.6).
    suppressed: bool = False


def _description(raw: RawPosting) -> str | None:
    if raw.description_md:
        return raw.description_md.strip() or None
    if raw.description_html:
        # Greenhouse content is HTML-ESCAPED HTML: unescape, then convert to markdown. Disable
        # markdownify's char escaping so "$210,000 - $245,000" is not mangled to "\-" etc.
        md = _md(
            html.unescape(raw.description_html),
            escape_asterisks=False,
            escape_underscores=False,
            escape_misc=False,
        )
        return md.strip() or None
    return None


def _domain(raw: RawPosting) -> str | None:
    for candidate in (raw.org_url, raw.url):
        if candidate:
            host = urlparse(candidate).netloc.lower()
            if host:
                return host.removeprefix("www.")
    return None


def _date_only(value: str | None) -> str | None:
    return value[:10] if value else None


def _mentions_team(desc: str | None) -> bool:
    return bool(desc and _TEAM_RE.search(desc))


# Public alias: the intelligence ghost layer reuses this "has team detail" check, so it has a stable
# name to import rather than reaching for a private one.
mentions_team = _mentions_team


def _opp_type(track: str) -> str:
    return "job_posting" if track == "employment" else "contract_posting"


def _resolve_comp(raw: RawPosting, description: str | None, track: str) -> tuple[Comp | None, str]:
    if raw.comp_min is not None or raw.comp_max is not None:
        comp = Comp(
            raw.comp_min, raw.comp_max, raw.comp_currency or "USD", raw.comp_interval or "year"
        )
        source = "inferred" if raw.comp_is_estimate else "structured"
    else:
        parsed = parse_comp(description)
        if parsed is None:
            return None, "none"
        comp, source = parsed, "parsed"
    # Employment comp is annual (SPEC.md 4.3); annualize sub-annual intervals.
    if track == "employment" and comp.interval != "year":
        comp = Comp(
            to_annual(comp.min, comp.interval),
            to_annual(comp.max, comp.interval),
            comp.currency,
            "year",
        )
    return comp, source


async def _find_same_posting(session: AsyncSession, source, raw: RawPosting) -> Opportunity | None:
    if raw.external_id:
        stmt = select(Opportunity).where(
            Opportunity.source_id == source.id, Opportunity.external_id == raw.external_id
        )
    elif raw.url:
        stmt = select(Opportunity).where(
            Opportunity.source_id == source.id, Opportunity.url == raw.url
        )
    else:
        return None
    return (await session.execute(stmt.limit(1))).scalar_one_or_none()


async def _recompute_canonical(session: AsyncSession, group_id: str) -> None:
    members = (
        (await session.execute(select(Opportunity).where(Opportunity.dedup_group_id == group_id)))
        .scalars()
        .all()
    )
    if not members:
        return
    source_ids = {m.source_id for m in members if m.source_id}
    kinds: dict[int, str] = {}
    if source_ids:
        rows = (
            await session.execute(select(Source.id, Source.kind).where(Source.id.in_(source_ids)))
        ).all()
        kinds = {sid: kind for sid, kind in rows}
    canonical = min(members, key=lambda m: (source_rank(kinds.get(m.source_id, "")), m.id))
    for m in members:
        m.is_canonical = m.id == canonical.id


async def _assign_dedup_group(
    session: AsyncSession, opp: Opportunity, org, title, hash_: str
) -> None:
    group_id: str | None = None
    exact = (
        await session.execute(
            select(Opportunity.dedup_group_id)
            .where(Opportunity.content_hash == hash_, Opportunity.id != opp.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if exact is not None:
        group_id = exact
    if group_id is None and org is not None:
        # Near-exact within the SAME org AND SAME track (never merge across tracks). Select only
        # id/title/group to avoid materializing every same-org row's description + raw payload.
        tset = title_tokens(title)
        rows = (
            await session.execute(
                select(Opportunity.title, Opportunity.dedup_group_id).where(
                    Opportunity.org_id == org.id,
                    Opportunity.track == opp.track,
                    Opportunity.id != opp.id,
                )
            )
        ).all()
        best_group, best_j = None, 0.0
        for cand_title, cand_group in rows:
            j = jaccard(tset, title_tokens(cand_title))
            if j > 0.85 and j > best_j:
                best_group, best_j = cand_group, j
        if best_group is not None:
            group_id = best_group
    opp.dedup_group_id = group_id or hash_[:16]
    await _recompute_canonical(session, opp.dedup_group_id)


def _comp_observation(opp, org, comp: Comp, comp_source, country, state, city) -> CompObservation:
    return CompObservation(
        opportunity_id=opp.id,
        normalized_title=(opp.title or "").lower(),
        seniority_rank=seniority_rank(opp.title),
        geo_bucket=(state or country or city or "unknown"),
        org_size_band=org.size_band if org else None,
        org_ownership=org.ownership if org else None,
        sector=org.sector if org else None,
        min=comp.min,
        max=comp.max,
        currency=comp.currency,
        interval=comp.interval,
        source=comp_source,
    )


async def _handle_reseen(session, existing, raw, description, now, *, source) -> NormalizeResult:
    existing.last_seen_at = now
    title = raw.title or existing.title
    desc = description or existing.description_md
    new_hash = content_hash(existing.org_name, title, desc)
    existing.seen_count += 1
    # A re-listing with a newer post date advances date_posted even when content is unchanged, so
    # the ghost "republished with a fresh date_posted but identical content_hash" signal can fire
    # (kestrel.intelligence.ghost.reposted_identical). Never move it backward.
    if raw.date_posted and (existing.date_posted is None or raw.date_posted > existing.date_posted):
        existing.date_posted = raw.date_posted
    if new_hash == existing.content_hash:
        existing.ghost_score = compute_ghost_score(
            seen_count=existing.seen_count,
            first_seen_at=existing.first_seen_at,
            last_seen_at=now,
            version=existing.version,
            has_comp=existing.comp_min is not None,
            has_location=bool(existing.location_raw),
            has_team=_mentions_team(existing.description_md),
        )
        return NormalizeResult(existing, "reseen_unchanged", False, False)

    material = (
        jaccard(
            set(normalize_desc(existing.description_md).split()), set(normalize_desc(desc).split())
        )
        < 0.9
    )
    prev_description = (
        existing.description_md
    )  # capture BEFORE overwrite (was stored as the new one)
    existing.content_hash = new_hash
    existing.description_md = description or existing.description_md
    existing.description_tokens = count_tokens(existing.description_md)
    if material:
        existing.version += 1
        existing.previous_description_md = prev_description
    # The content changed, so the derived fields must follow it: the title, the location, and the
    # compensation are re-read from the new posting (a source that omits one keeps the known
    # value), and a changed comp is a fresh observation for the comp corpus.
    if raw.title:
        existing.title = title
    if raw.location_raw:
        existing.location_raw = raw.location_raw
        existing.country, existing.state, existing.city = parse_location(raw.location_raw)
    comp, comp_source = _resolve_comp(raw, existing.description_md, source.track)
    if comp is not None:
        comp_changed = (comp.min, comp.max, comp.currency, comp.interval) != (
            existing.comp_min,
            existing.comp_max,
            existing.comp_currency,
            existing.comp_interval,
        )
        existing.comp_min, existing.comp_max = comp.min, comp.max
        existing.comp_currency, existing.comp_interval = comp.currency, comp.interval
        existing.comp_source = comp_source
        if comp_changed:
            org = await session.get(Org, existing.org_id) if existing.org_id else None
            session.add(
                _comp_observation(
                    existing,
                    org,
                    comp,
                    comp_source,
                    existing.country,
                    existing.state,
                    existing.city,
                )
            )
    existing.ghost_score = compute_ghost_score(
        seen_count=existing.seen_count,
        first_seen_at=existing.first_seen_at,
        last_seen_at=now,
        version=existing.version,
        has_comp=existing.comp_min is not None,
        has_location=bool(existing.location_raw),
        has_team=_mentions_team(existing.description_md),
    )
    return NormalizeResult(
        existing,
        "reseen_changed" if material else "reseen_minor",
        enqueue_embed=True,
        requeue_t2=material,
    )


async def attach_description(session: AsyncSession, opp: Opportunity, text: str) -> None:
    """The operator pasted the posting text for a role that came with none (issue #90). The same
    changes a re-scrape that brings a body makes (_handle_reseen): the text, its token count, the
    content hash and a new version with the earlier text kept; pay read from the text only when
    the role has none, recorded as a comp observation; and the ghost score recomputed. The caller
    moves the stage back to `new` (Tier 0 sweeps it again), queues the embed and commits. Nothing
    is fetched and nothing leaves the machine."""
    body = text.strip()
    opp.previous_description_md = opp.description_md
    opp.description_md = body
    opp.description_tokens = count_tokens(body)
    opp.content_hash = content_hash(opp.org_name, opp.title, body)
    opp.version = (opp.version or 1) + 1
    if opp.comp_min is None and opp.comp_max is None:
        comp, comp_source = _resolve_comp(RawPosting(external_id="pasted"), body, opp.track)
        if comp is not None:
            opp.comp_min, opp.comp_max = comp.min, comp.max
            opp.comp_currency, opp.comp_interval = comp.currency, comp.interval
            opp.comp_source = comp_source
            org = await session.get(Org, opp.org_id) if opp.org_id else None
            session.add(
                _comp_observation(opp, org, comp, comp_source, opp.country, opp.state, opp.city)
            )
    opp.ghost_score = compute_ghost_score(
        seen_count=opp.seen_count,
        first_seen_at=opp.first_seen_at,
        last_seen_at=opp.last_seen_at,
        version=opp.version,
        has_comp=opp.comp_min is not None,
        has_location=bool(opp.location_raw),
        has_team=_mentions_team(opp.description_md),
    )


async def normalize_and_store(
    session: AsyncSession, raw: RawPosting, *, source, scrape_run=None
) -> NormalizeResult:
    now = utcnow_iso()
    description = _description(raw)
    title = (raw.title or "").strip() or None
    org_name = (raw.org_name or "").strip() or None

    existing = await _find_same_posting(session, source, raw)
    if existing is not None:
        return await _handle_reseen(session, existing, raw, description, now, source=source)

    attrs = infer_org_attrs(raw, description)
    org = await resolve_org(session, name=org_name, domain=_domain(raw), attrs=attrs)
    hash_ = content_hash(org_name, title, description)
    country, state, city = parse_location(raw.location_raw)
    remote = classify_remote(
        title=title, description=description, hint=raw.remote_hint, is_remote=raw.is_remote
    )
    comp, comp_source = _resolve_comp(raw, description, source.track)

    opp = Opportunity(
        track=source.track,
        opp_type=_opp_type(source.track),
        org_id=org.id,
        source_id=source.id,
        scrape_run_id=getattr(scrape_run, "id", None),
        external_id=raw.external_id,
        url=raw.url,
        title=title,
        org_name=org_name,
        org_url=raw.org_url,
        location_raw=raw.location_raw,
        country=country,
        state=state,
        city=city,
        is_remote=raw.is_remote,
        remote_type=remote,
        employment_type=raw.employment_type,
        comp_min=comp.min if comp else None,
        comp_max=comp.max if comp else None,
        comp_currency=comp.currency if comp else None,
        comp_interval=comp.interval if comp else None,
        comp_source=comp_source,
        description_md=description,
        description_tokens=count_tokens(description),
        date_posted=_date_only(raw.date_posted),
        first_seen_at=now,
        last_seen_at=now,
        seen_count=1,
        version=1,
        content_hash=hash_,
        raw=raw.raw,
        stage="new",
    )
    session.add(opp)
    await session.flush()

    await _assign_dedup_group(session, opp, org, title, hash_)

    opp.ghost_score = compute_ghost_score(
        seen_count=1,
        first_seen_at=now,
        last_seen_at=now,
        version=1,
        has_comp=comp is not None,
        has_location=bool(country or state or city or raw.location_raw),
        has_team=_mentions_team(description),
    )
    if comp:
        session.add(_comp_observation(opp, org, comp, comp_source, country, state, city))

    suppressed = await suppress_if_applied(session, opp)
    return NormalizeResult(opp, "new", enqueue_embed=True, requeue_t2=False, suppressed=suppressed)


async def suppress_if_applied(session: AsyncSession, opp: Opportunity) -> bool:
    """SPEC.md 5.6: a NEW opportunity whose dedup group already holds a pursuit at `applied` or
    later (or a row the operator hid as applied) is hidden on arrival as a re-post, unless any
    such pursuit allows re-posts. Hidden means archived_at set: out of triage and the list, under
    the Hidden chip with its reason, and Unhide puts it back. Never deletes, never touches the
    row it matched."""
    if not opp.dedup_group_id or opp.archived_at:
        return False
    rows = (
        await session.execute(
            select(
                Opportunity.id,
                Opportunity.archive_reason,
                Pursuit.id,
                Pursuit.status,
                Pursuit.allow_reposts,
            )
            .outerjoin(Pursuit, Pursuit.opportunity_id == Opportunity.id)
            .where(Opportunity.dedup_group_id == opp.dedup_group_id, Opportunity.id != opp.id)
        )
    ).all()
    applied = [
        (opp_id, pursuit_id, bool(allow))
        for opp_id, reason, pursuit_id, status, allow in rows
        if (pursuit_id is not None and status in APPLIED_OR_LATER) or reason == "applied"
    ]
    if not applied:
        return False
    if any(allow for _, pursuit_id, allow in applied if pursuit_id is not None):
        return False  # a pursuit that asked for re-posts wins: never hide what was asked for
    # Name a pursuit when one matched; a row hidden as applied without one is named by id.
    opp_id, pursuit_id, _ = next((hit for hit in applied if hit[1] is not None), applied[0])
    opp.archived_at = utcnow_iso()
    opp.archive_reason = "applied"
    opp.stage_reason = (
        f"Re-post of pursuit #{pursuit_id}"
        if pursuit_id is not None
        else f"Re-post of hidden opportunity #{opp_id}"
    )
    return True
