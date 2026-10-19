"""`kestrel research <org>` runs the shared company research (kestrel.services.org_research).

No network, no model: research_org is replaced at the shared service's import site, so the polite
client is constructed but never asked to fetch."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from kestrel.cli import app
from kestrel.db import migrate
from kestrel.db.engine import get_sessionmaker, reset_engine
from kestrel.db.models import Org, OrgResearch

pytestmark = pytest.mark.usefixtures("_isolate_data_home")
runner = CliRunner()


def _seed(names: list[str]) -> list[int]:
    async def go() -> list[int]:
        migrate.run_migrations()
        async with get_sessionmaker()() as s:
            orgs = [Org(name=n, normalized_name=n.lower()) for n in names]
            s.add_all(orgs)
            await s.commit()
            ids = [o.id for o in orgs]
        await reset_engine()
        return ids

    return asyncio.run(go())


def _fake(insufficient: bool, calls: list[int]):
    async def fake_research_org(session, client, org, *, settings, http=None, operator_paste=None):
        calls.append(org.id)
        return OrgResearch(
            org_id=org.id,
            summary_md="Solid summary text.",
            signals=[
                {
                    "category": "hiring",
                    "statement": "opening a new office",
                    "url": "https://beacon.example/news",
                    "source_index": 0,
                }
            ],
            unknowns=["funding stage"],
            angle="lead with the data platform",
            insufficient_sources=insufficient,
        )

    return fake_research_org


def _patch(monkeypatch, insufficient=False):
    calls: list[int] = []
    monkeypatch.setattr(
        "kestrel.services.org_research.research_org", _fake(insufficient, calls)
    )
    return calls


def test_found_prints_summary_signals_unknowns_and_angle(monkeypatch):
    _seed(["Beacon Robotics"])
    calls = _patch(monkeypatch)
    r = runner.invoke(app, ["research", "BEACON robotics"])
    assert r.exit_code == 0, r.output
    assert "Solid summary text." in r.output
    assert "opening a new office" in r.output
    assert "https://beacon.example/news" in r.output
    assert "funding stage" in r.output
    assert "lead with the data platform" in r.output
    assert "INSUFFICIENT" not in r.output
    assert len(calls) == 1


def test_not_found_exits_1_and_creates_nothing(monkeypatch):
    _seed(["Beacon Robotics"])
    calls = _patch(monkeypatch)
    r = runner.invoke(app, ["research", "Nonexistent Co"])
    assert r.exit_code == 1
    assert "No organization matches" in r.output
    assert calls == []

    async def count() -> int:
        async with get_sessionmaker()() as s:
            n = len((await s.execute(select(Org))).scalars().all())
        await reset_engine()
        return n

    assert asyncio.run(count()) == 1


def test_ambiguous_lists_ids_and_exits_1_then_id_picks(monkeypatch):
    ids = _seed(["Acme Labs", "Acme Foods"])
    calls = _patch(monkeypatch)
    r = runner.invoke(app, ["research", "acme"])
    assert r.exit_code == 1
    assert "Acme Labs" in r.output and "Acme Foods" in r.output
    assert str(ids[0]) in r.output and str(ids[1]) in r.output
    assert calls == []

    r2 = runner.invoke(app, ["research", "acme", "--id", str(ids[1])])
    assert r2.exit_code == 0, r2.output
    assert "Acme Foods" in r2.output
    assert calls == [ids[1]]


def test_insufficient_is_labeled_and_not_a_summary(monkeypatch):
    _seed(["Beacon Robotics"])
    _patch(monkeypatch, insufficient=True)
    r = runner.invoke(app, ["research", "Beacon Robotics"])
    assert r.exit_code == 0, r.output
    assert "INSUFFICIENT" in r.output
    assert "fewer than two sources" in r.output
    assert "Solid summary text." not in r.output


def test_display_name_with_suffix_and_punctuation_matches(monkeypatch):
    _seed(["northwind freight"])
    calls = _patch(monkeypatch)
    r = runner.invoke(app, ["research", "Northwind Freight, LLC"])
    assert r.exit_code == 0, r.output
    assert len(calls) == 1


def test_percent_in_name_does_not_match_everything(monkeypatch):
    _seed(["Beacon Robotics", "Northwind Freight"])
    calls = _patch(monkeypatch)
    r = runner.invoke(app, ["research", "%"])
    assert r.exit_code == 1
    assert calls == []
