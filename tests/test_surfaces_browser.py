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

from tests.browser import (
    G4_LISTENER_PROBE_JS,
    LINES_JS,
    TIMER_PROBE_JS,
    assert_page_remounts_clean,
    launch_sync,
    wait_for_layout,
)
from tests.browser_plugin import browser_artifacts  # noqa: F401

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


RELEASE = "https://github.com/distantsoil/Kestrelapp/releases/download"
NEWER = "99.0.0"
FILES = {"windows": f"Kestrel-Setup-{NEWER}.exe", "macos": f"Kestrel-{NEWER}.dmg",
         "linux": f"Kestrel-{NEWER}-x86_64.AppImage"}
SHA = "ef" * 32


def update_feed() -> dict:
    """A cached feed with a valid release entry for every platform (D-UPD-8), so the dialog shows
    the direct download wherever the test runs. Notes carry bold markers and raw HTML."""
    return {
        "schema": 1, "version": NEWER, "published_at": "2026-10-01T00:00:00Z",
        "download_page": "https://kestrelapp.co/download",
        "downloads": {p: {"file": f, "url": f"{RELEASE}/v{NEWER}/{f}", "sha256": SHA,
                          "signed": False} for p, f in FILES.items()},
        "releases": [{"version": NEWER, "date": "2026-10-01",
                      "notes_md": "### Changed\n- **The role page shows where each match comes "
                                  "from** and lets you correct it <script>alert(1)</script>\n"
                                  "- A long line of release notes that has to wrap inside the "
                                  "dialog at phone width without pushing anything past the edge"}],
    }


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
                disclosure_shown=True, latest_version=NEWER,
                download_page=updater.DOWNLOAD_PAGE, cached_feed=update_feed())
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


def _open(live, theme: str, width: int, path: str, ready: str, probes: tuple[str, ...] = ()):
    base, browser, _oid = live
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    for probe in probes:
        page.add_init_script(probe)
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
        # D-UPD-8: the button downloads this platform's installer from the release.
        assert f"Download Kestrel {NEWER}" in page.inner_text('[data-testid="update-banner"]')
        # D-UI-218: at the top of the window, above the search row, and the button carries the
        # external-link icon (the mockup's arrow-out-of-box path), not an arrow.
        banner = page.get_by_test_id("update-banner").bounding_box()
        search = page.get_by_test_id("search").bounding_box()
        assert banner["y"] + banner["height"] <= search["y"] + 1, (banner, search)
        icon_d = page.evaluate("""() => document.querySelector(
            '[data-testid="update-banner"] a.k-btn--primary svg path').getAttribute('d')""")
        assert icon_d.startswith("M14 4h6v6M20 4l-9 9"), icon_d
        page.get_by_test_id("update-details-toggle").click()
        assert page.get_by_test_id("update-more").is_visible()
        _clean(page, errors, theme, width,
               lines=[".sf-bn .k-btn", ".sf-bn b"], groups=[".sf-bn"])
        page.locator('[data-testid="bell"]').click()
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
@pytest.mark.parametrize("width", [390, 480, 1280, 1440, 1680])
def test_the_error_page(live, monkeypatch, theme, width):
    async def boom(*_a, **_k):
        raise RuntimeError("the database was busy")

    monkeypatch.setattr("kestrel.web.pages.chrome._panel_ctx", boom)
    ctx, page, errors = _open(live, theme, width, "/notifications/list",
                              '[data-testid="error-page"]')
    try:
        page.get_by_test_id("error-details-toggle").click()
        assert page.inner_text('[data-testid="error-code"]') == "Code APP-001"
        details = page.inner_text('[data-testid="error-details"]')
        assert details.startswith("Code APP-001\n")
        assert "What happened:" in details
        _clean(page, errors, theme, width,
               # a long title may wrap on a phone; it must still not overlap or clip
               lines=([".sf-err h1"] if width >= 480 else []) + [".sf-err .k-btn", ".sf-err-code"],
               groups=[".sf-err-actions", ".sf-head"])
    finally:
        ctx.close()


def test_r1_today_run_now_with_usual_online_models_asks_once_then_starts(live):
    """R1 (owner, 2026-10-28): with online models as the usual ones, Run now on Today refused
    every checked send. Now the dialog shows what is sent, "Send and start" posts the approval,
    and the scan is queued on the first try."""
    from sqlalchemy import select

    from kestrel.db.engine import forget_engine, get_sessionmaker
    from kestrel.db.models import Setting, Task
    from tests.test_run_mode import _rows

    async def hosted():
        async with get_sessionmaker()() as s:
            for key, value in _rows(mode="hosted").items():
                await s.merge(Setting(key=key, value=value))
            await s.commit()
        forget_engine()

    async def scrapes() -> int:
        async with get_sessionmaker()() as s:
            n = len((await s.execute(select(Task).where(Task.type == "scrape"))).scalars().all())
        forget_engine()
        return n

    def off_loop(coro_fn):  # Playwright's sync API owns this thread's event loop
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(1) as pool:
            return pool.submit(lambda: asyncio.run(coro_fn())).result()

    off_loop(hosted)
    ctx, page, errors = _open(live, "light", 1440, "/", '[data-testid="scan-now"]')
    try:
        page.click('[data-testid="scan-now"]')
        page.wait_for_selector('[data-testid="run-now-dialog"][open]')
        page.click('[data-testid="run-now-go"]')
        page.wait_for_selector('[data-testid="run-now-confirm"]:not([hidden])')
        page.click('[data-testid="run-now-send"]')
        page.wait_for_function("!document.querySelector('[data-testid=\"run-now-dialog\"]')")
        page.wait_for_timeout(500)
        assert "Not started" not in page.inner_text("body")
        assert off_loop(scrapes) == 3  # one per enabled search, on the first try
        assert not errors, errors
    finally:
        ctx.close()


def test_the_error_page_mounts_unmounts_and_mounts_again_with_one_set_of_everything(
        live, monkeypatch):
    """The page lifecycle (D-UI-210): the error page has no script file; its two buttons are
    defined inline and mounted by the shell like any page's. Unmounted and mounted again it holds
    the same two listeners, and after it one press of "Show details" opens the details (a doubled
    listener would open and close them)."""
    async def boom(*_a, **_k):
        raise RuntimeError("the database was busy")

    monkeypatch.setattr("kestrel.web.pages.chrome._panel_ctx", boom)
    ctx, page, errors = _open(live, "light", 1280, "/notifications/list",
                              '[data-testid="error-page"]', probes=(TIMER_PROBE_JS,))
    try:
        first = assert_page_remounts_clean(page, "error", "error.js", settle_ms=200, quiet_ms=300)
        assert first["listeners"] == 2 and first["timers"] == 0, first
        page.get_by_test_id("error-details-toggle").click()
        assert page.is_visible('[data-testid="error-details"]')
        assert page.inner_text('[data-testid="error-details-toggle"]') == "Hide details"
        page.evaluate("KUI.page.unmount()")
        page.get_by_test_id("error-details-toggle").click()
        assert page.is_visible('[data-testid="error-details"]')  # nobody answered the press
        assert not errors, errors
    finally:
        ctx.close()


def _settled_listeners(page, selector: str) -> int:
    """The live listener count once it stops changing: the model picker binds its own after the
    dialog's script has run, a moment later on a busy machine."""
    last = -1
    for _ in range(40):
        now = page.evaluate("sel => window.__g4Listeners.live(sel)", selector)
        if now == last:
            return now
        last = now
        page.wait_for_timeout(250)
    raise AssertionError(f"listener count never settled: {last}")


def _reopen_dialog_test(live, opener, ready, cancel, markup_probe):
    """Opens a dialog the page loads on demand, cancels it, opens it again, then runs its script
    once more over the same dialog; at each step the dialog holds one set of listeners."""
    ctx, page, errors = _open(live, "light", 1440, "/", '[data-testid="scan-now"]',
                              probes=(G4_LISTENER_PROBE_JS,))
    try:
        opener(page)
        page.wait_for_selector(ready)
        mine = f"{markup_probe}, {markup_probe} *"
        one = _settled_listeners(page, mine)
        assert one >= 8, one  # its controls, the form and the dialog itself are all heard
        # Cancelled, the dialog is gone and holds nothing; opened again it holds the same set.
        page.click(cancel)
        page.wait_for_function("sel => !document.querySelector(sel)", arg=markup_probe)
        opener(page)
        page.wait_for_selector(ready)
        assert _settled_listeners(page, mine) == one
        # The dialog's script run again over the same dialog replaces its listeners.
        page.evaluate("""sel => {
          const dlg = document.querySelector(sel);
          const old = dlg.nextElementSibling;
          dlg.close();
          // The script is a file now (D-INST-8): the same file, with the same attributes.
          const again = document.createElement('script');
          Array.from(old.attributes).forEach((a) => again.setAttribute(a.name, a.value));
          dlg.after(again);
        }""", markup_probe)
        page.wait_for_selector(ready)
        assert _settled_listeners(page, mine) == one
        # And it still works as one: Escape closes it once and takes its listeners with it.
        page.keyboard.press("Escape")
        page.wait_for_function("sel => !document.querySelector(sel)", arg=markup_probe)
        assert page.evaluate("sel => window.__g4Listeners.live(sel)", mine) == 0
        assert not errors, errors
    finally:
        ctx.close()


def test_the_run_now_dialog_opened_twice_holds_one_set_of_handlers(live):
    _reopen_dialog_test(
        live, lambda page: page.click('[data-testid="scan-now"]'),
        '[data-testid="run-now-dialog"][open]', '[data-testid="run-now-cancel"]',
        '[data-testid="run-now-dialog"]')


def test_the_score_again_dialog_opened_twice_holds_one_set_of_handlers(live):
    oid = live[2]

    def opener(page):
        # The slot the Score again button fills: the same request, the same swap.
        page.evaluate("""oid => {
          let box = document.getElementById('g4-slot');
          if (!box) {
            box = document.createElement('span');
            box.className = 'score-again';
            box.innerHTML = '<button class="score-again-btn" type="button">Score again</button>'
              + '<span class="score-again-slot" id="g4-slot"></span>';
            document.body.appendChild(box);
          }
          htmx.ajax('GET', '/rescore/choose?opp_ids=' + oid,
                    { target: '#g4-slot', swap: 'innerHTML' });
        }""", oid)

    _reopen_dialog_test(live, opener, '[data-testid="score-again-dialog"][open]',
                        '[data-testid="score-again-cancel"]', '[data-testid="score-again-dialog"]')


# --- the update dialog (#199: D-UPD-8, D-UI-351) ----------------------------------------------

_DIALOG = '[data-testid="update-dialog"][open]'
_ANIMS = """() => { const d = document.querySelector('[data-testid="update-dialog"]');
  return d ? d.getAnimations().map(a => a.animationName) : null; }"""
# Settled means the open animation has started AND finished. An empty list is not settled: on a
# loaded machine the check could run before the animation registered (the owner's Windows gate
# on 156f622d read [] at 390px dark), and every() over [] is true.
_SETTLED = """() => { const d = document.querySelector('[data-testid="update-dialog"]');
  if (!d) return false; const a = d.getAnimations();
  return a.some(x => x.animationName === 'sf-upd-in')
    && a.every(x => x.playState === 'finished'); }"""
_FOOT_GEOMETRY = """() => {
  const d = document.querySelector('[data-testid="update-dialog"]');
  const foot = d.querySelector('.sf-foot');
  const box = e => { const b = e.getBoundingClientRect();
    return {l: b.left, r: b.right, t: b.top, b: b.bottom, text: (e.textContent || '').trim()}; };
  const pad = parseFloat(getComputedStyle(foot).paddingLeft);
  return {dialog: box(d), foot: box(foot), pad,
          footClipped: foot.scrollWidth > foot.clientWidth + 1,
          dialogClipped: d.scrollWidth > d.clientWidth + 1,
          buttons: Array.from(foot.querySelectorAll('.k-btn')).map(e => ({...box(e),
            clipped: e.scrollWidth > e.clientWidth + 1})),
          h2Clipped: (() => { const h = d.querySelector('h2');
            return h.scrollWidth > h.clientWidth + 1; })()};
}"""


def _open_update_dialog(page) -> None:
    page.get_by_test_id("update-details-toggle").click()
    page.get_by_test_id("update-whats-new").click()
    page.wait_for_selector(_DIALOG)
    page.wait_for_function(_SETTLED)
    wait_for_layout(page)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [390, 480, 1280])
def test_the_update_dialog_footer_wraps_and_nothing_is_clipped(live, theme, width):
    ctx, page, errors = _open(live, theme, width, "/help", '[data-testid="update-banner"]')
    try:
        _open_update_dialog(page)
        # The open is animated (D-UI-351); fill: both keeps the finished animation listed.
        assert "sf-upd-in" in (page.evaluate(_ANIMS) or []), (theme, width)
        geo = page.evaluate(_FOOT_GEOMETRY)
        d, pad = geo["dialog"], geo["pad"]
        assert pad >= 22, pad  # the body's inner padding, so the footer matches it
        assert not geo["footClipped"] and not geo["dialogClipped"] and not geo["h2Clipped"], geo
        labels = [b["text"] for b in geo["buttons"]]
        assert labels == ["Skip this version", "Remind me tomorrow", f"Download Kestrel {NEWER}"]
        for b in geo["buttons"]:
            # Every button sits inside the padded footer, on the screen, and is not cut.
            assert b["l"] >= d["l"] + pad - 1 and b["r"] <= d["r"] - pad + 1, (b, d, pad, width)
            assert b["t"] >= d["t"] and b["b"] <= d["b"] + 1 and b["b"] <= 900, (b, d)
            assert not b["clipped"], b
        rows = {round(b["t"]) for b in geo["buttons"]}
        # One button per row at phone width; wider, the three share at most two rows.
        assert len(rows) == (3 if width <= 480 else min(len(rows), 2)), (rows, width)
        _clean(page, errors, theme, width,
               lines=[".sf-upd .sf-foot .k-btn"], groups=[".sf-upd .sf-foot", ".sf-upd .sf-head"])
        # The link is the rebuilt release address for this platform, with no query string.
        from kestrel.services import updater

        href = page.get_attribute('.sf-upd [data-testid="update-download"]', "href")
        assert href == f"{RELEASE}/v{NEWER}/{FILES[updater.feed_platform()]}", href
        source = page.inner_text('[data-testid="update-source"]')
        assert "GitHub, the Kestrel release page" in source
        # Bold renders, the markers do not show, and the script tag is text (D-UPD-10).
        notes = page.inner_text(".sf-upd .sf-notes")
        assert "**" not in notes and "<script>alert(1)</script>" in notes
        assert page.evaluate("document.querySelector('.sf-upd .sf-notes strong').textContent"
                             ).startswith("The role page shows")
        assert page.evaluate("document.querySelectorAll('.sf-upd .sf-notes script').length") == 0
        # Show details lists the file, the release and the checksum.
        page.locator('[data-testid="update-file-details"] > summary').click()
        page.wait_for_selector('[data-testid="update-file-info"]', state="visible")
        info = page.inner_text('[data-testid="update-file-info"]')
        assert f"SHA-256: {SHA}" in info and "github.com/distantsoil/Kestrelapp" in info
        wait_for_layout(page)
        assert page.evaluate("""() => { const p = document.querySelector(
          '[data-testid="update-file-info"]'); return p.scrollWidth <= p.clientWidth + 1; }""")
        page.locator(".sf-upd .sf-x").click()
        page.wait_for_selector('[data-testid="update-dialog"]', state="detached")
    finally:
        ctx.close()


def test_the_update_dialog_does_not_animate_under_reduced_motion(live):
    base, browser, _oid = live
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="light",
                              reduced_motion="reduce")
    page = ctx.new_page()
    try:
        page.goto(f"{base}/help")
        page.wait_for_selector('[data-testid="update-banner"]')
        wait_for_layout(page)
        page.get_by_test_id("update-details-toggle").click()
        page.get_by_test_id("update-whats-new").click()
        page.wait_for_selector(_DIALOG)
        assert page.evaluate(_ANIMS) == []
        dialog_anim, backdrop_anim = page.evaluate("""() => { const d = document.querySelector(
          '[data-testid="update-dialog"]'); return [getComputedStyle(d).animationName,
          getComputedStyle(d, '::backdrop').animationName]; }""")
        assert dialog_anim == "none" and backdrop_anim == "none", (dialog_anim, backdrop_anim)
    finally:
        ctx.close()
