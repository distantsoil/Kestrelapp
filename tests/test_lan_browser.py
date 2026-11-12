"""The restore opt-in (D-SEC-11) and Models > On your network (D-LLM-15) in a real browser: tick the
box, read the preview, add a server and give a step a model on it; light, dark and narrow with no
overlap, clipping or sideways scroll.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1. Screenshots go to the folder
named by KESTREL_SHOTS when it is set."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from tests.browser import LINES_JS, launch_sync, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

SHOTS = os.environ.get("KESTREL_SHOTS", "")
VIEWS = [("light", 1280), ("dark", 1280), ("narrow", 420)]
PUBLIC = "http://203.0.113.50:8000/v1"  # invented documentation address, never reached

_CLIPPED = """root => Array.from(root.querySelectorAll('*')).filter(e => e.offsetParent !== null
  && e.children.length === 0 && (e.textContent || '').trim()
  && e.scrollWidth > e.clientWidth + 1
  && !['visible'].includes(getComputedStyle(e).overflowX))
  .map(e => (e.textContent || '').trim().slice(0, 40))"""
_OVERLAP = """root => { const kids = Array.from(root.querySelectorAll(
    '.st-rp__row, .mo-server, .mo-actions, .mo-head, .st-keep, .k-modal__foot'))
    .filter(e => e.offsetParent !== null).map(e => {
      // A row scrolled out of its scroll container is clipped, not overlapping what is outside.
      const b = e.getBoundingClientRect();
      const box = e.closest('.k-modal__body, .mo-drawer-body, .k-slideover__body');
      if (!box) return b;
      const c = box.getBoundingClientRect();
      return {left: b.left, right: b.right, top: Math.max(b.top, c.top),
              bottom: Math.min(b.bottom, c.bottom)};
    }).filter(b => b.bottom > b.top);
  const bad = [];
  for (let i = 0; i < kids.length; i++) for (let j = i + 1; j < kids.length; j++) {
    const a = kids[i], b = kids[j];
    const inside = (x, y) => x.left >= y.left - 1 && x.right <= y.right + 1
      && x.top >= y.top - 1 && x.bottom <= y.bottom + 1;
    if (inside(a, b) || inside(b, a)) continue;
    if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1
        && b.top < a.bottom - 1) bad.push([i, j]);
  }
  return bad; }"""


@pytest.fixture
def live(monkeypatch):
    from playwright.sync_api import sync_playwright

    from kestrel.db import migrate
    from kestrel.llm.profiles import Hardware
    from kestrel.services import lan_servers as lan
    from kestrel.services import setup_state as ss
    from kestrel.web.serve import running_server

    monkeypatch.setattr(ss, "detected_hardware", lambda: Hardware(
        os="windows", gpu_vendor="nvidia", vram_gb=12.0, ram_gb=32.0))
    monkeypatch.setattr(ss, "probe_ollama", lambda settings, fresh=False: ("0.5.0", []))

    async def fake_test(base_url, api_key):
        return {"ok": True, "models": ["qwen3-14b", "mini-8b"],
                "text": "Connected. 2 model(s) found."}

    monkeypatch.setattr(lan, "test_connection", fake_test)
    migrate.run_migrations()
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser
        browser.close()


def _shot(page, name: str, view: str) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}-{view}.png"))


def _context(browser, view: str, width: int):
    ctx = browser.new_context(
        viewport={"width": width, "height": 900 if view == "narrow" else 1000},
        color_scheme="dark" if view == "dark" else "light")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    return ctx, page, errors


def _put_setting(key: str, value) -> None:
    from kestrel import paths

    con = sqlite3.connect(str(paths.db_path()))
    try:
        con.execute("DELETE FROM setting WHERE key = ?", (key,))
        con.execute("INSERT INTO setting (key, value, updated_at) VALUES (?, ?, '2026-01-01')",
                    (key, json.dumps(value)))
        con.commit()
    finally:
        con.close()


def _get_setting(key: str):
    from kestrel import paths

    con = sqlite3.connect(str(paths.db_path()))
    try:
        row = con.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    finally:
        con.close()
    return None if row is None else json.loads(row[0])


def _backup(tmp_path):
    from kestrel.services.backup import create_backup

    for k, v in {"llm.backends.local.base_url": PUBLIC, "llm.mode": "local",
                 "mcp.enabled": True, "mcp.port": 4999,
                 "updates.feed_url": "http://10.0.0.2/f"}.items():
        _put_setting(k, v)
    archive = create_backup(tmp_path)
    _put_setting("llm.backends.local.base_url", "http://127.0.0.1:11434/v1")
    _put_setting("mcp.enabled", False)
    return archive


@pytest.mark.parametrize(("view", "width"), VIEWS)
def test_restore_opt_in_shows_the_values_and_ticking_restores_them(live, tmp_path, view, width):
    archive = _backup(tmp_path)
    base, browser = live
    ctx, page, errors = _context(browser, view, width)
    try:
        page.goto(f"{base}/settings#data")
        page.wait_for_selector('[data-testid="st-nav-data"]')
        wait_for_layout(page)
        page.set_input_files("#st-restore-file", str(archive))
        page.wait_for_selector('#st-restore-dialog[aria-hidden="false"]')
        page.wait_for_selector('[data-testid="st-restore-preview-servers"]')
        wait_for_layout(page)
        text = page.inner_text("#st-restore-preview")
        assert "203.0.113.50:8000" in text and "the internet" in text
        assert page.is_visible('[data-testid="st-restore-preview-warn"]')
        assert page.is_checked('[data-testid="st-restore-local"]') is False
        _shot(page, "restore-preview", view)
        dlg = page.query_selector("#st-restore-dialog")
        assert page.evaluate(_CLIPPED, dlg) == []
        assert page.evaluate(_OVERLAP, dlg) == []
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
        page.check('[data-testid="st-restore-local"]')
        _shot(page, "restore-preview-ticked", view)
        with page.expect_response(lambda r: r.request.method == "POST"
                                  and r.url.endswith("/settings/restore")) as info:
            page.click('[data-testid="st-restore-go"]')
        assert "restored, as you chose" in info.value.text()
        page.wait_for_selector('#st-restore-res [data-testid="st-restored"]')
        assert _get_setting("llm.backends.local.base_url") == PUBLIC
        assert _get_setting("mcp.enabled") is True
        assert not errors, errors
    finally:
        ctx.close()


def test_restore_unticked_keeps_what_is_here(live, tmp_path):
    archive = _backup(tmp_path)
    base, browser = live
    ctx, page, errors = _context(browser, "light", 1280)
    try:
        page.goto(f"{base}/settings#data")
        page.wait_for_selector('[data-testid="st-nav-data"]')
        wait_for_layout(page)
        page.set_input_files("#st-restore-file", str(archive))
        page.wait_for_selector('[data-testid="st-restore-preview-servers"]')
        with page.expect_response(lambda r: r.request.method == "POST"
                                  and r.url.endswith("/settings/restore")):
            page.click('[data-testid="st-restore-go"]')
        page.wait_for_selector('#st-restore-res [data-testid="st-restored"]')
        assert _get_setting("llm.backends.local.base_url") == "http://127.0.0.1:11434/v1"
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize(("view", "width"), VIEWS)
def test_add_a_server_on_your_network_and_give_a_step_a_model(live, view, width):
    base, browser = live
    ctx, page, errors = _context(browser, view, width)
    try:
        page.goto(f"{base}/models")
        page.wait_for_selector('[data-testid="mo-network-card"]')
        wait_for_layout(page)
        _shot(page, "models-network-card", view)
        page.click('[data-testid="mo-network-open"]')
        page.wait_for_selector('[data-testid="mo-nw-address"]', state="visible")
        wait_for_layout(page)
        page.fill('[data-testid="mo-nw-address"]', "192.168.2.25:8000")
        page.select_option('[data-testid="mo-nw-kind"]', "omlx")
        page.click('[data-testid="mo-nw-test"]')
        page.wait_for_selector('[data-testid="mo-nw-found"]')
        wait_for_layout(page)
        _shot(page, "models-network-test", view)
        page.click('[data-testid="mo-nw-save"]')
        page.wait_for_selector('[data-testid="mo-nw-found"]')
        wait_for_layout(page)
        page.select_option('[data-testid="mo-nw-model-tier2"]', "qwen3-14b")
        page.click('[data-testid="mo-nw-use-tier2"]')
        page.wait_for_selector("text=now uses qwen3-14b on 192.168.2.25")
        wait_for_layout(page)
        _shot(page, "models-network-assigned", view)
        drawer = page.query_selector("#mo-network-so")
        assert page.evaluate(_CLIPPED, drawer) == []
        assert page.evaluate(_OVERLAP, drawer) == []
        lines = [page.evaluate(LINES_JS, el)
                 for el in page.query_selector_all("#mo-network-so .mo-server b")
                 if el.is_visible()]
        assert max(lines) <= 2
        page.goto(f"{base}/models")
        page.wait_for_selector('[data-testid="mo-where-tier2"]')
        wait_for_layout(page)
        assert "On another computer on your network" in page.inner_text(
            '[data-testid="mo-where-tier2"]')
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
        assert page.evaluate(_OVERLAP, page.query_selector("#mo-network-card")) == []
        _shot(page, "models-steps-network", view)
        assert not errors, errors
    finally:
        ctx.close()
