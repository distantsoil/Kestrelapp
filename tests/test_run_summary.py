"""Two track tabs with badges (D-UI-57) and the live run summary (D-UI-58): Today's Review card,
the Runs page live panel, the status rail's step words, all derived from DB rows only so a
restart shows the same picture. Offline: no model, no network."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.analytics import run_summary as rs
from kestrel.db import migrate
from kestrel.db.base import iso
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import (
    Evaluation,
    Opportunity,
    Pursuit,
    Quarantine,
    Run,
    ScrapeRun,
    Setting,
    Source,
    Task,
)
from kestrel.pipeline import runlog
from kestrel.services import tracks
from kestrel.web import deps


def _ago(**kw) -> str:
    return iso(datetime.now(UTC) - timedelta(**kw))


def _fresh_engine():
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    return make_async_engine(paths.db_path())


async def _seed(rows: list | None = None) -> None:
    await reset_engine()
    engine = _fresh_engine()
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        for row in rows or []:
            s.add(row)
            await s.flush()
        await s.commit()
    await engine.dispose()
    forget_engine()
    deps.set_ui_scheme_cache(None)


def _summary(track: str | None = None) -> dict:
    """run_summary on a brand new engine: nothing but the DB file carries over (hard rule 5)."""

    async def _go():
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sm() as s:
                return await rs.run_summary(s, track)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


@pytest.fixture(autouse=True)
def _no_ollama(monkeypatch):
    from kestrel.web import routes_control

    async def _fake(native_url: str):
        return {"up": True, "loaded": []}

    monkeypatch.setattr(routes_control, "_probe_ollama", _fake)
    yield
    tracks.forget_cache()


_TIP = ' title="Waiting for you on this track"'


def _both() -> Setting:
    return Setting(key="tracks.enabled", value=["employment", "consulting"])


def _src(name: str, track: str = "employment", term: str | None = "Operations Analyst",
         sites: tuple[str, ...] = ("indeed",)) -> Source:
    config = {"site_name": list(sites)}
    if term:
        config["search_term"] = term
    return Source(track=track, kind="jobspy", name=name, enabled=True, config=config)


def _opp(i: int, *, track: str = "employment", stage: str = "t2_done", src: int = 1,
         run: int | None = None, title: str | None = None) -> Opportunity:
    return Opportunity(
        track=track, opp_type="job_posting", source_id=src, scrape_run_id=run,
        title=title or f"Role {i}", url=f"https://x/rs-{i}", stage=stage,
    )


def _ev(opp_id: int, *, tier: int = 2, verdict: str = "apply", score: float | None = 4.1,
        track: str = "employment", at: str | None = None, review: bool = False) -> Evaluation:
    return Evaluation(
        opportunity_id=opp_id, tier=tier, track=track, verdict=verdict, verdict_model=verdict,
        score=score, weighted_score=score, needs_review=review, created_at=at or _ago(minutes=5),
    )


# ------------------------------------------------------------------- D-UI-57: track tabs
def test_switch_has_two_tabs_no_both_and_a_badge_per_track():
    today = date.today().isoformat()
    asyncio.run(_seed([
        _both(),
        _src("Ops"),
        _opp(1), _opp(2), _opp(3, track="consulting", title="Interim CFO"),
        _ev(1), _ev(2, verdict="consider", score=3.2), _ev(3, track="consulting", verdict="pursue"),
        # One consulting follow-up due today, waiting on a reply.
        Pursuit(opportunity_id=3, track="consulting", status="outreach_sent",
                applied_at=_ago(days=9), next_action_at=today),
    ]))
    with _client() as c:
        html = c.get("/help").text
        switch = html.split('class="track-switch"')[1].split("</div>")[0]
        assert ">Both<" not in switch and "?track=\"" not in switch
        assert ">Employment<" in switch and ">Consulting<" in switch
        assert 'id="nav-count-track-employment"' in switch
        assert 'id="nav-count-track-consulting"' in switch
        counts = c.get("/status/counts").text
    # Employment: one Worth a look (the consider is Maybe). Consulting: its role is an open
    # pursuit at outreach_sent, so it is Applied (#37) and off Worth a look; the follow-up due
    # today is the one thing waiting.
    # R12: the badge keeps its tooltip through the out-of-band swap.
    assert ('<span id="nav-count-track-employment" class="n" hx-swap-oob="true"'
            f'{_TIP}>1</span>') in counts
    assert ('<span id="nav-count-track-consulting" class="n" hx-swap-oob="true"'
            f'{_TIP}>1</span>') in counts


def test_one_track_has_no_switch_and_no_track_badges():
    asyncio.run(_seed([_src("Ops"), _opp(1), _ev(1)]))
    with _client() as c:
        assert 'class="track-switch"' not in c.get("/help").text
        counts = c.get("/status/counts").text
    assert "nav-count-track-" not in counts and 'id="nav-count-shortlist"' in counts


def test_zero_badge_renders_empty_so_it_hides():
    asyncio.run(_seed([_both(), _src("Ops"), _opp(1), _ev(1)]))
    with _client() as c:
        counts = c.get("/status/counts").text
    assert ('<span id="nav-count-track-consulting" class="n" hx-swap-oob="true"'
            f'{_TIP}></span>') in counts


def test_remembered_tab_is_the_track_on_pages_without_a_query():
    asyncio.run(_seed([
        _both(), _src("Ops"),
        _opp(1, title="Employment role"), _opp(2, track="consulting", title="Consulting lead"),
        _ev(1), _ev(2, track="consulting", verdict="pursue"),
    ]))
    with _client() as c:
        c.cookies.set(deps.TRACK_COOKIE, "consulting")
        pipe = c.get("/opportunities").text
        today = c.get("/").text
        cal = c.get("/calibrate")
        board = c.get("/pursuits").text
        c.cookies.set(deps.TRACK_COOKIE, "employment")
        pipe_emp = c.get("/opportunities").text
        # An explicit query still wins over the remembered tab.
        pipe_q = c.get("/opportunities?track=consulting").text
    assert "Consulting lead" in pipe and "Employment role" not in pipe
    assert "Employment role" in pipe_emp and "Consulting lead" not in pipe_emp
    assert "Consulting lead" in pipe_q and "Employment role" not in pipe_q
    # Today reads the remembered tab too (the page carries its track).
    assert 'id="today" data-track="consulting"' in today
    assert cal.status_code == 200
    assert 'data-track="consulting"' in board
    active = today.split('class="track-switch"')[1].split("</div>")[0]
    assert 'class="active">Consulting' in active


def test_a_disabled_track_in_the_cookie_falls_back_to_the_track_in_use():
    asyncio.run(_seed([_src("Ops"), _opp(1, title="Employment role"), _ev(1)]))
    with _client() as c:
        c.cookies.set(deps.TRACK_COOKIE, "consulting")
        assert "Employment role" in c.get("/opportunities").text


# ---------------------------------------------------------------- D-UI-58: run_summary
def _active_rows(*, stall: bool = False) -> list:
    """An open desktop run: one finished search that stored 8 new roles (2 set aside by the rules,
    3 through the quick screen, 2 fully reviewed, 1 failed), one search running, one waiting; a
    consulting search beside it."""
    shift = 30 if stall else 0

    def t(minutes: int = 0) -> str:
        return _ago(minutes=minutes + shift)

    rows: list = [
        _both(),
        _src("Jobs near Austin"),                                          # 1
        _src("Remote jobs", term="Data Analyst", sites=("indeed", "google")),  # 2
        _src("Fractional", track="consulting", term="interim CFO"),         # 3
        _src("Waiting"),                                                   # 4
        Run(name=runlog.APP_RUN_NAME, status="running", started_at=t(minutes=20), counts={}),
        ScrapeRun(source_id=1, status="ok", started_at=t(minutes=19), finished_at=t(minutes=18),
                  counts={"new": 8, "reseen": 1}),                         # 1
        ScrapeRun(source_id=3, status="ok", started_at=t(minutes=17), finished_at=t(minutes=16),
                  counts={"new": 3}),                                      # 2
        ScrapeRun(source_id=2, status="running", started_at=t(minutes=1)),  # 3
        Task(type="scrape", status="leased", payload={"source_id": 2}, updated_at=t()),
        Task(type="scrape", status="pending", payload={"source_id": 4}, updated_at=t()),
    ]
    for i in range(1, 9):
        stage = "t0_reject" if i <= 2 else "t2_done" if i <= 4 else "quarantined" if i == 5 \
            else "t1_reject" if i == 6 else "t1_pass"
        rows.append(_opp(i, run=1, stage=stage, title=f"Analyst {i}"))
    rows += [
        _ev(3, tier=1, verdict="keep", at=t(minutes=15)),
        _ev(4, tier=1, verdict="keep", at=t(minutes=15)),
        _ev(5, tier=1, verdict="keep", at=t(minutes=15)),
        _ev(6, tier=1, verdict="kill", at=t(minutes=15)),
        _ev(3, score=4.6, at=t(minutes=10)),                         # strong fit
        _ev(4, verdict="consider", score=3.1, at=t(minutes=8)),      # maybe
        Quarantine(opportunity_id=5, tier=2, error="generation loop detected",
                   created_at=t(minutes=6)),
        Task(type="t2", status="pending", payload={"opportunity_id": 7}, updated_at=t()),
        Task(type="t1", status="pending", payload={"opportunity_id": 8}, updated_at=t()),
    ]
    return rows


def test_run_summary_from_seeded_rows_names_the_search_and_the_events():
    asyncio.run(_seed(_active_rows()))
    s = _summary("employment")
    assert s["active"] and s["stage"] == "searching"
    assert s["current_search"] == "Data Analyst on Indeed and Google"
    # Employment only: two finished-or-running searches plus the waiting one; consulting's apart.
    assert (s["searches_done"], s["searches_total"]) == (1, 3)
    assert s["found_new"] == 8 and s["set_aside"] == 2
    assert (s["quick_screen_done"], s["quick_screen_kept"], s["quick_screen_total"]) == (4, 3, 5)
    assert s["rated"] == 2 and s["failed"] == 1 and s["to_rate"] == 4
    assert s["groups"] == {"strong": 1, "good": 0, "maybe": 1, "not_fit": 0, "your_call": 0}
    texts = [e["text"] for e in s["recent_events"]]
    assert texts == [
        "Scoring failed for Analyst 5: the model repeated itself",
        "Scored Analyst 4: maybe (3.1)",
        "Scored Analyst 3: strong fit (4.6)",
        "Quick screen kept 3 of 4",
        "Found 8 roles on Indeed for 'Operations Analyst'",
        "Rules set aside 2",
    ]
    assert all("interim CFO" not in t for t in texts)  # the consulting search is the other tab
    assert rs.stage_line(s) == ("Searching 2 of 3 searches: Data Analyst on Indeed and Google · "
                                "8 found so far")
    assert rs.rail_line(s) == "Searching 2 of 3 searches"

    both = _summary(None)
    assert both["found_new"] == 11 and both["searches_total"] == 4
    assert "Found 3 leads on Indeed for 'interim CFO'" in [e["text"] for e in both["recent_events"]]
    cons = _summary("consulting")
    assert cons["found_new"] == 3 and cons["rated"] == 0 and cons["current_search"] is None


def test_run_summary_stage_words_for_quick_screen_and_full_review():
    base = {**rs._empty(), "active": True, "quick_screen_done": 18, "quick_screen_total": 42,
            "rated": 2, "to_rate": 12, "failed": 1}
    assert rs.stage_line({**base, "stage": "quick_screen"}) == "Quick screen 18 of 42"
    assert rs.rail_line({**base, "stage": "quick_screen"}) == "Quick screen 18 of 42"
    # One figure (R1): done is rated plus failed, everywhere it is printed.
    assert rs.stage_line({**base, "stage": "full_review"}) == "Rating 3 of 12 · 1 failed"
    assert rs.rail_line({**base, "stage": "full_review"}) == "Rating 3 of 12"
    assert rs.rail_line({**base, "active": False, "paused": True}) == "Paused"
    assert rs.rail_line({**base, "active": False, "stage": "idle"}) is None


def test_run_summary_survives_a_restart_and_reads_as_the_last_run():
    """Hard rule 5: the summary is rows, not memory. The app dies mid-run; the next start
    closes the run as interrupted and the same numbers read as the last run."""
    asyncio.run(_seed(_active_rows()))
    before = _summary("employment")

    async def _restart():
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await runlog.close_orphaned_runs(sm)
            async with sm() as s:  # the dead process's tasks: the restart cancels nothing,
                from sqlalchemy import update  # but here they are finished for the idle view

                await s.execute(update(Task).values(status="done"))
                await s.commit()
        finally:
            await engine.dispose()

    asyncio.run(_restart())
    after = _summary("employment")
    assert not after["active"] and after["status"] == "interrupted"
    for key in ("found_new", "set_aside", "rated", "failed", "groups", "searches_done"):
        assert after[key] == before[key], key
    # The search that was running when the app died reads as cut off (R11), not running.
    assert after["searches_cut_off"] == 1 and after["current_search"] is None
    texts = [e["text"] for e in after["recent_events"]]
    assert "Search for 'Data Analyst' on Indeed and Google was cut off" in texts
    assert before["recent_events"][0]["text"] in texts


def test_run_summary_empty_install():
    asyncio.run(_seed())
    s = _summary("employment")
    assert not s["has_run"] and not s["active"] and s["recent_events"] == []


def test_idle_last_run_is_the_newest_that_touched_the_track():
    asyncio.run(_seed([
        _both(), _src("Ops"), _src("Cons", track="consulting"),
        Run(name=runlog.APP_RUN_NAME, status="done", started_at=_ago(hours=5),
            finished_at=_ago(hours=4)),
        ScrapeRun(source_id=1, status="ok", started_at=_ago(hours=5), finished_at=_ago(hours=4),
                  counts={"new": 4}),
        Run(name=runlog.APP_RUN_NAME, status="done", started_at=_ago(hours=2),
            finished_at=_ago(hours=1)),
        ScrapeRun(source_id=2, status="ok", started_at=_ago(hours=2), finished_at=_ago(hours=1),
                  counts={"new": 7}),
    ]))
    emp, cons = _summary("employment"), _summary("consulting")
    assert emp["run_id"] == 1 and emp["found_new"] == 4
    assert cons["run_id"] == 2 and cons["found_new"] == 7


# ------------------------------------------------------------------- Today's run panel
def _panel(html: str) -> str:
    return html.split('data-testid="today-run"')[1].split('data-testid="today-review"')[0]


def test_today_run_panel_while_searching_and_the_rail_agree():
    """R1: one progress figure. The panel's headline and the rail's worker line are the same
    string from the same global run summary."""
    asyncio.run(_seed(_active_rows()))
    with _client() as c:
        c.cookies.set(deps.TRACK_COOKIE, "employment")
        # The run panel is the /today/run partial (Today prints the status strip instead,
        # D-UI-156); the strip's headline is the same string.
        html = c.get("/today/run?track=employment").text
        strip = c.get("/today/strip").text
        part = c.get("/today/run?track=employment").text
        rail = c.get("/status/rail").text
    panel = _panel(html)
    assert 'data-run="active"' in panel
    head = panel.split('data-testid="run-headline">')[1].split("<")[0]
    assert head == "Searching 3 of 4 searches"  # global: both tracks' searches
    assert f">{head}<" in rail
    assert f'data-testid="run-headline">{head}<' in strip  # the strip says the same words
    assert 'class="run-prog breathe"' in panel and "last activity " in panel
    assert 'data-testid="run-current">Data Analyst on Indeed and Google<' in panel
    assert 'hx-trigger="every 5s, run-refresh from:body"' in part
    assert 'hx-get="/today/run?track=employment"' in part
    # The poll brings the Review card and the Schedule bar along out of band.
    assert 'id="today-review"' in part and 'hx-swap-oob="true"' in part
    assert f'data-tip="{head}"' in part
    # The panel sits above the cards, full width.
    assert html.index('data-testid="today-run"') < html.index('data-testid="today-review"')


def test_today_run_panel_while_rating_shows_the_segments_and_matches_the_rail():
    rows = [r for r in _active_rows() if not (isinstance(r, Task) and r.type in ("scrape", "t1"))]
    rows = [r for r in rows if not (isinstance(r, ScrapeRun) and r.status == "running")]
    rows.append(Task(type="t2", status="leased", payload={"opportunity_id": 8},
                     updated_at=_ago()))
    asyncio.run(_seed(rows))
    with _client() as c:
        panel = _panel(c.get("/today/run?track=employment").text)
        strip = c.get("/today/strip?track=employment").text
        rail = c.get("/status/rail").text
    head = panel.split('data-testid="run-headline">')[1].split("<")[0]
    assert head.startswith("Rating ") and f">{head}<" in rail
    # The strip's live counts: "Rating new roles", with rated, failed and waiting in place.
    counts = strip.split('data-testid="found-counts">')[1].split("</span>")[0]
    assert "Rating new roles" in strip and "2 rated" in counts and "1 failed" in counts
    legend = panel.split('data-testid="run-legend"')[1].split("</p>")[0]
    assert "2 scored" in legend and "1 failed" in legend and "1 rating now" in legend
    assert 'class="s-good"' in panel and 'class="s-bad"' in panel


def test_today_run_panel_when_finished_reads_two_lines_and_review_counts_agree():
    asyncio.run(_seed([
        _src("Ops"),
        Source(track="employment", kind="sec_edgar", name="EDGAR", enabled=True,
               config={"queries": ['"x"']}),
        Run(name=runlog.APP_RUN_NAME, status="done", started_at=_ago(hours=2),
            finished_at=_ago(hours=1)),
        ScrapeRun(source_id=1, status="ok", started_at=_ago(hours=2), finished_at=_ago(hours=1),
                  counts={"new": 20}),
        # R4 and R5: EDGAR answered 500 (partial, amber) and promoted one signal.
        ScrapeRun(source_id=2, status="partial", started_at=_ago(hours=2),
                  finished_at=_ago(hours=1), error="AdapterError: HTTP 500 from efts",
                  counts={"promoted": 1, "postings": 0}),
        *[_opp(i, run=1) for i in range(1, 4)],
        _opp(4, run=1, stage="t0_reject"), _opp(5, run=1, stage="t0_reject"),
        _opp(6, run=1, stage="t0_reject"),
        _ev(1, score=4.8, at=_ago(minutes=90)), _ev(2, at=_ago(minutes=89)),
        _ev(3, verdict="pass", score=1.5, at=_ago(minutes=88)),
    ]))

    async def reasons():
        from sqlalchemy import update

        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            for oid, why in ((4, "blocker:geo: Dallas"), (5, "blocker:geo: Reno"),
                             (6, "blocker:anti_title: Intern")):
                await s.execute(update(Opportunity).where(Opportunity.id == oid)
                                .values(stage_reason=why))
            await s.commit()
        await engine.dispose()

    asyncio.run(reasons())
    with _client() as c:
        html = c.get("/today/run").text
    panel = _panel(html)
    review = html.split('data-testid="today-review"')[1].split('data-testid="today-act"')[0]
    assert 'data-run="idle"' in panel and ">Last run<" in panel
    status = panel.split('data-testid="run-status"')[1].split("</span>")[0]
    assert "finished, 1 source had a problem" in status and "pill warn" in panel
    assert "SEC filings returned a server error. It is retried next run." in panel
    line1 = panel.split('data-testid="run-line1"')[1].split("</p>")[0]
    assert "<b>21</b> new roles found, <b>3</b> rated." in line1
    n_review = review.split('data-testid="review-n">')[1].split("<")[0]
    assert f"(now {n_review} in Review)" in line1  # the Review card's own number
    assert 'run-good">2</b> to Worth a look' in line1 and "1 to Not a fit" in line1
    line2 = panel.split('data-testid="run-line2"')[1].split("</p>")[0]
    assert "3 set aside by your rules (location 2, title 1)" in line2
    assert ">Review rules<" in line2
    assert 'hx-trigger="every 30s, run-refresh from:body"' in panel
    # Same font size for both lines: one class, no inline sizes.
    assert 'class="run-line"' in panel and 'class="quiet run-line"' in panel


def test_today_run_panel_names_the_track_with_both_and_an_untouched_track_says_so():
    """R1: only the additions are per track, and with both tracks the clause names it. R10: a
    track that never ran says so instead of borrowing the other track's run."""
    asyncio.run(_seed([
        _both(), _src("Ops"),
        Run(name=runlog.APP_RUN_NAME, status="done", started_at=_ago(hours=2),
            finished_at=_ago(hours=1)),
        ScrapeRun(source_id=1, status="ok", started_at=_ago(hours=2), finished_at=_ago(hours=1),
                  counts={"new": 2}),
        _opp(1, run=1), _ev(1, score=4.8, at=_ago(minutes=90)),
    ]))
    with _client() as c:
        emp = _panel(c.get("/today/run?track=employment").text)
        cons = _panel(c.get("/today/run?track=consulting").text)
    assert "to Employment&#39;s Worth a look" in emp or "to Employment's Worth a look" in emp
    assert 'data-testid="run-no-track">No runs yet on this track.<' in cons
    assert _summary("consulting")["has_run"] is False


def test_today_run_panel_with_no_run_yet():
    asyncio.run(_seed())
    with _client() as c:
        panel = _panel(c.get("/today/run").text)
        strip = c.get("/today/strip").text
    assert 'data-run="none"' in panel and ">No runs yet<" in panel
    assert 'data-state="none"' in strip and ">No runs yet<" in strip


def test_paused_worker_reads_paused_with_no_stall_and_a_slow_poll(monkeypatch):
    """R3: paused is not active. The panel and the rail say Paused, nothing pulses, no stall."""
    from kestrel.web import app as app_mod

    asyncio.run(_seed(_active_rows(stall=True)))
    monkeypatch.setattr(app_mod, "_paused", type("E", (), {"is_set": lambda self: True})())
    s = _summary(None)
    assert s["paused"] and not s["active"] and s["stage"] == "paused"
    assert not rs.view(s)["stalled"]
    with _client() as c:
        panel = _panel(c.get("/today/run").text)
        strip = c.get("/today/strip").text
        rail = c.get("/status/rail").text
    # A search is still under its lease: it finishes, nothing new starts (review finding 2).
    assert s["finishing"] == 1 and rs.step_words(s) == "Pausing: finishing 1"
    assert 'data-run="paused"' in panel and ">Pausing: finishing 1<" in panel
    assert "breathe" not in panel and 'data-testid="run-resume"' in panel
    assert "tray" not in panel  # finding 14: worded without assuming a tray
    # The strip says Paused too, offers Resume, and polls slowly (nothing is moving).
    assert ">Pausing: finishing 1<" in strip and 'data-testid="run-resume"' in strip
    assert 'hx-trigger="every 30s' in strip and "breathe" not in strip
    assert "Pausing: finishing 1" in rail


def test_a_days_old_pending_task_does_not_stretch_this_run():
    """R3: the window never starts before the newest run start or the latest resume."""
    asyncio.run(_seed([
        _src("Ops"),
        Run(name=runlog.APP_RUN_NAME, status="done", started_at=_ago(days=3),
            finished_at=_ago(days=3)),
        ScrapeRun(source_id=1, status="ok", started_at=_ago(days=3), finished_at=_ago(days=3),
                  counts={"new": 40}),
        Run(name=runlog.APP_RUN_NAME, status="done", started_at=_ago(hours=1),
            finished_at=_ago(minutes=50)),
        Task(type="t2", status="pending", payload={"opportunity_id": 1},
             created_at=_ago(days=4), updated_at=_ago(days=4)),
        _opp(1, stage="t1_pass"),
        Setting(key=rs.RESUMED_KEY, value=_ago(minutes=5)),
    ]))
    s = _summary(None)
    assert s["active"] and s["found_new"] == 0
    assert s["started_at"] >= _ago(minutes=6)


def test_switched_off_track_is_held_and_its_pursuits_stay_visible():
    """R2: a switched-off track does not scan or score (its tasks are not claimable and not
    live work); its open pursuits still show on Today (Applications needing action) with the
    track's name."""
    from kestrel.queue.queue import claimable

    asyncio.run(_seed([
        Setting(key="tracks.enabled", value=["employment"]),
        _src("Cons", track="consulting"),
        _opp(1, track="consulting", stage="t1_pass"),
        Task(type="t2", status="pending", payload={"opportunity_id": 1}),
        Task(type="scrape", status="pending", payload={"source_id": 1}),
        Pursuit(opportunity_id=1, track="consulting", status="researching"),
    ]))
    tracks._cache["value"] = ("employment",)

    async def go():
        from sqlalchemy import func, select

        from kestrel.db.base import utcnow_iso

        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            n = (await s.execute(select(func.count()).select_from(Task)
                                 .where(claimable(utcnow_iso())))).scalar_one()
        await engine.dispose()
        return n

    assert asyncio.run(go()) == 0
    assert not _summary(None)["active"]
    with _client() as c:
        html = c.get("/").text
    act = html.split('data-testid="td-apps"')[1].split("</section>")[0]
    off = act.split('data-testid="act-off-track"')[1].split("</p>")[0]
    assert "Consulting (switched off): 1 to act on" in off
    assert 'href="/pursuits?track=consulting"' in off
    with _client() as c:
        apps = c.get("/pursuits").text
    banner = apps.split('data-testid="off-track-pursuits"')[1].split("</p>")[0]
    assert "Consulting is switched off" in banner and "1 open lead is still there" in banner
    assert 'href="/pursuits?track=consulting"' in banner
    tracks._cache["value"] = None


def test_tier3_failure_of_a_scored_role_is_not_a_failure_and_done_never_exceeds_total():
    """R6: a failed Tier 3 read of a role with a Tier 2 score leaves the score standing, and
    rated plus failed can never exceed the total ("11 of 10")."""
    rows = _active_rows()
    rows.append(Quarantine(opportunity_id=3, tier=3, error="schema", created_at=_ago(minutes=2)))
    asyncio.run(_seed(rows))
    s = _summary("employment")
    assert s["failed"] == 1 and s["rated"] == 2
    assert s["rated"] + s["failed"] <= s["to_rate"]
    assert all("Analyst 3: " not in e["text"] or "Scored" in e["text"]
               for e in s["recent_events"])


def test_confidential_mode_redacts_search_names_in_the_run(monkeypatch):
    """R9: a search named after the employer is redacted in the current search and events."""
    from kestrel.privacy import Employer, EmployerMap
    from kestrel.web import redact

    rows = [r for r in _active_rows() if not isinstance(r, Source)]
    rows[1:1] = [
        _src("Jobs near Austin"),
        _src("Northwind Freight careers", term=None, sites=("indeed", "google")),
        _src("Fractional", track="consulting", term="interim CFO"),
        _src("Waiting"),
    ]
    asyncio.run(_seed(rows))

    async def conf(_s):
        return True, EmployerMap([Employer(name="Northwind Freight", shown_as="[Prior employer]")])

    monkeypatch.setattr(redact, "confidential", conf)
    s = _summary("employment")
    assert s["current_search"] == "[Prior employer] careers on Indeed and Google"
    assert all("Northwind" not in e["text"] for e in s["recent_events"])


# ---------------------------------------------------------------------- Runs page and rail
def test_runs_live_panel_shows_the_step_bars_and_events():
    asyncio.run(_seed(_active_rows()))
    with _client() as c:
        html = c.get("/runs", headers={"HX-Request": "true"}).text
    panel = html.split('data-testid="run-live"')[1]
    step = panel.split('data-testid="run-step"')[1].split("</div>")[0]
    assert "Searching 3 of 4 searches: Data Analyst on Indeed and Google · 11 found so far" in step
    assert 'data-bar="search"' in panel and 'data-bar="quick"' in panel
    assert 'data-bar="rating"' in panel and 'data-bar="deeper"' in panel
    assert "Only when you ask" in panel  # the fifth stage (Deeper read) always reads this
    events = panel.split('data-testid="run-events"')[1].split("</ul>")[0]
    assert "Scoring failed for Analyst 5: the model repeated itself" in events
    assert "Found 3 leads on Indeed for &#39;interim CFO&#39;" in events
    # The newest row was written at seed time, so the words are "N seconds ago" for the time the
    # seed, app start and request took: "0 seconds ago" on a fast machine, "1 second ago"
    # (singular, correctly) on a slow Windows runner. Assert the exact words for whatever N is.
    activity = re.search(
        r'data-testid="run-activity">\s*<span class="k-dot[^>]*></span>Last activity (\d+) '
        r'(seconds?) ago\s*<', panel)
    assert activity is not None, panel
    secs = int(activity.group(1))
    assert secs < 60 and activity.group(2) == ("second" if secs == 1 else "seconds")
    assert 'data-testid="run-stall"' not in panel
    with _client() as c:  # the poll that patches the card in place lives on the page
        assert 'hx-trigger="every 5s, run-refresh from:body"' in c.get("/runs").text


def test_ago_words_says_one_second_in_the_singular():
    assert rs.ago_words(0) == "0 seconds ago"
    assert rs.ago_words(1) == "1 second ago"
    assert rs.ago_words(59) == "59 seconds ago"
    assert rs.ago_words(60) == "1 minute ago"


def test_runs_live_panel_warns_after_ten_minutes_without_activity():
    asyncio.run(_seed(_active_rows(stall=True)))
    with _client() as c:
        html = c.get("/runs").text
    panel = html.split('data-testid="run-live"')[1]
    warn = panel.split('data-testid="run-stall"')[1].split("</div>")[0]
    assert "No activity for 31 minutes" in warn or "No activity for 30 minutes" in warn
    assert 'href="/about#diagnostics"' in warn


def test_rail_uses_the_current_step():
    asyncio.run(_seed(_active_rows()))
    with _client() as c:
        rail = c.get("/status/rail").text
    assert "Searching 3 of 4 searches" in rail

    def _without(*types: str) -> list:  # fresh rows each time: a seeded row is not re-added
        rows = [r for r in _active_rows() if not (isinstance(r, Task) and r.type in types)]
        return [r for r in rows if not (isinstance(r, ScrapeRun) and r.status == "running")]

    asyncio.run(_seed(_without("scrape")))
    with _client() as c:
        rail = c.get("/status/rail").text
    assert "Quick screen 4 of 5" in rail

    asyncio.run(_seed(_without("scrape", "t1")))
    with _client() as c:
        rail = c.get("/status/rail").text
    assert "Rating 3 of 4" in rail


def test_every_run_summary_class_has_a_css_rule():
    import re
    from pathlib import Path

    tdir = Path(paths.web_templates_dir()) / "partials"
    css = (Path(paths.web_static_dir()) / "css" / "kestrel.css").read_text("utf-8")
    shell_css = (Path(paths.web_static_dir()) / "css" / "components.css").read_text("utf-8")
    block = css.split("/* --- run summary (D-UI-58) --- */")[1]
    classes: set[str] = set()
    for name in ("today_run.html", "run_live_panel.html"):
        for attr in re.findall(r'class="([^"]+)"', (tdir / name).read_text("utf-8")):
            classes |= {c for c in attr.split() if c.startswith("run-") and "{" not in c}
    assert classes and all(f".{c}" in block for c in classes), classes
    assert ".track-switch a .n" in shell_css  # the shell's track switch (components.css)
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", block)  # theme variables only


# ---------------------------------------------------------------- nav badge and ring (D-UI-61)
def test_pipeline_badge_counts_roles_not_yet_rated_and_the_ring_shows_only_while_active():
    """The Pipeline badge is the active tab's Being scored count, the same rows as the
    Pipeline's own group header; the ring shows beside it (and on the group) only while a run
    is active. Reduced motion stops the spin (CSS)."""
    asyncio.run(_seed(_active_rows()))
    with _client() as c:
        c.cookies.set(deps.TRACK_COOKIE, "employment")
        counts = c.get("/status/counts").text
        pipe = c.get("/opportunities").text
        page = c.get("/help").text
    being = pipe.split("Being scored · ")[1].split("<")[0].strip()
    assert (f'<span id="nav-count-unrated" class="n" hx-swap-oob="true" '
            f'title="Roles not yet rated">{being}</span>') in counts
    assert '<span id="nav-ring" class="ring" hx-swap-oob="true" title="A run is active">' in counts
    assert 'data-testid="being-scored-ring"' in pipe
    side = page.split('<aside class="side"')[1].split("</aside>")[0]
    assert 'id="nav-ring"' in side and 'id="nav-count-unrated"' in side
    assert 'id="nav-count-needs_review"' not in side

    asyncio.run(_seed([_src("Ops"), _opp(1, stage="t1_pass")]))
    with _client() as c:
        counts = c.get("/status/counts").text
        pipe = c.get("/opportunities").text
    assert 'title="A run is active" hidden></span>' in counts
    assert 'data-testid="being-scored-ring"' not in pipe
    from pathlib import Path

    css = (Path(paths.web_static_dir()) / "css" / "kestrel.css").read_text("utf-8")
    motion = css.split("/* The nav ring beside the Pipeline badge")[1].split("/* About and Help")[0]
    assert "prefers-reduced-motion: reduce" in motion and "animation: none" in motion


def test_a_prompt_quarantine_beyond_tier_3_is_not_a_scoring_failure():
    """Quarantine tiers 8, 9, 13, 14 and 15 name prompts (interview questions, titles and
    others), not scoring tiers: a role without a Tier 2 score whose question list failed must not
    show as "Scoring failed" (Wave C review)."""
    asyncio.run(_seed(_active_rows()))
    base = _summary("employment")["failed"]
    rows = _active_rows()
    for opp_id, tier in ((7, 14), (8, 8), (7, 13)):  # roles with no Tier 2 score
        rows.append(Quarantine(opportunity_id=opp_id, tier=tier, error="schema",
                               created_at=_ago(minutes=2)))
    asyncio.run(_seed(rows))
    assert _summary("employment")["failed"] == base


# ------------------------------------------------------------ #92: work in flight on the card
def test_in_flight_line_says_retries_and_how_long_the_run_has_gone():
    base = {**rs._empty(), "active": True, "stage": "full_review", "quick_screen_done": 18,
            "quick_screen_total": 42, "rated": 2, "to_rate": 12, "failed": 1}
    assert rs.stage_line(base) == "Rating 3 of 12 · 1 failed"  # the chip and rail stay as drawn
    assert rs.stage_line({**base, "retrying": 2}, elapsed=125) == (
        "Rating 3 of 12 · 1 failed · 2 retrying · 2 min so far")
    assert rs.stage_line({**base, "retrying": 0}, elapsed=20) == (
        "Rating 3 of 12 · 1 failed · just started")
    assert rs.stage_line({**base, "retrying": 1}, elapsed=3900).endswith("1 hr 5 min so far")
    # Paused or idle: no elapsed clock and no retry count.
    assert rs.stage_line({**base, "active": False, "paused": True, "stage": "paused"},
                         elapsed=125) == "Paused"
    assert rs.stage_line({**rs._empty(), "stage": "idle"}, elapsed=125) == ""
    v = rs.view({**base, "retrying": 1, "started_at": _ago(minutes=7)})
    assert v["step"] == "Rating 3 of 12 · 1 failed"
    assert v["step_detail"] == "Rating 3 of 12 · 1 failed · 1 retrying · 7 min so far"


def test_run_summary_counts_the_tasks_on_another_try():
    rows = [r for r in _active_rows() if not (isinstance(r, Task) and r.type in ("scrape", "t1"))]
    rows += [
        # failed once and back in the queue: a retry waiting
        Task(type="t2", status="pending", payload={"opportunity_id": 1}, attempts=1,
             last_error="the model repeated itself", updated_at=_ago()),
        # on its second lease: a retry running
        Task(type="t2", status="leased", payload={"opportunity_id": 2}, attempts=2,
             lease_until=iso(datetime.now(UTC) + timedelta(minutes=5)), updated_at=_ago()),
        # a task given back because the key is out of credit is waiting, not retrying
        Task(type="t2", status="pending", payload={"opportunity_id": 4, "deferred": 1},
             attempts=1, last_error="out of credit", updated_at=_ago()),
        # a first lease and a fresh pending task are not retries
        Task(type="t2", status="leased", payload={"opportunity_id": 3}, attempts=1,
             lease_until=iso(datetime.now(UTC) + timedelta(minutes=5)), updated_at=_ago()),
    ]
    asyncio.run(_seed(rows))
    s = _summary("employment")
    assert s["active"] and s["retrying"] == 2
    asyncio.run(_seed([r for r in rows if not (isinstance(r, Task) and r.attempts)]))
    assert _summary("employment")["retrying"] == 0


# ----------------------------------------------------------- #91: Stopping comes from the rows
def _live() -> str:
    """A lease still running when the test reads it. A function, not a module constant: a value
    taken at collection had expired by the time a slow suite (41 min) reached these tests."""
    return iso(datetime.now(UTC) + timedelta(minutes=5))


def _stopping_rows(*, run_status: str = "stopping", lease_until: str | None = None,
                   asked: bool | int = True, task_status: str = "leased") -> list:
    """The newest run, a task under a lease, and (when `asked`) the Stop press kept for run 1
    (`asked=2` keeps it for another run)."""
    rows: list = [
        _src("Ops"),
        Run(name=runlog.APP_RUN_NAME, status=run_status, started_at=_ago(minutes=20),
            finished_at=None if run_status == "running" else _ago(minutes=1), counts={}),
        Task(type="t2", status=task_status, payload={"opportunity_id": 1}, attempts=1,
             lease_until=lease_until, updated_at=_ago()),
    ]
    if asked:
        rows.append(Setting(key=rs.STOP_KEY, value={"run_id": 1 if asked is True else 99}))
    return rows


def _runs_card(rows: list, *, paused: bool = True) -> str:
    from kestrel.web.app import set_paused

    asyncio.run(_seed(rows))
    set_paused(paused)
    try:
        with _client() as c:
            return c.get("/runs").text
    finally:
        set_paused(False)


def test_the_card_says_stopping_for_a_requested_stop_with_a_live_lease():
    html = _runs_card(_stopping_rows(lease_until=_live()))
    assert 'data-run="stopping"' in html and "finishing 1 current step" in html
    assert 'data-testid="run-stopping"' in html


@pytest.mark.parametrize(
    ("what", "kwargs", "paused"),
    [
        ("a lease with no expiry", {"lease_until": None}, True),
        # A quit writes "stopping" with no request behind it.
        ("a quit-written stopping row", {"live": True, "asked": False}, True),
        ("a request for another run", {"live": True, "asked": 2}, True),
        # The stop write failed: the run is still running.
        ("a stop whose write failed",
         {"run_status": "running", "asked": False, "live": True}, True),
        # A restart: the worker is not held, whatever the rows say.
        ("a restarted worker", {"live": True}, False),
    ],
)
def test_the_card_never_says_stopping_without_a_requested_stop_on_a_held_worker(
    what, kwargs, paused
):
    kwargs = dict(kwargs)
    if kwargs.pop("live", False):
        kwargs["lease_until"] = _live()  # built when the test runs, never at collection
    rows = _stopping_rows(**kwargs)
    html = _runs_card(rows, paused=paused)
    assert 'data-run="stopping"' not in html, what


def test_stop_then_resume_then_pause_shows_paused_with_resume_not_stopping():
    """The worker carries the stopped run, so its row stays "stopping" unless Resume reopens it."""
    from types import SimpleNamespace

    from kestrel.web import app as web_app

    rows = [r for r in _active_rows() if not (isinstance(r, Task) and r.type == "scrape")]
    rows.append(Task(type="t2", status="leased", payload={"opportunity_id": 8},
                     lease_until=_live(), updated_at=_ago()))
    asyncio.run(_seed(rows))
    try:
        with _client() as c:
            assert c.post("/runs/stop").status_code == 204
            assert 'data-run="stopping"' in c.get("/runs").text
            web_app._held_cycle["cycle"] = SimpleNamespace(run_id=1)  # the loop carries it
            assert c.post("/runs/resume").status_code == 204
            assert c.post("/runs/pause").status_code == 204
            html = c.get("/runs").text
            assert 'data-run="paused"' in html and 'data-testid="run-resume"' in html
            assert 'data-run="stopping"' not in html
    finally:
        web_app._held_cycle["cycle"] = None
        web_app.set_paused(False)


def test_a_restart_forgets_the_stop_request():
    asyncio.run(_seed(_stopping_rows(lease_until=_live())))

    async def go():
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await runlog.close_orphaned_runs(sm)
            async with sm() as s:
                return await s.get(Setting, rs.STOP_KEY)
        finally:
            await engine.dispose()

    assert asyncio.run(go()) is None
