"""Tests for drift fixes (drift review Groups 1-4).

Each test renders a page and asserts:
- NEW text is present
- OLD text is absent
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import make_async_engine, reset_engine
from kestrel.db.models import (
    Evaluation,
    Opportunity,
    Org,
    Setting,
    Source,
)
from kestrel.web.app import build_app

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


def _client() -> TestClient:
    return TestClient(build_app(worker=False))


async def _seed_basic() -> tuple[int, int]:
    """Seed a basic opportunity with evaluation for testing."""
    sm = await _fresh_sm()
    async with sm() as s:
        org = Org(name="Northwind Freight", normalized_name="northwind freight")
        src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
        s.add_all([org, src])
        await s.flush()
        opp = Opportunity(
            track="employment",
            opp_type="job_posting",
            org_id=org.id,
            source_id=src.id,
            title="VP of Information Technology",
            org_name="Northwind Freight",
            url="https://example.com/j/1",
            description_md="Own IT strategy and security for a services firm.",
            stage="t2_done",
            ghost_score=0.1,
        )
        s.add(opp)
        await s.flush()
        opp_id = opp.id
        s.add(
            Evaluation(
                opportunity_id=opp.id,
                tier=2,
                track="employment",
                verdict="apply",
                verdict_model="apply",
                score=4.3,
                weighted_score=4.2,
                needs_review=False,
                dimensions={
                    "role_fit": {"score": 4.5, "why": "strong ownership"},
                    "domain_match": {"score": 4.0, "why": "good overlap"},
                },
                blocks={"a_role_summary": {"what_this_actually_is": "A real IT leadership role."}},
                blockers=[],
                legitimacy={"status": "clean", "signals": []},
            )
        )
        # Create a pursuit and offer so the opportunity appears in the offers list
        from kestrel.db.models import Offer, Pursuit
        pursuit = Pursuit(opportunity_id=opp_id, track="employment", status="offer")
        s.add(pursuit)
        await s.flush()
        offer = Offer(
            pursuit_id=pursuit.id,
            base=185000,
            status="written",
        )
        s.add(offer)
        await s.commit()
    await reset_engine()
    return opp_id, org.id


async def _seed_with_hosted() -> tuple[int, int]:
    """Seed an opportunity with hosted mode enabled."""
    sm = await _fresh_sm()
    async with sm() as s:
        org = Org(name="Northwind Freight", normalized_name="northwind freight")
        src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
        s.add_all([org, src])
        await s.flush()
        opp = Opportunity(
            track="employment",
            opp_type="job_posting",
            org_id=org.id,
            source_id=src.id,
            title="VP of Information Technology",
            org_name="Northwind Freight",
            url="https://example.com/j/1",
            description_md="Own IT strategy and security for a services firm.",
            stage="t2_done",
            ghost_score=0.1,
        )
        s.add(opp)
        await s.flush()
        opp_id = opp.id
        # Enable hosted mode - acknowledged_terms must be a dict
        s.add(Setting(key="llm.hosted_mode.enabled", value=True))
        s.add(Setting(key="llm.hosted_mode.acknowledged_at", value="2026-01-01T00:00:00+00:00"))
        s.add(Setting(key="llm.hosted_mode.acknowledged_terms",
                      value={"tier1": "ack", "tier2": "ack"}))
        s.add(Setting(key="llm.tiers.tier1.backend", value="frontier"))
        s.add(Setting(key="llm.tiers.tier1.model", value="kestrel-tier1"))
        s.add(Setting(key="llm.tiers.tier2.backend", value="frontier"))
        s.add(Setting(key="llm.tiers.tier2.model", value="kestrel-tier2"))
        # Also need to set the hosted mode tier assignments
        s.add(Setting(key="llm.hosted_tiers.tier1.backend", value="frontier"))
        s.add(Setting(key="llm.hosted_tiers.tier1.model", value="kestrel-tier1"))
        s.add(Setting(key="llm.hosted_tiers.tier2.backend", value="frontier"))
        s.add(Setting(key="llm.hosted_tiers.tier2.model", value="kestrel-tier2"))
        await s.commit()
    await reset_engine()
    return opp_id, org.id


async def _seed_second_offer(opp_id: int) -> int:
    """Add a second offer for matrix comparison."""
    sm = await _fresh_sm()
    async with sm() as s:
        org = Org(name="Acme Corp", normalized_name="acme corp")
        src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
        s.add_all([org, src])
        await s.flush()
        opp2 = Opportunity(
            track="employment",
            opp_type="job_posting",
            org_id=org.id,
            source_id=src.id,
            title="Senior IT Director",
            org_name="Acme Corp",
            url="https://example.com/j/2",
            description_md="Lead IT operations.",
            stage="t2_done",
            ghost_score=0.1,
        )
        s.add(opp2)
        await s.flush()
        opp2_id = opp2.id
        s.add(
            Evaluation(
                opportunity_id=opp2.id,
                tier=2,
                track="employment",
                verdict="consider",
                verdict_model="consider",
                score=3.8,
                weighted_score=3.7,
                needs_review=False,
                dimensions={
                    "role_fit": {"score": 3.5, "why": "good fit"},
                    "domain_match": {"score": 4.0, "why": "matches domain"},
                },
                blocks={},
                blockers=[],
                legitimacy={"status": "clean", "signals": []},
            )
        )
        # Create a pursuit and offer for the second opportunity
        from kestrel.db.models import Offer, Pursuit
        pursuit2 = Pursuit(opportunity_id=opp2_id, track="employment", status="offer")
        s.add(pursuit2)
        await s.flush()
        offer2 = Offer(
            pursuit_id=pursuit2.id,
            base=200000,
            status="written",
        )
        s.add(offer2)
        await s.commit()
    await reset_engine()
    return opp2_id


async def _fresh_sm():
    await reset_engine()
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    from sqlalchemy.ext.asyncio import async_sessionmaker
    return async_sessionmaker(engine, expire_on_commit=False)


# =========================================================================
# A1: "operator" -> "you/your" in user-facing text
# =========================================================================


def test_a1_operator_replaced_in_models_page():
    """A1: models.html should say 'your latest label' not 'operator's latest label'."""
    opp_id, _ = asyncio.run(_seed_basic())
    c = _client()
    html = c.get("/models").text

    # New text present
    assert "your latest label" in html
    # Old text absent
    assert "operator's latest label" not in html


# =========================================================================
# A8: "Use for Tier 3" -> "Use for deeper reads"
# =========================================================================


def test_a8_use_for_deeper_reads():
    """A8: hosted_status.html button says 'Use for deeper reads'."""
    opp_id, _ = asyncio.run(_seed_with_hosted())
    c = _client()
    # The hosted panel is loaded via HTMX from /settings/hosted
    html = c.get("/settings/hosted").text

    assert "Use for deeper reads" in html
    assert "Use for Tier 3" not in html


# =========================================================================
# A12: Axis labels in comparison matrix
# =========================================================================


def test_a12_axis_labels_in_comparison_matrix():
    """A12: comparison matrix uses human-readable axis labels."""
    opp_id, _ = asyncio.run(_seed_basic())
    asyncio.run(_seed_second_offer(opp_id))
    c = _client()
    c.post(f"/offers/{opp_id}/qual", data={"qual_level_scope": "5", "qual_org_risk": "1"})

    html = c.get("/offers").text

    assert "Level and scope" in html
    assert "Company risk" in html
    assert "Reporting line" in html
    assert "Location and travel" in html
    assert "Fit with where you are headed" in html
    assert "Total first-year comp" in html
    assert "Four-year value" in html


# =========================================================================
# A18: Sidebar status rail model name
# =========================================================================


def test_a18_sidebar_status_rail_model_name(monkeypatch):
    """A18: status rail shows plain tier name for Kestrel models."""
    from kestrel.web import routes_control

    async def _fake_ollama(native_url: str):
        return {"up": True, "loaded": ["kestrel-tier2:latest"]}

    monkeypatch.setattr(routes_control, "_probe_ollama", _fake_ollama)

    asyncio.run(_seed_basic())
    c = _client()
    html = c.get("/status/rail").text

    # Plain tier name present
    assert "Full review" in html
    # Raw model name absent
    assert "kestrel-tier2:latest" not in html
    assert "kestrel-tier3:latest" not in html

