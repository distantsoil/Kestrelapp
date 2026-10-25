"""The small surfaces in a real browser (UI overhaul parity check against the locked mockup,
the locked design mockup (kept privately)): Score again, Run a search now, the update
banner, the bell panel and the error page, light and dark at 1280 and 1680. No overlap, no wrapped
titles or buttons, no horizontal scroll, and the dialogs stay inside the window.

Gated like the other live-browser design-system tests: set KESTREL_LIVE_BROWSER=1. The browser is
the Edge already on the machine, else a Playwright Chromium (tests/browser.py)."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest

from tests.browser import LINES_JS, launch_sync, wait_for_layout

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

# One line of text: every client rect of the element's text sits at the same height.
_LINES = LINES_JS

_KIDS = """el => Array.from(el.children).filter(e => e.offsetParent !== null)
  .map(e => { const b = e.getBoundingClientRect();
    return {t: (e.textContent || '').trim().slice(0, 30), l: b.left, r: b.right, tp: b.top,
            bt: b.bottom}; })"""


def _overlaps(a, b) -> bool:
    return a["l"] < b["r"] - 1 and b["l"] < a["r"] - 1 and a["tp"] < b["bt"] - 1 \
        and b["tp"] < a["bt"] - 1


def _seed() -> int:
    from kestrel.db import migrate
    from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
    from kestrel.db.models import Notification, Opportunity, Source
    from kestrel.services import updater

    async def go() -> int:
        await reset_engine()
        migrate.run_migrations()
        now = datetime.now(UTC)
        async with get_sessionmaker()() as s:
            o = Opportunity(track="employment", opp_type="job_posting",
                            title="Senior Operations Analyst", org_name="Pinecrest Clinics",
                            stage="t2_done")
            s.add(o)
            s.add_all([Source(track="employment", kind="greenhouse", name=n, enabled=True)
                       for n in ("Meridian board", "Harbor board", "Larkspur board")])
            s.add_all([
                Notification(kind="source_disabled", severity="warning",
                             title="Harbor board stopped after three failures"),
                Notification(kind="run_finished", severity="success", title="Run finished: 4 new",
                             created_at=(now - timedelta(days=2)).isoformat()),
            ])
            await s.commit()
            oid = o.id
            state = updater.UpdateState(
                disclosure_shown=True, latest_version="99.0.0",
                download_page=updater.DOWNLOAD_PAGE)
            await updater.save_state(s, state)
        forget_engine()
        return oid

    return asyncio.run(go())


@pytest.fixture
def live(monkeypatch):
    """A server on the test's own throwaway data directory and a browser."""
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    async def no_hosted(_settings):
        return ("", None, [], [], {}, None)

    monkeypatch.setattr("kestrel.web.pages.rescore._local_models",
                        lambda settings: (["kestrel-tier3:latest"], None))
    monkeypatch.setattr("kestrel.web.pages.rescore._hosted_models", no_hosted)
    oid = _seed()
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, oid
        browser.close()


def _open(live, theme: str, width: int, path: str, ready: str):
    base, browser, _oid = live
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}{path}")
    page.wait_for_selector(ready)
    wait_for_layout(page)
    return ctx, page, errors


def _clean(page, errors, theme, width, *, lines, groups) -> None:
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
    for sel in lines:
        for el in page.query_selector_all(sel):
            if el.is_visible():
                assert page.evaluate(_LINES, el) <= 1, (sel, theme, width, el.inner_text()[:40])
    for sel in groups:
        for group in page.query_selector_all(sel):
            if not group.is_visible():
                continue
            kids = page.evaluate(_KIDS, group)
            for i, a in enumerate(kids):
                for b in kids[i + 1:]:
                    assert not _overlaps(a, b), (sel, a, b, theme, width)
    assert not errors, errors


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_score_again_and_run_now_dialogs(live, theme, width):
    oid = live[2]
    ctx, page, errors = _open(live, theme, width, f"/rescore/choose?opp_ids={oid}&page=1",
                              '[data-testid="score-again-dialog"]')
    try:
        _clean(page, errors, theme, width,
               lines=[".sf-head h2", ".sf-foot .k-btn", ".sf-choice .sf-tx > b"],
               groups=[".sf-head", ".sf-foot", ".sf-choice"])
        dlg = page.evaluate("""() => {
          const b = document.querySelector('dialog').getBoundingClientRect();
          return [b.left >= 0, b.right <= innerWidth, b.bottom <= innerHeight + 1]; }""")
        assert all(dlg), dlg
    finally:
        ctx.close()
    ctx, page, errors = _open(live, theme, width, "/scan/choose?page=1",
                              '[data-testid="run-now-dialog"]')
    try:
        assert page.get_by_text("All my searches (3)").is_visible()
        _clean(page, errors, theme, width,
               lines=[".sf-head h2", ".sf-foot .k-btn", ".sf-choice .sf-tx > b"],
               groups=[".sf-head", ".sf-foot", ".sf-choice"])
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_update_banner_and_the_bell_panel(live, theme, width):
    ctx, page, errors = _open(live, theme, width, "/help", '[data-testid="update-banner"]')
    try:
        assert "Get it from kestrelapp.co" in page.inner_text('[data-testid="update-banner"]')
        page.get_by_test_id("update-details-toggle").click()
        assert page.get_by_test_id("update-more").is_visible()
        _clean(page, errors, theme, width,
               lines=[".sf-bn .k-btn", ".sf-bn b"], groups=[".sf-bn"])
        page.locator(".bell > button").click()
        page.wait_for_selector('[data-testid="notifications-panel"]')
        page.get_by_test_id("notif-tab-attention").click()
        page.wait_for_selector('[data-testid="notif-unresolved"]')
        _clean(page, errors, theme, width,
               lines=[".sf-note-open"], groups=[".sf-tabs", ".sf-note-meta"])
        # A tab (its count pill is smaller type) stays one row tall.
        for h in page.evaluate("Array.from(document.querySelectorAll('.sf-tab')).map("
                               "e => e.getBoundingClientRect().height)"):
            assert h < 44, h
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_the_error_page(live, monkeypatch, theme, width):
    async def boom(*_a, **_k):
        raise RuntimeError("the database was busy")

    monkeypatch.setattr("kestrel.web.pages.chrome._panel_ctx", boom)
    ctx, page, errors = _open(live, theme, width, "/notifications/list",
                              '[data-testid="error-page"]')
    try:
        page.get_by_test_id("error-details-toggle").click()
        assert "What happened:" in page.inner_text('[data-testid="error-details"]')
        _clean(page, errors, theme, width,
               lines=[".sf-err h1", ".sf-err .k-btn"], groups=[".sf-err-actions", ".sf-head"])
    finally:
        ctx.close()
