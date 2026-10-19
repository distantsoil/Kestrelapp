"""Comp corpus query (intelligence layer): correct percentiles and sample size, and a refusal below
five observations (constraint 6). Fully offline."""

from __future__ import annotations

from kestrel.db.base import utcnow_iso
from kestrel.db.models import CompObservation
from kestrel.intelligence.comp import comp_range

_TITLE = "vp of it"


async def _obs(
    sm, mids: list[float], *, geo="CA", size="mid", currency="USD", interval="year"
) -> None:
    async with sm() as s:
        for m in mids:
            s.add(
                CompObservation(
                    normalized_title=_TITLE, geo_bucket=geo, org_size_band=size,
                    min=m, max=m, currency=currency, interval=interval, source="posting",
                    observed_at=utcnow_iso(),
                )
            )
        await s.commit()


async def test_day_rates_are_never_pooled_with_annual_salaries(sm):
    # Five annual figures plus three fractional day rates under the same title: the range is
    # computed on the annual subset ONLY, and n states that subset.
    await _obs(sm, [100000, 200000, 300000, 400000, 500000])
    await _obs(sm, [1200, 1500, 1800], interval="day")
    async with sm() as s:
        rng = await comp_range(s, normalized_title=_TITLE, geo_bucket="CA", org_size_band="mid")
    assert rng.sufficient is True and rng.n == 5
    assert rng.median == 300000 and rng.low == 100000  # a 1200/day never dragged the range down
    assert rng.interval == "year" and "annual only" in rng.scope
    assert rng.as_dict()["interval"] == "year"


async def test_mixed_corpus_with_too_few_annual_stays_insufficient(sm):
    # Three annual + four day rates is NOT seven observations (hard rule 6).
    await _obs(sm, [200000, 250000, 300000])
    await _obs(sm, [1200, 1300, 1400, 1500], interval="day")
    async with sm() as s:
        rng = await comp_range(s, normalized_title=_TITLE, geo_bucket="CA", org_size_band="mid")
    assert rng.sufficient is False and rng.n == 3


async def test_single_interval_corpus_of_day_rates_pools_and_says_so(sm):
    await _obs(sm, [1000, 1200, 1400, 1600, 1800], interval="day")
    async with sm() as s:
        rng = await comp_range(s, normalized_title=_TITLE, geo_bucket="CA", org_size_band="mid")
    assert rng.sufficient is True and rng.n == 5 and rng.interval == "day"
    assert "per day" in rng.summary()


async def test_percentiles_are_correct(sm):
    # Midpoints 100,200,300,400,500 -> median 300, p25 200, p75 400 (inclusive quartiles).
    await _obs(sm, [100000, 200000, 300000, 400000, 500000])
    async with sm() as s:
        rng = await comp_range(s, normalized_title=_TITLE, geo_bucket="CA", org_size_band="mid")
    assert rng.sufficient is True
    assert rng.n == 5
    assert rng.median == 300000
    assert rng.p25 == 200000
    assert rng.p75 == 400000
    assert rng.low == 100000 and rng.high == 500000
    d = rng.as_dict()
    assert d["sufficient"] is True and d["insufficient_message"] is None


async def test_refuses_below_five(sm):
    await _obs(sm, [200000, 250000, 300000])  # only 3
    async with sm() as s:
        rng = await comp_range(s, normalized_title=_TITLE, geo_bucket="CA", org_size_band="mid")
    assert rng.sufficient is False
    assert rng.n == 3
    assert rng.median is None and rng.p25 is None
    d = rng.as_dict()
    assert d["sufficient"] is False
    assert "not enough data yet" in d["insufficient_message"]
