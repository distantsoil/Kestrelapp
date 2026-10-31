"""Sources page, UI overhaul (D-UI-102, SPEC-INTERFACES 2.1 /sources). Offline: every HTTP call is
mocked with respx; no model, no live endpoint. One test per mockup state: the grouped rows, health
and the rate limit, the drawer, the Add sheet and its checks, suggestions, Check all now."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from html import unescape
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
from kestrel.db.models import Opportunity, Org, Pursuit, ScrapeRun, Source, Task
from kestrel.services import tracks
from kestrel.web import source_view as sv

FIXTURES = Path(__file__).parent / "fixtures"
HX = {"HX-Request": "true"}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    from kestrel.web import routes_control

    async def _down(*a, **k):
        return {"up": False, "loaded": []}

    monkeypatch.setattr(routes_control, "_probe_ollama", _down, raising=False)
    tracks.forget_cache()
    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()
    yield
    tracks.forget_cache()
    asyncio.run(reset_engine())


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _run(coro):
    async def _wrap():
        try:
            return await coro
        finally:
            await reset_engine()

    return asyncio.run(_wrap())


def _both():
    async def _go():
        async with get_sessionmaker()() as s:
            await tracks.save_enabled(s, tracks.BOTH)
            await s.commit()

    _run(_go())


def _iso(**ago) -> str:
    return (datetime.now(UTC) - timedelta(**ago)).isoformat()


def _add(**kw) -> int:
    """One source row (and optional runs: a list of (status, error, counts)). Returns its id."""
    runs = kw.pop("runs", [])
    row = dict(track="employment", kind="greenhouse", name="Northwind Health",
               config={"token": "northwind"}, enabled=True, origin="custom")
    if runs:
        row["last_run_at"] = _iso(hours=1)
    row.update(kw)

    async def _go():
        async with get_sessionmaker()() as s:
            src = Source(**row)
            s.add(src)
            await s.flush()
            for i, (status, error, counts) in enumerate(runs):
                s.add(ScrapeRun(source_id=src.id, status=status, error=error, counts=counts,
                                started_at=_iso(hours=len(runs) - i)))
            await s.commit()
            return src.id

    return _run(_go())


async def _rows() -> dict[str, Source]:
    async with get_sessionmaker()() as s:
        return {r.name: r for r in (await s.execute(select(Source))).scalars().all()}


def rows() -> dict[str, Source]:
    return _run(_rows())


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _row_html(html: str, sid: int) -> str:
    return html.split(f'id="src-row-{sid}"')[1].split('data-testid="source-details"')[0]


OK = ("ok", None, {"fetched": 14, "new": 1})
RATE = ("partial", "AdapterError: jobspy scrape failed: HTTP 429 Too Many Requests", {"fetched": 3})
OK_BOARDS = ("ok", None, {"fetched": 14, "new": 1, "by_site": {"indeed": 9, "glassdoor": 5}})
MOVED = ("failed", "AdapterError: HTTP 404 Not Found", {})


# ---------------------------------------------------------------------------- health words
def _src(**kw) -> Source:
    base = dict(id=1, track="employment", kind="greenhouse", name="X", config={}, enabled=True,
                cadence_hours=24, consecutive_failures=0, origin="custom",
                last_run_at=_iso(hours=2))
    base.update(kw)
    return Source(**base)


def _run_row(status: str, error: str | None = None, counts: dict | None = None) -> ScrapeRun:
    return ScrapeRun(source_id=1, status=status, error=error, counts=counts or {},
                     started_at=_iso(hours=1))


def test_a_rate_limit_is_slowed_down_never_needs_you():
    v = sv.build_view(_src(), [_run_row(*RATE)])
    assert (v.health, v.health_label, v.tone) == ("w", "Slowed down", "warn")
    assert v.reason == "Site asked us to slow down. Retrying later."
    assert "429" in v.raw  # the raw status is kept for Show details
    assert sv.run_char(_run_row(*RATE)) == "w"  # amber in the sparkline, never red


def test_a_moved_page_needs_you_and_says_what_happened():
    v = sv.build_view(_src(consecutive_failures=2), [_run_row(*MOVED)])
    assert (v.health, v.tone) == ("b", "bad")
    assert "could not find it at the saved address" in v.reason
    assert sv.run_char(_run_row(*MOVED)) == "b"


def test_healthy_paused_and_not_yet_checked():
    assert sv.build_view(_src(), [_run_row(*OK)]).health == "g"
    paused = sv.build_view(_src(enabled=False), [_run_row(*RATE)])
    assert paused.health == "m" and paused.reason == ""  # off: its history does not shout
    assert sv.build_view(_src(last_run_at=None), []).health == "n"


def test_ten_checks_oldest_first_padded_and_counts_come_from_the_last_finished_run():
    runs = [_run_row("running"), _run_row(*OK), _run_row(*RATE)]  # newest first
    v = sv.build_view(_src(), runs)
    assert v.spark == "m" * 8 + "wg"
    assert (v.open_n, v.new_n) == (14, 1)


def test_tiles_count_only_sources_that_are_on():
    views = [sv.build_view(_src(id=i, enabled=on), [_run_row(*r)])
             for i, (on, r) in enumerate([(True, OK), (True, RATE), (False, MOVED), (True, MOVED)])]
    assert sv.health_counts(views) == {"g": 1, "w": 1, "b": 1}


def test_origin_tags_and_groups():
    for origin, key in (("shipped", "built"), ("generated", "profile"), ("discovered", "found"),
                        ("suggested", "sug"), ("custom", "you")):
        assert sv.origin_of(_src(origin=origin)) == key
    assert sv.group_of(_src(kind="jobspy")) == "boards"
    assert sv.group_of(_src(kind="lever")) == "ats"
    assert sv.group_of(_src(kind="sam_gov", track="consulting")) == "gov"
    assert sv.group_of(_src(kind="sec_edgar", track="consulting")) == "sig"
    assert sv.group_of(_src(kind="rss", track="consulting")) == "sig"
    assert sv.group_of(_src(kind="rss", track="employment")) == "feeds"


# ------------------------------------------------------------------------------ the page
def test_rows_are_grouped_with_the_platform_shown_per_company():
    _add(name="Northwind Health", kind="greenhouse", runs=[OK])
    _add(name="Larkspur Logistics", kind="lever", config={"company": "larkspur"},
         origin="discovered",
         runs=[OK])
    _add(name="Operations roles", kind="jobspy", config={"search_term": "Operations Analyst",
                                                         "site_name": ["indeed", "glassdoor"]},
         origin="generated", runs=[OK_BOARDS])
    _add(name="Meadowbrook Foods", kind="smartrecruiters", config={"company": "m"}, enabled=False)
    with _client() as c:
        html = c.get("/sources").text
    for title in ("Job board searches", "Company career pages", "Feeds", "Paused"):
        assert title in html
    assert "Public bids" not in html and "Company signals" not in html  # consulting only
    ats = html.split('data-testid="group-ats"')[1].split('data-testid="group-feeds"')[0]
    assert "Greenhouse" in ats and "Lever" in ats  # one Greenhouse row never hides which company
    assert "Northwind Health" in ats and "Larkspur Logistics" in ats
    # A row with an issue shows the reason where the platform goes (the mockup's issue rows).
    moved = _add(name="Pinecrest Labs", kind="lever", config={"company": "pine"}, runs=[MOVED],
                 consecutive_failures=4)
    with _client() as c:
        issue = _row_html(c.get("/sources").text, moved)
    assert "Pinecrest Labs" in issue and "Needs you" in issue
    boards = html.split('data-testid="group-boards"')[1].split('data-testid="group-ats"')[0]
    assert "Indeed + Glassdoor" in boards and "Operations Analyst" in boards
    paused = html.split('data-testid="group-paused"')[1]
    assert "Meadowbrook Foods" in paused and "is-open" not in paused.split(">")[0]  # collapsed
    assert "Meadowbrook Foods" not in ats  # a paused source sits only in Paused


def test_each_row_has_switch_pill_last_check_counts_sparkline_and_one_reason_line():
    ok = _add(name="Northwind Health", runs=[OK, OK])
    slow = _add(name="Remote roles", kind="jobspy", config={"search_term": "x"}, runs=[OK, RATE])
    with _client() as c:
        html = c.get("/sources").text
    good, slowed = _row_html(html, ok), _row_html(html, slow)
    assert 'role="switch"' in good and "Healthy" in good and "Checked" in good
    assert "14 open" in good and "<b>1 new</b>" in good and "src-spark" in good
    assert 'data-testid="source-reason"' not in good  # a healthy row has no reason line
    assert "Slowed down" in slowed
    assert "Site asked us to slow down. Retrying later." in slowed
    assert "429" not in html.split('data-testid="source-groups"')[1]  # raw status: details only
    assert "k-pill--warn" in slowed and "k-pill--bad" not in slowed  # amber, never red


def test_health_tiles_count_sources_that_are_on_and_the_toolbar_holds_the_buttons():
    _add(name="A", runs=[OK])
    _add(name="B", kind="lever", config={"company": "b"}, runs=[OK])
    _add(name="C", kind="ashby", config={"token": "c"}, runs=[RATE])
    _add(name="D", kind="workable", config={"slug": "d"}, enabled=False, runs=[MOVED])
    with _client() as c:
        html = c.get("/sources").text
    tiles = html.split('data-testid="health-tiles"')[1].split("src-tools")[0]
    counts = re.findall(r'data-testid="health-n">(\d+)<', tiles)
    assert counts == ["2", "1", "0"]  # D is paused: not counted
    assert "3 on · 1 paused" in _text(html)
    scene = html.split('class="k-scene"')[1].split("k-layout")[0]
    assert "Check all now" not in scene and "Add a source" not in scene
    tools = html.split('class="src-tools"')[1].split('id="src-list"')[0]
    assert 'data-testid="check-all"' in tools and 'data-testid="add-source"' in tools
    for label in ("All types", "All origins", "All health"):
        assert label in tools
    for tag in ("Built in", "From your profile", "Found in results", "Suggested", "Added by you"):
        assert tag in tools  # the origin filter lists every tag


def test_empty_install_still_renders_every_group_with_a_nothing_here_line():
    with _client() as c:
        html = c.get("/sources").text
    assert html.count("Nothing here yet.") == 3
    assert 'data-testid="no-searches"' in html


def test_the_helper_column_and_its_flyouts():
    with _client() as c:
        html = c.get("/sources").text
    col = html.split('class="k-help-col"')[1]
    for card in ("How sources work", "Where sources come from", "What to add", "Good to know"):
        assert card in col
    for fly in ("src-how-fly", "src-origin-fly", "src-add-fly", "src-limits-fly"):
        assert f'id="{fly}"' in html and f'data-k-flyout="#{fly}"' in html
    assert "LinkedIn is not searched" in col
    flat = _text(html)
    assert "ZipRecruiter blocks automated searches" in flat
    assert "Google Jobs returns nothing right now" in flat
    assert "—" not in html  # no em dash in the page


def test_consulting_shows_its_own_groups():
    _both()
    _add(track="consulting", kind="sam_gov", name="SAM.gov bids", config={"naics": ["541512"]})
    _add(track="consulting", kind="sec_edgar", name="Vantage Mutual",
         config={"queries": ['"Vantage"']})
    with _client() as c:
        html = c.get("/sources?track=consulting").text
        emp = c.get("/sources?track=employment").text
    for title in ("Public bids", "Company signals"):
        assert title in html and title not in emp
    assert "SAM.gov bids" in html and "SAM.gov bids" not in emp
    assert "Company career pages" not in html


# ------------------------------------------------------------------------- switch and Undo
def test_switching_off_pauses_and_a_toast_offers_undo_and_never_deletes():
    sid = _add(name="Northwind Health", runs=[OK])
    with _client() as c:
        r = c.post(f"/sources/{sid}/toggle", headers=HX)
        assert r.status_code == 200 and 'id="src-list"' in r.text
        trig = json.loads(r.headers["HX-Trigger"])
        assert trig["k-toast"]["undoUrl"] == f"/sources/{sid}/toggle?set=1"
        assert "paused" in trig["k-toast"]["message"] and "Nothing is deleted" in trig["k-toast"][
            "message"]
        assert trig["src-moved"] == {"id": sid}
        assert 'hx-swap-oob="outerHTML"' in r.text and 'id="src-health"' in r.text
        assert not rows()["Northwind Health"].enabled
        assert "Northwind Health" in r.text.split('data-testid="group-paused"')[1]
        c.post(trig["k-toast"]["undoUrl"], headers=HX)  # Undo
    assert rows()["Northwind Health"].enabled


def test_a_manual_switch_on_clears_the_failure_streak():
    sid = _add(name="Northwind Health", enabled=False, consecutive_failures=3)
    with _client() as c:
        c.post(f"/sources/{sid}/toggle", headers=HX)
    row = rows()["Northwind Health"]
    assert row.enabled and row.consecutive_failures == 0


# ------------------------------------------------------------------------------- the drawer
def test_drawer_shows_history_settings_cadence_robots_note_details_and_actions():
    sid = _add(name="Larkspur Logistics", kind="lever", config={"company": "larkspur"},
               origin="custom", runs=[OK, OK, MOVED], consecutive_failures=2)
    with _client() as c:
        html = c.get(f"/sources/{sid}/drawer").text
    assert 'role="dialog"' in html and 'aria-modal="true"' in html
    assert "Needs you" in html and "could not find it at the saved address" in html
    assert "Last 10 checks" in html and html.count("<i") >= 10
    assert "Hiring system" in html and "Lever" in html and "larkspur" in html
    assert 'data-testid="drawer-cadence"' in html and "Daily" in html and "Weekly" in html
    assert "robots.txt" in html or "never signs in" in html
    assert "Show details" in html and "last status: AdapterError: HTTP 404" in html
    for testid in ("check-now", "drawer-edit", "drawer-remove"):
        assert f'data-testid="{testid}"' in html
    assert c is not None


def test_found_by_kestrel_cannot_be_edited_or_removed_only_turned_off():
    sid = _add(name="Cedar Grove", origin="discovered")
    with _client() as c:
        html = c.get(f"/sources/{sid}/drawer").text
        assert c.post(f"/sources/{sid}/remove", headers=HX).status_code == 400
    assert "can turn it off, but not edit it" in html
    assert 'data-testid="drawer-edit"' not in html and 'data-testid="drawer-remove"' not in html
    assert 'data-testid="drawer-toggle"' in html
    assert rows()["Cedar Grove"].enabled  # nothing happened


def test_a_profile_search_points_at_profile_not_edit():
    sid = _add(name="Jobs near Austin", kind="jobspy", origin="generated",
               config={"search_term": "x", "site_name": ["indeed"]})
    with _client() as c:
        html = c.get(f"/sources/{sid}/drawer").text
    assert "Change on Profile" in html and "/profile?tab=employment" in html
    assert 'data-testid="drawer-edit"' not in html


def test_cadence_is_saved_and_validated():
    sid = _add(name="Northwind Health")
    with _client() as c:
        r = c.post(f"/sources/{sid}/cadence", data={"cadence_hours": "168"})
        assert r.status_code == 200 and "weekly" in r.headers["HX-Trigger"]
        for bad in ("0", "abc", "9999"):
            assert c.post(f"/sources/{sid}/cadence", data={"cadence_hours": bad}).status_code == 400
        assert c.post("/sources/9999/cadence", data={"cadence_hours": "24"}).status_code == 404
    assert rows()["Northwind Health"].cadence_hours == 168


def test_remove_hides_never_deletes_and_undo_restores_it_as_it_was():
    sid = _add(name="Harbor Point", runs=[OK])
    with _client() as c:
        r = c.post(f"/sources/{sid}/remove", headers=HX)
        trig = json.loads(r.headers["HX-Trigger"])
        assert trig["k-toast"]["undoUrl"].startswith(f"/sources/{sid}/restore?at=")
        assert "Roles already found stay" in trig["k-toast"]["message"]
        assert "Harbor Point" not in c.get("/sources").text
        assert c.get(f"/sources/{sid}/drawer").status_code == 404
        gone = rows()["Harbor Point"]  # the row is still there, with its runs
        assert gone.enabled is False and gone.config.get(sv.REMOVED_KEY)
        assert _run(_count_runs(sid)) == 1
        c.post(trig["k-toast"]["undoUrl"], headers=HX)
        assert "Harbor Point" in c.get("/sources").text
    back = rows()["Harbor Point"]
    assert back.enabled is True and sv.REMOVED_KEY not in back.config


async def _count_runs(sid: int) -> int:
    async with get_sessionmaker()() as s:
        return len((await s.execute(select(ScrapeRun).where(ScrapeRun.source_id == sid))).all())


def test_a_removed_source_does_not_count_in_the_tiles_or_the_counts():
    sid = _add(name="Harbor Point", runs=[OK])
    _add(name="Other", kind="lever", config={"company": "o"}, runs=[OK])
    with _client() as c:
        c.post(f"/sources/{sid}/remove", headers=HX)
        html = c.get("/sources").text
    assert "1 on · 0 paused" in _text(html)


def test_check_now_runs_the_real_runner_and_answers_in_the_drawer(monkeypatch):
    sid = _add(name="Northwind Health", runs=[OK])
    called = []

    async def fake(source_id, override=None, label=None):
        called.append((source_id, override))
        return "ok: {}"

    monkeypatch.setattr("kestrel.web.pages.sources._trigger_run", fake)
    with _client() as c:
        r = c.post(f"/sources/{sid}/check", headers=HX)
        assert c.post("/sources/9999/check", headers=HX).status_code == 404
    assert called == [(sid, None)]  # the usual models
    assert 'data-testid="drawer-note"' in r.text and "checked: 14 open" in r.text
    assert 'id="src-list"' in r.text  # the row behind it updates too


def test_check_now_never_500s_on_a_dead_network(monkeypatch):
    sid = _add(name="Northwind Health")

    async def boom(source_id, override=None, label=None):
        return "queued, but the run failed immediately: no network"

    monkeypatch.setattr("kestrel.web.pages.sources._trigger_run", boom)
    with _client() as c:
        r = c.post(f"/sources/{sid}/check", headers=HX)
    assert r.status_code == 200 and "the run failed immediately" in r.text


def test_edit_in_the_drawer_validates_before_saving_and_keeps_the_values():
    sid = _add(name="Acme board", kind="greenhouse", config={"token": "acme"})
    form = {"drawer": "1", "name": "Acme board", "track": "employment", "kind": "greenhouse",
            "cadence_hours": "24", "config": "{}", "enabled": "1"}
    with _client() as c:
        bad = c.post(f"/sources/{sid}", data=form, headers=HX)
        assert 'data-testid="edit-error"' in bad.text and "token" in bad.text
        assert rows()["Acme board"].config == {"token": "acme"}  # nothing written
        good = c.post(f"/sources/{sid}", data={**form, "config": '{"token": "acme2"}'}, headers=HX)
    assert "Saved." in good.text and rows()["Acme board"].config == {"token": "acme2"}


# ------------------------------------------------------------------------- Check all now
def test_check_all_queues_every_enabled_source_once():
    a = _add(name="A", runs=[OK])
    _add(name="B", kind="lever", config={"company": "b"}, enabled=False)
    with _client() as c:
        r = c.post("/sources/check-all", headers=HX)
        assert "Checking 1 source" in json.loads(r.headers["HX-Trigger"])["k-toast"]["message"]
        again = c.post("/sources/check-all", headers=HX)
        assert "Nothing to check" in json.loads(again.headers["HX-Trigger"])["k-toast"]["message"]
        page = c.get("/sources").text
    assert 'data-testid="checking-note"' in page and "Checking 1 source" in page
    assert 'hx-trigger="every 6s [srcPollOk()]"' in page  # the rows refresh while it runs

    async def _tasks():
        async with get_sessionmaker()() as s:
            return [t.payload for t in (await s.execute(select(Task))).scalars().all()
                    if t.type == "scrape"]

    assert _run(_tasks()) == [{"source_id": a}]


# ------------------------------------------------------------------------------- the sheet
def test_the_sheet_offers_a_tile_per_type_and_never_linkedin():
    _both()
    with _client() as c:
        emp = c.get("/sources/add?track=employment").text
        con = c.get("/sources/add?track=consulting").text
    for testid in ("type-boards", "type-ats", "type-name", "type-paste"):
        assert f'data-testid="{testid}"' in emp
    assert 'data-testid="type-gov"' not in emp
    for testid in ("type-boards", "type-gov", "type-sig", "type-feeds", "type-paste"):
        assert f'data-testid="{testid}"' in con
    assert "linkedin" not in (emp + con).lower().replace("linkedin does not allow", "")
    assert "Which ones, and why?" in emp
    assert 'href="/pursuits?track=employment"' in emp  # One posting: the paste-by-hand path


def test_job_board_sheet_has_the_indeed_one_filter_radio_group_and_honest_limits():
    with _client() as c:
        html = c.get("/sources/add/boards").text
    assert html.count('name="indeed_filter"') == 5
    assert "Indeed allows one extra filter per search" in html
    assert "Need two? Add a second search." in html
    assert "ZipRecruiter blocks automated searches, so this always finds 0." in html
    assert "Google Jobs returns nothing right now" in html
    assert 'data-testid="board-dice"' in html and "Dice has no public feed" in html
    assert 'name="board_linkedin"' not in html
    assert 'name="board_indeed" value="1" data-board="indeed" checked' in html


def test_adding_a_board_search_builds_the_config_and_toasts_with_undo():
    with _client() as c:
        r = c.post(
            "/sources/add/boards",
            headers=HX,
            data={
                "title": "Operations Analyst",
                "place": "Austin, TX",
                "board_indeed": "1",
                "board_glassdoor": "1",
                "indeed_filter": "hours",
                "hours_days": "3",
            },
        )
        trig = json.loads(r.headers["HX-Trigger"])
        assert r.headers["HX-Retarget"] == "#src-list"
        assert "src-sheet-close" in trig and trig["src-arrived"]["id"]
        assert trig["k-toast"]["undoUrl"].__contains__("/remove?token=")
    row = rows()["Operations Analyst in Austin, TX"]
    assert row.kind == "jobspy" and row.origin == "custom" and row.enabled
    assert row.config == {
        "search_term": "Operations Analyst",
        "site_name": ["indeed", "glassdoor"],
        "location": "Austin, TX",
        "hours_old": 72,
    }


def test_the_sheet_only_ever_sends_one_indeed_filter_and_refuses_the_rest_plainly():
    from kestrel.web.pages.sources_ui import _indeed_config

    assert _indeed_config({"indeed_filter": "easy"}) == {"easy_apply": True}
    assert _indeed_config({"indeed_filter": "remote"}) == {"is_remote": True}
    assert _indeed_config({"indeed_filter": "type", "job_type": "contract"}) == {
        "job_type": "contract"}
    assert _indeed_config({"indeed_filter": "none"}) == {}
    # A conflicting config from any other caller is refused by the same validator the runner uses.
    from kestrel.web.pages.sources import _validate_source_config

    msg = _validate_source_config("jobspy", {"search_term": "x", "site_name": ["indeed"],
                                              "hours_old": 72, "easy_apply": True})
    assert msg and "Indeed permits only ONE" in msg


def test_a_board_search_needs_a_title_and_a_board_and_adds_nothing_when_refused():
    with _client() as c:
        no_title = c.post("/sources/add/boards", headers=HX, data={"board_indeed": "1"})
        no_board = c.post("/sources/add/boards", headers=HX, data={"title": "Analyst"})
    assert 'data-testid="add-error"' in no_title.text and "job title" in no_title.text
    assert "Pick at least one job board" in no_board.text
    assert "HX-Trigger" not in no_title.headers and rows() == {}


def test_google_jobs_is_added_with_its_own_search_term_and_a_dice_post_is_ignored():
    with _client() as c:
        c.post("/sources/add/boards", headers=HX, data={
            "title": "Analyst", "place": "Remote", "board_google": "1", "board_dice": "1"})
    row = rows()["Analyst in Remote"]
    assert row.config["site_name"] == ["google"]
    assert row.config["google_search_term"] == "Analyst jobs near Remote"


def test_link_check_reads_the_hiring_system_and_counts_open_roles():
    gh = json.loads((FIXTURES / "greenhouse.json").read_text(encoding="utf-8"))
    with respx.mock(assert_all_called=False) as mock:
        hit = mock.get("https://boards-api.greenhouse.io/v1/boards/harborpoint/jobs?content=true"
                       ).mock(return_value=httpx.Response(200, json=gh))
        with _client() as c:
            r = c.post("/sources/check/link", data={"url": "https://boards.greenhouse.io/harborpoint"})
    assert hit.called and hit.calls.last.request.headers["user-agent"].startswith("Kestrel/")
    assert 'data-testid="check-good"' in r.text and "Greenhouse job board" in r.text
    assert "open role" in r.text
    assert 'name="pick" value="pick:greenhouse|' in r.text and 'value="Harborpoint"' in r.text


def test_a_lever_404_is_caught_when_adding_not_silently_later():
    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(return_value=httpx.Response(404, json={}))
        with _client() as c:
            r = c.post("/sources/check/link", data={"url": "https://jobs.lever.co/nobody"})
    assert 'data-testid="check-bad"' in r.text and "Nothing found at that link" in r.text
    assert 'name="pick"' not in r.text  # Add stays off


@pytest.fixture
def _public_dns(monkeypatch):
    """A careers page on another domain is read once; its address must look public (no DNS)."""
    from kestrel.ingest import http as ingest_http

    async def _resolve(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(ingest_http, "_resolve", _resolve)


def test_a_page_with_no_job_data_is_said_plainly_and_points_at_paste_by_hand(_public_dns):
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/careers").mock(
            return_value=httpx.Response(200, text="<html>Join us</html>"))
        mock.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
        with _client() as c:
            r = c.post("/sources/check/link", data={"url": "https://example.com/careers"})
            junk = c.post("/sources/check/link", data={"url": "not a link"})
    assert "lists its jobs with JavaScript, which Kestrel cannot read yet" in r.text
    assert "One posting" in r.text
    assert 'name="pick"' not in r.text
    assert "Nothing found at that link" in junk.text


CAREERS = FIXTURES / "careers"


def _careers(name: str) -> str:
    return (CAREERS / name).read_text(encoding="utf-8")


def test_a_careers_page_with_job_data_is_counted_named_by_its_employer_and_offered_for_add(
        _public_dns):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://[^/]+/robots\.txt").mock(return_value=httpx.Response(404))
        mock.get("https://jobs.fabrikam.example/").mock(
            return_value=httpx.Response(200, text=_careers("graph.html")))
        with _client() as c:
            r = c.post("/sources/check/link", data={"url": "https://jobs.fabrikam.example/"})
    assert 'data-testid="check-good"' in r.text and "Careers page. 3 open roles." in r.text
    pick = unescape(re.search(r'name="pick" value="([^"]+)"', r.text).group(1))
    kind, cfg, name = pick[5:].split("|", 2)
    assert kind == "careers_page" and json.loads(cfg)["url"] == "https://jobs.fabrikam.example/"
    assert json.loads(cfg)["company_name"] == name == "Harbor Point Analytics"


def test_a_checked_careers_page_is_added_once_as_a_company_page_source(_public_dns):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://[^/]+/robots\.txt").mock(return_value=httpx.Response(404))
        mock.get("https://jobs.fabrikam.example/").mock(
            return_value=httpx.Response(200, text=_careers("graph.html")))
        with _client() as c:
            check = c.post("/sources/check/link",
                           data={"url": "https://jobs.fabrikam.example/"}).text
            pick = unescape(re.search(r'name="pick" value="([^"]+)"', check).group(1))
            c.post("/sources/add/ats", headers=HX, data={"pick": pick})
            dupe = c.post("/sources/add/ats", headers=HX, data={"pick": pick})
            page = c.get("/sources")
    row = rows()["Harbor Point Analytics"]
    assert row.kind == "careers_page" and row.enabled and row.config["url"].startswith("https://")
    assert "already one of your sources" in dupe.text
    assert page.status_code == 200 and "Harbor Point Analytics" in page.text
    assert sv.group_of(row) == sv.ATS and sv.type_label(row) == "Careers page"


def test_a_careers_page_that_uses_javascript_or_disallows_robots_or_is_gone_is_said(_public_dns):
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://closed.example/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n"))
        mock.get(url__regex=r"https://(talent|gone)\.[a-z.]+/robots\.txt").mock(
            return_value=httpx.Response(404))
        mock.get("https://talent.contoso.example/").mock(
            return_value=httpx.Response(200, text=_careers("js_only.html")))
        mock.get("https://gone.example/careers").mock(return_value=httpx.Response(404))
        with _client() as c:
            js = c.post("/sources/check/link", data={"url": "https://talent.contoso.example/"})
            closed = c.post("/sources/check/link", data={"url": "https://closed.example/careers"})
            gone = c.post("/sources/check/link", data={"url": "https://gone.example/careers"})
    assert 'data-testid="check-warn"' in js.text and "lists its jobs with JavaScript" in js.text
    assert 'data-testid="check-bad"' in closed.text and "robots.txt" in closed.text
    assert 'data-testid="check-bad"' in gone.text and "not found" in gone.text
    assert 'name="pick"' not in js.text + closed.text + gone.text


def test_a_recruitee_link_is_checked_and_a_custom_domain_page_is_read_for_its_board(_public_dns):
    board = json.loads((FIXTURES / "recruitee_offers.json").read_text(encoding="utf-8"))
    page = '<script>fetch("https://northwind.recruitee.com/api/offers/")</script>'
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://northwind.recruitee.com/robots.txt").mock(return_value=httpx.Response(404))
        mock.get("https://northwind.recruitee.com/api/offers/").mock(
            return_value=httpx.Response(200, json=board))
        mock.get("https://careers.northwind.example/").mock(
            return_value=httpx.Response(200, text=page))
        mock.get("https://careers.northwind.example/robots.txt").mock(
            return_value=httpx.Response(404))
        with _client() as c:
            direct = c.post("/sources/check/link", data={"url": "https://northwind.recruitee.com/"})
            custom = c.post("/sources/check/link",
                            data={"url": "https://careers.northwind.example/"})
    for r in (direct, custom):
        assert 'data-testid="check-good"' in r.text and "Recruitee job board" in r.text
        assert "3 open roles" in r.text
        assert 'name="pick" value="pick:recruitee|' in r.text


def test_a_teamtailor_link_is_checked_named_by_its_employer_and_a_dead_feed_is_said(_public_dns):
    feed = json.loads((FIXTURES / "teamtailor_jobs.json").read_text(encoding="utf-8"))
    script = '<script src="//assets-aws.teamtailor-cdn.com/assets/careersite-1.js"></script>'
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://[^/]+/robots\.txt").mock(return_value=httpx.Response(404))
        mock.get("https://careers.northwind.example/jobs.json").mock(
            return_value=httpx.Response(200, json=feed))
        mock.get("https://careers.northwind.example/").mock(
            return_value=httpx.Response(200, text=script))
        mock.get("https://quiet.teamtailor.com/jobs.json").mock(return_value=httpx.Response(404))
        mock.get("https://quiet.teamtailor.com/jobs.rss").mock(return_value=httpx.Response(404))
        with _client() as c:
            custom = c.post("/sources/check/link",
                            data={"url": "https://careers.northwind.example/"})
            off = c.post("/sources/check/link", data={"url": "https://quiet.teamtailor.com/jobs"})
    assert 'data-testid="check-good"' in custom.text and "Teamtailor job board" in custom.text
    assert "3 open roles" in custom.text and 'value="Northwind"' in custom.text
    assert 'name="pick" value="pick:teamtailor|' in custom.text
    assert 'data-testid="check-warn"' in off.text and "turned off" in off.text
    assert 'name="pick"' not in off.text  # nothing to add until the feed is on


def test_a_link_source_is_added_only_from_a_checked_pick():
    gh = json.loads((FIXTURES / "greenhouse.json").read_text(encoding="utf-8"))
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://boards-api.greenhouse.io/v1/boards/harborpoint/jobs?content=true"
                 ).mock(return_value=httpx.Response(200, json=gh))
        with _client() as c:
            unchecked = c.post("/sources/add/ats", headers=HX, data={
                "url": "https://boards.greenhouse.io/harborpoint"})
            assert "Check the link first" in unchecked.text and rows() == {}
            check = c.post("/sources/check/link",
                           data={"url": "https://boards.greenhouse.io/harborpoint"}).text
            pick = unescape(re.search(r'name="pick" value="([^"]+)"', check).group(1))
            added = c.post("/sources/add/ats", headers=HX, data={
                "pick": pick, "company": "Harbor Point Analytics"})
            assert "HX-Trigger" in added.headers
            dupe = c.post("/sources/add/ats", headers=HX, data={"pick": pick})
    row = rows()["Harbor Point Analytics"]
    assert row.kind == "greenhouse" and row.config == {"token": "harborpoint"} and row.enabled
    assert "already one of your sources" in dupe.text


def test_a_forged_pick_is_refused():
    with _client() as c:
        for pick in ("pick:nonsense|{}|X", "pick:greenhouse|not json|X", "greenhouse|{}|X"):
            r = c.post("/sources/add/ats", headers=HX, data={"pick": pick})
            assert "Check the link first" in r.text
    assert rows() == {}


def test_company_by_name_shows_the_match_and_adds_it_on_pick():
    lever = json.loads((FIXTURES / "lever.json").read_text(encoding="utf-8"))
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://api.lever.co/v0/postings/acme?mode=json").mock(
            return_value=httpx.Response(200, json=lever))
        mock.route().mock(return_value=httpx.Response(404, json={}))
        with _client() as c:
            found = c.post("/sources/check/company", data={"q": "Acme, LLC"}).text
            assert 'data-testid="check-match"' in found and "Lever job board" in found
            pick = unescape(re.search(r'name="pick" value="([^"]+)"', found).group(1))
            c.post("/sources/add/name", headers=HX, data={"pick": pick, "company": "Acme"})
            again = c.post("/sources/check/company", data={"q": "Acme, LLC"}).text
            none = c.post("/sources/check/company", data={"q": ""}).text
    assert rows()["Acme"].kind == "lever" and rows()["Acme"].config == {"company": "acme"}
    assert "already one of your sources" in again
    assert "Type a company name first" in none


def test_company_by_name_with_no_board_says_so():
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route().mock(return_value=httpx.Response(404, json={}))
        with _client() as c:
            r = c.post("/sources/check/company", data={"q": "Nobody Corp"})
    assert "No board found for Nobody Corp" in r.text and "careers page link" in r.text
    assert 0 < route.call_count <= 12 and 'name="pick"' not in r.text


def test_a_feed_is_read_before_it_can_be_added_and_a_doctype_is_refused():
    feed = ("<rss><channel><item><title>a</title></item><item><title>b</title></item>"
            "</channel></rss>")
    evil = '<!DOCTYPE x [<!ENTITY a "b">]><rss><channel/></rss>'
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://news.example/rss").mock(return_value=httpx.Response(200, text=feed))
        mock.get("https://evil.example/rss").mock(return_value=httpx.Response(200, text=evil))
        mock.get("https://down.example/rss").mock(return_value=httpx.Response(404))
        with _client() as c:
            ok = c.post("/sources/check/feed", data={"url": "https://news.example/rss"}).text
            refused = c.post("/sources/check/feed", data={"url": "https://evil.example/rss"}).text
            down = c.post("/sources/check/feed", data={"url": "https://down.example/rss"}).text
            _both()
            c.cookies.set("kestrel-track", "consulting")
            added = c.post("/sources/add/feeds", headers=HX, data={
                "url": "https://news.example/rss", "name": "Ironbridge news"})
    assert "A feed with 2 items" in ok and 'name="pick" value="feed"' in ok
    assert "Nothing found" in refused and "Nothing found" in down
    assert "HX-Trigger" in added.headers
    row = rows()["Ironbridge news"]
    assert (row.kind, row.track, row.config) == ("rss", "consulting", {"feeds": [
        "https://news.example/rss"]})


def test_bid_search_and_company_watch_are_consulting_sources(monkeypatch):
    monkeypatch.setenv("SAM_GOV_API_KEY", "test-key")  # the validator needs one; never real config
    _both()
    with _client() as c:
        c.cookies.set("kestrel-track", "consulting")
        c.post("/sources/add/gov", headers=HX, data={"naics": "541511, 541512", "name": "Bids"})
        c.post("/sources/add/sig", headers=HX, data={"company": "Vantage Mutual"})
        empty = c.post("/sources/add/sig", headers=HX, data={"company": ""})
    assert rows()["Bids"].config["naics"] == ["541511", "541512"]
    assert rows()["Bids"].kind == "sam_gov" and rows()["Bids"].track == "consulting"
    assert rows()["Vantage Mutual"].config == {"queries": ['"Vantage Mutual"']}
    assert "Type a company name first" in empty.text


def test_an_unknown_type_is_not_offered_for_this_track():
    with _client() as c:
        assert c.get("/sources/add/gov").status_code == 404  # employment has no bid search
        assert c.get("/sources/add/linkedin").status_code == 404


# ------------------------------------------------------------------------------ suggestions
def _pursuit(org: str, status: str = "shortlisted", track: str = "employment") -> None:
    from kestrel.ingest.orgs import normalized_name

    async def _go():
        async with get_sessionmaker()() as s:
            o = Org(name=org, normalized_name=normalized_name(org))
            s.add(o)
            await s.flush()
            opp = Opportunity(track=track, opp_type="job", external_id=org, title="Role",
                              org_name=org, org_id=o.id)
            s.add(opp)
            await s.flush()
            s.add(Pursuit(opportunity_id=opp.id, track=track, status=status))
            await s.commit()

    _run(_go())


def test_suggestions_come_from_pursuits_and_skip_covered_and_closed_companies():
    _pursuit("Tidewater Freight Group")
    _pursuit("Ironbridge Supply", "applied")
    _pursuit("Northwind Health")  # already a source
    _pursuit("Old Co", "rejected")
    _add(name="Northwind Health")
    with _client() as c:
        html = c.get("/sources").text
    sug = html.split('data-testid="group-sug"')[1].split('data-testid="group-boards"')[0]
    assert "Tidewater Freight Group" in sug and "You shortlisted a role here" in sug
    assert "Ironbridge Supply" in sug and "You have an application here" in sug
    assert "Northwind Health" not in sug and "Old Co" not in sug
    assert "Nothing is added until you say so" in sug
    assert 'data-testid="suggestion-add"' in sug and 'data-testid="suggestion-dismiss"' in sug


def test_dismiss_is_remembered_per_track_and_undo_brings_it_back():
    _pursuit("Tidewater Freight Group")
    with _client() as c:
        r = c.post("/sources/suggestions/dismiss", data={"name": "Tidewater Freight Group"},
                   headers=HX)
        undo = json.loads(r.headers["HX-Trigger"])["k-toast"]["undoUrl"]
        assert "Tidewater" not in r.text.split('data-testid="group-boards"')[0]
        assert "Tidewater" not in c.get("/sources").text.split('data-testid="group-boards"')[0]
        c.post(undo, headers=HX)
        assert "Tidewater Freight Group" in c.get("/sources").text
    assert rows() == {}  # dismissing adds nothing


def test_add_a_suggestion_looks_up_its_board_adds_it_and_marks_it_suggested():
    gh = json.loads((FIXTURES / "greenhouse.json").read_text(encoding="utf-8"))
    _pursuit("Tidewater Freight")
    with respx.mock(assert_all_called=False) as mock:
        mock.get(
            "https://boards-api.greenhouse.io/v1/boards/tidewaterfreight/jobs?content=true"
        ).mock(return_value=httpx.Response(200, json=gh))
        mock.route().mock(return_value=httpx.Response(404, json={}))
        with _client() as c:
            r = c.post("/sources/suggestions/add", data={"name": "Tidewater Freight"}, headers=HX)
            page = c.get("/sources").text
    trig = json.loads(r.headers["HX-Trigger"])
    assert "Added Tidewater Freight (Greenhouse" in trig["k-toast"]["message"]
    assert trig["k-toast"]["undoUrl"].__contains__("/remove?token=")
    row = rows()["Tidewater Freight"]
    assert row.origin == "suggested" and row.enabled and row.kind == "greenhouse"
    assert "Suggested" in page.split('data-testid="group-ats"')[1]
    assert 'data-testid="group-sug"' not in page  # it is a source now


def test_a_suggestion_with_no_public_board_says_so_and_stays():
    _pursuit("Nobody Corp")
    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(return_value=httpx.Response(404, json={}))
        with _client() as c:
            r = c.post("/sources/suggestions/add", data={"name": "Nobody Corp"}, headers=HX)
    assert "No public job board found for Nobody Corp" in json.loads(
        r.headers["HX-Trigger"])["k-toast"]["message"]
    assert rows() == {} and "Nobody Corp" in r.text  # still suggested


def test_consulting_suggestions_watch_public_filings():
    _both()
    _pursuit("Vantage Mutual", track="consulting")
    with _client() as c:
        c.cookies.set("kestrel-track", "consulting")
        page = c.get("/sources?track=consulting").text
        assert "Vantage Mutual" in page.split('data-testid="group-sug"')[1]
        assert "companies to watch" in page
        c.post("/sources/suggestions/add", data={"name": "Vantage Mutual"}, headers=HX)
    row = rows()["Vantage Mutual"]
    assert (row.kind, row.origin, row.track) == ("sec_edgar", "suggested", "consulting")


# --------------------------------------------------------------------------- hard rules
def test_linkedin_is_still_refused_and_the_paste_path_is_named():
    from kestrel.web.pages.sources import _validate_source_config

    msg = _validate_source_config("jobspy", {"search_term": "x", "site_name": ["linkedin"]})
    assert msg and "LinkedIn is not searched" in msg and "Add a role I found elsewhere" in msg
    with _client() as c:
        page = _text(c.get("/sources").text)
    assert "LinkedIn is not searched. Paste a LinkedIn posting into Applications instead." in page


def test_nothing_on_the_page_sends_anything_for_the_operator():
    """Every action is a read of a public page or a local write: no apply, send or message verb."""
    for name in ("sources.html", "partials/sources_panel.html", "partials/sources_groups.html",
                 "partials/source_drawer.html", "partials/source_sheet.html"):
        text = (Path("kestrel/web/templates") / name).read_text(encoding="utf-8").lower()
        assert not re.search(r"\b(apply now|send email|send message|submit application)\b", text)


# --------------------------------------------------------------------------- static checks
def test_page_css_uses_tokens_only_and_the_mockups_container_steps():
    css = Path("kestrel/web/static/css/sources.css").read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", css)
    assert not re.search(r"^\.k-(layout|help-col)\s*\{", css, re.M)  # shared stacking is kept
    # The mockup's steps measure the window (the kwin container), not the page wrapper.
    assert "@container kwin (max-width: 1300px)" in css
    assert "@container kwin (max-width: 1000px)" in css
    assert "grid-template-columns: 32px minmax(0, 1fr) 40px 118px 132px 112px 98px 86px" in css
    assert ".src-rl { display: none; }" in css  # the mockup's 1300px step
    assert "prefers-reduced-motion" in css
    js = Path("kestrel/web/static/js/sources.js").read_text(encoding="utf-8")
    assert "—" not in js and "—" not in css


def test_the_page_stops_loading_the_legacy_stylesheet():
    with _client() as c:
        html = c.get("/sources").text
    assert "/static/css/kestrel.css" not in html
    assert "/static/css/sources.css" in html and "/static/js/sources.js" in html
    assert "/static/css/shell.css" in html


def test_every_template_for_the_page_has_no_em_dash_and_no_hex_color():
    for name in ("sources.html", "partials/sources_panel.html", "partials/sources_groups.html",
                 "partials/sources_list.html", "partials/sources_macros.html",
                 "partials/sources_health.html", "partials/source_drawer.html",
                 "partials/source_sheet.html", "partials/source_check.html"):
        text = (Path("kestrel/web/templates") / name).read_text(encoding="utf-8")
        assert "—" not in text and not re.search(r"#[0-9a-fA-F]{6}\b|rgba?\(", text), name


# ------------------------------------------------------------------ independent review fixes
def test_undo_puts_back_exactly_the_state_the_action_changed():
    sid = _add(name="Northwind Health", runs=[OK])
    with _client() as c:
        off = c.post(f"/sources/{sid}/toggle", headers=HX)
        assert f"/sources/{sid}/toggle?set=1" in off.headers["HX-Trigger"]
        c.post(f"/sources/{sid}/toggle?set=1", headers=HX)  # Undo: back on
        c.post(f"/sources/{sid}/toggle?set=1", headers=HX)  # a second Undo click changes nothing
    assert rows()["Northwind Health"].enabled is True


def test_a_removed_source_cannot_be_switched_back_on():
    sid = _add(name="Pinecrest Labs", runs=[OK])
    with _client() as c:
        c.post(f"/sources/{sid}/remove", headers=HX)
        r = c.post(f"/sources/{sid}/toggle", headers=HX)
    assert r.status_code == 404 and rows()["Pinecrest Labs"].enabled is False


def test_the_same_board_search_is_not_added_twice():
    form = {"title": "Operations Analyst", "place": "Austin, TX", "board_indeed": "1"}
    with _client() as c:
        c.post("/sources/add/boards", headers=HX, data=form)
        again = c.post("/sources/add/boards", headers=HX, data=form)
    assert "already one of your sources" in again.text
    assert len([r for r in rows().values() if r.kind == "jobspy"]) == 1


def test_a_job_feed_is_an_employment_source_and_lands_on_the_employment_track():
    feed = "<rss><channel><item><title>a</title></item></channel></rss>"
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/jobs.xml").mock(return_value=httpx.Response(200, text=feed))
        with _client() as c:
            sheet = c.get("/sources/add").text
            assert "Job feed" in sheet
            c.post("/sources/add/feeds", headers=HX, data={
                "url": "https://example.com/jobs.xml", "name": "Example jobs", "pick": "feed"})
    row = rows()["Example jobs"]
    assert row.kind == "rss" and row.track == "employment"


def test_the_sparkline_has_no_invented_heights_and_the_drawer_counts_checks():
    sid = _add(name="Northwind Health", runs=[OK, OK, OK])
    with _client() as c:
        html = c.get("/sources").text
        drawer = c.get(f"/sources/{sid}/drawer").text
    assert 'data-testid="hist-n">3 of 10 recorded' in drawer
    assert len(set(re.findall(r'<i class="" style="height:(\d+)px"', html))) == 1


def test_scoring_everything_on_an_online_model_names_it_and_is_checked_on_the_server(monkeypatch):
    from kestrel.pipeline import lane

    monkeypatch.setattr(lane, "scores_on_hosted", lambda settings: True)
    monkeypatch.setattr("kestrel.web.pages.sources._lane_model", lambda settings: "vendor/model-x")
    released = []

    async def _release(sm, *, hosted=False):
        assert hosted
        return 0

    monkeypatch.setattr(lane, "release_all", _release)
    with _client() as c:
        no_model = c.post("/sources/score-everything", data={"confirm": "1"})
        wrong = c.post("/sources/score-everything", data={"confirm": "1", "model": "other"})
    assert no_model.status_code == 400 and wrong.status_code == 400 and not released
    assert "Nothing was sent" in no_model.text


def test_old_source_restore_cannot_restore_a_later_removal():
    sid = _add(name="Harbor Point", runs=[OK])
    with _client() as c:
        first = json.loads(c.post(f"/sources/{sid}/remove", headers=HX).headers["HX-Trigger"])[
            "k-toast"
        ]["undoUrl"]
        c.post(first, headers=HX)
        c.post(f"/sources/{sid}/remove", headers=HX)
        assert "This can no longer be undone." in c.post(first, headers=HX).text
    assert rows()["Harbor Point"].enabled is False


def test_source_add_undo_keeps_later_cadence_edit():
    with _client() as c:
        result = c.post(
            "/sources/add/boards", headers=HX, data={"title": "Analyst", "board_indeed": "1"}
        )
        events = json.loads(result.headers["HX-Trigger"])
        sid = events["src-arrived"]["id"]
        c.post(f"/sources/{sid}/cadence", data={"cadence_hours": "168"})
        assert "This can no longer be undone." in c.post(events["k-toast"]["undoUrl"]).text
    assert rows()["Analyst"].enabled
