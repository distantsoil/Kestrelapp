"""Adzuna and the remote boards on the Sources page (B2 and B3, D-SRC-15), and one Add that creates
several sources (Undo and Restore take all of them). Offline; no key appears in a page."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
from kestrel.db.models import ScrapeRun, Setting, Source
from kestrel.ingest.adapters.adzuna import NEEDS_KEY
from kestrel.services import tracks

HX = {"HX-Request": "true"}
APP_ID = "az-ui-id-4455"
APP_KEY = "az-ui-key-5566778899"
EMAIL = "seeker@example.com"


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


def _run(coro_fn):
    async def _wrap():
        try:
            async with get_sessionmaker()() as s:
                return await coro_fn(s)
        finally:
            await reset_engine()

    return asyncio.run(_wrap())


def _sources() -> dict[str, Source]:
    async def go(s):
        return {r.name: r for r in (await s.execute(select(Source))).scalars().all()}

    return _run(go)


def _setting(key: str):
    async def go(s):
        row = await s.get(Setting, key)
        return None if row is None else row.value

    return _run(go)


def _env() -> str:
    p = paths.dotenv_path()
    return p.read_text(encoding="utf-8") if p.exists() else ""


FORM = {"title": "Program Analyst", "place": "Columbus, Ohio", "board_adzuna": "1",
        "adzuna_id": APP_ID, "adzuna_key": APP_KEY}


def _undo_url(response) -> str:
    return json.loads(response.headers["HX-Trigger"])["k-toast"]["undoUrl"]


def test_the_add_sheet_offers_adzuna_with_a_hidden_key_panel_and_the_remote_group():
    with _client() as c:
        html = c.get("/sources/add/boards").text
    assert 'name="board_adzuna"' in html and "Official public access" in html
    panel = html.split('data-testid="adzuna-panel"', 1)[1].split("board-note", 1)[0]
    assert " hidden" in html.split('id="src-adzuna"', 1)[1][:80]
    assert 'href="https://developer.adzuna.com/signup"' in panel
    assert 'type="password" name="adzuna_id"' in panel and 'name="adzuna_key"' in panel
    assert "your search words and place and your app ID and app key" in panel
    assert "up to 8 of your free calls" in panel and "—" not in panel
    group = html.split("Remote jobs</div>", 1)[1].split("Limited</div>", 1)[0]
    assert 'name="board_himalayas"' in group and 'name="board_weworkremotely"' in group
    assert "remotive" not in html.lower()


def test_adding_adzuna_alone_saves_both_keys_to_env_and_stores_none_in_the_source():
    with _client() as c:
        r = c.post("/sources/add/boards", headers=HX, data=FORM)
        trig = json.loads(r.headers["HX-Trigger"])
        assert "src-sheet-close" in trig and "needs" not in trig["k-toast"]["message"].lower()
        assert APP_KEY not in r.text and APP_ID not in r.text
    row = _sources()["Program Analyst on Adzuna in Columbus, Ohio"]
    assert row.kind == "adzuna" and row.enabled and row.origin == "custom"
    assert row.config == {"keyword": "Program Analyst", "location": "Columbus, Ohio"}
    env = _env()
    assert f"ADZUNA_APP_ID_KEY={APP_ID}" in env and f"ADZUNA_APP_KEY={APP_KEY}" in env
    assert APP_KEY not in json.dumps(row.config)


def test_without_keys_it_is_added_and_says_what_is_missing():
    with _client() as c:
        r = c.post("/sources/add/boards", headers=HX, data={
            "title": "Analyst", "board_adzuna": "1"})
        assert "Adzuna needs your app ID and app key" in json.loads(
            r.headers["HX-Trigger"])["k-toast"]["message"]
    assert [r.kind for r in _sources().values()] == ["adzuna"]


def test_a_second_add_of_the_same_search_is_refused_for_every_new_board():
    form = {"title": "Analyst", "board_himalayas": "1", "board_weworkremotely": "1",
            "board_adzuna": "1", "adzuna_id": APP_ID, "adzuna_key": APP_KEY}
    with _client() as c:
        c.post("/sources/add/boards", headers=HX, data=form)
        again = c.post("/sources/add/boards", headers=HX, data=form)
    assert "already one of your sources" in again.text and len(_sources()) == 3


def test_the_remote_boards_add_keyless_sources_named_after_the_board():
    with _client() as c:
        c.post("/sources/add/boards", headers=HX, data={
            "title": "Operations Analyst", "place": "Remote",
            "board_himalayas": "1", "board_weworkremotely": "1"})
    got = _sources()
    him = got["Operations Analyst on Himalayas"]
    wwr = got["We Work Remotely"]
    assert (him.kind, him.config) == ("himalayas", {"keyword": "Operations Analyst"})
    assert (wwr.kind, wwr.config) == ("weworkremotely", {})
    assert _env() == "" or "ADZUNA" not in _env()


def test_a_sheet_with_no_board_ticked_still_asks_for_one():
    with _client() as c:
        r = c.post("/sources/add/boards", headers=HX, data={"title": "Analyst"})
    assert "Pick at least one job board" in r.text and _sources() == {}


def test_the_card_saves_both_keys_and_reports_ready():
    with _client() as c:
        card = c.get("/sources").text.split('data-testid="adzuna-card"', 1)[1].split(
            "<!-- /adzuna -->", 1)[0]
        assert "Needs app ID and key" in card and "Get a free Adzuna key" in card
        assert "your app ID and app key" in card and "up to 8 of your free calls" in card
        assert "start of each description" in card and "—" not in card
        assert c.post("/sources/adzuna/key", data={}).text.count(
            "Paste your app ID and app key first.") == 1
        half = c.post("/sources/adzuna/key", data={"app_key": APP_KEY})
        assert "Saved on this computer." in half.text and "Needs app ID and key" in half.text
        r = c.post("/sources/adzuna/key", data={"app_id": APP_ID})
        assert "Ready" in r.text and "Key saved: ********" in r.text
        assert APP_KEY not in r.text and APP_ID not in r.text
    env = _env()
    assert f"ADZUNA_APP_ID_KEY={APP_ID}" in env and f"ADZUNA_APP_KEY={APP_KEY}" in env


def test_a_source_row_without_keys_says_so_plainly_and_boards_group_together():
    from kestrel.web import source_view as sv

    async def go(s):
        src = Source(track="employment", kind="adzuna", name="Analysts on Adzuna", enabled=True,
                     config={"keyword": "Analyst"}, origin="custom")
        s.add(src)
        await s.flush()
        s.add(ScrapeRun(source_id=src.id, status="partial", error=NEEDS_KEY, counts={},
                        started_at="2026-10-29T10:00:00+00:00"))
        await s.commit()
        runs = list((await s.execute(select(ScrapeRun))).scalars())
        return sv.build_view(src, runs)

    v = _run(go)
    assert v.health == "b" and v.health_label == "Needs you"
    assert v.reason == NEEDS_KEY + "." and v.group == "boards"
    assert v.type_line == "Adzuna · Analyst"
    for kind, line in (("himalayas", "Himalayas"), ("weworkremotely", "We Work Remotely")):
        view = sv.build_view(Source(track="employment", kind=kind, name="x", enabled=True,
                                    config={}, origin="custom"), [])
        assert view.group == "boards" and view.type_line.startswith(line)


# ------------------------------------------------ one Add, several sources: Undo takes them all
def test_undo_after_ticking_two_boards_removes_both_and_restore_brings_both_back():
    form = {"title": "Program Analyst", "place": "Columbus, Ohio", "board_indeed": "1",
            "board_himalayas": "1", "board_adzuna": "1", "adzuna_id": APP_ID,
            "adzuna_key": APP_KEY}
    with _client() as c:
        added = c.post("/sources/add/boards", headers=HX, data=form)
        assert {r.kind for r in _sources().values()} == {"jobspy", "himalayas", "adzuna"}
        undo = _undo_url(added)
        assert "token=" in undo
        removed = c.post(undo, headers=HX)
        assert removed.status_code == 200
        gone = _sources()
        assert len(gone) == 3 and all(sv_removed(r) and not r.enabled for r in gone.values())
        restore = _undo_url(removed)
        assert c.post(restore, headers=HX).status_code == 200
        back = _sources()
        assert all(not sv_removed(r) and r.enabled for r in back.values())
        # A used token cannot be used twice.
        assert "can no longer be undone" in c.post(undo, headers=HX).text


def test_undo_is_refused_for_all_when_one_of_the_sources_changed_since():
    form = {"title": "Analyst", "board_indeed": "1", "board_himalayas": "1"}
    with _client() as c:
        added = c.post("/sources/add/boards", headers=HX, data=form)
        undo = _undo_url(added)
        async def edit(s):
            row = next(r for r in (await s.execute(select(Source))).scalars()
                       if r.kind == "himalayas")
            row.cadence_hours = 6
            await s.commit()

        _run(edit)
        assert "can no longer be undone" in c.post(undo, headers=HX).text
    assert all(not sv_removed(r) and r.enabled for r in _sources().values())


def sv_removed(src) -> bool:
    from kestrel.web import source_view as sv

    return sv.is_removed(src)
