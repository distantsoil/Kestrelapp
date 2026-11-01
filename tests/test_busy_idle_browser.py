"""#99 in a real browser: the busy bar belongs to what the person pressed. Real pages sit idle
past their poll intervals (the page clock runs the timers), and the bar must never light, so any
automatic request that is not marked as background fails here, wherever it comes from. The
onboarding case finishes a model action so the poll answers ob-refresh and the step reloads.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests import runs_seed
from tests.browser import launch_sync

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

_WATCH = """(() => {
  window.__busy = [];
  document.addEventListener('DOMContentLoaded', () => {
    new MutationObserver(() => {
      if (document.body.classList.contains('busy')) window.__busy.push(location.pathname);
    }).observe(document.body, {attributes: true, attributeFilter: ['class']});
  });
})()"""
_IDLE_SECONDS = 15  # past the 5 s Runs poll and the 10 s status rail


def _track_inflight(page) -> dict:
    """Track the page's xhr/fetch requests still in flight, one by one. `wait_for_load_state(
    "networkidle")` returns at once when the page was already idle, so it never waits for what a
    clock step started; this does. Requests that started before tracking are simply not in the
    set, so they can never drive the count below zero."""
    state: dict = {"live": set()}

    def started(r):
        if r.resource_type in ("xhr", "fetch"):
            state["live"].add(r)

    def ended(r):
        state["live"].discard(r)

    page.on("request", started)
    page.on("requestfinished", ended)
    page.on("requestfailed", ended)
    return state


def _settle(page, state: dict, timeout_s: float = 10.0) -> None:
    """Wait (real time) until every request a clock step started has finished, plus one beat
    so a reply that triggers a follow-up request is caught too."""
    import time

    deadline = time.monotonic() + timeout_s
    quiet = 0
    while time.monotonic() < deadline:
        page.wait_for_timeout(50)
        quiet = quiet + 1 if not state["live"] else 0
        if quiet >= 4:
            return
    raise AssertionError(f"{len(state['live'])} request(s) still in flight after {timeout_s}s")


def _sit_idle(page, seconds: int = _IDLE_SECONDS) -> list[str]:
    """Run the page's timers forward in 5 s steps; each waits until its requests have finished."""
    asked: list[str] = []
    page.on("request", lambda r: asked.append(r.url)
            if r.resource_type in ("xhr", "fetch") else None)
    inflight = _track_inflight(page)
    for _ in range(seconds // 5):
        page.clock.run_for(5000)
        _settle(page, inflight)
    return asked


# (path, whether the page polls on its own while idle)
@pytest.mark.parametrize(
    ("path", "polls"), [("/runs", True), ("/", True), ("/settings", True), ("/pipeline", False)]
)
def test_a_page_sitting_idle_never_lights_the_busy_bar(path, polls):
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    runs_seed.seed(active=True)
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.add_init_script(_WATCH)
            page.clock.install()
            page.goto(f"{base}{path}")
            page.wait_for_load_state("networkidle")
            asked = _sit_idle(page)
            if polls:
                assert asked, f"{path} made no automatic request while idle; the test saw nothing"
            assert page.evaluate("window.__busy") == [], (path, asked)
            assert not errors, errors
        finally:
            ctx.close()
            browser.close()


def _run(coro_fn):
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro_fn())).result()


def test_an_onboarding_step_reloading_when_a_model_action_finishes_leaves_the_bar_idle():
    """The local box polls while a model download runs; when it has finished the poll answers
    HX-Trigger ob-refresh and the whole step reloads. The reload is automatic work."""
    from playwright.sync_api import sync_playwright
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel import paths
    from kestrel.db.engine import make_async_engine
    from kestrel.db.models import Task
    from kestrel.services import model_tasks as mt
    from kestrel.web.serve import running_server
    from tests.test_onboarding_browser import _seed

    _seed()

    async def start_pull():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                s.add(Task(type=mt.PULL_TYPE, status="pending",
                           payload={"tag": "qwen3:8b", "batch": 1}))
                await s.commit()
        finally:
            await engine.dispose()

    async def finish_pull():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                rows = await s.execute(select(Task).where(Task.type == mt.PULL_TYPE))
                for t in rows.scalars():
                    t.status = "done"
                await s.commit()
        finally:
            await engine.dispose()

    _run(start_pull)
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        try:
            page.add_init_script(_WATCH)
            page.clock.install()
            page.goto(f"{base}/onboarding?step=models")
            page.wait_for_selector("#ob-step")
            page.wait_for_selector('[hx-get*="/onboarding/models/local?poll=1"]', state="attached")
            _run(finish_pull)
            reloads: list[str] = []
            page.on("request", lambda r: reloads.append(r.url)
                    if r.url.endswith("/onboarding/step") else None)
            inflight = _track_inflight(page)
            for _ in range(3):
                page.clock.run_for(2000)
                _settle(page, inflight)
            assert reloads, "the finished action never reloaded the step"
            assert page.evaluate("window.__busy") == []
        finally:
            ctx.close()
            browser.close()
