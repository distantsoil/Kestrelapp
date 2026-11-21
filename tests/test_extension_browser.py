"""Kestrel Capture in a real Chromium (issue #186): setup, every popup screen, the role summary,
Read with Save, the settings page and the board badges, against a seeded Kestrel and a local test
board with invented postings. Light, dark and narrow screenshots go to KESTREL_SHOTS.

Invariants proved here, in the browser:
- The extension's pages cannot connect to any host but this computer: a fetch to another
  address from the popup is refused by the extension's own Content Security Policy.
- Badges never write into the page: after they render, the page's own DOM outside the hosts the
  extension inserted is byte-identical, and the hosts hold closed shadow roots.
- Read fills only the role the person picked, after the check, and creates no role.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.browser import fallback_chromium

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

SHOTS = os.environ.get("KESTREL_SHOTS", "")
KEY = "test-extension-key-0123456789"
WORDS = " ".join(["Plan the weekly delivery schedule and keep the team informed."] * 14)

_OVERLAP = """() => {
  const els = [...document.querySelectorAll(
    'header, button, h1, h2, p, .opt, .tile, .dim, .chip, dt, dd, label, li')]
    .filter(e => e.offsetParent !== null);
  const boxes = [...document.querySelectorAll(
    'header, footer, .docs, .grid2, .tiles, .box:not(.preview)')]
    .filter(e => e.offsetParent !== null);
  const clipped = els.filter(e => e.scrollWidth > e.clientWidth + 1
    && ['hidden', 'clip'].includes(getComputedStyle(e).overflowX))
    .concat(boxes.filter(e => e.scrollHeight > e.clientHeight + 1
      && ['hidden', 'clip'].includes(getComputedStyle(e).overflowY)));
  const wide = document.documentElement.scrollWidth > document.documentElement.clientWidth + 1;
  return {clipped: clipped.map(e => e.textContent.trim().slice(0, 40)), wide};
}"""


def _board_html(port: int) -> str:
    rows = "".join(
        f'<li><a href="http://127.0.0.1:{port}/jobs/{n}">{title}</a> <span>Northwind</span></li>'
        for n, title in (
            (101, "Warehouse Planner"),
            (102, "Route Coordinator"),
            (103, "Fleet Analyst"),
            (104, "Dock Supervisor"),
            (105, "Yard Lead"),
            (106, "Unknown Role"),
        )
    )
    return (
        f"<!doctype html><html><head><title>Sample board</title></head><body>"
        f"<h1>Sample board</h1><ul id='list'>{rows}"
        # A LinkedIn link Kestrel knows: it must never be looked up or badged (D-SRC-5).
        "<li><a href='https://www.linkedin.com/jobs/view/999'>Linked Role</a> "
        "<span>Northwind</span></li></ul></body></html>"
    )


def _posting_html(title: str, body: str) -> str:
    return (
        f"<!doctype html><html><head><title>{title}</title></head>"
        f"<body><h1>{title}</h1><p>{body}</p></body></html>"
    )


class _Board(BaseHTTPRequestHandler):
    pages: dict[str, str] = {}

    def do_GET(self):  # noqa: N802 - http.server API
        body = self.pages.get(self.path, "<html><body>missing</body></html>").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


def _seed(board: int) -> dict[str, int]:
    from kestrel import paths
    from kestrel.db.base import utcnow_iso
    from kestrel.db.engine import make_async_engine
    from kestrel.db.models import Evaluation, Opportunity, Org, Pursuit, Setting, Source
    from kestrel.web.fit import EMPTY_POSTING_STAGE_REASON

    async def run() -> dict[str, int]:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        eng = make_async_engine(paths.db_path())
        sm = async_sessionmaker(eng, expire_on_commit=False)
        ids: dict[str, int] = {}
        async with sm() as s:
            org = Org(name="Northwind", normalized_name="northwind", size_band="mid")
            src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
            s.add_all([org, src, Setting(key="server.shared_secret", value=KEY)])
            await s.flush()

            def opp(n: int, title: str, **kw) -> Opportunity:
                o = Opportunity(
                    track="employment",
                    opp_type="job_posting",
                    org_id=org.id,
                    source_id=src.id,
                    title=title,
                    org_name="Northwind",
                    url=f"http://127.0.0.1:{board}/jobs/{n}",
                    first_seen_at=kw.pop("seen", utcnow_iso()),
                    **kw,
                )
                s.add(o)
                return o

            scored = opp(
                101,
                "Warehouse Planner",
                stage="t2_done",
                description_md=WORDS,
                location_raw="Springfield, IL",
            )
            waiting = opp(
                102, "Route Coordinator", stage="t1_pass", stage_reason=EMPTY_POSTING_STAGE_REASON
            )
            other = opp(
                201,
                "Night Dispatcher",
                stage="t1_pass",
                stage_reason=EMPTY_POSTING_STAGE_REASON,
                seen="2026-11-01T10:00:00Z",
            )
            other.url = "https://jobs.example.org/201"
            opp(103, "Fleet Analyst", stage="t2_done", description_md=WORDS, ghost_score=0.9)
            opp(104, "Dock Supervisor", stage="t2_done", description_md=WORDS, liveness="closed")
            linked = opp(999, "Linked Role", stage="t2_done", description_md=WORDS,
                         liveness="closed")  # a state that always draws a badge
            linked.url = "https://www.linkedin.com/jobs/view/999"
            yard = opp(105, "Yard Lead", stage="t2_done", description_md=WORDS)
            await s.flush()
            s.add(Pursuit(opportunity_id=yard.id, track="employment", status="applied",
                          applied_at="2026-11-03"))
            await s.flush()
            s.add(
                Evaluation(
                    opportunity_id=scored.id,
                    tier=2,
                    track="employment",
                    verdict="apply",
                    verdict_model="apply",
                    score=4.2,
                    weighted_score=4.1,
                    needs_review=False,
                    dimensions={
                        "role_fit": {"score": 4.5},
                        "domain_match": {"score": 4.0},
                        "trajectory": {"score": 3.5},
                        "company_context": {"score": 3.0},
                    },
                    blocks={
                        "b_fit": {
                            "matches": [
                                {
                                    "requirement": "Plans weekly delivery schedules",
                                    "strength": "strong",
                                },
                                {
                                    "requirement": "Keeps a small team informed",
                                    "strength": "partial",
                                },
                            ],
                            "gaps": [
                                {
                                    "requirement": "Forklift certificate",
                                    "severity": "minor",
                                    "mitigation": "Mention the course you booked.",
                                }
                            ],
                        },
                        "c_level": {"relative_level": "at"},
                    },
                    blockers=[],
                    legitimacy={"status": "clean", "signals": []},
                )
            )
            await s.commit()
            ids.update(scored=scored.id, waiting=waiting.id, other=other.id)
        await eng.dispose()
        return ids

    return asyncio.run(run())


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.db import migrate
    from kestrel.db.engine import forget_engine
    from kestrel.web import auth
    from kestrel.web.serve import running_server

    browser_path = fallback_chromium()
    if browser_path is None:
        pytest.skip("Browser unavailable: no Chromium to load the extension in")
    board = ThreadingHTTPServer(("127.0.0.1", 0), _Board)
    bport = board.server_address[1]
    _Board.pages = {
        "/board": _board_html(bport),
        "/jobs/102": _posting_html("Route Coordinator | Sample board", WORDS),
        "/check": _posting_html("Just a moment...", "Verify you are human. Press and hold."),
        "/jobs/101": _posting_html("Warehouse Planner | Sample board", WORDS),
    }
    threading.Thread(target=board.serve_forever, daemon=True).start()
    migrate.run_migrations()
    ids = _seed(bport)
    forget_engine()
    auth.reset_cache()
    ext = Path(tempfile.mkdtemp()) / "ext"
    shutil.copytree("extension", ext)
    manifest = json.loads((ext / "manifest.json").read_text(encoding="utf-8"))
    # The test board is served from this computer, so the badges' content script is pointed at
    # it; nothing else in the manifest changes.
    manifest["content_scripts"][0]["matches"] = ["http://127.0.0.1/*"]
    (ext / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    # The shipped popup describes the active tab. Here it is opened as a page in its own tab, so
    # the test copy (only) reads ?tab= to name the board tab it describes. Reading that tab still
    # needs the browser's grant, which the loopback host permission gives the test board.
    popup = (ext / "popup.js").read_text(encoding="utf-8")
    marker = (
        "    state.tab = (await chrome.tabs.query({ active: true, currentWindow: true }))[0]"
        " || null;\n"
    )
    assert popup.count(marker) == 1, "popup.js no longer has the active-tab line the test hooks"
    hook = (
        "    const named = Number(new URLSearchParams(location.search).get('tab'));\n"
        "    if (Number.isInteger(named) && named > 0) state.tab = await chrome.tabs.get(named);\n"
    )
    (ext / "popup.js").write_text(popup.replace(marker, marker + hook), encoding="utf-8")
    with running_server(worker=False) as base, sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            tempfile.mkdtemp(),
            headless=True,
            executable_path=browser_path,
            args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"],
            viewport={"width": 380, "height": 600},
            device_scale_factor=2,
        )
        sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker")
        ext_id = sw.url.split("/")[2]
        port = int(base.rsplit(":", 1)[1])
        yield {"ctx": ctx, "id": ext_id, "port": port, "board": bport, "ids": ids, "base": base}
        ctx.close()
    board.shutdown()


def _shot(page, name: str) -> None:
    page.wait_for_timeout(450)  # let the open animation finish
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def _ext_page(live, path: str, *, scheme: str = "light", width: int = 380, height: int = 600):
    page = live["ctx"].new_page()
    page.set_viewport_size({"width": width, "height": height})
    page.emulate_media(color_scheme=scheme)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"chrome-extension://{live['id']}/{path}")
    return page, errors


def _store(page, values: dict) -> None:
    page.evaluate("v => chrome.storage.local.set(v)", values)


def _tab_id(page, url: str) -> int:
    return page.evaluate("u => chrome.tabs.query({}).then(ts => ts.find(t => t.url === u).id)", url)


def _popup_for(live, url: str, scheme: str, name: str):
    tab = live["ctx"].new_page()
    tab.goto(url)
    helper, _ = _ext_page(live, "options.html")
    tid = _tab_id(helper, url)
    helper.close()
    page, errors = _ext_page(live, f"popup.html?tab={tid}", scheme=scheme)
    page.wait_for_selector(".view")
    return tab, page, errors


def _features(page, port: int, **values) -> None:
    page.evaluate(
        """([port, key, body]) => fetch(`http://127.0.0.1:${port}/api/extension/features`, {
            method: 'PUT', headers: {'X-Kestrel-Key': key, 'Content-Type': 'application/json'},
            body: JSON.stringify(body)})""",
        [port, KEY, values],
    )


def _no_overlap(page, label: str) -> None:
    out = page.evaluate(_OVERLAP)
    assert not out["wide"] and not out["clipped"], (label, out)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_setup_wrong_key_then_done(live, scheme):
    page, errors = _ext_page(live, "popup.html", scheme=scheme)
    _store(page, {"port": live["port"], "secret": ""})
    page.reload()
    page.wait_for_selector("text=Found Kestrel")
    _shot(page, f"setup-step1-{scheme}")
    page.fill("#key", "not-the-key")
    page.get_by_role("button", name="Connect", exact=True).click()
    page.get_by_text("Kestrel didn't accept that key.").first.wait_for()
    _no_overlap(page, "wrong key")
    _shot(page, f"setup-wrong-key-{scheme}")
    page.fill("#key", KEY)
    page.get_by_role("button", name="Connect", exact=True).click()
    page.get_by_text("You're set up").first.wait_for()
    _shot(page, f"setup-done-{scheme}")
    assert page.evaluate("chrome.storage.local.get(['secret']).then(r => r.secret)") == KEY
    assert not errors


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_role_summary_and_documents(live, scheme):
    helper, _ = _ext_page(live, "options.html")
    _store(helper, {"port": live["port"], "secret": KEY})
    helper.close()
    url = f"http://127.0.0.1:{live['board']}/jobs/101"
    tab, page, errors = _popup_for(live, url, scheme, "summary")
    page.wait_for_selector("text=Warehouse Planner")
    page.wait_for_selector("text=Why it fits")
    assert page.locator(".ring b").text_content() == "4.2"
    _no_overlap(page, "summary")
    _shot(page, f"known-role-{scheme}")
    page.get_by_role("button", name="Documents").click()
    page.wait_for_selector("text=Your documents for this role")
    page.get_by_text(
        "Kestrel won't fill in this application. Attach these yourself."
    ).first.wait_for()
    _shot(page, f"known-role-documents-open-{scheme}")
    page.get_by_role("button", name="I've applied").click()
    done = page.get_by_role("button", name=re.compile(r"^Applied [A-Z][a-z]{2} \d{1,2}$"))
    done.wait_for()
    assert done.is_disabled()
    _shot(page, f"known-role-applied-{scheme}")
    assert not errors
    tab.close()


def test_read_saves_only_the_picked_role(live):
    import sqlite3

    from kestrel import paths

    helper, _ = _ext_page(live, "options.html")
    _store(helper, {"port": live["port"], "secret": KEY})
    helper.close()
    con = sqlite3.connect(str(paths.db_path()))
    before = con.execute("SELECT count(*) FROM opportunity").fetchone()[0]
    for scheme in ("light", "dark"):
        url = f"http://127.0.0.1:{live['board']}/jobs/102"
        tab, page, errors = _popup_for(live, url, scheme, "waiting")
        page.wait_for_selector("text=Waiting for a description")
        _no_overlap(page, "waiting")
        _shot(page, f"popup-roles-waiting-{scheme}")
        page.get_by_role("button", name="Read this page").click()
        page.wait_for_selector("text=Check before saving")
        page.wait_for_selector("text=Titles match")
        _shot(page, f"popup-check-before-saving-{scheme}")
        if scheme == "dark":
            page.get_by_role("button", name="Save", exact=True).click()
            page.wait_for_selector("text=Saved. Kestrel will score it in its next run.")
            _shot(page, "popup-saved-dark")
        else:
            page.get_by_role("button", name="Cancel").click()
        assert not errors
        tab.close()
    rows = dict(con.execute("SELECT id, stage FROM opportunity").fetchall())
    assert rows[live["ids"]["waiting"]] == "new"
    assert rows[live["ids"]["other"]] == "t1_pass"
    assert con.execute("SELECT count(*) FROM opportunity").fetchone()[0] == before
    con.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_check_page_offers_no_save(live, scheme):
    helper, _ = _ext_page(live, "options.html")
    _store(helper, {"port": live["port"], "secret": KEY})
    helper.close()
    # Served as localhost so the site differs from the waiting roles' (127.0.0.1): state 5.
    url = f"http://localhost:{live['board']}/check"
    tab, page, errors = _popup_for(live, url, scheme, "check")
    page.wait_for_selector("text=Or fill in a waiting role")
    _shot(page, f"popup-none-from-this-site-{scheme}")
    page.fill("input[type=search]", "Night")
    page.wait_for_selector("text=Night Dispatcher")
    page.get_by_role("button", name="Read this page").click()
    page.wait_for_selector("text=This looks like a check page, not a job posting.")
    assert page.locator("button:has-text('Save')").count() == 0
    _no_overlap(page, "check page")
    _shot(page, f"popup-not-a-job-posting-{scheme}")
    page.click("button[aria-label='Back']")
    page.click("button[aria-label='Settings and help']")
    page.wait_for_selector("text=About Kestrel Capture")
    _shot(page, f"popup-gear-menu-{scheme}")
    assert not errors
    tab.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_nothing_waiting_and_not_running(live, scheme):
    helper, _ = _ext_page(live, "options.html")
    _store(helper, {"port": live["port"], "secret": KEY})
    page, errors = _ext_page(live, "popup.html", scheme=scheme)
    _features(page, live["port"], waiting=False)
    page.reload()
    page.wait_for_selector("text=Capture this role")
    page.click("button[aria-label='More ways to capture']")
    page.wait_for_selector("text=Capture and score now")
    _shot(page, f"popup-nothing-waiting-{scheme}")
    _features(page, live["port"], waiting=True)
    _store(helper, {"port": 1, "secret": KEY})  # nothing listens there
    page.reload()
    page.wait_for_selector("text=Open Kestrel on this computer")
    _shot(page, f"popup-kestrel-not-running-{scheme}")
    _store(helper, {"port": live["port"], "secret": KEY})
    assert not errors


def test_pages_cannot_reach_any_other_host(live):
    page, _ = _ext_page(live, "popup.html")
    # 192.0.2.1 is a documentation address; the request must be refused before it leaves.
    out = page.evaluate("""() => fetch('http://192.0.2.1/', {signal: AbortSignal.timeout(3000)})
        .then(() => 'sent', e => String(e))""")
    violations = page.evaluate(
        "() => new Promise(r => { const v = []; "
        "document.addEventListener('securitypolicyviolation', e => v.push(e.blockedURI)); "
        "fetch('http://198.51.100.7/').catch(() => setTimeout(() => r(v), 200)); })"
    )
    assert out != "sent"
    assert any("198.51.100.7" in v for v in violations), violations


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_options_page_sections(live, scheme):
    page, errors = _ext_page(live, "options.html", scheme=scheme, width=1100, height=800)
    _store(page, {"port": live["port"], "secret": KEY})
    page.reload()
    page.wait_for_selector("text=Connected to Kestrel")
    _shot(page, f"options-connection-{scheme}")
    for sec in ("browser", "about"):
        page.goto(f"chrome-extension://{live['id']}/options.html#{sec}")
        page.wait_for_selector(f"#{sec}.on")
        _shot(page, f"options-{sec}-{scheme}")
    page.set_viewport_size({"width": 390, "height": 800})
    page.goto(f"chrome-extension://{live['id']}/options.html#about")
    page.wait_for_selector("#about.on")
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth + 1")
    _shot(page, f"options-about-narrow-{scheme}")
    assert not errors


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_badges_live_in_shadow_roots_and_leave_the_page_alone(live, scheme):
    helper, _ = _ext_page(live, "options.html")
    _store(helper, {"port": live["port"], "secret": KEY})
    helper.close()
    page = live["ctx"].new_page()
    page.set_viewport_size({"width": 700, "height": 420})
    page.emulate_media(color_scheme=scheme)
    page.goto(f"http://127.0.0.1:{live['board']}/board")
    before = page.evaluate("document.documentElement.outerHTML")
    page.wait_for_function(
        "[...document.querySelectorAll('#list a + span')].filter(s => !s.textContent).length >= 5",
        timeout=15000,
    )
    hosts = page.evaluate(
        "[...document.querySelectorAll('#list a + span')].filter(s => !s.textContent).length"
    )
    assert hosts == 5, hosts  # five known postings; the unknown and the LinkedIn one have none
    # Closed roots: the page sees an empty span with no shadowRoot it can reach.
    assert page.evaluate(
        "[...document.querySelectorAll('#list a + span')]"
        ".filter(s => !s.textContent).every(s => s.shadowRoot === null)"
    )
    after = page.evaluate("""() => { const c = document.documentElement.cloneNode(true);
        c.querySelectorAll('#list a + span').forEach(s => {
          if (!s.textContent && !s.attributes.length) s.remove(); });
        return c.outerHTML; }""")
    assert after == before
    _shot(page, f"board-badges-{scheme}")
    page.close()
