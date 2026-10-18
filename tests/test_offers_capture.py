"""Offer capture on the /offers page, and the comparison matrix's qualitative axes.

The negotiation module was complete and unreachable: nothing anywhere in the application ever
constructed an `Offer` or `EngagementOffer` row, so `/offers` rendered zero rows, the matrix was
guarded behind `len(rows) > 1` and never appeared, `benchmark_offer` always got `offer=None`, and
`_competing` always returned `[]` -- which meant the no-bluff guard silently stripped the
competing-offer scenario from every negotiation script. The five qualitative axes the matrix
advertises also read None forever, because nothing ever produced a rating.

These tests record offers through the HTTP routes the operator uses, then assert the figures the
rest of the module reads are actually there.

Cloud-safe: database and web layer only, no model, no network.
"""

from __future__ import annotations

from itertools import count

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import make_async_engine
from kestrel.db.models import EngagementOffer, Offer, Opportunity, Org, Pursuit, Setting, Source

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


_ORG_SEQ = count(1)


def _fresh_sm() -> async_sessionmaker:
    """A sessionmaker on the database the WEB app uses, so rows seeded in a test are the rows the
    route sees (the `sm` fixture builds its own file, which the app never opens)."""
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed_pursuit(*, track: str = "employment", org_name: str | None = None) -> int:
    sm = _fresh_sm()
    async with sm() as s:
        src = Source(track=track, kind="greenhouse", name=f"{track}-src", enabled=True)
        # org.normalized_name is UNIQUE, and two offers must be two DIFFERENT organizations to be
        # worth comparing, so each seed gets its own.
        org_name = org_name or f"Acme {next(_ORG_SEQ)}"
        org = Org(name=org_name, normalized_name=org_name.lower())
        s.add_all([src, org])
        await s.flush()
        opp = Opportunity(
            track=track,
            opp_type="job_posting" if track == "employment" else "contract_posting",
            source_id=src.id,
            org_id=org.id,
            title="VP of IT",
            stage="t3_pass",
            comp_min=180000,
            comp_max=220000,
        )
        s.add(opp)
        await s.flush()
        p = Pursuit(opportunity_id=opp.id, track=track, status="shortlisted")
        s.add(p)
        await s.commit()
        return p.id


async def _rows(model=Offer) -> list:
    sm = _fresh_sm()
    async with sm() as s:
        return list((await s.execute(select(model))).scalars().all())


async def _record(*, track: str = "employment", **extra) -> int:
    """Post one offer through the real route and return the pursuit id."""
    pid = await _seed_pursuit(track=track)
    payload = {"pursuit_id": pid, "track": track, **extra}
    with _client() as c:
        r = c.post("/offers", data=payload)
    assert r.status_code == 200
    return pid


async def test_recording_an_offer_creates_the_row():
    """/offers was the only place an Offer could be created, and nothing created one."""
    await _record(
        base="185000",
        bonus_target_pct="15",
        equity_value_estimate="200000",
        signing="10000",
        level_title="Director",
        location_policy="hybrid 3 days",
        status="written",
    )
    rows = await _rows()
    assert len(rows) == 1
    o = rows[0]
    assert o.base == 185000.0
    assert o.bonus_target_pct == 15.0
    assert o.equity_value_estimate == 200000.0
    assert o.signing == 10000.0
    assert o.level_title == "Director"
    assert o.status == "written"


async def test_the_recorded_offer_becomes_visible_on_the_page():
    pid = await _record(base="185000")
    with _client() as c:
        r = c.get("/offers")
    assert r.status_code == 200
    assert "Acme " in r.text  # the seeded org
    assert "185,000" in r.text  # the offer's own figure is rendered
    assert f'value="{pid}"' in r.text  # the pursuit it belongs to


async def test_a_blank_field_is_not_recorded_but_a_zero_is():
    """A blank means "was not told" and must stay None. A typed 0 is a real figure the operator
    gave (a zero bonus target, a zero signing bonus) and must not be flattened to None, or the
    valuation silently reads it as missing."""
    await _record(base="0", signing="0")
    o = (await _rows())[0]
    assert o.base == 0.0  # typed 0 is kept
    assert o.signing == 0.0
    assert o.bonus_target_pct is None  # blank stays None, it is not 0


async def test_recording_an_offer_moves_the_pursuit_to_negotiating():
    pid = await _record(base="185000")
    sm = _fresh_sm()
    async with sm() as s:
        p = await s.get(Pursuit, pid)
        assert p.status == "offer"  # the employment track's own offer stage


async def test_a_consulting_engagement_offer_is_recorded_on_its_own_table():
    await _record(
        track="consulting",
        day_rate="1500",
        days_committed="120",
        duration_weeks="24",
        payment_terms="net 30",
    )
    rows = await _rows(EngagementOffer)
    assert len(rows) == 1
    assert rows[0].day_rate == 1500.0 and rows[0].days_committed == 120
    assert not (await _rows(Offer))  # the employment table stays empty


async def test_qualitative_ratings_are_the_operators_and_reach_the_matrix():
    """The five qualitative axes exist in compare.py and nothing ever produced a rating, so every
    one read None and the matrix ranked offers on money alone while listing seven columns."""
    from kestrel.negotiation.compare import QUAL_AXES, offer_comp_from

    await _record(base="185000")
    oid = (await _rows())[0].id
    with _client() as c:
        r = c.post(f"/offers/{oid}/qual", data={"qual_level_scope": "4", "qual_org_risk": "2"})
    assert r.status_code == 200

    o = (await _rows())[0]
    assert o.qual_json == {"level_scope": 4.0, "org_risk": 2.0}

    comp = offer_comp_from(o, offer_id=o.id, label="Acme", qual=o.qual_json)
    assert comp.raw("level_scope") == 4.0
    assert comp.raw("org_risk") == 2.0
    # the axes the operator did not rate stay absent rather than becoming 0
    assert comp.raw("reporting_line") is None
    assert "level_scope" in QUAL_AXES and "org_risk" in QUAL_AXES


async def test_a_rating_is_clamped_to_zero_five():
    """The form says 0-5; a hand-rolled POST must not put 99 into the matrix and skew every
    normalization built from it."""
    await _record(base="185000")
    oid = (await _rows())[0].id
    with _client() as c:
        r = c.post(f"/offers/{oid}/qual", data={"qual_level_scope": "99", "qual_org_risk": "-3"})
    assert r.status_code == 200
    assert (await _rows())[0].qual_json == {"level_scope": 5.0, "org_risk": 0.0}


async def test_matrix_weights_are_the_operators_and_persist():
    await _record(base="185000")
    await _record(base="200000")
    with _client() as c:
        r = c.post("/offers/weights", data={"total_first_year": "3", "four_year_value": "1"})
    assert r.status_code == 200
    sm = _fresh_sm()
    async with sm() as s:
        row = await s.get(Setting, "negotiation.matrix_weights")
        assert row is not None
        assert row.value["total_first_year"] == 3.0
        assert row.value["four_year_value"] == 1.0
        # an axis left blank is simply absent, so compare_offers renormalizes over what is set
        assert "org_risk" not in row.value


async def test_two_offers_produce_a_comparison_matrix_with_both_parts():
    """The matrix was guarded behind len(rows) > 1 and never rendered, because no offer could
    exist. With two recorded it appears, carrying both the quantitative and the operator-rated
    qualitative columns."""
    await _record(base="185000")
    await _record(base="200000")

    ids = [o.id for o in await _rows()]
    with _client() as c:
        for oid in ids:
            c.post(f"/offers/{oid}/qual", data={"qual_level_scope": "5", "qual_org_risk": "1"})
        r = c.get("/offers")

    assert r.status_code == 200
    assert "Comparison matrix" in r.text
    for axis in ("total first year", "four year value", "level scope", "org risk"):
        assert axis in r.text.lower()


async def test_recording_against_a_missing_pursuit_is_refused_not_a_500():
    _fresh_sm()  # the app's database must exist before the route touches it
    with _client() as c:
        r = c.post("/offers", data={"pursuit_id": "999999", "track": "employment"})
    assert r.status_code == 200
    assert not (await _rows())


async def test_the_pursuit_track_decides_the_table_not_the_form():
    """D-RR1-2. The form's `track` select defaults to employment regardless of the pursuit, so
    trusting it filed a consulting pursuit's offer in the employment table -- which the benchmark,
    which picks its table from `pursuit.track`, never reads. The pursuit is authoritative.

    The posted track is ignored entirely, including when it is nonsense: the pursuit still decides,
    so a stale or hand-crafted select value cannot misfile the offer."""
    pid = await _seed_pursuit(track="consulting")
    with _client() as c:
        r = c.post("/offers", data={"pursuit_id": pid, "track": "made-up", "day_rate": "1500"})
    assert r.status_code == 200
    assert not (await _rows(Offer)), "a consulting pursuit's offer was filed as employment"
    assert await _rows(EngagementOffer), "the offer was not filed on the pursuit's own track"


async def test_a_pursuit_with_an_unknown_track_is_refused():
    """The one case that is still an error: the PURSUIT's own track is not a track we know, so
    there is no correct table to file into."""
    pid = await _seed_pursuit(track="employment")
    sm = _fresh_sm()
    async with sm() as s:
        p = await s.get(Pursuit, pid)
        p.track = "made-up"
        await s.commit()

    with _client() as c:
        r = c.post("/offers", data={"pursuit_id": pid, "base": "185000"})
    assert r.status_code == 200
    assert not (await _rows()), "an offer was filed for a pursuit on an unknown track"


async def test_nothing_on_this_page_sends_anything():
    """Hard rule 1: recording an offer must not contact anyone, and the page has no send action."""
    await _record(base="185000")
    with _client() as c:
        r = c.get("/offers")
    assert r.status_code == 200
    assert "mailto:" not in r.text
    assert "tel:" not in r.text
