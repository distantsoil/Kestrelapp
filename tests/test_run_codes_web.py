"""RUN and SRC codes on the Runs pages (#174 task E): the timeline, the drawer, the run page and the
live feed. Fictional runs from tests/runs_seed.py; no model, no network."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.analytics import run_summary as rsum
from kestrel.db.engine import make_async_engine
from kestrel.db.models import Run, ScrapeRun
from kestrel.web.app import build_app
from kestrel.web.pages import runs_page as rp
from tests import runs_seed

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


def _db(fn):
    async def go():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                out = await fn(s)
                await s.commit()
                return out
        finally:
            await engine.dispose()

    return asyncio.run(go())


@pytest.fixture
def idle():
    seed = runs_seed.seed()
    with TestClient(build_app(worker=False)) as c:
        yield c, seed


def _run_ids(status: str) -> list[int]:
    async def go(s):
        rows = await s.execute(select(Run.id).where(Run.status == status).order_by(Run.id))
        return list(rows.scalars())

    return _db(go)


def _partial_scrape_id() -> int:
    async def go(s):
        rows = await s.execute(select(ScrapeRun.id).where(ScrapeRun.status == "partial"))
        return rows.scalars().first()

    return _db(go)


def _store_code(code: str | None) -> None:
    sid = _partial_scrape_id()

    async def go(s):
        counts = {"new": 11} if code is None else {"new": 11, "error_code": code}
        await s.execute(update(ScrapeRun).where(ScrapeRun.id == sid).values(counts=counts))

    _db(go)


def _run_of_partial() -> int:
    """The run whose window holds the seeded partial search (the latest finished run)."""
    async def go(s):
        scrape = (await s.execute(select(ScrapeRun).where(ScrapeRun.status == "partial"))
                  ).scalars().first()
        runs = (await s.execute(select(Run).where(Run.status == "done"))).scalars().all()
        return next(r.id for r in runs if r.started_at <= scrape.started_at <= r.finished_at)

    return _db(go)


def test_status_of_a_run_that_did_not_run_carries_run_001():
    run = Run(name="desktop", status="failed", counts={})
    assert rp.status_of(run)["code"] == "RUN-001"


def test_status_of_an_interrupted_run_carries_run_002():
    run = Run(name="desktop", status="interrupted", counts={})
    assert rp.status_of(run)["code"] == "RUN-002"


@pytest.mark.parametrize("status", ["done", "running", "stopping"])
def test_a_run_that_ended_well_or_is_going_has_no_code(status):
    assert rp.status_of(Run(name="desktop", status=status, counts={}))["code"] is None


def test_the_timeline_shows_a_code_only_beside_the_runs_that_need_one(idle):
    c, _ = idle
    html = c.get("/runs/history?all=1").text
    assert html.count('data-testid="rn-code">Code RUN-001<') == 1
    assert html.count('data-testid="rn-code">Code RUN-002<') == 1
    # the sentence of each stays whole
    assert "No search could run. Nothing was lost; they run again." in html
    assert "Kestrel closed before this run ended. Nothing was lost." in html


def test_the_drawer_of_a_failed_run_shows_the_code_and_repeats_it_in_details(idle):
    c, _ = idle
    rid = _run_ids("failed")[0]
    html = c.get(f"/runs/{rid}/drawer").text
    assert 'data-testid="rn-code">Code RUN-001<' in html
    details = html.split('data-testid="rn-run-details"')[1]
    assert "Code RUN-001" in details.split("<pre>")[1].splitlines()[0]


def test_the_drawer_of_a_finished_run_has_no_run_code(idle):
    c, seed = idle
    html = c.get(f"/runs/{_run_of_partial()}/drawer").text
    assert "Code RUN-" not in html


def test_a_search_in_the_drawer_shows_its_stored_code(idle):
    c, _ = idle
    _store_code("SRC-002")
    html = c.get(f"/runs/{_run_of_partial()}/drawer").text
    assert 'data-testid="rn-src-code">Code SRC-002<' in html
    assert ", Code SRC-002" in html.split("<pre>")[1]


def test_a_search_with_no_stored_code_shows_none(idle):
    c, _ = idle
    _store_code(None)
    html = c.get(f"/runs/{_run_of_partial()}/drawer").text
    assert "Code SRC" not in html


def test_the_run_page_shows_the_search_code_beside_its_status(idle):
    c, _ = idle
    _store_code("SRC-003")
    html = c.get(f"/runs/{_run_of_partial()}").text
    assert 'data-testid="rn-code">Code SRC-003<' in html


def test_an_unregistered_stored_code_is_never_shown(idle):
    c, _ = idle
    _store_code("SRC-999")
    html = c.get(f"/runs/{_run_of_partial()}/drawer").text
    assert "SRC-999" not in html


@pytest.fixture
def active():
    from kestrel.web.app import set_paused

    seed = runs_seed.seed(active=True)
    with TestClient(build_app(worker=False)) as c:
        yield c, seed
    set_paused(False)


def _mark_active_search(code: str | None) -> None:
    async def go(s):
        run = (await s.execute(select(Run).where(Run.status == "running"))).scalars().one()
        scrape = (await s.execute(
            select(ScrapeRun).where(ScrapeRun.started_at >= run.started_at)
            .where(ScrapeRun.finished_at.is_not(None)).order_by(ScrapeRun.id)
        )).scalars().first()
        counts = {"new": 6} if code is None else {"new": 6, "error_code": code}
        await s.execute(update(ScrapeRun).where(ScrapeRun.id == scrape.id).values(
            status="partial", error="HTTP 429", counts=counts))

    _db(go)


def _feed(html: str) -> str:
    return html.split('data-testid="run-events"')[1].split("</ul>")[0]


def test_the_live_feed_shows_the_code_of_a_search_that_stopped_early(active):
    c, _ = active
    _mark_active_search("SRC-002")
    feed = _feed(c.get("/runs").text)
    assert "was rate-limited; it is retried next run" in feed
    assert 'data-testid="rn-event-code">Code SRC-002<' in feed


def test_the_live_feed_shows_no_code_for_a_search_with_none_stored(active):
    c, _ = active
    _mark_active_search(None)
    feed = _feed(c.get("/runs").text)
    assert "was rate-limited; it is retried next run" in feed
    assert "rn-event-code" not in feed


def test_the_cut_off_search_event_carries_run_003():
    from kestrel import error_codes

    assert rsum.CUT_OFF_CODE == "RUN-003"
    assert error_codes.get(rsum.CUT_OFF_CODE).area == "RUN"


@pytest.mark.parametrize(("status", "code", "fails", "tone"), [
    ("failed", "SRC-002", 0, "bad"),  # a failed check counts toward the switch-off: red
    ("failed", "SRC-007", 0, "bad"),
    ("partial", "SRC-002", 0, "warn"),  # a partial check: amber
    ("partial", "SRC-007", 0, "warn"),
    ("partial", "SRC-003", 2, "warn"),  # earlier failures do not recolor a partial check
    ("failed", "SRC-003", 2, "bad"),
])
def test_runs_and_sources_color_a_coded_search_the_same(idle, status, code, fails, tone):
    """One rule on both pages (D-ERR-24 round 5): the Runs per-search tone and the Sources row and
    sparkline bar agree for the same stored check."""
    from kestrel.db.models import Source
    from kestrel.web import source_view as sv

    sid = _partial_scrape_id()
    rid = _run_of_partial()

    async def go(s):
        await s.execute(update(ScrapeRun).where(ScrapeRun.id == sid).values(
            status=status, counts={"new": 0, "error_code": code}, error="invented text"))
        await s.flush()
        run = await s.get(Run, rid)
        rows = await rp.sources_in(s, run)
        scrape = await s.get(ScrapeRun, sid)
        src = await s.get(Source, scrape.source_id)
        return rows, scrape, src

    rows, scrape, src = _db(go)
    mine = [r for r in rows if r["code"] == code]
    assert len(mine) == 1
    assert mine[0]["tone"] == tone
    as_char = {"bad": "b", "warn": "w"}[tone]
    assert sv.run_char(scrape) == as_char
    src.consecutive_failures = fails
    assert sv.build_view(src, [scrape]).health == as_char
