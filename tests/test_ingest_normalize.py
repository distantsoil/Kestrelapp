"""normalize.py: canonical mapping plus the pure-arithmetic intelligence (org attrs, comp
observation, ghost score, dedup + canonical, re-seen handling)."""

from __future__ import annotations

from sqlalchemy import func, select

from kestrel.db.models import CompObservation, Opportunity, Org, Source
from kestrel.ingest.base import RawPosting
from kestrel.ingest.normalize import normalize_and_store


async def _source(session, *, kind="greenhouse", track="employment", name="src") -> Source:
    s = Source(track=track, kind=kind, name=name, config={}, enabled=True)
    session.add(s)
    await session.flush()
    return s


async def test_normalize_creates_opportunity_with_intelligence(sm):
    async with sm() as session:
        src = await _source(session)
        raw = RawPosting(
            external_id="1",
            url="https://x/1",
            title="Vice President of Information Technology",
            org_name="Meridian Health Partners",
            location_raw="Austin, TX",
            remote_hint="Hybrid",
            description_html="<p>Lead a 25-person team. Comp: $210,000 - $245,000. HIPAA.</p>",
        )
        res = await normalize_and_store(session, raw, source=src)
        await session.commit()
        opp = res.opportunity

        assert res.action == "new" and res.enqueue_embed is True
        assert opp.org_id is not None
        assert opp.content_hash and opp.dedup_group_id and opp.is_canonical is True
        assert opp.remote_type == "hybrid"
        assert opp.comp_min == 210000 and opp.comp_max == 245000 and opp.comp_source == "parsed"
        assert opp.description_tokens > 0 and opp.ghost_score is not None
        assert opp.country == "US" and opp.state == "TX" and opp.city == "Austin"

        org = await session.get(Org, opp.org_id)
        assert org.sector == "healthcare"  # inferred from "HIPAA"

        count = (await session.execute(select(func.count()).select_from(CompObservation))).scalar()
        assert count == 1


async def test_reseen_unchanged_increments_seen_count(sm):
    async with sm() as session:
        src = await _source(session)
        raw = RawPosting(
            external_id="1",
            url="u",
            title="VP IT",
            org_name="Acme",
            description_md="A stable description of the role.",
        )
        await normalize_and_store(session, raw, source=src)
        await session.commit()
        res = await normalize_and_store(session, raw, source=src)
        await session.commit()
        assert res.action == "reseen_unchanged"
        assert res.opportunity.seen_count == 2
        assert res.enqueue_embed is False and res.requeue_t2 is False


async def test_reseen_material_change_bumps_version_and_requeues(sm):
    async with sm() as session:
        src = await _source(session)
        await normalize_and_store(
            session,
            RawPosting(
                external_id="1",
                url="u",
                title="VP IT",
                org_name="Acme",
                description_md="Original text about infrastructure and security leadership.",
            ),
            source=src,
        )
        await session.commit()
        res = await normalize_and_store(
            session,
            RawPosting(
                external_id="1",
                url="u",
                title="VP IT",
                org_name="Acme",
                description_md="Different text now about data analytics and ML platform work.",
            ),
            source=src,
        )
        await session.commit()
        assert res.action == "reseen_changed"
        assert res.opportunity.version == 2
        assert res.opportunity.previous_description_md is not None
        assert res.requeue_t2 is True


async def test_reseen_changed_refreshes_title_comp_location_and_records_comp(sm):
    async with sm() as session:
        src = await _source(session)
        first = await normalize_and_store(
            session,
            RawPosting(
                external_id="1",
                url="u",
                title="VP IT",
                org_name="Acme",
                location_raw="Austin, TX",
                description_md="Lead infrastructure and security. Comp: $200,000 - $220,000.",
            ),
            source=src,
        )
        await session.commit()
        assert first.opportunity.comp_min == 200000 and first.opportunity.state == "TX"

        res = await normalize_and_store(
            session,
            RawPosting(
                external_id="1",
                url="u",
                title="VP of Information Technology",
                org_name="Acme",
                location_raw="Denver, CO",
                description_md="Now a broader data platform and analytics remit. "
                "Comp: $230,000 - $250,000.",
            ),
            source=src,
        )
        await session.commit()
        opp = res.opportunity
        assert res.action == "reseen_changed"
        assert opp.title == "VP of Information Technology"
        assert opp.comp_min == 230000 and opp.comp_max == 250000
        assert opp.location_raw == "Denver, CO" and opp.state == "CO" and opp.city == "Denver"
        count = (await session.execute(select(func.count()).select_from(CompObservation))).scalar()
        assert count == 2  # the changed comp is a new observation for the corpus
        latest = (
            await session.execute(
                select(CompObservation).order_by(CompObservation.id.desc()).limit(1)
            )
        ).scalar_one()
        assert latest.min == 230000 and latest.geo_bucket == "CO"


async def test_dedup_groups_same_role_and_selects_ats_canonical(sm):
    async with sm() as session:
        gh = await _source(session, kind="greenhouse", name="gh")
        lv = await _source(session, kind="lever", name="lv")
        js = await _source(session, kind="jobspy", name="js")
        title = "Vice President of Information Technology"
        org = "Meridian Health Partners"
        desc = "Lead IT for a growing healthcare provider network."
        for src, xid in ((gh, "g1"), (lv, "l1"), (js, "j1")):
            raw = RawPosting(
                external_id=xid,
                url=f"https://{src.kind}/{xid}",
                title=title,
                org_name=org,
                description_md=desc,
            )
            await normalize_and_store(session, raw, source=src)
        await session.commit()

        opps = (await session.execute(select(Opportunity))).scalars().all()
        assert len(opps) == 3
        assert len({o.dedup_group_id for o in opps}) == 1  # all one group

        canonical = [o for o in opps if o.is_canonical]
        assert len(canonical) == 1
        canon_source = await session.get(Source, canonical[0].source_id)
        assert canon_source.kind in ("greenhouse", "lever")  # an ATS copy, not jobspy
