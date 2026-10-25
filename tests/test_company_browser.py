"""The Company page in a real browser (UI overhaul parity check against the locked mockup,
the locked design mockup (kept privately)). Light and dark at 1280 and 1680: no
overlap, no wrapping titles or buttons, no horizontal scroll; then Hide with Undo and a source
marker, in place.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import (
    ConnectionPosition,
    Contact,
    ContactImport,
    Opportunity,
    Org,
    OrgSummary,
    Pursuit,
)
from tests import retry_seed
from tests.browser import LINES_JS, launch_sync, wait_for_layout

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

_LINES = LINES_JS


def _ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


async def _seed() -> int:
    await reset_engine()
    retry_seed._wipe_db()
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        org = Org(name="Larkspur Logistics", normalized_name="larkspur logistics",
                  sector="Freight and logistics", size_band="1,000 to 5,000 people",
                  hq_location="Memphis, TN", domain="larkspurlogistics.example",
                  signals=[{"statement": "Opened a new regional hub in Reno",
                            "source_url": "https://larkspurlogistics.example/news",
                            "observed_at": _ago(9)}])
        s.add(org)
        await s.flush()
        titles = ["Operations Analyst", "Supply Chain Planner", "Process Improvement Lead",
                  "Warehouse Systems Analyst", "Planning Manager"]
        for i, t in enumerate(titles):
            o = Opportunity(
                track="employment", opp_type="job_posting", org_id=org.id, title=t,
                url=f"https://x.example/{i}", stage="t2_done", location_raw="Memphis, TN",
                liveness="live", liveness_checked_at=_ago(0), date_posted=_ago(5 + 20 * i),
                comp_min=70000 + i * 4000, comp_max=95000 + i * 4000, comp_currency="USD",
                comp_interval="year",
            )
            s.add(o)
            await s.flush()
            if i == 0:
                s.add(Pursuit(opportunity_id=o.id, track="employment", status="interviewing",
                              applied_at=_ago(60)))
        sources = [
            {"index": 1, "kind": "website", "url": "https://larkspurlogistics.example/about",
             "title": "larkspurlogistics.example/about", "fetched_at": _ago(2)},
            {"index": 2, "kind": "filing", "url": "https://sec.example/f",
             "title": "Annual report", "fetched_at": _ago(3)},
        ]
        s.add(OrgSummary(
            org_id=org.id, status="ok", source_count=2, sources=sources, model="m",
            sentences=[
                {"text": "Larkspur Logistics moves freight by truck and rail.",
                 "source_indexes": [1]},
                {"text": "About 2,400 people work here, mostly around its Memphis base.",
                 "source_indexes": [2]},
            ],
            tags=[{"key": "does", "text": "Trucking, rail and warehousing", "source_indexes": [1]},
                  {"key": "serves", "text": "Retailers, food, healthcare", "source_indexes": [1]},
                  {"key": "direction", "text": "Growing west: new Reno hub", "source_indexes": [2]},
                  {"key": "hiring", "text": "Posts most roles on its own careers page",
                   "source_indexes": [1]}],
            generated_at=_ago(1)))
        imp = ContactImport(file_name="Connections.csv", row_count=2)
        s.add(imp)
        await s.flush()
        for name, title, current in (("Dana Lee", "Operations Director", True),
                                     ("Sam Ortiz", "Planning Manager", False)):
            c = Contact(name=name, title=title, source="linkedin_import", org_id=org.id)
            s.add(c)
            await s.flush()
            s.add(ConnectionPosition(
                contact_id=c.id, company_name="Larkspur Logistics", org_id=org.id, match="sure",
                title=title, connected_on="2021-05-01", last_import_id=imp.id,
                first_import_id=imp.id, is_current=current))
        s.add(Contact(name="Priya Raman", title="Director of Operations", org_id=org.id,
                      source="research", source_url="https://larkspurlogistics.example/team"))
        await s.commit()
        org_id = org.id
    await engine.dispose()
    forget_engine()
    return org_id


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    org_id = asyncio.run(_seed())
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, org_id
        browser.close()


def _open(live, theme: str, width: int):
    base, browser, org_id = live
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}/orgs/{org_id}")
    page.wait_for_selector('[data-testid="co-summary"]')
    wait_for_layout(page)
    return ctx, page, errors


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_no_overlap_no_wrapping_no_sideways_scroll(live, theme, width):
    ctx, page, errors = _open(live, theme, width)
    try:
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
        assert page.evaluate("document.querySelector('.main, main').scrollWidth <= "
                             "document.querySelector('.main, main').clientWidth + 1") is True
        for sel in ("h1", ".co-meta span", ".co-actions .k-btn", ".k-pill", ".co-rt b", ".co-score",
                    ".co-open", ".co-chk", ".co-seen", ".co-pn b", ".co-hist .co-dt"):
            for el in page.query_selector_all(sel):
                if el.is_visible():
                    assert page.evaluate(_LINES, el) <= 1, (sel, theme, width, el.inner_text()[:40])
        for container in (".co-role", ".co-row", ".co-kn", ".co-person", ".co-hist", ".co-abf"):
            for group in page.query_selector_all(container):
                if not group.is_visible():
                    continue
                kids = page.evaluate(
                    """el => Array.from(el.children).filter(e => e.offsetParent !== null)
                      .map(e => { const b = e.getBoundingClientRect();
                        return {t: (e.textContent || '').trim().slice(0, 30), l: b.left,
                                r: b.right, tp: b.top, bt: b.bottom}; })""", group)
                for i, a in enumerate(kids):
                    for b in kids[i + 1:]:
                        assert not (a["l"] < b["r"] - 1 and b["l"] < a["r"] - 1
                                    and a["tp"] < b["bt"] - 1 and b["tp"] < a["bt"] - 1), (
                            container, a, b, theme, width)
        # The summary tiles and the pay tiles keep three across at both widths, as the mockup.
        for row in (".co-ab3", ".co-stats"):
            tops = page.evaluate(
                "sel => Array.from(document.querySelectorAll(sel + ' > div'))"
                ".map(e => Math.round(e.getBoundingClientRect().top))", row)
            assert len(tops) == 3 and len(set(tops)) == 1, (row, tops, theme, width)
        # The initials tile sits before the name, and the whole "used to work here" line shows.
        assert page.evaluate("(() => { const m = document.querySelector('.co-mono');"
                             " const h = document.querySelector('h1');"
                             " return !!m && h.contains(m) &&"
                             " m.getBoundingClientRect().width > 40; })()")
        for how in page.query_selector_all(".co-kn .co-how"):
            if how.is_visible():
                assert page.evaluate("e => e.scrollWidth <= e.clientWidth + 1", how), (
                    how.inner_text(), theme, width)
        main = page.evaluate("(() => { const b = document.querySelector('.k-layout__main')"
                             ".getBoundingClientRect(); return {r: b.right, t: b.top}; })()")
        help_col = page.evaluate("(() => { const b = document.querySelector('.k-help-col')"
                                 ".getBoundingClientRect(); return {l: b.left, t: b.top}; })()")
        if width >= 1680:
            assert help_col["l"] >= main["r"] - 1 and abs(help_col["t"] - main["t"]) <= 4
        else:
            assert help_col["t"] >= main["t"]
        assert errors == []
    finally:
        ctx.close()


def test_hide_and_undo_swap_in_place(live):
    ctx, page, errors = _open(live, "light", 1680)
    try:
        page.click('[data-testid="co-hide"]')
        page.wait_for_selector('[data-testid="co-undo"]')
        assert page.inner_text('[data-testid="co-show"]').strip() == "Show this company here"
        page.click('[data-testid="co-undo"]')
        page.wait_for_selector('[data-testid="co-hide"]')
        assert page.query_selector('[data-testid="co-undo"]') is None
        assert errors == []
    finally:
        ctx.close()


def test_a_source_marker_flashes_the_row_it_points_at(live):
    ctx, page, errors = _open(live, "light", 1680)
    try:
        page.click('.co-sup[data-src="co-src-2"]')
        page.wait_for_selector("#co-src-2.is-flash")
        assert errors == []
    finally:
        ctx.close()
