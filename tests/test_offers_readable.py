"""The Offers page shows readable values (drift A15, A16, A17, A19): whole-dollar money, a stated
sample size, organization and role for competing offers (current employer redacted in confidential
mode), and the walk-away echoed as typed. Database and web layer only."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.base import utcnow_iso
from kestrel.db.engine import make_async_engine
from kestrel.db.models import Offer, Opportunity, Org, Pursuit, Source
from kestrel.privacy import Employer, EmployerMap
from kestrel.web.deps import format_money, format_typed_amount

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


async def _seed() -> None:
    migrate.run_migrations()
    sm = async_sessionmaker(make_async_engine(paths.db_path()), expire_on_commit=False)
    async with sm() as s:
        src = Source(track="employment", kind="greenhouse", name="src", enabled=True)
        s.add(src)
        await s.flush()
        for name, title, base, lo, hi in (
            ("Cascade Labs", "Head of IT", 210000.0, 178500, 210000),
            ("Northwind Freight", "VP Technology", 195000.0, 150000, 200000),
        ):
            org = Org(name=name, normalized_name=name.lower())
            s.add(org)
            await s.flush()
            opp = Opportunity(
                track="employment", opp_type="job_posting", source_id=src.id, org_id=org.id,
                title=title, stage="t3_pass", comp_min=lo, comp_max=hi,
            )
            s.add(opp)
            await s.flush()
            p = Pursuit(opportunity_id=opp.id, track="employment", status="shortlisted")
            s.add(p)
            await s.flush()
            s.add(Offer(pursuit_id=p.id, base=base, status="written", received_at=utcnow_iso()))
        await s.commit()


async def test_money_band_and_benchmark_are_readable():
    await _seed()
    with _client() as c:
        t = c.get("/offers").text
    assert "$210,000" in t
    assert "$210,000.0" not in t
    flat = " ".join(t.split())
    assert "$178,500 to $210,000" in flat
    assert "$178,500.0" not in t
    bench = flat[flat.index("Benchmark:"):]
    bench = bench[: bench.index("</div>")]
    assert ">=" not in bench and "&gt;=" not in bench
    assert "not enough data yet (0 of the 5 needed)" in bench


async def test_competing_offers_show_org_and_role_not_pursuit_number():
    await _seed()
    with _client() as c:
        t = c.get("/offers").text
    # Each of the two offers lists the other as competing, so both must show, by name and role.
    lines = []
    rest = t
    while "Real open competing offers" in rest:
        rest = rest[rest.index("Real open competing offers"):]
        lines.append(rest[: rest.index("</p>")])
        rest = rest[len("Real open competing offers"):]
    assert len(lines) == 2
    joined = " ".join(lines)
    assert "pursuit " not in joined
    assert "Northwind Freight" in joined
    assert "Cascade Labs" in joined
    assert "VP Technology" in joined
    assert "Head of IT" in joined


async def test_confidential_mode_redacts_the_current_employer_in_competing_offers(monkeypatch):
    await _seed()
    emap = EmployerMap(employers=[Employer(name="Northwind Freight", shown_as="a freight company")])

    async def fake(_session):
        return True, emap

    monkeypatch.setattr("kestrel.web.pages.offers.confidential_once", fake)
    with _client() as c:
        t = c.get("/offers").text
    assert "Northwind Freight" not in t
    assert "a freight company" in t


async def _set_offer_text(org_name: str, **fields: str) -> int:
    """Put operator-typed text on one seeded offer; returns its pursuit id."""
    from sqlalchemy import select

    sm = async_sessionmaker(make_async_engine(paths.db_path()), expire_on_commit=False)
    async with sm() as s:
        offer, pursuit_id = (
            await s.execute(
                select(Offer, Pursuit.id)
                .join(Pursuit, Offer.pursuit_id == Pursuit.id)
                .join(Opportunity, Pursuit.opportunity_id == Opportunity.id)
                .join(Org, Opportunity.org_id == Org.id)
                .where(Org.name == org_name)
            )
        ).one()
        for k, v in fields.items():
            setattr(offer, k, v)
        await s.commit()
        return pursuit_id


def _script_result():
    from kestrel.negotiation.scripts import ScriptResult

    return ScriptResult(
        situation_read="Northwind Freight may counter; your Northwind Freight tenure helps.",
        walk_away="200000",
        scripts=[
            {
                "scenario": "counter_offer",
                "script": "I am leaving Northwind Freight for the right package.",
                "justification_basis": "Pay at Northwind Freight today.",
                "likely_pushback": "Northwind Freight pays less.",
                "supported": True,
            }
        ],
        leverage_inventory=["A standing offer from Northwind Freight to stay"],
        risks=["Northwind Freight hears about the search"],
    )


@pytest.mark.parametrize("on", [True, False])
async def test_confidential_mode_redacts_the_negotiation_scripts(monkeypatch, on):
    """The scripts partial is model prose: with confidential mode on, the current employer named
    anywhere in it shows as the operator's "Show it as" text; with it off, the name shows."""
    from kestrel.privacy import EmployerMap as _Map

    await _seed()
    pursuit_id = await _set_offer_text("Cascade Labs")
    emap = EmployerMap(employers=[Employer(name="Northwind Freight", shown_as="a freight company")])

    async def fake_conf(_session):
        return (True, emap) if on else (False, _Map())

    async def fake_draft(_s, _client, pid, *, walk_away):
        assert pid == pursuit_id and walk_away == "200000"
        return _script_result()

    monkeypatch.setattr("kestrel.web.pages.offers.confidential_once", fake_conf)
    monkeypatch.setattr("kestrel.web.pages.offers.draft_scripts", fake_draft)
    with _client() as c:
        r = c.post(f"/offers/{pursuit_id}/scripts", data={"walk_away": "200000"})
    assert r.status_code == 200
    t = r.text
    assert "Situation read" in t
    if on:
        assert "Northwind Freight" not in t
        assert t.count("a freight company") >= 7
    else:
        assert "Northwind Freight" in t
        assert "a freight company" not in t
    assert 'data-exact="200000"' in t  # the walk-away is echoed unchanged either way


async def test_confidential_mode_redacts_typed_offer_terms(monkeypatch):
    """Level, equity, payment terms and exclusivity are free text and can name the employer."""
    await _seed()
    await _set_offer_text(
        "Cascade Labs",
        level_title="Level above my Northwind Freight grade",
        equity_description="Matches Northwind Freight RSUs",
    )
    emap = EmployerMap(employers=[Employer(name="Northwind Freight", shown_as="a freight company")])

    async def fake(_session):
        return True, emap

    monkeypatch.setattr("kestrel.web.pages.offers.confidential_once", fake)
    with _client() as c:
        t = c.get("/offers").text
    assert "Northwind Freight" not in t
    assert "Level above my a freight company grade" in t
    assert "Matches a freight company RSUs" in t


def test_money_formatter():
    assert format_money(210000.0) == "$210,000"
    assert format_money(178500) == "$178,500"


def test_walk_away_echo_formats_plain_amounts_and_keeps_the_rest_verbatim():
    assert format_typed_amount("200000") == "$200,000"
    assert format_typed_amount("$200,000.50") == "$200,000.50"
    assert format_typed_amount("200k base") == "200k base"
    assert format_typed_amount("180-200") == "180-200"
