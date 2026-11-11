"""GET /api/pursuits (D-IMPORT-10, issue 147 M3): a read-only list of the person's applications
with the same Kestrel ID the applications export carries, so a script can bring an outside tracker
up to date by ID. Invented rows only."""

from __future__ import annotations

import asyncio
import csv
import io
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import (
    DocMaster,
    DocSection,
    Opportunity,
    Org,
    Pursuit,
    Setting,
    Source,
    StatusEvent,
)
from kestrel.web import auth

_EMPLOYER = "Northwind Freight"
_PLACEHOLDER = "[Confidential]"


@pytest.fixture(autouse=True)
def _reset_key():
    yield
    auth.reset_cache()


async def _seed(*, confidential: bool = False, old_days: int = 40) -> dict[str, int]:
    await reset_engine()
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    old = (now - timedelta(days=old_days)).isoformat(timespec="microseconds")
    new = now.isoformat(timespec="microseconds")
    async with sm() as s:
        src = Source(track="employment", kind="import", name="Applied history import (employment)",
                     config={}, enabled=False)
        remotive = Source(track="employment", kind="remotive", name="Remotive", config={},
                          enabled=False)
        s.add_all([src, remotive])
        await s.flush()
        ids: dict[str, int] = {}
        rows = [
            ("old", "Acme Corp", "Planner", "applied", old, old,
             "Imported from Board on 2026-09-01."),
            ("new", _EMPLOYER, "Analyst", "screening", old, new,
             "Imported from Site on 2026-09-02."),
            ("third", "Birch Co", "Buyer", "rejected", old, old, "Imported on 2026-09-03."),
        ]
        for key, company, title, status, applied, active, desc in rows:
            org = Org(name=company, normalized_name=company.lower())
            s.add(org)
            await s.flush()
            opp = Opportunity(track="employment", opp_type="job_posting", title=title,
                              org_id=org.id, org_name=company, url=f"https://x/{key}",
                              stage="new", source_id=src.id, description_md=desc)
            s.add(opp)
            await s.flush()
            p = Pursuit(opportunity_id=opp.id, track="employment", status=status,
                        applied_at=applied, last_activity_at=active)
            s.add(p)
            await s.flush()
            s.add(StatusEvent(pursuit_id=p.id, from_status=None, to_status=status,
                              occurred_at=applied))
            ids[key] = p.id
        # A role from a source whose terms forbid passing postings on, with no other copy.
        org = Org(name="Remote Co", normalized_name="remote co")
        s.add(org)
        await s.flush()
        opp = Opportunity(track="employment", opp_type="job_posting", title="Remote Role",
                          org_id=org.id, org_name="Remote Co", url="https://x/remote",
                          stage="new", source_id=remotive.id)
        s.add(opp)
        await s.flush()
        s.add(Pursuit(opportunity_id=opp.id, track="employment", status="applied",
                      applied_at=old, last_activity_at=old))
        master = DocMaster(track="employment", name="Resume", doc_kind="resume")
        s.add(master)
        await s.flush()
        s.add(DocSection(doc_master_id=master.id, section_kind="experience", sort_order=0,
                         org_label=_EMPLOYER, org_label_redacted=_PLACEHOLDER))
        if confidential:
            s.add(Setting(key="confidential_mode", value=True))
        await s.commit()
    await engine.dispose()
    forget_engine()
    return ids


def _client(host: str = "127.0.0.1") -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False), base_url="http://127.0.0.1",
                      raise_server_exceptions=False, client=(host, 12345))


def test_the_list_has_each_application_with_the_fields_a_script_needs():
    asyncio.run(_seed())
    with _client() as c:
        body = c.get("/api/pursuits").json()
    rows = {r["role"]: r for r in body["rows"]}
    assert set(rows) == {"Planner", "Analyst", "Buyer"}  # the no-export source is left out
    one = rows["Planner"]
    assert set(one) == {"id", "track", "role", "company", "source", "applied_at", "status",
                        "last_change"}
    assert one["company"] == "Acme Corp" and one["status"] == "applied"
    assert one["source"] == "Board"  # the board the import recorded, not our import source
    assert rows["Buyer"]["source"] == "Applied history import (employment)"
    assert one["applied_at"] == (datetime.now(UTC) - timedelta(days=40)).date().isoformat()
    assert body["next_cursor"] is None


def test_the_ids_are_the_ones_the_applications_export_carries():
    asyncio.run(_seed())
    with _client() as c:
        api = {r["role"]: r["id"] for r in c.get("/api/pursuits").json()["rows"]}
        exported = c.get("/analytics/export/applications.csv").text.lstrip("﻿")
    rows = list(csv.DictReader(io.StringIO(exported)))
    assert {r["Role"]: r["Kestrel ID"] for r in rows} == api


def test_since_keeps_only_applications_that_changed_after_it():
    asyncio.run(_seed(old_days=40))
    since = (datetime.now(UTC) - timedelta(days=5)).date().isoformat()
    with _client() as c:
        rows = c.get("/api/pursuits", params={"since": since}).json()["rows"]
        none = c.get("/api/pursuits", params={"since": "2999-01-01"}).json()["rows"]
        bad = c.get("/api/pursuits", params={"since": "last tuesday"})
    assert [r["role"] for r in rows] == ["Analyst"] and none == []
    assert bad.status_code == 400 and "since" in bad.json()["error"]


def test_status_and_track_filters_and_cursor_pages():
    asyncio.run(_seed())
    with _client() as c:
        only = c.get("/api/pursuits", params={"status": "rejected"}).json()["rows"]
        first = c.get("/api/pursuits", params={"limit": 2}).json()
        second = c.get("/api/pursuits", params={"limit": 2, "cursor": first["next_cursor"]}).json()
        consulting = c.get("/api/pursuits", params={"track": "consulting"}).json()["rows"]
        badcur = c.get("/api/pursuits", params={"cursor": "x; drop"})
        badlimit = c.get("/api/pursuits", params={"limit": "many"})
    assert [r["role"] for r in only] == ["Buyer"]
    assert len(first["rows"]) == 2 and first["next_cursor"]
    assert len(second["rows"]) == 1 and second["next_cursor"] is None
    assert {r["role"] for r in first["rows"] + second["rows"]} == {"Planner", "Analyst", "Buyer"}
    assert consulting == [] and badcur.status_code == 400 and badlimit.status_code == 400


def test_confidential_mode_redacts_the_current_employer_as_the_opportunities_api_does():
    asyncio.run(_seed(confidential=True))
    with _client() as c:
        companies = [r["company"] for r in c.get("/api/pursuits").json()["rows"]]
    assert _EMPLOYER not in companies and _PLACEHOLDER in companies


async def _employer_named_sources() -> None:
    from sqlalchemy import select

    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        src = Source(track="employment", kind="greenhouse", name=f"{_EMPLOYER} jobs (Greenhouse)",
                     config={"token": "nw"}, enabled=True)
        s.add(src)
        await s.flush()
        org = (await s.execute(select(Org).where(Org.name == _EMPLOYER))).scalar_one()
        opp = Opportunity(track="employment", opp_type="job_posting", title="Internal Mover",
                          org_id=org.id, org_name=_EMPLOYER, url="https://x/int", stage="new",
                          source_id=src.id, description_md="body")
        s.add(opp)
        await s.flush()
        s.add(Pursuit(opportunity_id=opp.id, track="employment", status="applied"))
        imp = (await s.execute(select(Source).where(Source.kind == "import"))).scalar_one()
        other = Opportunity(
            track="employment", opp_type="job_posting", title="Other", org_id=org.id,
            org_name="Zed", url="https://x/zed", stage="new", source_id=imp.id,
            description_md=f"Imported from {_EMPLOYER} referral on 2026-09-01.")
        s.add(other)
        await s.flush()
        s.add(Pursuit(opportunity_id=other.id, track="employment", status="applied"))
        await s.commit()
    await engine.dispose()
    forget_engine()


def test_confidential_mode_also_hides_the_employer_in_the_source_name():
    asyncio.run(_seed(confidential=True))
    asyncio.run(_employer_named_sources())
    with _client() as c:
        rows = c.get("/api/pursuits").json()["rows"]
    assert any(r["source"] and "jobs (Greenhouse)" in r["source"] for r in rows)  # still shown
    assert not [r for r in rows if _EMPLOYER in str(r)]


def test_the_source_name_is_shown_as_it_is_when_confidential_mode_is_off():
    asyncio.run(_seed())
    asyncio.run(_employer_named_sources())
    with _client() as c:
        rows = c.get("/api/pursuits").json()["rows"]
    assert f"{_EMPLOYER} jobs (Greenhouse)" in {r["source"] for r in rows}


def test_the_shared_key_is_required_off_this_computer():
    asyncio.run(_seed())
    auth.set_cached_key("s3cret-key")
    with _client("192.168.1.50") as c:
        refused = c.get("/api/pursuits")
        wrong = c.get("/api/pursuits", headers={"X-Kestrel-Key": "nope"})
        ok = c.get("/api/pursuits", headers={"X-Kestrel-Key": "s3cret-key"})
    assert refused.status_code == 401 and wrong.status_code == 401
    assert ok.status_code == 200 and len(ok.json()["rows"]) == 3


def test_it_only_reads_and_never_calls_out(monkeypatch):
    import socket

    asyncio.run(_seed())
    before = _snapshot()
    attempts: list[object] = []
    real = socket.socket.connect

    def watch(self, address):
        if isinstance(address, tuple) and address and address[0] not in ("127.0.0.1", "::1"):
            attempts.append(address)
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", watch)
    with _client() as c:
        assert c.get("/api/pursuits").status_code == 200
        assert c.post("/api/pursuits", json={}).status_code == 405  # no write endpoint
        assert c.delete("/api/pursuits").status_code == 405
    assert attempts == []
    assert _snapshot() == before  # the ID salt is the one thing a first call may add
    assert _salt_rows() == 1


def _snapshot() -> list:
    import sqlite3

    con = sqlite3.connect(paths.db_path())
    try:
        return [con.execute(f"select * from {t} order by 1").fetchall()
                for t in ("pursuit", "opportunity", "status_event")]
    finally:
        con.close()


def _salt_rows() -> int:
    import sqlite3

    con = sqlite3.connect(paths.db_path())
    try:
        return con.execute("select count(*) from setting where key = 'ids.salt'").fetchone()[0]
    finally:
        con.close()

