"""Shared browser launch for tests that need a real Chromium (CI gate: scripts/check.py).

Production drives the Edge already on the machine (CLAUDE.md: never bundle Chromium), so tests
try Edge first. Where Edge is absent (the cloud container) they fall back to a Chromium found
via KESTREL_BROWSER_PATH or the Playwright browsers dir, and skip only when neither exists.
A skip is never silent: scripts/check.py prints skip reasons and warns on browser skips.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

BROWSER_PATH_ENV = "KESTREL_BROWSER_PATH"
SKIP_PREFIX = "Browser unavailable"  # scripts/check.py greps skip reasons for this


def _browser_roots() -> list[Path]:
    roots = []
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override and override != "0":
        roots.append(Path(override))
    roots += [Path("/opt/pw-browsers"), Path.home() / ".cache" / "ms-playwright",
              Path.home() / "Library" / "Caches" / "ms-playwright"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "ms-playwright")
    return roots


def fallback_chromium() -> str | None:
    """A Chromium executable other than Edge: KESTREL_BROWSER_PATH if it names a file, else the
    newest chromium-*/ build in the Playwright browsers dir. None when there is none."""
    explicit = os.environ.get(BROWSER_PATH_ENV)
    if explicit and Path(explicit).is_file():
        return explicit
    patterns = ("chromium-*/chrome-linux/chrome", "chromium-*/chrome-linux64/chrome",
                "chromium-*/chrome-win/chrome.exe", "chromium-*/chrome-win64/chrome.exe",
                "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium")
    for root in _browser_roots():
        for pattern in patterns:
            hits = sorted(glob.glob(str(root / pattern)))
            if hits:
                return hits[-1]
    return None


def _edge_installed() -> bool:
    from kestrel.docs import render

    return bool(render._edge_candidates())  # noqa: SLF001 (same probe the app's doctor uses)


def use_browser_fallback(monkeypatch: pytest.MonkeyPatch) -> str | None:
    """For tests that render through the app (docs PDF): when Edge is absent, point
    KESTREL_BROWSER_PATH at the fallback Chromium for this test. Skips if there is none."""
    explicit = os.environ.get(BROWSER_PATH_ENV)
    if (explicit and Path(explicit).is_file()) or _edge_installed():
        return explicit
    path = fallback_chromium()
    if path is None:
        pytest.skip(f"{SKIP_PREFIX}: no Edge, no {BROWSER_PATH_ENV}, no Playwright Chromium")
    monkeypatch.setenv(BROWSER_PATH_ENV, path)
    return path


def _launch_kwargs() -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = [{"channel": "msedge"}]
    fb = fallback_chromium()
    if fb:
        attempts.append({"executable_path": fb})
    return attempts


TIMEOUT_ENV = "KESTREL_BROWSER_TIMEOUT_MS"
DEFAULT_TIMEOUT_MS = 60000


def default_timeout_ms() -> int:
    """One timeout for every page and context a helper creates (D-TEST-8). The owner's slower
    Windows PC, or a machine under load, needs more than Playwright's 30 s; raise it with
    KESTREL_BROWSER_TIMEOUT_MS rather than editing a test."""
    try:
        return max(1000, int(os.environ.get(TIMEOUT_ENV, DEFAULT_TIMEOUT_MS)))
    except ValueError:
        return DEFAULT_TIMEOUT_MS


class ArtifactTracker:
    """Per-test record of the contexts a browser made, so a failing test leaves a screenshot of
    each page and a Playwright trace in a folder under the pytest tmp dir (path printed).
    Created by the browser_artifacts fixture (tests/browser_plugin.py)."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.failed = False
        self.contexts: list[Any] = []
        self.saved = False
        self._done: set[int] = set()

    def track(self, ctx: Any) -> None:
        self.contexts.append(ctx)
        try:
            ctx.tracing.start(screenshots=False, snapshots=False)
        except Exception:  # noqa: BLE001, S110 (tracing is a diagnostic aid, never a test failure)
            pass

    def capture(self, ctx: Any, *, keep: bool) -> None:
        """Stop tracing; with keep, write a screenshot per page and the trace to the folder."""
        if id(ctx) in self._done:
            return
        self._done.add(id(ctx))
        n = self.contexts.index(ctx) if ctx in self.contexts else len(self._done)
        try:
            if not keep:
                ctx.tracing.stop()
                return
            self.folder.mkdir(parents=True, exist_ok=True)
            for m, page in enumerate(ctx.pages):
                try:
                    page.screenshot(path=str(self.folder / f"ctx{n}-page{m}.png"), timeout=5000)
                except Exception:  # noqa: BLE001, S110 (a crashed or closed page has no picture)
                    pass
            ctx.tracing.stop(path=str(self.folder / f"ctx{n}-trace.zip"))
            if not self.saved:
                print(f"\nBrowser failure artifacts (screenshots, traces): {self.folder}")
            self.saved = True
        except Exception:  # noqa: BLE001, S110 (a stopped Playwright cannot be captured)
            pass

    def capture_open(self) -> None:
        for ctx in list(self.contexts):
            self.capture(ctx, keep=True)


_TRACKER: ArtifactTracker | None = None


def set_tracker(tracker: ArtifactTracker | None) -> None:
    global _TRACKER  # noqa: PLW0603
    _TRACKER = tracker


def _prepare_context(ctx: Any) -> Any:
    """The default timeouts and failure capture for one context."""
    import sys

    ms = default_timeout_ms()
    ctx.set_default_timeout(ms)
    ctx.set_default_navigation_timeout(ms)
    tracker = _TRACKER
    if tracker is None:
        return ctx
    tracker.track(ctx)
    original_close = ctx.close

    def close(*args: Any, **kwargs: Any) -> Any:
        # An exception in flight (the finally: ctx.close() of a failing test) or a failure
        # already reported by the hook means the evidence is worth keeping.
        tracker.capture(ctx, keep=tracker.failed or sys.exc_info()[0] is not None)
        return original_close(*args, **kwargs)

    ctx.close = close
    return ctx


def _wrap_browser(browser: Any) -> Any:
    original_new_context = browser.new_context

    def new_context(*args: Any, **kwargs: Any) -> Any:
        return _prepare_context(original_new_context(*args, **kwargs))

    def new_page(*args: Any, **kwargs: Any) -> Any:
        return new_context(*args, **kwargs).new_page()

    browser.new_context = new_context
    browser.new_page = new_page
    return browser


def _capture_before_stop(playwright: Any) -> None:
    """A test that holds `with sync_playwright()` inside its body leaves that block, and so stops
    Playwright, before pytest reports the failure: capture while the exception is in flight."""
    import sys

    impl = getattr(playwright, "_impl_obj", None)
    connection = getattr(impl, "_connection", None)  # what sync_playwright().__exit__ stops
    original_stop = getattr(connection, "stop_sync", None)
    if original_stop is None:
        return

    def stop(*args: Any, **kwargs: Any) -> Any:
        tracker = _TRACKER
        if tracker is not None and sys.exc_info()[0] is not None:
            tracker.capture_open()
        return original_stop(*args, **kwargs)

    connection.stop_sync = stop


def wait_for_app_ready(page: Page) -> None:
    """Wait for the app's own ready signal: htmx and KUI have initialized (kestrel-ui.js sets
    data-kui-ready on <html> at the end of its init). Use it instead of a fixed sleep after goto."""
    page.wait_for_function(
        "() => document.documentElement.dataset.kuiReady === '1' && !!window.htmx")


_SWAP_JS = """async ([path, trigger, read, arg, ms]) => {
  const wait = new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      document.removeEventListener('htmx:afterSwap', onSwap);
      reject(new Error('timed out after ' + ms + ' ms waiting for the htmx swap of ' + path));
    }, ms);
    function onSwap(e) {
      const d = e.detail || {};
      const p = (d.pathInfo && d.pathInfo.requestPath) || (d.requestConfig && d.requestConfig.path)
        || '';
      if (!p.includes(path)) return;   // another swap (a poll, a toast) is not this one
      clearTimeout(timer);
      document.removeEventListener('htmx:afterSwap', onSwap);
      try { resolve(new Function('arg', read)(arg)); } catch (err) { reject(err); }
    }
    document.addEventListener('htmx:afterSwap', onSwap);
  });
  new Function('arg', trigger)(arg);
  return await wait;
}"""


def swap_result(page: Page, path: str, trigger_js: str, read_js: str, arg: Any = None) -> Any:
    """Run trigger_js (a function body, with `arg`) and return read_js (also a body) evaluated in
    the afterSwap of the swap whose request path contains `path`. Other swaps are ignored, the
    response is awaited by URL, and a swap that never comes rejects with a message naming it,
    after the default timeout (never an unbounded wait)."""
    ms = default_timeout_ms()
    with page.expect_response(lambda r: path in r.url, timeout=ms):
        return page.evaluate(_SWAP_JS, [path, trigger_js, read_js, arg, ms])


def launch_sync(playwright: Any, monkeypatch: pytest.MonkeyPatch | None = None) -> Any:
    """Edge first, then the fallback Chromium; skip only when neither starts. The browser's
    contexts and pages get the default timeout and failure capture (D-TEST-8)."""
    errors: list[str] = []
    for kwargs in _launch_kwargs():
        try:
            browser = playwright.chromium.launch(headless=True, **kwargs)
        except Exception as exc:  # Playwright Error: missing channel/executable
            errors.append(str(exc).splitlines()[0][:120])
            continue
        if "executable_path" in kwargs and monkeypatch is not None:
            monkeypatch.setenv(BROWSER_PATH_ENV, kwargs["executable_path"])
        _capture_before_stop(playwright)
        return _wrap_browser(browser)
    pytest.skip(f"{SKIP_PREFIX}: {'; '.join(errors) or 'no candidates'}")


async def launch_async(playwright: Any, monkeypatch: pytest.MonkeyPatch | None = None) -> Any:
    errors: list[str] = []
    for kwargs in _launch_kwargs():
        try:
            browser = await playwright.chromium.launch(headless=True, **kwargs)
        except Exception as exc:
            errors.append(str(exc).splitlines()[0][:120])
            continue
        if "executable_path" in kwargs and monkeypatch is not None:
            monkeypatch.setenv(BROWSER_PATH_ENV, kwargs["executable_path"])
        return browser
    pytest.skip(f"{SKIP_PREFIX}: {'; '.join(errors) or 'no candidates'}")



def wait_for_layout(page: Page) -> None:
    """Wait for loaded fonts, completed HTMX swaps, and finite layout animations."""
    page.wait_for_function("""() => !document.querySelector(
        '.htmx-request, .htmx-swapping, .htmx-settling')""")
    for frame in page.frames:
        frame.evaluate("document.fonts.ready")
        frame.wait_for_function("""() => {
            void document.documentElement.offsetWidth;
            return document.fonts.status === 'loaded' && document.getAnimations().every(
                animation => animation.effect.getComputedTiming().iterations === Infinity ||
                    ['finished', 'idle'].includes(animation.playState));
        }""")


# Shared by the *_browser.py tests: the left edge of what the scene art visibly draws, or None
# with no art. Measured in pixels, not geometry: the art's box starts in open sky, and a scene's
# far ridges fade in from nothing under a mask, so neither the box nor a shape's bounds says where
# a title would meet the drawing. Two screenshots of the band, the scene shown and hidden (its
# motion and the seasonal touch paused or hidden), and the first column that differs clearly.
def art_visible_left(page, threshold: int = 28, min_pixels: int = 2) -> float | None:
    import io

    from PIL import Image, ImageChops

    box = page.evaluate("""() => {
        const a = document.querySelector('.k-scene__art:not(.k-scene__touch)');
        if (!a) return null;
        const band = a.closest('.k-scene');
        if (band) {
          band.setAttribute('data-was-animate', band.getAttribute('data-animate') || '');
          band.setAttribute('data-animate', '0');
        }
        document.querySelectorAll('.k-scene__touch').forEach(t => t.style.visibility = 'hidden');
        const r = a.getBoundingClientRect();
        return {x: r.left, y: r.top, width: r.width, height: r.height}; }""")
    if not box or box["width"] < 2 or box["height"] < 2:
        return None
    page.wait_for_timeout(120)
    shown = Image.open(io.BytesIO(page.screenshot(clip=box))).convert("RGB")
    page.evaluate("""() => {
        document.querySelector('.k-scene__art:not(.k-scene__touch)').style.visibility = 'hidden';
    }""")
    page.wait_for_timeout(60)
    hidden = Image.open(io.BytesIO(page.screenshot(clip=box))).convert("RGB")
    page.evaluate("""() => {
        document.querySelectorAll('.k-scene__art').forEach(a => { a.style.visibility = ''; });
        const b = document.querySelector('.k-scene[data-was-animate]');
        if (b) {
          b.setAttribute('data-animate', b.getAttribute('data-was-animate'));
          b.removeAttribute('data-was-animate');
        } }""")
    diff = ImageChops.difference(shown, hidden).convert("L")
    w, h = diff.size
    px = diff.load()
    scale = w / box["width"]
    for x in range(w):
        if sum(1 for y in range(h) if px[x, y] > threshold) >= min_pixels:
            return box["x"] + x / scale
    return None


# Shared by the *_browser.py tests: the right edge of what the scene title row actually shows (its
# children), not the row's box, which can stretch past its content.
TITLE_RIGHT_JS = """() => {
    const t = document.querySelector('.k-scene__title');
    let r = -Infinity;
    for (const c of t.children) {
      if (c.getClientRects().length) r = Math.max(r, c.getBoundingClientRect().right);
    }
    return r === -Infinity ? t.getBoundingClientRect().right : r; }"""

# Shared by the *_browser.py tests: how many lines of text an element shows. Lines are
# clusters of text tops more than 6px apart; rounding top / 6 split one line in two at a bucket
# edge (a pill and its neighbor either side of it).
LINES_JS = """el => {
  // Lines are clusters of text tops more than 6px apart. Bucketing by rounding top / 6 split one
  // line in two whenever its page offset put a pill and its neighbor on either side of a bucket
  // edge (the announcement above the strip moved that offset).
  const tops = [];
  const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  for (let n = walk.nextNode(); n; n = walk.nextNode()) {
    if (!n.textContent.trim()) continue;
    // Decorative badges (monograms, glyphs) and tooltips (shown on hover only) are not lines.
    if (n.parentElement && n.parentElement.closest('[aria-hidden="true"],[role="tooltip"]')) {
      continue;
    }
    const r = document.createRange(); r.selectNodeContents(n);
    for (const x of r.getClientRects()) if (x.width > 0) tops.push(x.top);
  }
  tops.sort((a, b) => a - b);
  let lines = 0, last = -1e9;
  for (const t of tops) { if (t - last > 6) lines += 1; last = t; }
  return lines;
}"""


# The page lifecycle leak check (D-UI-210), shared by every page's *_browser.py test. Add
# TIMER_PROBE_JS with page.add_init_script() before the page loads: it notes every timer and who
# started it (the call stack, so the page's own script file shows in it), so the check can tell
# what a page left pending, or what fired, once the page was unmounted. A timer htmx starts for
# a declarative poll is htmx's (it goes with its element), not the page script's.
TIMER_PROBE_JS = """(() => {
  Error.stackTraceLimit = 64;
  const live = new Map(), fired = [];
  const set = { t: window.setTimeout, i: window.setInterval };
  const clear = { t: window.clearTimeout, i: window.clearInterval };
  const own = stack => !/htmx\\.min\\.js/.test(stack.split('\\n')[2] || '');
  function make(kind) {
    return function (fn, ms, ...args) {
      const stack = new Error().stack || '';
      let id = 0;
      const run = typeof fn !== 'function' ? fn : function () {
        if (kind === 't') live.delete(id);
        fired.push({ stack: stack, at: performance.now() });
        return fn.apply(this, arguments);
      };
      id = set[kind].call(window, run, ms, ...args);
      live.set(id, stack);
      return id;
    };
  }
  window.setTimeout = make('t');
  window.setInterval = make('i');
  window.clearTimeout = function (id) { live.delete(id); return clear.t.call(window, id); };
  window.clearInterval = function (id) { live.delete(id); return clear.i.call(window, id); };
  window.__kTimers = {
    pending: src => [...live.values()].filter(s => s.includes(src) && own(s)).length,
    firedSince: (src, t) => fired.filter(f => f.at >= t && f.stack.includes(src) && own(f.stack))
      .length,
  };
})();"""

_NO_PAGE = {"name": None, "listeners": 0, "timers": 0, "observers": 0}


def assert_page_remounts_clean(page: Page, name: str, script: str, settle_ms: int = 2600,
                               quiet_ms: int = 3000) -> dict:
    """The leak test every page copies (docs/handoff/D-UI-210-plan.md): the page base.html
    mounted is unmounted and mounted again through the KUI.page hooks. After the unmount nothing
    the page started is left (no listener, timer or observer, no timer of its script pending or
    firing); the second mount holds exactly what the first did. settle_ms lets the one-shot start
    timers (entrances) run out before each count. Returns the first mount's counts."""
    src = f"/static/js/{script}"
    assert page.evaluate("window.__kTimers !== undefined"), "add TIMER_PROBE_JS before goto"
    assert page.evaluate("KUI.page.current()") == name
    page.wait_for_timeout(settle_ms)
    first = page.evaluate("KUI.page._stats()")
    assert first["name"] == name and first["listeners"] > 0, first
    started = page.evaluate("() => { KUI.page.unmount(); return performance.now(); }")
    assert page.evaluate("KUI.page.current()") is None
    assert page.evaluate("KUI.page._stats()") == _NO_PAGE
    assert page.evaluate("src => window.__kTimers.pending(src)", src) == 0, "a timer survived"
    page.wait_for_timeout(quiet_ms)
    assert page.evaluate("([src, t]) => window.__kTimers.firedSince(src, t)", [src, started]) == 0
    assert page.evaluate("KUI.page.mount()") is True
    assert page.evaluate("KUI.page.current()") == name
    page.wait_for_timeout(settle_ms)
    assert page.evaluate("KUI.page._stats()") == first
    return first


# Counts the event listeners that are still live on matching elements (group 4 of D-UI-210: the
# partials an htmx swap brings in, whose scripts must not double their handlers). Add it with
# page.add_init_script() before the page loads: it wraps addEventListener / removeEventListener, and
# a listener counts while it was not removed, its AbortSignal is not aborted and its element is
# still in the document. window.__g4Listeners.live(selector[, type]) answers the count.
G4_LISTENER_PROBE_JS = """(() => {
  const reg = [];
  const add = EventTarget.prototype.addEventListener;
  const rm = EventTarget.prototype.removeEventListener;
  const capOf = o => (o && typeof o === 'object') ? !!o.capture : !!o;
  EventTarget.prototype.addEventListener = function (type, fn, opts) {
    reg.push({ t: this, type, fn, cap: capOf(opts), gone: false,
               signal: (opts && typeof opts === 'object') ? opts.signal : null });
    return add.call(this, type, fn, opts);
  };
  EventTarget.prototype.removeEventListener = function (type, fn, opts) {
    for (const r of reg) {
      if (r.t === this && r.type === type && r.fn === fn && r.cap === capOf(opts)) r.gone = true;
    }
    return rm.call(this, type, fn, opts);
  };
  window.__g4Listeners = {
    live: (sel, type) => reg.filter(r => !r.gone && !(r.signal && r.signal.aborted)
      && r.t instanceof Element && r.t.isConnected && r.t.matches(sel)
      && (!type || r.type === type)).length,
  };
})();"""
