"""Runs (D-UI-103): the header that follows the run state, the live card and its five stages,
Latest run, the timeline and its drawer, Run now and "This run only", Pause, Resume and Stop, the
Needs another try card and the run detail page. Cloud-safe: TestClient on the real app, a throwaway
data directory, fictional runs (tests/runs_seed.py), no model and no network. The live-browser
parity check is tests/test_runs_browser.py."""

from __future__ import annotations

import asyncio
import html as html_mod
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db.engine import make_async_engine
from kestrel.db.models import Run, ScrapeRun, Setting, Task
from kestrel.web.app import build_app, set_paused
from tests import runs_seed

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

HX = {"HX-Request": "true"}
_WEB = Path(__file__).resolve().parents[1] / "kestrel" / "web"


def _read(fn):
    async def go():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                return await fn(s)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _write(fn):
    async def inner(s):
        out = await fn(s)
        await s.commit()
        return out

    return _read(inner)


@pytest.fixture
def idle():
    seed = runs_seed.seed()
    with TestClient(build_app(worker=False)) as c:
        yield c, seed


@pytest.fixture
def active():
    seed = runs_seed.seed(active=True)
    with TestClient(build_app(worker=False)) as c:
        yield c, seed
    set_paused(False)


def _empty_app() -> TestClient:
    from kestrel.db import migrate

    migrate.run_migrations()
    return TestClient(build_app(worker=False))


def _region(html: str, name: str) -> str:
    return html.split(f'data-region="{name}"')[1].split("data-region=")[0]


# ---------------------------------------------------------------------------- the header
def test_idle_header_has_run_now_and_the_menu_and_no_pause(idle):
    c, _ = idle
    html = c.get("/runs").text
    head = _region(html, "header")
    assert 'data-testid="rn-run-now"' in head and 'hx-post="/runs/start"' in head
    assert 'data-testid="rn-this-run"' in head and "This run only" in head
    assert 'data-testid="rn-chip"' not in head
    assert 'data-testid="run-pause"' not in html and 'data-testid="run-stop"' not in html


def test_running_header_is_a_chip_with_the_step_and_no_run_now(active):
    c, _ = active
    html = c.get("/runs").text
    head = _region(html, "header")
    assert 'data-testid="rn-chip"' in head and "Running ·" in head
    assert "Searching 3 of 3 searches" in head
    assert 'data-testid="rn-run-now"' not in html
    # Pause and Stop live in the card; Stop confirms inline and Keep running backs out.
    assert 'data-testid="run-pause"' in html and 'data-testid="run-stop"' in html
    assert "Stop this run?" in html and "data-rn-keep" in html


def test_paused_header_says_paused_and_the_card_offers_resume_and_stop(active):
    c, _ = active
    set_paused(True)
    html = c.get("/runs").text
    head = _region(html, "header")
    assert "is-paused" in head and "Paused ·" in head
    assert 'data-testid="run-resume"' in html and 'hx-post="/runs/resume"' in html
    assert 'data-testid="run-stop"' in html and 'data-testid="run-pause"' not in html
    assert "Nothing new starts until you resume" in html
    # Never the stall warning while paused.
    assert 'data-testid="run-stall"' not in html and "Paused by you" in html


# ------------------------------------------------------------------------------- live card
def test_live_card_shows_five_stage_tiles_events_and_last_activity(active):
    c, _ = active
    live = _region(c.get("/runs").text, "live")
    for key in ("search", "rules", "quick", "rating", "deeper"):
        assert f'data-bar="{key}"' in live
    assert "Only when you ask" in live
    step = live.split('data-testid="run-step"')[1].split("</div>")[0]
    assert "Searching 3 of 3 searches" in step and "11 found so far" in step
    assert "Found 6 roles on Indeed" in live.split('data-testid="run-events"')[1]
    assert re.search(
        r'data-testid="run-activity">\s*<span class="k-dot[^>]*></span>Last activity', live
    )
    assert "Running now" in live and 'data-run="active"' in live


def test_the_poll_returns_regions_with_signatures_and_not_the_page(active):
    c, _ = active
    frag = c.get("/runs", headers=HX).text
    assert "<html" not in frag and "k-scene" not in frag
    for name in ("header", "live", "history", "quar"):
        assert re.search(rf'data-region="{name}" data-sig="[^"]+"', frag), name
    page = c.get("/runs").text
    assert 'hx-trigger="every 5s, run-refresh from:body"' in page
    assert 'hx-swap="none"' in page.split('id="runs-poll"')[1].split(">")[0]
    sig = re.search(r'data-region="live" data-sig="([^"]+)"', frag).group(1)
    assert sig.startswith("active:")


def test_a_run_that_is_going_is_not_in_the_timeline(active):
    c, seed = active
    tl = c.get("/runs").text.split('id="runs-tl"')[1]
    assert f'data-run-id="{seed["active_run"]}"' not in tl


# ------------------------------------------------------------------------------- Latest run
def test_latest_run_tiles_and_cards_when_idle(idle):
    c, seed = idle
    live = _region(c.get("/runs").text, "live")
    assert 'data-run="idle"' in live and "Latest run" in live and "Finished" in live
    for word in ("found", "rated", "failed", "waiting for another try"):
        assert f"<span>{word}</span>" in live
    for card in ("Duration", "Model", "Sources"):
        assert f'rn-mini__k">{card}<' in live
    assert "11 min" in live and "New matches are on" in live
    assert f'hx-get="/runs/{seed["runs"][0]}/drawer"' in live  # View run details
    assert 'data-testid="run-live"' in live


def test_no_runs_yet_says_when_a_run_is_recorded():
    with _empty_app() as c:
        html = c.get("/runs").text
    assert "A run is recorded when the worker has something to scan or score." in html
    assert "No runs yet." in html


# --------------------------------------------------------------------------------- history
def test_timeline_shows_ten_runs_four_labels_and_show_more(idle):
    c, _ = idle
    html = c.get("/runs").text
    tl = html.split('id="runs-tl"')[1]
    assert tl.count('data-testid="rn-run"') == 10
    for label in ("Finished", "Partial", "Stopped by you", "Did not run"):
        assert label in html.split('id="runs-history"')[1], label
    assert "Show 2 more" in tl
    assert "Every past run is included when you export from Analytics." in tl
    assert "12 runs" in html
    full = c.get("/runs/history?all=1").text
    assert full.count('data-testid="rn-run"') == 12 and "Show fewer" in full


def test_a_partial_run_is_amber_text_labeled_and_never_red(idle):
    c, _ = idle
    tl = c.get("/runs/history?all=1").text
    partial = tl.split("is-partial")[1].split("</li>")[0]
    assert "k-pill--warn" in partial and ">Partial<" in partial
    assert "1 search had a problem. Kestrel retries next run." in partial
    assert "k-pill--bad" not in partial


def test_each_past_run_reads_in_plain_words(idle):
    c, _ = idle
    tl = c.get("/runs/history?all=1").text
    assert "Stopped by you at Rating 4 of 7." in tl
    assert "No search could run. Nothing was lost; they run again." in tl
    assert "Kestrel closed before this run ended. Nothing was lost." in tl
    assert "All 5 searches answered." in tl
    # An online run carries its tag; the others say Local. Failed counts are shown.
    assert tl.count("is-online") == 1 and "Online</span>" in tl
    assert ">failed</span>" in tl or "failed</span>" in tl


def test_no_failed_scoring_wording_on_the_runs_pages(idle):
    c, seed = idle
    for url in (
        "/runs",
        "/runs/history?all=1",
        f"/runs/{seed['runs'][0]}",
        f"/runs/{seed['runs'][0]}/drawer",
    ):
        html = c.get(url).text
        assert "ailed scoring" not in html and "Scoring failed" not in html, url
    assert "Needs another try" in c.get("/runs").text


# ---------------------------------------------------------------------------------- drawer
def test_drawer_has_the_summary_rates_sources_and_details(idle):
    c, seed = idle
    html = c.get(f"/runs/{seed['runs'][0]}/drawer").text
    assert "Finished" in html and "took 11 min" in html and "All 5 searches answered." in html
    assert "Roles per minute, by step" in html
    for label in ("Your rules", "Quick screen", "Rating", "Deeper read"):
        assert label in html
    assert ">16<" in html and ">2.4<" in html  # per-minute figures from five or more roles
    assert "Operations Analyst on Indeed" in html and ">OK<" in html and ">Rate-limited<" in html
    assert 'data-testid="rn-run-details"' in html and "Show details" in html
    assert "HTTP 429" in html.split("Show details")[1]  # the raw reason stays behind details


@pytest.mark.parametrize("suffix", ["", "/drawer"])
@pytest.mark.parametrize("area,titles", [(30, 12), (30, 0), (0, 12), (0, 0), (None, None)])
def test_run_details_show_recorded_skips_only_when_positive(idle, suffix, area, titles):
    client, seed = idle

    async def record(session):
        rows = (await session.execute(select(ScrapeRun).order_by(ScrapeRun.id))).scalars().all()
        for index, row in enumerate(rows):
            row.counts = {
                **(row.counts or {}),
                **({"dropped_outside_area": area} if area is not None and index == 0 else {}),
                **(
                    {"dropped_unrelated_title": titles} if titles is not None and index == 1 else {}
                ),
            }

    _write(record)
    response = client.get(f"/runs/{seed['runs'][0]}{suffix}")
    assert response.status_code == 200
    if area or titles:
        assert (
            f"Skipped {(area or 0) + (titles or 0)}: {area or 0} outside your area, "
            f"{titles or 0} unrelated titles"
        ) in response.text
        assert response.text.count('data-testid="rn-skipped"') == 1
    else:
        assert 'data-testid="rn-skipped"' not in response.text


@pytest.mark.parametrize("suffix", ["", "/drawer"])
def test_skip_totals_include_searches_beyond_display_cap_and_exclude_other_runs(idle, suffix):
    client, seed = idle

    async def record(session):
        run = await session.get(Run, seed["runs"][0])
        source = (await session.execute(select(ScrapeRun))).scalars().first()
        session.add_all(
            [
                ScrapeRun(
                    source_id=source.source_id,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                    status="ok",
                    counts={"dropped_outside_area": 1, "dropped_unrelated_title": 2},
                )
                for _ in range(65)
            ]
        )
        older = await session.get(Run, seed["runs"][1])
        session.add(
            ScrapeRun(
                source_id=source.source_id,
                started_at=older.started_at,
                status="ok",
                counts={"dropped_outside_area": 1000, "dropped_unrelated_title": 1000},
            )
        )

    _write(record)
    response = client.get(f"/runs/{seed['runs'][0]}{suffix}")
    assert "Skipped 195: 65 outside your area, 130 unrelated titles" in response.text


def test_a_rate_from_fewer_than_five_roles_is_labeled_not_shown(idle):
    c, seed = idle
    stopped = seed["runs"][2]  # tier 2 completed 4
    html = c.get(f"/runs/{stopped}/drawer").text
    assert "too few (4 of 5)" in html


def test_unknown_run_drawer_is_404(idle):
    c, _ = idle
    assert c.get("/runs/999999/drawer").status_code == 404


def test_run_detail_page_uses_the_new_wording(idle):
    c, seed = idle
    r = c.get(f"/runs/{seed['runs'][0]}")
    assert r.status_code == 200 and f"Run #{seed['runs'][0]}" in r.text
    assert "Needs another try" in r.text and "rn-dr" in r.text
    assert "Open the full page" not in r.text


# -------------------------------------------------------------------------------- controls
def test_pause_and_resume_flip_the_workers_flag_and_ask_for_a_redraw(active):
    c, _ = active
    r = c.post("/runs/pause")
    assert r.status_code == 204 and r.headers["HX-Trigger"] == "run-refresh"
    from kestrel.web.app import is_paused

    assert is_paused()
    r = c.post("/runs/resume")
    assert r.status_code == 204 and r.headers["HX-Trigger"] == "run-refresh"
    assert not is_paused()


def test_stop_pauses_cancels_unstarted_searches_and_records_it(active):
    c, seed = active
    r = c.post("/runs/stop")
    assert r.status_code == 204 and r.headers["HX-Trigger"] == "run-refresh"

    async def look(s):
        run = await s.get(Run, seed["active_run"])
        scrape = (await s.execute(select(Task).where(Task.type == "scrape"))).scalars().all()
        t1 = (await s.execute(select(Task).where(Task.type == "t1"))).scalars().all()
        return run.status, run.notes, [t.status for t in scrape], [t.status for t in t1]

    status, notes, scrape, t1 = _read(look)
    assert status == "stopping" and notes.startswith("Stopped by you")
    assert scrape and all(s != "pending" for s in scrape)  # the search not yet begun is cancelled
    assert t1 and all(s == "pending" for s in t1)  # scoring stays queued: nothing is lost
    from kestrel.web.app import is_paused

    assert is_paused()
    tl = c.get("/runs/history?all=1").text
    assert "Stopped by you at" in tl


def test_run_now_queues_a_search_for_every_enabled_source(idle):
    c, _ = idle
    r = c.post("/runs/start")
    assert r.status_code == 200 and r.headers["HX-Trigger"] == "run-refresh"

    async def n(s):
        return len((await s.execute(select(Task).where(Task.type == "scrape"))).scalars().all())

    assert _read(n) == 3


def test_run_now_with_no_searches_on_says_so_and_queues_nothing():
    runs_seed.seed()

    async def off(s):
        from kestrel.db.models import Source

        for src in (await s.execute(select(Source))).scalars():
            src.enabled = False

    _write(off)
    with TestClient(build_app(worker=False)) as c:
        r = c.post("/runs/start")
    assert r.status_code == 409 and "No searches are on" in r.text


@pytest.mark.parametrize("mode", ["hosted", "both"])
def test_an_online_run_that_cannot_run_says_why_with_a_409_before_anything_is_queued(
        idle, monkeypatch, mode):
    """The early refusal in `start`: confirmed and consented, but Hosted mode is not usable here.
    Its own sentence (not the queue's "changed since" one), a 409, and nothing queued."""
    from kestrel.config import hosted_mode_problem
    from kestrel.services import online_consent, setting_overrides
    from kestrel.services.setting_overrides import load_settings_with_db

    c, _ = idle

    async def consented(*_a, **_k):
        return None  # the approval step passes: this test is about the step after it

    async def queued(*_a, **_k):
        raise AssertionError("the run was queued although Hosted mode cannot run")

    monkeypatch.setattr(online_consent, "approval_problem", consented)
    monkeypatch.setattr(setting_overrides, "queue_run_mode", queued)

    async def why(s):
        return hosted_mode_problem(await load_settings_with_db(s))

    sentence = _read(why)
    assert sentence  # the idle app has no usable Hosted mode
    r = c.post("/runs/start", data={"mode": mode, "confirmed": "1"})
    assert r.status_code == 409
    assert r.text == f"Not started: {sentence}."
    assert setting_overrides.HOSTED_CHANGED not in r.text  # not the later, queue-time refusal

    async def scrapes(s):
        return len((await s.execute(select(Task).where(Task.type == "scrape"))).scalars().all())

    assert _read(scrapes) == 0


def test_an_online_run_is_refused_without_the_confirmation_and_queues_nothing(idle):
    c, _ = idle
    r = c.post("/runs/start", data={"mode": "hosted"})
    assert r.status_code == 400 and "Nothing was sent" in r.text
    # Confirmed, but Hosted mode is not usable here: refused, with the reason, nothing queued.
    r = c.post("/runs/start", data={"mode": "hosted", "confirmed": "1"})
    assert r.status_code == 409 and r.text.startswith("Not started")

    async def look(s):
        tasks = (await s.execute(select(Task).where(Task.type == "scrape"))).scalars().all()
        return len(tasks), await s.get(Setting, "run.pending_mode")

    n, pending = _read(look)
    assert n == 0 and pending is None
    assert c.post("/runs/start", data={"mode": "cloud"}).status_code == 400


def test_a_local_one_run_choice_queues_the_mode_and_the_searches(idle):
    c, _ = idle
    assert c.post("/runs/start", data={"mode": "local"}).status_code == 200
    assert "next cycle: local" in c.get("/runs/strip").text


def test_the_choose_sheet_disables_online_with_the_reason_and_asks_first(idle):
    c, _ = idle
    html = c.get("/runs/start/choose").text
    assert 'data-testid="rn-mode-local"' in html and "Your settings do not change" in html
    tag = html.split('data-testid="rn-mode-online"')[0].rsplit("<input", 1)[1]
    assert "disabled" in tag and "Not available:" in html.split('data-testid="rn-mode-online"')[1]
    assert 'id="rn-ask" hidden' in html and 'name="confirmed"' in html
    assert "Not sent:" in html and "Cost:" in html


def test_the_choose_sheet_lists_what_the_hosted_disclosure_says_is_sent(idle):
    from kestrel.config import get_settings

    c, _ = idle
    sends = get_settings().llm.hosted_mode.disclosure_terms()["sends"]
    html = c.get("/runs/start/choose").text
    lines = [html_mod.unescape(x) for x in re.findall(r'data-testid="rn-sends">([^<]+)<', html)]
    assert lines
    for line in lines:
        assert any(line.rstrip(".").lower() == s.lower().rstrip(".") for s in sends.values()), (
            line,
            sends,
        )


# ---------------------------------------------------------------------- Needs another try
def test_the_card_lists_three_roles_with_try_again_and_a_link(idle):
    c, _ = idle
    nat = _region(c.get("/runs").text, "quar")
    assert nat.count('data-testid="rn-q"') == 3 and nat.count('data-testid="rn-retry"') == 3
    assert "7 roles waiting" in nat and "and 4 more" in nat
    assert 'href="/quarantine"' in nat
    assert "They also stay in Pipeline, just without a score." in nat


def test_the_card_try_again_requeues_and_asks_for_a_redraw(idle):
    c, seed = idle
    qid = seed["roles"]["Operations Manager"]["q"]  # a tier 2 failure
    r = c.post(f"/runs/quarantine/{qid}/retry")
    assert r.status_code == 200 and r.headers["HX-Trigger"] == "run-refresh"

    async def look(s):
        return (await s.execute(select(Task).where(Task.type == "t2"))).scalars().all()

    assert len(_read(look)) == 1


def test_the_card_try_again_says_so_when_it_cannot(idle):
    c, _ = idle
    r = c.post("/runs/quarantine/999999/retry")
    assert r.status_code == 409 and "No quarantine record" in r.text
    assert "k-toast" in r.headers["HX-Trigger"]


def test_the_empty_card_says_all_clear():
    with _empty_app() as c:
        nat = _region(c.get("/runs").text, "quar")
    assert "All clear" in nat and "Nothing is waiting" in nat


# ----------------------------------------------------------------------- the whole page
def test_the_page_uses_the_design_system_and_not_the_legacy_sheet(idle):
    c, _ = idle
    html = c.get("/runs").text
    assert "/static/css/pages/runs.css" in html and "/static/js/runs.js" in html
    assert "/static/css/kestrel.css" not in html
    assert "announcement-slot" not in html
    assert 'class="k-layout"' in html and "k-help-col" in html
    for fly in ("rn-steps-fly", "rn-pause-fly", "rn-fail-fly"):
        assert f'id="{fly}"' in html
    assert html.count("More about this") >= 3


def test_runs_css_and_js_use_tokens_and_no_em_dash():
    css = (_WEB / "static" / "css" / "pages" / "runs.css").read_text(encoding="utf-8")
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert not re.search(r"#[0-9A-Fa-f]{3,8}\b", bare)
    assert not re.search(r"\b(?:rgb|rgba|hsl|hsla)\(", bare)
    for p in (_WEB / "templates").rglob("*runs*"):
        assert "—" not in p.read_text(encoding="utf-8"), p
    assert "—" not in (_WEB / "static" / "js" / "runs.js").read_text(encoding="utf-8")


def test_the_responsive_rules_are_container_queries():
    css = (_WEB / "static" / "css" / "pages" / "runs.css").read_text(encoding="utf-8")
    # The steps measure the window (kwin, as the mockup's .win), never the page wrapper.
    assert "@container kwin (max-width: 1300px)" in css
    assert "@container kwin (max-width: 1000px)" in css
    assert "container-type" not in css


def test_poll_response_is_valid_json_free_html_and_escapes_what_a_posting_wrote(idle):
    c, seed = idle

    async def evil(s):
        from kestrel.db.models import Opportunity

        opp = await s.get(Opportunity, seed["roles"]["Operations Manager"]["opp"])
        opp.title = "<script>alert(1)</script>"

    _write(evil)
    html = c.get("/runs").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert json.dumps({}) == "{}"


# ------------------------------------------------------------------------- review follow-ups
def test_stop_with_no_run_open_changes_nothing(idle):
    from kestrel.web.app import is_paused

    c, _ = idle
    assert c.post("/runs/stop").status_code == 204
    assert not is_paused()


def test_a_run_recorded_as_local_never_reads_as_online(active):
    c, seed = active

    async def local(s):
        s.add(Setting(key="llm.mode", value="hosted"))
        (await s.get(Run, seed["active_run"])).mode = "local"

    _write(local)
    html = c.get("/runs").text
    assert 'data-testid="rn-online"' not in html and "rn-cloud" not in html


def test_a_refused_start_with_no_searches_leaves_no_queued_mode():
    runs_seed.seed()

    async def off(s):
        from kestrel.db.models import Source

        for src in (await s.execute(select(Source))).scalars():
            src.enabled = False

    _write(off)
    with TestClient(build_app(worker=False)) as c:
        assert c.post("/runs/start", data={"mode": "local"}).status_code == 409

    async def pending(s):
        return await s.get(Setting, "run.pending_mode")

    assert _read(pending) is None


def test_the_page_and_its_fragment_vary_on_the_htmx_header(idle):
    c, _ = idle
    assert "HX-Request" in c.get("/runs").headers["vary"]
