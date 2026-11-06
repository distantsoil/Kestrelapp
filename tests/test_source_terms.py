"""Source terms (D-SRC-19): a Remotive posting never leaves through an export or a share feature,
and Remotive, Arbeitnow and Jobicy postings show their credit with a link. Offline: a throwaway
database with invented roles."""

from __future__ import annotations

import asyncio
import csv
import io
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.base import utcnow_iso
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import (
    Evaluation,
    Label,
    Opportunity,
    Org,
    Pursuit,
    Source,
    VerdictOverride,
)
from kestrel.web.app import build_app

SOLO = "Remote Only Zebra Role"        # on Remotive alone
SHARED_R = "Shared Heron Role Remotive"  # the same role on Remotive ...
SHARED_I = "Shared Heron Role Indeed"    # ... and on Indeed (one dedup group)
PLAIN = "Plain Otter Role"             # on Indeed alone
MARKERS = (SOLO, SHARED_R)


async def _seed() -> dict[str, int]:
    await reset_engine()
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    ids: dict[str, int] = {}
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        rem = Source(track="employment", kind="remotive", name="Remotive", enabled=True)
        ind = Source(track="employment", kind="indeed", name="Indeed", enabled=True)
        s.add_all([rem, ind])
        await s.flush()
        org = Org(name="Sample Co", normalized_name="sample co")
        s.add(org)
        await s.flush()

        async def role(key, title, src, group, url):
            o = Opportunity(
                track="employment", opp_type="job_posting", title=title, org_id=org.id,
                org_name="Sample Co", url=url, stage="t2_done", source_id=src.id,
                dedup_group_id=group, first_seen_at=utcnow_iso(), description_md="Body text.",
            )
            s.add(o)
            await s.flush()
            s.add(Evaluation(
                opportunity_id=o.id, tier=2, track="employment", verdict="apply",
                verdict_model="apply", score=4.4, weighted_score=4.4, needs_review=False,
                dimensions={}, blocks={}, blockers=[],
                legitimacy={"status": "clean", "signals": []}))
            s.add(Pursuit(opportunity_id=o.id, track="employment", status="applied",
                          applied_at=utcnow_iso()))
            s.add(Label(opportunity_id=o.id, track="employment", score=4.0, verdict="apply",
                        source="manual"))
            s.add(VerdictOverride(opportunity_id=o.id, operator_verdict="apply",
                                  operator_score=4.5, system_verdict="apply"))
            ids[key] = o.id

        await role("solo", SOLO, rem, "g-solo", "https://remotive.com/remote-jobs/x/zebra-1")
        await role("shared_r", SHARED_R, rem, "g-shared", "https://remotive.com/remote-jobs/x/heron-2")
        await role("shared_i", SHARED_I, ind, "g-shared", "https://indeed.example/heron")
        await role("plain", PLAIN, ind, "g-plain", "https://indeed.example/otter")
        await s.commit()
    await engine.dispose()
    forget_engine()
    return ids


@pytest.fixture
def web(_isolate_data_home):
    ids = asyncio.run(_seed())
    with TestClient(build_app(worker=False)) as c:
        c.ids = ids  # type: ignore[attr-defined]
        yield c


def _mcp(tool: str, args: dict | None = None):
    from fastmcp import Client

    from kestrel.mcp.server import build_mcp

    async def run():
        async with Client(build_mcp()) as c:
            return (await c.call_tool(tool, args or {})).data

    return asyncio.run(run())


def _clean(text: str) -> None:
    for m in MARKERS:
        assert m not in text, f"{m!r} leaked: {text[:300]}"


# ---------------------------------------------------------------------------------- exports
def test_applications_csv_leaves_out_remotive_and_uses_the_other_boards_copy(web):
    r = web.get("/analytics/export/applications.csv")
    assert r.status_code == 200
    rows = list(csv.reader(io.StringIO(r.text.lstrip("﻿"))))
    roles = sorted(row[0] for row in rows[1:])
    # the Remotive-only role is gone; the shared role is exported as the Indeed copy, once more
    # next to the Indeed copy's own application; the plain role stays
    assert SOLO not in r.text and SHARED_R not in r.text
    assert roles == sorted([SHARED_I, SHARED_I, PLAIN])


def test_ratings_and_labels_exports_leave_out_remotive(web):
    r = web.get("/settings/scoring/export")
    assert r.status_code == 200
    _clean(r.text)
    titles = sorted({row["title"] for row in json.loads(r.text)["ratings"]})
    assert titles == sorted([SHARED_I, PLAIN])


def test_the_command_line_export_leaves_out_remotive(web, capsys):
    from kestrel.cli import _export

    asyncio.run(_export("json", None, None))
    out = capsys.readouterr().out
    _clean(out)
    assert PLAIN in out and SHARED_I in out


def test_the_diagnostics_bundle_role_rows_leave_out_remotive(web):
    import sqlite3

    from kestrel.services.diagnostics import role_rows

    conn = sqlite3.connect(str(paths.db_path()))
    conn.row_factory = sqlite3.Row
    try:
        data = role_rows(conn, lambda t: t)
    finally:
        conn.close()
    text = json.dumps(data)
    _clean(text)
    assert PLAIN in text and SHARED_I in text


def test_the_analytics_pdf_names_no_remotive_role(web, monkeypatch):
    from kestrel.docs import render

    seen: list[str] = []

    async def fake_pdf(html: str):
        seen.append(html)
        return render.RenderedPdf(pdf=b"%PDF-1.4 fake", page_count=1)

    monkeypatch.setattr(render, "render_pdf", fake_pdf)
    # make the Remotive-only application the oldest one waiting
    async def age():
        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            p = (await s.execute(
                __import__("sqlalchemy").select(Pursuit).where(
                    Pursuit.opportunity_id == web.ids["solo"]))).scalar_one()
            p.applied_at = "2026-10-01T00:00:00+00:00"
            await s.commit()
        await engine.dispose()
        forget_engine()

    asyncio.run(age())
    page = web.get("/analytics?range=all").text
    assert SOLO in page  # the page itself is the person's own screen and still names it
    r = web.get("/analytics/export/summary.pdf?range=all")
    assert r.status_code == 200
    _clean(seen[0])


# ----------------------------------------------------------------------- REST and MCP reads
def test_the_read_api_leaves_out_remotive(web):
    listing = web.get("/api/opportunities?page_size=200&include_duplicates=1").text
    _clean(listing)
    assert PLAIN in listing and SHARED_I in listing
    for key in ("solo", "shared_r"):
        r = web.get(f"/api/opportunities/{web.ids[key]}")
        assert r.status_code == 403 and "terms" in r.json()["error"]
    detail = web.get(f"/api/opportunities/{web.ids['shared_i']}").json()
    assert web.ids["shared_r"] not in detail["dedup_members"]


@pytest.mark.parametrize("tool", ["list_opportunities", "get_shortlist"])
def test_mcp_lists_leave_out_remotive(web, tool):
    data = _mcp(tool)
    _clean(str(data))
    assert PLAIN in str(data)


def test_mcp_get_opportunity_refuses_a_remotive_role(web):
    data = _mcp("get_opportunity", {"opportunity_id": web.ids["solo"]})
    assert "terms" in data["error"] and SOLO not in str(data)
    ok = _mcp("get_opportunity", {"opportunity_id": web.ids["shared_i"]})
    assert ok["title"] == SHARED_I


def test_mcp_pursuits_use_the_other_boards_copy_or_nothing(web):
    data = _mcp("get_pursuits")
    _clean(str(data))
    assert sorted(d["title"] for d in data) == sorted([SHARED_I, SHARED_I, PLAIN])


# ------------------------------------------------------------------------------------ credit
CREDIT_CASES = [
    ("remotive", "Remotive", "https://remotive.com/remote-jobs/x/credit-1"),
    ("arbeitnow", "Arbeitnow", "https://www.arbeitnow.com/jobs/credit-2"),
    ("jobicy", "Jobicy", "https://jobicy.com/jobs/credit-3"),
]


@pytest.mark.parametrize("kind,name,url", CREDIT_CASES)
def test_the_posting_tab_shows_source_and_links_to_the_board(_isolate_data_home, kind, name, url):
    async def seed() -> int:
        await reset_engine()
        for suffix in ("", "-wal", "-shm"):
            paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
        migrate.run_migrations()
        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            src = Source(track="employment", kind=kind, name=f"{name} custom label", enabled=True)
            s.add(src)
            await s.flush()
            o = Opportunity(track="employment", opp_type="job_posting", title="Credit Role",
                            org_name="Sample Co", url=url, stage="t2_done", source_id=src.id,
                            first_seen_at=utcnow_iso(), description_md="Body.")
            s.add(o)
            await s.commit()
            oid = o.id
        await engine.dispose()
        forget_engine()
        return oid

    oid = asyncio.run(seed())
    with TestClient(build_app(worker=False)) as c:
        html = c.get(f"/opportunities/{oid}/tab/posting").text
    from html.parser import HTMLParser

    class Find(HTMLParser):
        def __init__(self):
            super().__init__()
            self.href = None
            self.text = ""
            self._in = False
            self.before = ""

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "a" and a.get("data-testid") == "source-credit":
                self.href, self._in = a.get("href"), True

        def handle_endtag(self, tag):
            if tag == "a":
                self._in = False

        def handle_data(self, data):
            if self._in:
                self.text += data

    f = Find()
    f.feed(html)
    assert f.href == url
    assert f.text.replace("↗", "").strip() == name
    assert "Source: <a" in " ".join(html.split())


def test_other_sources_show_no_credit_line(_isolate_data_home, web):
    html = web.get(f"/opportunities/{web.ids['plain']}/tab/posting").text
    assert 'data-testid="source-credit"' not in html


def test_mcp_search_leaves_out_remotive(web, monkeypatch):
    from kestrel.intelligence.search import SearchHit
    from kestrel.mcp import server

    def hit(key, title):
        return SearchHit(opportunity_id=web.ids[key], title=title, org_name="Sample Co",
                         track="employment", score=0.9, verdict="apply", eval_score=4.4)

    async def fake_search(session, client, query, *, track=None, limit=10):
        return [hit("solo", SOLO), hit("shared_r", SHARED_R), hit("plain", PLAIN)]

    class _Client:
        _http = None

    async def fake_client():
        return _Client()

    monkeypatch.setattr("kestrel.intelligence.search.search_opportunities", fake_search)
    monkeypatch.setattr(server, "_client", fake_client)
    data = _mcp("search_opportunities", {"query": "anything"})
    _clean(str(data))
    assert [d["title"] for d in data] == [PLAIN]


def test_a_credit_whose_posting_link_is_not_on_the_board_links_to_the_board_home(
    _isolate_data_home,
):
    from kestrel.web.routes_pages import _source_credit

    src = Source(track="employment", kind="jobicy", name="x")
    assert _source_credit(src, {"href": "https://elsewhere.example/a"}) == {
        "name": "Jobicy", "href": "https://jobicy.com"}
    assert _source_credit(src, {"href": None}) == {"name": "Jobicy", "href": "https://jobicy.com"}
    assert _source_credit(src, {"href": "https://jobicy.com/j/1"})["href"] == "https://jobicy.com/j/1"
    assert _source_credit(Source(track="employment", kind="greenhouse", name="g"), None) is None
