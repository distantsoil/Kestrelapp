"""Soak-tier layout crawl (D-TEST-8). Visits every main page with invented seeded data at three
window sizes in light and dark and fails on: horizontal page overflow, clipped text, overlapping
text boxes, and console errors. Each failure names the page, width, theme, the element's selector
and the rule broken. Runs only with KESTREL_LAYOUT_CRAWL=1 (not part of the gate):

    KESTREL_LAYOUT_CRAWL=1 uv run --extra dev pytest -q -p no:cacheprovider \\
        tests/test_layout_crawl_browser.py

The seeds wipe the database, so the pages are grouped by the seed that gives them data; each
group seeds, starts a server, and visits its pages."""

from __future__ import annotations

import asyncio
import os

import pytest

from tests.browser import launch_sync, wait_for_app_ready, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = [
    pytest.mark.usefixtures("_isolate_data_home"),
    pytest.mark.skipif(
        os.environ.get("KESTREL_LAYOUT_CRAWL") != "1",
        reason="soak tier: set KESTREL_LAYOUT_CRAWL=1 to run the layout crawl",
    ),
]

SIZES = ((1440, 900), (1024, 768), (390, 844))
THEMES = ("light", "dark")
SETTINGS = ("computer", "roles", "scoring", "privacy", "data", "preferences")
ROLE_TABS = ("", "/posting", "/organization", "/documents", "/history")

# Every check runs in the page. Returns [{rule, selector, detail}].
_CHECK_JS = """() => {
  const out = [];
  const vw = window.innerWidth;
  const sel = el => {
    if (!el || el.nodeType !== 1) return '(none)';
    const parts = [];
    for (let e = el, i = 0; e && e.nodeType === 1 && i < 4; e = e.parentElement, i++) {
      let p = e.tagName.toLowerCase();
      const tid = e.getAttribute('data-testid');
      if (e.id) { parts.unshift(p + '#' + e.id); break; }
      if (tid) { parts.unshift(p + '[data-testid=' + tid + ']'); break; }
      const cls = (e.getAttribute('class') || '').split(/\\s+/).filter(Boolean).slice(0, 2);
      if (cls.length) p += '.' + cls.join('.');
      parts.unshift(p);
    }
    return parts.join(' > ');
  };
  const style = el => getComputedStyle(el);
  const shown = el => {
    for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
      const s = style(e);
      if (s.display === 'none' || s.visibility === 'hidden' || +s.opacity === 0) return false;
      if (e.hasAttribute('hidden') || e.getAttribute('aria-hidden') === 'true') return false;
      if (e.getAttribute('role') === 'tooltip') return false;
    }
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  const scrollable = el => {
    for (let e = el.parentElement; e && e !== document.body; e = e.parentElement) {
      const o = style(e).overflowX;
      if (o === 'auto' || o === 'scroll') return true;
    }
    return false;
  };
  const offscreenParked = el => {
    const r = el.getBoundingClientRect();
    return r.right < 0 || r.left > vw + 4000;
  };

  // 1. Horizontal page overflow.
  const de = document.documentElement;
  if (de.scrollWidth > vw + 1) {
    const culprits = [];
    for (const el of document.body.querySelectorAll('*')) {
      if (!shown(el) || scrollable(el) || offscreenParked(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.right > vw + 1 && style(el).position !== 'fixed') culprits.push([r.right, el]);
    }
    culprits.sort((a, b) => b[0] - a[0]);
    const top = culprits.length ? culprits[0] : null;
    out.push({rule: 'horizontal-page-overflow', selector: top ? sel(top[1]) : 'html',
      detail: 'scrollWidth ' + de.scrollWidth + ' > window ' + vw});
  }

  // 2. Clipped text: an element with its own text whose content is wider than its box and that
  // hides the rest (overflow hidden/clip or an ellipsis), outside any scrolling container.
  const texty = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (n.textContent.trim() && n.parentElement) texty.push(n);
  }
  const seen = new Set();
  for (const n of texty) {
    const el = n.parentElement;
    if (seen.has(el)) continue;
    seen.add(el);
    if (['SCRIPT', 'STYLE', 'OPTION', 'TEXTAREA'].includes(el.tagName)) continue;
    if (!shown(el) || scrollable(el)) continue;
    const s = style(el);
    if (s.display === 'inline') continue;
    if (el.clientWidth <= 2) continue;  // visually hidden helpers
    const hides = ['hidden', 'clip'].includes(s.overflowX) || s.textOverflow === 'ellipsis';
    if (hides && el.scrollWidth > el.clientWidth + 1) {
      out.push({rule: 'clipped-text', selector: sel(el),
        detail: 'scrollWidth ' + el.scrollWidth + ' > clientWidth ' + el.clientWidth + ': "' +
          n.textContent.trim().slice(0, 40) + '"'});
    }
  }

  // 3. Overlapping text boxes: painted line boxes of two different elements that intersect by
  // more than 2px each way, where the top element at the middle of the overlap is one of them
  // (text covered by a dialog or panel is not an overlap).
  const boxes = [];
  for (const n of texty) {
    const el = n.parentElement;
    if (!shown(el) || ['SCRIPT', 'STYLE', 'OPTION', 'TEXTAREA'].includes(el.tagName)) continue;
    const range = document.createRange();
    range.selectNodeContents(n);
    for (const r of range.getClientRects()) {
      if (r.width < 3 || r.height < 3) continue;
      if (r.bottom < 0 || r.top > window.innerHeight * 6) continue;
      boxes.push({el, l: r.left, r: r.right, t: r.top + window.scrollY,
                  b: r.bottom + window.scrollY, txt: n.textContent.trim().slice(0, 30)});
    }
  }
  boxes.sort((a, b) => a.l - b.l);
  const reported = new Set();
  for (let i = 0; i < boxes.length && out.length < 40; i++) {
    const a = boxes[i];
    for (let j = i + 1; j < boxes.length; j++) {
      const b = boxes[j];
      if (b.l >= a.r - 2) break;
      if (a.el === b.el || a.el.contains(b.el) || b.el.contains(a.el)) continue;
      const w = Math.min(a.r, b.r) - Math.max(a.l, b.l);
      const h = Math.min(a.b, b.b) - Math.max(a.t, b.t);
      if (w <= 2 || h <= 2) continue;
      const cx = (Math.max(a.l, b.l) + Math.min(a.r, b.r)) / 2;
      const cy = (Math.max(a.t, b.t) + Math.min(a.b, b.b)) / 2 - window.scrollY;
      if (cy < 0 || cy > window.innerHeight || cx < 0 || cx > vw) continue;  // off screen
      const top = document.elementFromPoint(cx, cy);
      if (!top || !(a.el.contains(top) || b.el.contains(top))) continue;
      const key = sel(a.el) + '|' + sel(b.el);
      if (reported.has(key)) continue;
      reported.add(key);
      out.push({rule: 'overlapping-text', selector: sel(a.el) + '  <>  ' + sel(b.el),
        detail: '"' + a.txt + '" and "' + b.txt + '" overlap ' + Math.round(w) + 'x' +
          Math.round(h) + 'px'});
    }
  }
  return out;
}"""


def _groups():
    """(group name, seed callable, [(page label, path, wait selector or None)])."""
    from tests import (
        apps_seed,
        offers_seed,
        prep_seed,
        profile_seed,
        role_seed,
        runs_seed,
        today_seed,
    )

    def role_pages(ids):
        return [(f"Role{t or '/overview'}", f"/opportunities/{ids['strong']}{t}", None)
                for t in ROLE_TABS]

    def prep_pages(ids):
        opp = ids["Larkspur Logistics"]["opp"]
        pages = [(f"Prep {tab}", f"/prep/{opp}?tab={tab}", "#pp-layout")
                 for tab in ("pack", "practice", "bank")]
        return [("Interview prep stories", "/stories", None)] + pages

    return [
        ("today", lambda: asyncio.run(today_seed.seed()), lambda ids: [
            ("Today", "/", None), ("Calibrate", "/calibrate", None),
            ("Analytics", "/analytics", None), ("Sources", "/sources", None),
            ("Models", "/models", None), ("Help", "/help", None), ("About", "/about", None),
            *[(f"Settings {sec}", f"/settings#{sec}", f"#st-sec-{sec}:not([hidden])")
              for sec in SETTINGS]]),
        ("pipeline", _pipeline_seed, lambda ids: [
            ("Pipeline", "/opportunities", '[data-testid="pl-row"]')]),
        ("role", lambda: asyncio.run(role_seed.seed_async()), role_pages),
        ("applications", lambda: apps_seed.seed("employment"), lambda ids: [
            ("Applications board", "/pursuits?view=board", None),
            ("Applications table", "/pursuits?view=table", None)]),
        ("profile", lambda: profile_seed.seed(), lambda ids: [("Profile", "/profile", None)]),
        ("prep", lambda: prep_seed.seed(with_pack=True), prep_pages),
        ("offers", lambda: offers_seed.seed(), lambda ids: [("Offers", "/offers", None)]),
        ("runs", lambda: runs_seed.seed(), lambda ids: [("Runs", "/runs", None)]),
    ]


def _pipeline_seed():
    from tests import test_pipeline_view as T
    from tests.test_pipeline_browser import _extras

    ids = asyncio.run(T._seed())
    asyncio.run(T._with_session(_extras))
    return ids


def _group_params():
    return [g[0] for g in _groups()]


@pytest.mark.parametrize("group", _group_params())
def test_layout_crawl(group):
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    _name, seed, pages_for = next(g for g in _groups() if g[0] == group)
    ids = seed()
    pages = pages_for(ids)
    failures: list[str] = []
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        for width, height in SIZES:
            for theme in THEMES:
                ctx = browser.new_context(viewport={"width": width, "height": height},
                                          color_scheme=theme)
                ctx.add_init_script(
                    f"try{{localStorage.setItem('kestrel-theme','{theme}')}}catch(e){{}}")
                page = ctx.new_page()
                console: list[str] = []
                page.on("console", lambda m, c=console: c.append(m.text)
                        if m.type == "error" else None)
                page.on("pageerror", lambda e, c=console: c.append(f"pageerror: {e}"))
                for label, path, wait in pages:
                    console.clear()
                    where = f"page={label} ({path}) width={width} theme={theme}"
                    try:
                        page.goto(f"{base}{path}")
                        wait_for_app_ready(page)
                        if wait:
                            page.wait_for_selector(wait, timeout=15000)
                        wait_for_layout(page)
                        found = page.evaluate(_CHECK_JS)
                    except Exception as exc:  # noqa: BLE001 (report, keep crawling)
                        failures.append(f"{where} rule=page-did-not-load: "
                                        f"{str(exc).splitlines()[0][:160]}")
                        continue
                    for f in found:
                        failures.append(f"{where} element={f['selector']} rule={f['rule']}: "
                                        f"{f['detail']}")
                    for msg in console:
                        failures.append(f"{where} element=(console) rule=console-error: "
                                        f"{msg[:160]}")
                ctx.close()
        browser.close()
    assert not failures, f"{len(failures)} layout failure(s) in group {group}:\n" + "\n".join(
        sorted(set(failures)))
