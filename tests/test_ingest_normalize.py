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


async def test_a_currency_is_known_only_when_the_posting_or_board_states_it(sm):
    """Issue #208 (D-SCORE-28): a defaulted USD is stored as not known, so the Tier 2 blocker
    check never compares it with a USD floor."""
    cases = [
        ("a", {"description_html": "<p>Pay: $120,000 - $140,000.</p>"}, ("USD", True)),
        ("b", {"description_html": "<p>Pay: 120k-140k a year.</p>"}, ("USD", False)),
        ("c", {"description_html": "<p>Pay: CA$90k-110k.</p>"}, ("CAD", True)),
        ("d", {"description_html": "<p>A role.</p>", "comp_min": 90000.0, "comp_max": 110000.0},
         ("USD", False)),
        ("e", {"description_html": "<p>A role.</p>", "comp_min": 90000.0, "comp_max": 110000.0,
               "comp_currency": "EUR"}, ("EUR", True)),
    ]
    async with sm() as session:
        src = await _source(session)
        for ext, extra, want in cases:
            raw = RawPosting(external_id=ext, url=f"https://x/{ext}", title=f"Harbor Planner {ext}",
                             org_name=f"Quillfeather Docks {ext}", location_raw="Austin, TX",
                             **extra)
            opp = (await normalize_and_store(session, raw, source=src)).opportunity
            assert (opp.comp_currency, opp.comp_currency_known) == want, ext


async def test_a_bare_dollar_is_usd_only_for_a_us_posting_and_an_interval_must_be_stated(sm):
    """Round 4: "$" outside the US is not proof of USD; a figure with no unit is not a year."""
    cases = [
        ("u", "Austin, TX", {"description_html": "<p>Pay: $120,000 - $140,000.</p>"},
         (True, False)),
        ("v", "Toronto, Ontario, Canada", {"description_html": "<p>Pay: $120,000 - $140,000.</p>"},
         (False, False)),
        ("w", "Austin, TX", {"description_html": "<p>Pay: $120,000 - $140,000 per year.</p>"},
         (True, True)),
        ("x", "Austin, TX", {"description_html": "<p>A role.</p>", "comp_min": 40.0,
                             "comp_max": 50.0, "comp_currency": "USD"}, (True, False)),
        ("y", "Austin, TX", {"description_html": "<p>A role.</p>", "comp_min": 40.0,
                             "comp_max": 50.0, "comp_currency": "USD", "comp_interval": "hour"},
         (True, True)),
    ]
    async with sm() as session:
        src = await _source(session)
        for ext, place, extra, want in cases:
            raw = RawPosting(external_id=ext, url=f"https://x/{ext}", title=f"Dock Planner {ext}",
                             org_name=f"Larkspur Quays {ext}", location_raw=place, **extra)
            opp = (await normalize_and_store(session, raw, source=src)).opportunity
            assert (opp.comp_currency_known, opp.comp_interval_known) == want, ext


async def test_an_assumed_interval_or_currency_is_stored_as_not_known(sm):
    """Issue #208 round 5: a value the adapter filled in itself is not a stated one."""
    base = {"description_html": "<p>A role.</p>", "comp_min": 90000.0, "comp_max": 110000.0,
            "comp_currency": "USD", "comp_interval": "year"}
    cases = [
        ("p", {}, (True, True)),
        ("q", {"comp_interval_assumed": True}, (True, False)),
        ("r", {"comp_currency_assumed": True}, (False, True)),
    ]
    async with sm() as session:
        src = await _source(session)
        for ext, extra, want in cases:
            raw = RawPosting(external_id=ext, url=f"https://x/{ext}", title=f"Pier Planner {ext}",
                             org_name=f"Marrow Wharf {ext}", location_raw="Austin, TX",
                             **base, **extra)
            opp = (await normalize_and_store(session, raw, source=src)).opportunity
            assert (opp.comp_currency_known, opp.comp_interval_known) == want, ext


async def test_a_spaced_currency_code_before_a_dollar_is_that_code_on_a_us_posting(sm):
    """Round 5: "CAD $100k-$120k/year" is Canadian dollars, never a bare (US) "$"."""
    cases = [
        ("s1", "<p>Pay: CAD $100k-$120k/year.</p>", ("CAD", True)),
        ("s2", "<p>Pay: AUD $90k - $110k per year.</p>", ("AUD", True)),
        ("s3", "<p>Pay: SGD $8,000/month.</p>", ("SGD", True)),
        ("s4", "<p>Pay: NZD $95k-$105k a year.</p>", ("NZD", True)),
        ("s5", "<p>Pay: BRL $5k/month.</p>", ("USD", False)),  # an unlisted code: unknown
        ("s6", "<p>Pay: $100k-$120k/year.</p>", ("USD", True)),  # a bare "$" in the US
    ]
    async with sm() as session:
        src = await _source(session)
        for ext, body, want in cases:
            raw = RawPosting(external_id=ext, url=f"https://x/{ext}", title=f"Quay Lead {ext}",
                             org_name=f"Brindle Port {ext}", location_raw="Austin, TX",
                             description_html=body)
            opp = (await normalize_and_store(session, raw, source=src)).opportunity
            assert (opp.comp_currency, opp.comp_currency_known) == want, ext
