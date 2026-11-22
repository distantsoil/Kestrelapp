"""Settings > Browser extension in a real browser (issue #186). Light and dark at 1280, 1024 and
390 wide: no overlapping or clipped text, no sideways scroll, titles and buttons on one line.
Then the interactions: Copy key puts the key on the clipboard and says Copied without turning on
access from other devices, a switch saves the shared value, Make a new key asks first, and the
OS reduced-motion setting stops the section's motion.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1. Screenshots go to
KESTREL_SHOTS_DIR when it is set, else a temporary folder."""

from __future__ import annotations

import json
import os
import pathlib
import re
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta

import pytest

from tests.browser import launch_sync, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("KESTREL_LIVE_BROWSER") != "1",
        reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
    ),
    pytest.mark.usefixtures("_isolate_data_home"),
]

_SHOTS = pathlib.Path(os.environ.get("KESTREL_SHOTS_DIR") or tempfile.mkdtemp())
SEC = "#st-sec-extension"

# Every visible text box in a root. Closed details, closed dialogs and invisible text are left
# out; clipped text (an ellipsis) is judged by its own box.
_TEXT_BOXES = """root => {
  const out = [];
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const invisible = el => { let a = el; while (a && a !== document.documentElement) {
    const cs = getComputedStyle(a);
    if (['hidden', 'collapse'].includes(cs.visibility) || cs.display === 'none') return true;
    a = a.parentElement; } return false; };
  for (let n = walk.nextNode(); n; n = walk.nextNode()) {
    if (!n.textContent.trim()) continue;
    const el = n.parentElement;
    const det = el && el.closest('details');
    const hiddenDet = det && !det.open && !el.closest('summary');
    if (!el || el.offsetParent === null || invisible(el) || hiddenDet) continue;
    if (el.closest('.k-sr-only')) continue;
    const r = document.createRange(); r.selectNodeContents(n);
    const clipped = getComputedStyle(el).textOverflow === 'ellipsis';
    const rects = clipped ? [el.getBoundingClientRect()] : r.getClientRects();
    for (const b of rects) if (b.width > 0 && b.height > 0)
      out.push({t: n.textContent.trim().slice(0, 24), l: b.left, r: b.right, tp: b.top,
                bt: b.bottom});
  }
  return out;
}"""

# Text cut off by its own box: an ellipsis that is in use, or content wider than an element that
# hides overflow.
_CLIPPED = """root => Array.from(root.querySelectorAll('*')).filter(el => {
  if (el.offsetParent === null || el.closest('details:not([open]) > :not(summary)')) return false;
  if (el.closest('.k-modal:not(.is-open)') || el.closest('.k-sr-only')) return false;
  const cs = getComputedStyle(el);
  if (!['hidden', 'clip'].includes(cs.overflowX) && cs.textOverflow !== 'ellipsis') return false;
  if (!el.textContent.trim() || el.tagName === 'svg' || el.closest('svg')) return false;
  return el.scrollWidth > el.clientWidth + 1;
}).map(el => (el.textContent || '').trim().slice(0, 40))"""

_LINES = """el => {
  const tops = new Set();
  const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  for (let n = walk.nextNode(); n; n = walk.nextNode()) {
    if (!n.textContent.trim()) continue;
    const r = document.createRange(); r.selectNodeContents(n);
    for (const x of r.getClientRects()) if (x.width > 0) tops.add(Math.round(x.bottom / 8));
  }
  return tops.size;
}"""


def _overlaps(a, b) -> bool:
    return a["l"] < b["r"] - 1 and b["l"] < a["r"] - 1 and a["tp"] < b["bt"] - 1 \
        and b["tp"] < a["bt"] - 1


def _db_put(key: str, value) -> None:
    from kestrel import paths

    con = sqlite3.connect(paths.db_path())
    try:
        con.execute("insert or replace into setting (key, value, updated_at) values (?, ?, ?)",
                    (key, json.dumps(value), "2026-01-01T00:00:00Z"))
        con.commit()
    finally:
        con.close()


def _db_get(key: str):
    from kestrel import paths

    con = sqlite3.connect(paths.db_path())
    try:
        row = con.execute("select value from setting where key = ?", (key,)).fetchone()
    finally:
        con.close()
    return json.loads(row[0]) if row else None


def _seen_two_minutes_ago() -> None:
    at = (datetime.now(UTC) - timedelta(minutes=2, seconds=20)).isoformat()
    _db_put("extension.last_seen", {"at": at.replace("+00:00", "Z"), "browser": "Chrome"})


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.web.serve import running_server

    paths.ensure_dirs()
    (paths.config_dir() / "settings.yml").write_text(
        (paths.packaged_config() / "settings.yml").read_text(encoding="utf-8"), encoding="utf-8")
    migrate.run_migrations()
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser
        browser.close()


def _open(live, theme: str, width: int, *, height: int = 1000, reduced: bool = False):
    base, browser = live
    ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme=theme,
                              reduced_motion="reduce" if reduced else "no-preference")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}/settings#extension")
    page.wait_for_selector(f'{SEC}:not([hidden]) [data-testid="xt-key"]')
    wait_for_layout(page)
    page.wait_for_timeout(700)  # the steps' entry motion has finished
    return ctx, page, errors


def _assert_clean(page, label) -> None:
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True, label
    roots = [page.query_selector(SEC), page.query_selector('[data-st-help="extension"]')]
    for root in roots:
        assert root is not None and root.is_visible(), label
        boxes = page.evaluate(_TEXT_BOXES, root)
        assert len(boxes) > 5, label
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                assert not _overlaps(a, b), (label, a["t"], b["t"])
        assert page.evaluate(_CLIPPED, root) == [], label
    one_line = [f"{SEC} .k-btn", f"{SEC} .st-head h3", f"{SEC} .st-xband h2", ".st-si"]
    for sel in one_line:
        for el in page.query_selector_all(sel):
            if el.is_visible():
                assert page.evaluate(_LINES, el) <= 1, (label, sel, el.inner_text()[:40])
    # Text stays inside its card.
    for card in page.query_selector_all(f"{SEC} .st-card, {SEC} .st-xband"):
        cb = card.bounding_box()
        for box in page.evaluate(_TEXT_BOXES, card):
            assert box["l"] >= cb["x"] - 1 and box["r"] <= cb["x"] + cb["width"] + 1, (
                label, box["t"])


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1024, 390])
def test_the_section_has_no_overlap_or_clipping_and_says_connected(live, theme, width):
    _seen_two_minutes_ago()
    ctx, page, errors = _open(live, theme, width)
    try:
        status = page.inner_text('[data-testid="xt-status"]')
        # D-TEST-825: the seeded 2 min 20 s ages under load; require at least 2 minutes.
        seen = re.fullmatch(r"Connected · Chrome · used (\d+) minutes? ago", status)
        assert seen is not None and int(seen.group(1)) >= 2, status
        assert page.get_attribute('[data-testid="st-nav-extension"]', "aria-current") == "page"
        _assert_clean(page, (theme, width))
        page.screenshot(path=str(_SHOTS / f"app-settings-extension-{theme}-{width}.png"),
                        full_page=True)
        page.click('[data-testid="xt-details"] > summary')
        page.wait_for_timeout(400)
        _assert_clean(page, (theme, width, "details"))
        assert "chrome://extensions" in page.inner_text('[data-testid="xt-unpacked"]')
        port = page.inner_text('[data-testid="xt-port"]')
        assert port == f"Kestrel is on port {live[0].rsplit(':', 1)[1]} on this computer."
        if width == 390:
            page.query_selector('[data-testid="xt-details"]').screenshot(
                path=str(_SHOTS / f"app-settings-extension-{theme}-{width}-details.png"))
        assert not errors, errors
    finally:
        ctx.close()


def test_not_connected_yet_before_the_extension_has_called(live):
    ctx, page, errors = _open(live, "light", 1280)
    try:
        assert page.inner_text('[data-testid="xt-status"]') == "Not connected yet"
        assert page.get_attribute('[data-testid="xt-get-chrome"]', "href") == "/help"
        assert page.get_attribute('[data-testid="xt-get-firefox"]', "href") == "/help"
        assert not errors, errors
    finally:
        ctx.close()


def test_copy_key_copies_says_copied_and_leaves_access_off(live):
    from kestrel import paths
    from kestrel.services import lan_access

    yml = (paths.config_dir() / "settings.yml").read_bytes()
    assert lan_access.file_value() is False
    ctx, page, errors = _open(live, "light", 1280)
    try:
        ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=live[0])
        assert _db_get("server.shared_secret") is None
        assert page.input_value('[data-testid="xt-keyfield"]') == ""
        page.click('[data-testid="xt-copy"]')
        page.wait_for_selector('[data-testid="xt-copied"].is-on')
        assert page.inner_text('[data-testid="xt-copied"]') == "Copied"
        key = _db_get("server.shared_secret")
        assert isinstance(key, str) and len(key) >= 32
        assert page.evaluate("navigator.clipboard.readText()") == key
        assert key not in page.content()  # copied, never written into the page
        assert page.input_value('[data-testid="xt-keyfield"]') == "•" * 20
        page.screenshot(path=str(_SHOTS / "app-settings-extension-light-1280-copied.png"))
        # The Copied note fades away again.
        page.wait_for_selector('[data-testid="xt-copied"]:not(.is-on)', timeout=4000)
        page.click('[data-testid="xt-copy"]')
        page.wait_for_selector('[data-testid="xt-copied"].is-on')
        assert _db_get("server.shared_secret") == key  # a second copy keeps the same key
        assert (paths.config_dir() / "settings.yml").read_bytes() == yml
        assert lan_access.file_value() is False and _db_get("server.bind_all") is None
        assert not errors, errors
    finally:
        ctx.close()


def test_a_switch_saves_the_value_the_extension_reads(live):
    ctx, page, errors = _open(live, "dark", 1280)
    try:
        assert page.is_checked('[data-testid="xt-sw-toolbar_count"]')
        page.click('[data-testid="xt-sw-toolbar_count"] + .k-switch__track')
        page.wait_for_selector('.k-toast:has-text("The toolbar icon will no longer show a count.")')
        assert _db_get("extension.toolbar_count") is False
        assert _db_get("extension.badges") is None  # the others are untouched
        page.reload()
        page.wait_for_selector(f'{SEC}:not([hidden]) [data-testid="xt-does"]')
        wait_for_layout(page)
        assert not page.is_checked('[data-testid="xt-sw-toolbar_count"]')
        assert not errors, errors
    finally:
        ctx.close()


def test_make_a_new_key_asks_first_then_replaces_the_key(live):
    _db_put("server.shared_secret", "invented-old-key-for-the-browser-test-0001")
    ctx, page, errors = _open(live, "light", 1024)
    try:
        page.click('[data-testid="xt-details"] > summary')
        page.click('[data-testid="xt-rotate-open"]')
        page.wait_for_selector('#st-xt-rotate-dialog.is-open')
        assert _db_get("server.shared_secret") == "invented-old-key-for-the-browser-test-0001"
        page.screenshot(path=str(_SHOTS / "app-settings-extension-light-1024-new-key.png"))
        page.click('[data-testid="xt-rotate"]')
        page.wait_for_selector('[data-testid="xt-note"]')
        assert page.inner_text('[data-testid="xt-note"]') == (
            "New key made. Paste it into the extension again.")
        new = _db_get("server.shared_secret")
        assert new != "invented-old-key-for-the-browser-test-0001" and new not in page.content()
        assert not errors, errors
    finally:
        ctx.close()


def test_motion_runs_normally_and_reduced_motion_turns_it_off(live):
    probe = """() => {
      const step = document.querySelector('#st-sec-extension .st-xstep');
      const note = document.querySelector('[data-testid="xt-copied"]');
      return {step: getComputedStyle(step).animationName,
              note: getComputedStyle(note).transitionDuration};
    }"""
    ctx, page, errors = _open(live, "light", 1280)
    try:
        moving = page.evaluate(probe)
        assert moving["step"] == "st-xin" and moving["note"] not in ("0s", "0s, 0s"), moving
    finally:
        ctx.close()
    ctx, page, errors = _open(live, "light", 1280, reduced=True)
    try:
        still = page.evaluate(probe)
        assert still["step"] == "none", still
        assert set(still["note"].replace(" ", "").split(",")) == {"0s"}, still
        assert not errors, errors
    finally:
        ctx.close()
