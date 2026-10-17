"""Comp benchmarking: the range is grounded in the operator's own corpus and ALWAYS carries its
sample size; under five observations it reports insufficient rather than a precise-looking number,
and at n>=20 it reports a sensible median and range (constraint 6). benchmark_offer also surfaces
the stated band, real competing offers, and history. Fully offline."""

from __future__ import annotations

from kestrel.db.base import utcnow_iso
from kestrel.db.models import CompObservation, Offer, Opportunity, Org, Pursuit, Source
from kestrel.negotiation.benchmark import benchmark_offer, comp_range

_TITLE = "vp of it"


async def _add_obs(sm, n: int, *, lo: float, hi: float, geo="CA", size="mid") -> None:
    async with sm() as s:
        step = (hi - lo) / max(n - 1, 1)
        for i in range(n):
            mid = lo + step * i
            s.add(
                CompObservation(
                    normalized_title=_TITLE, geo_bucket=geo, org_size_band=size,
                    min=mid - 10000, max=mid + 10000, currency="USD", interval="year",
                    source="posting", observed_at=utcnow_iso(),
                )
            )
        await s.commit()


async def test_comp_range_insufficient_below_five(sm):
    await _add_obs(sm, 3, lo=200000, hi=260000)
    async with sm() as s:
        rng = await comp_range(s, normalized_title=_TITLE, geo_bucket="CA", org_size_band="mid")
    assert rng.sufficient is False
    assert rng.n == 3
    assert rng.median is None  # no precise number is manufactured from three points
    assert "insufficient data" in rng.summary()
    assert "n=3" in rng.summary()


async def test_comp_range_sensible_at_twenty(sm):
    # 20 observations with midpoints evenly spread 200k..295k (each obs is mid +/- 10k).
    await _add_obs(sm, 20, lo=200000, hi=295000)
    async with sm() as s:
        rng = await comp_range(s, normalized_title=_TITLE, geo_bucket="CA", org_size_band="mid")
    assert rng.sufficient is True
    assert rng.n == 20
    assert rng.low == 200000 and rng.high == 295000
    assert 240000 <= rng.median <= 260000  # centered, not skewed
    assert rng.p25 is not None and rng.p75 is not None
    assert rng.p25 < rng.median < rng.p75
    assert "n=20" in rng.summary() and "median" in rng.summary()


async def test_comp_range_never_pools_across_currencies(sm):
    # 3 USD + 20 GBP for the same title. The USD query must not borrow the GBP rows to reach five.
    await _add_obs(sm, 3, lo=250000, hi=260000)  # USD (default in _add_obs)
    async with sm() as s:
        for i in range(20):
            s.add(
                CompObservation(
                    normalized_title=_TITLE, geo_bucket="UK", org_size_band="mid",
                    min=140000 + i * 1000, max=160000 + i * 1000, currency="GBP",
                    interval="year", source="posting", observed_at=utcnow_iso(),
                )
            )
        await s.commit()
    async with sm() as s:
        usd = await comp_range(s, normalized_title=_TITLE, currency="USD")
        gbp = await comp_range(s, normalized_title=_TITLE, currency="GBP")
    assert usd.sufficient is False and usd.n == 3  # only the 3 USD rows, not pooled to 23
    assert gbp.sufficient is True and gbp.n == 20


async def test_comp_range_widens_scope_when_tight_cut_is_thin(sm):
    # Only 2 in CA/mid, but 20 across all geos -> falls back and states the wider scope.
    await _add_obs(sm, 2, lo=200000, hi=210000, geo="CA", size="mid")
    await _add_obs(sm, 20, lo=210000, hi=300000, geo="NY", size="large")
    async with sm() as s:
        rng = await comp_range(s, normalized_title=_TITLE, geo_bucket="CA", org_size_band="mid")
    assert rng.sufficient is True
    assert rng.scope == "all geographies"  # neither CA/mid nor CA alone had five


async def _mk_opp(s, org, src, url: str) -> Opportunity:
    opp = Opportunity(
        track="employment", opp_type="job_posting", org_id=org.id, source_id=src.id,
        title="VP of IT", org_name="Acme", url=url, state="CA",
        comp_min=210000, comp_max=260000, comp_currency="USD", comp_interval="year",
    )
    s.add(opp)
    await s.flush()
    return opp


async def _seed_pursuit_with_offer(sm) -> int:
    async with sm() as s:
        org = Org(name="Acme", normalized_name="acme", size_band="mid")
        src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
        s.add_all([org, src])
        await s.flush()
        # Pursuit.opportunity_id is unique, so each pursuit needs its own opportunity.
        opp = await _mk_opp(s, org, src, "https://x/1")
        opp2 = await _mk_opp(s, org, src, "https://x/2")
        opp3 = await _mk_opp(s, org, src, "https://x/3")
        p = Pursuit(opportunity_id=opp.id, track="employment", status="offer")
        p2 = Pursuit(opportunity_id=opp2.id, track="employment", status="offer")
        p3 = Pursuit(opportunity_id=opp3.id, track="employment", status="closed")
        s.add_all([p, p2, p3])
        await s.flush()
        pid = p.id
        s.add(Offer(pursuit_id=pid, base=240000, bonus_target_pct=20,
                    equity_value_estimate=200000, signing=20000, level_title="VP",
                    status="written", received_at=utcnow_iso()))
        # A REAL competing offer on another pursuit (open) and an accepted historical one.
        s.add(Offer(pursuit_id=p2.id, base=255000, status="verbal", received_at=utcnow_iso()))
        s.add(Offer(pursuit_id=p3.id, base=230000, status="accepted", received_at=utcnow_iso()))
        await s.commit()
        return pid


async def test_benchmark_offer_surfaces_band_competing_history(sm):
    pid = await _seed_pursuit_with_offer(sm)
    await _add_obs(sm, 20, lo=200000, hi=295000)
    async with sm() as s:
        b = await benchmark_offer(s, pid)

    assert b is not None
    # first-year = 240000 + 20% bonus (48000) + signing 20000 + equity 200000/4 (50000) = 358000
    assert b.offer_first_year == 358000
    assert b.stated_band == (210000, 260000)
    assert b.inside_stated_band is True  # base 240k within 210-260k
    assert b.comp.sufficient is True and b.comp.n == 20
    assert len(b.competing) == 1  # a real, open offer on another pursuit
    assert b.competing[0]["status"] == "verbal"
    assert len(b.history) == 1  # the accepted offer
