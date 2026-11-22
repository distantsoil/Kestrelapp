"""The MDL and ONL codes on the page (#174 task C, D-ERR-511, D-ERR-512): a failed model download
shows its code under the message on Models and in setup, and a refused Run now shows its ONL code
under the reason on Today. Light, dark and a 390-wide window; the code sits on its own line below
the sentence, never over it, and nothing scrolls sideways. Invented data only."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from tests import today_seed
from tests.browser import launch_sync, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

SENTENCE = "Ollama is not answering at the address you set. Open Ollama, then press Try again."

_BOX = """sel => { const e = document.querySelector(sel); if (!e) return null;
  const b = e.getBoundingClientRect();
  return {l: b.left, r: b.right, tp: b.top, bt: b.bottom, w: b.width, h: b.height,
          sw: e.scrollWidth, cw: e.clientWidth, t: (e.textContent || '').trim()}; }"""


def _seed_failed_pull(tag: str = "invented-model:4b") -> None:
    import httpx
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.db.engine import make_async_engine
    from kestrel.db.models import Task
    from kestrel.errors import coded_record

    migrate.run_migrations(paths.db_path())

    async def go() -> None:
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                s.add(Task(type="ollama_pull", status="dead", attempts=3, max_attempts=3,
                           payload={"tag": tag, "batch": "b1"},
                           last_error=coded_record(SENTENCE, "MDL-004",
                                                   httpx.ConnectError("refused"))))
                await s.commit()
        finally:
            await engine.dispose()

    asyncio.run(go())


def _in_thread(fn, *args) -> None:
    """Run a seeding helper off the browser's event loop (sync Playwright keeps one running)."""
    import threading

    failure: list[BaseException] = []

    def run() -> None:
        try:
            fn(*args)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the test's thread below
            failure.append(exc)

    t = threading.Thread(target=run)
    t.start()
    t.join()
    if failure:
        raise failure[0]


def _jump_to_models() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel import paths
    from kestrel.db.engine import make_async_engine
    from kestrel.services import onboarding as ob

    async def go() -> None:
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                state = await ob.load(s) or ob.OnboardingState()
                state.step = "models"
                await ob.save(s, state)
                from kestrel.db.models import Setting
                from kestrel.services import setup_state as ss

                for key in (ss.MODE_KEY, ss.CHOICE_KEY):
                    await s.merge(Setting(key=key, value="local"))
                await s.commit()
        finally:
            await engine.dispose()

    asyncio.run(go())


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    asyncio.run(today_seed.seed())
    _seed_failed_pull()
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser
        browser.close()


def _shot(page, name: str) -> None:
    out = Path(os.environ.get("KESTREL_SHOT_DIR") or "")
    if os.environ.get("KESTREL_SHOT_DIR"):
        out.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out / f"{name}.png"), full_page=False)


def _check_below(page, sentence_sel: str, code_sel: str, want: str) -> None:
    page.locator(code_sel).scroll_into_view_if_needed()
    sentence = page.evaluate(_BOX, sentence_sel)
    code = page.evaluate(_BOX, code_sel)
    assert sentence is not None and code is not None, (sentence, code)
    assert code["t"] == want, code
    assert code["w"] > 0 and code["h"] > 0, code
    assert code["sw"] <= code["cw"] + 1, code  # the code is never clipped
    assert code["tp"] >= sentence["bt"] - 1, (sentence, code)  # its own line, below
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True


@pytest.fixture
def models_live(monkeypatch):
    """The Models page with the Ollama probe and hardware patched, as in test_models_browser."""
    from playwright.sync_api import sync_playwright

    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.llm.profiles import Hardware
    from kestrel.services import setup_state as ss
    from kestrel.web.serve import running_server

    monkeypatch.setattr(ss, "detected_hardware", lambda: Hardware(
        os="windows", gpu_vendor="nvidia", vram_gb=12.0, ram_gb=32.0))
    monkeypatch.setattr(ss, "probe_ollama", lambda settings, fresh=False: ("0.5.0", []))
    migrate.run_migrations(paths.db_path())
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser
        browser.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 390])
def test_models_shows_the_code_under_a_failed_download(models_live, theme, width):
    base, browser = models_live
    ctx = browser.new_context(viewport={"width": width, "height": 1000}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"{base}/models")
        page.wait_for_selector('[data-testid="mo-dl-1"]', state="attached")
        tag = page.locator('[data-testid="mo-dl-1"] b').first.text_content().strip()
        _in_thread(_seed_failed_pull, tag)
        page.goto(f"{base}/models")
        page.wait_for_selector('[data-testid="mo-dl-code"]', state="attached")
        details = page.locator("#mo-downloads > details")
        if details.count() and details.get_attribute("open") is None:
            page.locator("#mo-downloads > details > summary").click()
        page.wait_for_selector('[data-testid="mo-dl-code"]', state="visible")
        wait_for_layout(page)
        _check_below(page, '[data-testid="mo-dl-1"] .mo-bad', '[data-testid="mo-dl-code"]',
                     "Code MDL-004")
        assert page.inner_text('[data-testid="mo-dl-1"] .mo-bad').startswith(SENTENCE)
        _shot(page, f"model-code-models-{theme}-{width}")
        assert errors == []
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 390])
def test_setup_shows_the_code_under_a_failed_download(models_live, theme, width):
    base, browser = models_live
    _in_thread(_seed_failed_pull, "invented-model:4b")
    _in_thread(_jump_to_models)
    ctx = browser.new_context(viewport={"width": width, "height": 1000}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"{base}/onboarding")
        page.wait_for_selector('#ob-step[data-step="models"]')
        page.wait_for_selector('[data-testid="setup-progress-code"]', state="visible")
        wait_for_layout(page)
        _check_below(page, ".ob-err", '[data-testid="setup-progress-code"]', "Code MDL-004")
        _shot(page, f"model-code-setup-{theme}-{width}")
        assert errors == []
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 390])
def test_today_shows_the_code_under_a_refused_run_now(live, theme, width):
    base, browser = live
    ctx = browser.new_context(viewport={"width": width, "height": 1000}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"{base}/?scan_outcome=held")
        page.wait_for_selector('[data-testid="scan-code"]', state="visible")
        wait_for_layout(page)
        msg = page.inner_text("[data-scan-msg]")
        assert msg.startswith("Not started: nothing was sent. Confidential mode is on"), msg
        code = page.evaluate(_BOX, '[data-testid="scan-code"]')
        assert code["t"] == "Code ONL-005" and code["sw"] <= code["cw"] + 1, code
        # The code is the last line of the message box, below the words.
        box = page.evaluate(_BOX, "[data-scan-msg]")
        assert box["bt"] >= code["bt"] - 1 and code["tp"] > box["tp"] + 1, (box, code)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
        # Never over the run line beside or above it.
        run = page.evaluate(_BOX, ".td-seg:has([data-scan-msg]) .td-seg__main")
        overlap = (box["l"] < run["r"] - 1 and run["l"] < box["r"] - 1
                   and box["tp"] < run["bt"] - 1 and run["tp"] < box["bt"] - 1)
        assert not overlap, (run, box)
        assert run["w"] >= 120, run  # the run line keeps its room; it is never squeezed shut
        # Inside the strip's clipped box, not past its edge (the strip hides overflow).
        strip = page.evaluate(_BOX, ".td-strip")
        for part in (box, code):
            assert strip["l"] - 1 <= part["l"] and part["r"] <= strip["r"] + 1, (strip, part)
        _shot(page, f"model-code-today-{theme}-{width}")
        assert errors == []
    finally:
        ctx.close()
