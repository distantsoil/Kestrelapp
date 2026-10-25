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


def launch_sync(playwright: Any, monkeypatch: pytest.MonkeyPatch | None = None) -> Any:
    """Edge first, then the fallback Chromium; skip only when neither starts."""
    errors: list[str] = []
    for kwargs in _launch_kwargs():
        try:
            browser = playwright.chromium.launch(headless=True, **kwargs)
        except Exception as exc:  # Playwright Error: missing channel/executable
            errors.append(str(exc).splitlines()[0][:120])
            continue
        if "executable_path" in kwargs and monkeypatch is not None:
            monkeypatch.setenv(BROWSER_PATH_ENV, kwargs["executable_path"])
        return browser
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
