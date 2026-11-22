"""The error codes of #174 task E in a real browser: Runs, Sources and Models. Each surface that
shows a code is opened in light and dark at 1440, 480 and 390 wide. The code is visible, on one
line, not clipped, off every neighbor, readable (AA for its size), and the page never scrolls
sideways. Screenshots go to KESTREL_CODE_SHOTS when it is set.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import doctor, paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import Run, ScrapeRun, Source
from kestrel.services import setup_state as ss
from tests import clock_seed, runs_seed
from tests.browser import LINES_JS, launch_sync, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

SHOTS = os.environ.get("KESTREL_CODE_SHOTS")
THEMES = ["light", "dark"]
WIDTHS = [1440, 480, 390]

# surface -> (path or a callable of the seed, the code selector, the text it must show)
_CONTRAST = r"""sel => {
  const parse = c => { const m = c.match(/[\d.]+/g).map(Number);
    return {r: m[0], g: m[1], b: m[2], a: m.length > 3 ? m[3] : 1}; };
  const lum = ({r, g, b}) => { const f = v => { v /= 255;
    return v <= .03928 ? v / 12.92 : Math.pow((v + .055) / 1.055, 2.4); };
    return .2126 * f(r) + .7152 * f(g) + .0722 * f(b); };
  const bgOf = el => { for (let e = el; e; e = e.parentElement) {
      const c = parse(getComputedStyle(e).backgroundColor); if (c.a > .5) return c; }
    return {r: 255, g: 255, b: 255, a: 1}; };
  return Array.from(document.querySelectorAll(sel)).filter(el => el.offsetParent).map(el => {
    const fg = parse(getComputedStyle(el).color), bg = bgOf(el);
    const [a, b] = [lum(fg), lum(bg)].sort((x, y) => y - x);
    return {t: el.textContent.trim(), ratio: (a + .05) / (b + .05),
            px: parseFloat(getComputedStyle(el).fontSize)}; });
}"""

_GEOM = """sel => Array.from(document.querySelectorAll(sel)).filter(e => e.offsetParent).map(el => {
  const b = el.getBoundingClientRect();
  const sibs = Array.from(el.parentElement.children).filter(s => s !== el && s.offsetParent)
    .map(s => s.getBoundingClientRect());
  const hit = sibs.some(s => b.left < s.right - 1 && s.left < b.right - 1
                          && b.top < s.bottom - 1 && s.top < b.bottom - 1);
  return {t: el.textContent.trim(), l: b.left, r: b.right, w: b.width, hit: hit,
          clipped: el.scrollWidth > el.clientWidth + 1}; })"""


def _seed_sources() -> None:
    async def go():
        await reset_engine()
        migrate.run_migrations()
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        rows = [
            # a failed check that was told to slow down: red, as on Runs (D-ERR-24 round 5)
            ("Remote roles", "jobspy", {"search_term": "Remote Analyst", "site_name": ["indeed"]},
             "failed", "HTTP 429 Too Many Requests", {"fetched": 0, "error_code": "SRC-002"}, 1),
            # a partial check that used today's allowance: amber
            ("Daily board", "jobspy", {"search_term": "Data Analyst", "site_name": ["glassdoor"]},
             "partial", "Daily allowance used", {"fetched": 2, "error_code": "SRC-007"}, 0),
            ("Pinecrest Labs", "ashby", {"token": "pinecrest"}, "failed", "HTTP 404 Not Found",
             {"error_code": "SRC-008"}, 4),
            ("Northwind Health", "greenhouse", {"token": "northwind"}, "ok", None,
             {"fetched": 14, "new": 1}, 0),
        ]
        async with sm() as s:
            for name, kind, config, status, error, counts, fails in rows:
                src = Source(track="employment", kind=kind, name=name, config=config,
                             origin="custom", enabled=True, cadence_hours=24,
                             consecutive_failures=fails,
                             last_run_at=clock_seed.ago(hours=1).isoformat())
                s.add(src)
                await s.flush()
                s.add(ScrapeRun(source_id=src.id, status=status, error=error, counts=counts,
                                started_at=clock_seed.ago(hours=1).isoformat()))
            await s.commit()
        await engine.dispose()
        forget_engine()

    asyncio.run(go())


def _seed_runs() -> dict:
    seed = runs_seed.seed()

    async def go():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                partial = (await s.execute(select(ScrapeRun).where(ScrapeRun.status == "partial"))
                           ).scalars().first()
                partial.counts = {"new": 11, "error_code": "SRC-007"}
                # a failed search beside it in the same run window, told to slow down
                s.add(ScrapeRun(source_id=partial.source_id, status="failed",
                                error="HTTP 429 Too Many Requests",
                                counts={"new": 0, "error_code": "SRC-002"},
                                started_at=partial.started_at, finished_at=partial.finished_at))
                runs = (await s.execute(select(Run).where(Run.status == "done"))).scalars().all()
                searched = next(r.id for r in runs
                                if r.started_at <= partial.started_at <= r.finished_at)
                failed = (await s.execute(select(Run.id).where(Run.status == "failed"))
                          ).scalars().first()
                await s.commit()
                return failed, searched
        finally:
            await engine.dispose()

    seed["failed"], seed["searched"] = asyncio.run(go())
    return seed


def _seed_models() -> None:
    migrate.run_migrations()
    rep = doctor.DoctorReport()
    rep.add("Ollama reachable", "FAIL", "not reachable", "Open Ollama, then press Check again.",
            code="CHK-005")
    rep.add("num_ctx tier2", "FAIL", "kestrel-tier2: num_ctx 8192 < required 16384",
            code="CHK-007")
    rep.add("Local AI: backend address", "WARN", "http://192.168.1.9:11434",
            "Your text goes to another address, not this computer.", code="CHK-014")
    rep.add("Local AI: egress", "WARN", "Ollama connected to another computer during the check.",
            "Open the details of the check.", code="CHK-017")
    ss.save_report(rep)


@pytest.fixture
def browser_and_base(monkeypatch, request):
    from playwright.sync_api import sync_playwright

    from kestrel.web import routes_control
    from kestrel.web.serve import running_server

    async def _down(*a, **k):
        return {"up": False, "loaded": []}

    monkeypatch.setattr(routes_control, "_probe_ollama", _down, raising=False)
    surface = request.node.callspec.params["surface"]
    seed = {}
    if surface.startswith("sources"):
        _seed_sources()
    elif surface.startswith("runs"):
        seed = _seed_runs()
    else:
        _seed_models()
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, seed
        browser.close()


SURFACES = {
    "sources-row": ("/sources", ".src-ty .src-code", "Code SRC-002"),
    "sources-drawer": ("/sources", ".src-drawer .src-code", "Code SRC-008"),
    "runs-timeline": ("/runs", ".rn-tl .rn-code", "Code RUN-001"),
    "runs-run": ("/runs/{failed}", ".rn-dr .rn-code", "Code RUN-001"),
    "runs-searches": ("/runs/{searched}", ".rn-dr [data-testid='rn-src-code']", "Code SRC-002"),
    "models-health": ("/models", ".mo-checks .mo-code", "Code CHK-005"),
}


# surface -> (one item, its tone element, its status words, code -> the tone class it ends with)
TONES = {
    "sources-row": ('[data-testid="source-row"]', ".k-dot", ".k-pill",
                    {"SRC-002": "k-dot--bad", "SRC-007": "k-dot--warn"}),
    "runs-searches": (".rn-dr li", ".k-pill", ".k-pill",
                      {"SRC-002": "k-pill--bad", "SRC-007": "k-pill--warn"}),
}


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("surface", list(SURFACES))
def test_a_code_is_visible_readable_and_never_overlaps(browser_and_base, surface, theme, width):
    base, browser, seed = browser_and_base
    path, selector, expected = SURFACES[surface]
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme,
                              device_scale_factor=2 if SHOTS else 1)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(base + path.format(**seed))
        if surface == "sources-drawer":
            page.wait_for_selector('[data-testid="source-row"]')
            page.locator('[data-testid="source-row"]', has_text="Pinecrest Labs").locator(
                '[data-testid="source-details"]').click()
            page.wait_for_selector(".src-drawer.is-open, .src-drawer[aria-hidden='false']")
        page.wait_for_selector(selector, state="visible")
        wait_for_layout(page)
        shown = [g["t"] for g in page.evaluate(_GEOM, selector)]
        assert expected in shown, (surface, shown)
        # No sideways scroll, nothing clipped, nothing on a neighbor, nothing off screen.
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 0") is True
        for geo in page.evaluate(_GEOM, selector):
            assert not geo["hit"], (surface, theme, width, geo)
            assert not geo["clipped"], (surface, theme, width, geo)
            assert geo["l"] >= 0 and geo["r"] <= width + 1, (surface, theme, width, geo)
        one_line = [page.evaluate(LINES_JS, el) <= 1
                    for el in page.query_selector_all(selector) if el.is_visible()]
        assert one_line and all(one_line), (surface, theme, width)
        # AA for its size: 4.5, or 3 for text of 24px or more (these are 12 to 13px).
        measured = page.evaluate(_CONTRAST, selector)
        for c in measured:
            assert c["ratio"] >= 4.5, (surface, theme, width, c)
        if SHOTS:  # keep the numbers with the screenshots
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            with (Path(SHOTS) / "contrast.txt").open("a", encoding="utf-8") as fh:
                for c in measured:
                    fh.write(f"{surface} {theme} {width} {c['t']} {c['ratio']:.2f} {c['px']}px\n")
        assert not errors, errors
        if SHOTS:
            page.locator(selector).first.scroll_into_view_if_needed()
            wait_for_layout(page)
            out = Path(SHOTS)
            out.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out / f"{surface}-{theme}-{width}.png"))
    finally:
        ctx.close()


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("surface", list(TONES))
def test_a_failed_check_is_red_and_a_partial_one_amber(browser_and_base, surface, theme, width):
    """One rule on Sources and Runs (D-ERR-24 round 5): a coded failed check is red, a coded
    partial one amber, and the status words stay readable (AA) in both themes."""
    base, browser, seed = browser_and_base
    path = SURFACES[surface][0]
    item, mark, words, want = TONES[surface]
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = ctx.new_page()
    try:
        page.goto(base + path.format(**seed))
        page.wait_for_selector(item, state="visible")
        wait_for_layout(page)
        for code, tone in want.items():
            row = page.locator(item, has_text=f"Code {code}")
            assert row.locator(mark).first.get_attribute("class").split()[-1] == tone, code
        pills = page.evaluate(_CONTRAST, f"{item} {words}")
        assert len(pills) >= len(want), pills
        for c in pills:
            assert c["ratio"] >= 4.5, (surface, theme, width, c)
    finally:
        ctx.close()
