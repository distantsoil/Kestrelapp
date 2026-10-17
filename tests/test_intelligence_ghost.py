"""Ghost detection: a posting reposted six times with an identical hash is flagged, the cross-org
aggregate signals compute, and the recomputed score reaches Block G of the eval prompt. Offline."""

from __future__ import annotations

from kestrel.db.models import Opportunity, Org, Source
from kestrel.intelligence.ghost import (
    ghost_badge,
    org_simultaneous_generic,
    recompute_ghost,
    req_reappeared,
)


async def _org_src(s) -> tuple[int, int]:
    org = Org(name="GhostCorp", normalized_name="ghostcorp")
    src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
    s.add_all([org, src])
    await s.flush()
    return org.id, src.id


def _generic_opp(org_id, src_id, **kw) -> Opportunity:
    base = dict(
        track="employment", opp_type="job_posting", org_id=org_id, source_id=src_id,
        org_name="GhostCorp", liveness="live", comp_min=None, location_raw=None,
        description_md="We are hiring. Apply now.", version=1, seen_count=1,
    )
    base.update(kw)
    return Opportunity(**base)


async def test_reposted_six_times_identical_hash_is_flagged(sm):
    async with sm() as s:
        org_id, src_id = await _org_src(s)
        opp = _generic_opp(
            org_id, src_id, url="https://x/1", external_id="REQ-1", title="VP of IT",
            content_hash="abc", seen_count=6, version=1,
            first_seen_at="2026-04-01T00:00:00+00:00",
            last_seen_at="2026-07-01T00:00:00+00:00",
            date_posted="2026-07-01",  # fresh post date, but content_hash never changed
        )
        s.add(opp)
        await s.flush()
        score = await recompute_ghost(s, opp)
        await s.commit()

    # seen_count>=5 (+0.3) + reposted_identical (+0.5) + generic no comp/loc/team (+0.2) -> >=0.7.
    assert score >= 0.7
    badge = ghost_badge(score)
    assert badge is not None and badge["label"] == "Likely ghost"


async def test_org_simultaneous_generic_counts_live_generic_postings(sm):
    async with sm() as s:
        org_id, src_id = await _org_src(s)
        for i in range(21):
            s.add(_generic_opp(org_id, src_id, url=f"https://x/{i}", external_id=f"R{i}"))
        # one non-generic (has comp) and one closed generic must NOT count
        s.add(_generic_opp(org_id, src_id, url="https://x/comp", external_id="C", comp_min=200000))
        s.add(_generic_opp(org_id, src_id, url="https://x/closed", external_id="Z",
                           liveness="closed"))
        await s.commit()
    async with sm() as s:
        n = await org_simultaneous_generic(s, org_id)
    assert n == 21  # only the live, generic ones


async def test_req_reappeared_after_close(sm):
    async with sm() as s:
        org_id, src_id = await _org_src(s)
        s.add(_generic_opp(org_id, src_id, url="https://x/old", external_id="REQ-9",
                           liveness="closed"))
        live = _generic_opp(org_id, src_id, url="https://x/new", external_id="REQ-9",
                            liveness="live")
        s.add(live)
        await s.flush()
        reappeared = await req_reappeared(s, live)
    assert reappeared is True


async def test_ghost_score_reaches_block_g(sm):
    # The recomputed score must appear in the tier-2 evaluation placeholder map (Block G input).
    from kestrel.pipeline.context import build_tier2_values

    async with sm() as s:
        org_id, src_id = await _org_src(s)
        opp = _generic_opp(org_id, src_id, url="https://x/1", external_id="REQ-1", title="VP IT",
                           seen_count=6, date_posted="2026-07-01",
                           first_seen_at="2026-04-01T00:00:00+00:00")
        s.add(opp)
        await s.flush()
        score = await recompute_ghost(s, opp)
        org = await s.get(Org, org_id)
        values = build_tier2_values(
            opp=opp, org=org, profile=None, source_name="board", max_jd_tokens=2000,
            few_shot="", comp_intel=None,
        )
    assert values["opp.ghost_score"] == f"{score:.2f}"
    assert float(values["opp.ghost_score"]) >= 0.7  # the ghost signal the model reasons with
