"""USAJobs on the Sources page (B1, D-SRC-15): the board on the Add sheet with its key panel, the
card under "Sources that need your own key", where the key and email are stored, and what a source
row says when the key is missing. Offline; the key never appears in a page."""

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
from kestrel.ingest.adapters.usajobs import NEEDS_KEY
from kestrel.services import tracks

HX = {"HX-Request": "true"}
KEY = "uj-ui-key-5566778899"
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


FORM = {"title": "Program Analyst", "place": "Austin, Texas", "board_usajobs": "1",
        "usajobs_key": KEY, "usajobs_email": EMAIL}


def test_the_add_sheet_offers_usajobs_with_a_hidden_key_panel_and_what_is_sent():
    with _client() as c:
        html = c.get("/sources/add/boards").text
    assert "Official public access" in html and 'name="board_usajobs"' in html
    assert "US government jobs" in html
    panel = html.split('data-testid="usajobs-panel"', 1)[1].split("board-note", 1)[0]
    assert " hidden" in html.split('id="src-usajobs"', 1)[1][:80]
    assert 'href="https://developer.usajobs.gov/apirequest/"' in panel
    assert 'type="password" name="usajobs_key"' in panel and 'name="usajobs_email"' in panel
    assert "your search words and place, your key and your email address" in panel
    assert "—" not in panel


def test_adding_usajobs_alone_saves_the_key_to_env_and_the_email_as_a_setting():
    with _client() as c:
        r = c.post("/sources/add/boards", headers=HX, data=FORM)
        trig = json.loads(r.headers["HX-Trigger"])
        assert "src-sheet-close" in trig and "needs" not in trig["k-toast"]["message"].lower()
        assert KEY not in r.text
    row = _sources()["Program Analyst on USAJobs in Austin, Texas"]
    assert row.kind == "usajobs" and row.enabled and row.origin == "custom"
    assert row.config == {"keyword": "Program Analyst", "location": "Austin, Texas",
                          "contact_email": EMAIL}
    assert f"USAJOBS_API_KEY={KEY}" in _env() and KEY not in json.dumps(row.config)
    assert _setting("sources.usajobs_email") == EMAIL


def test_usajobs_beside_indeed_adds_both_searches():
    with _client() as c:
        c.post("/sources/add/boards", headers=HX, data={**FORM, "board_indeed": "1"})
    got = _sources()
    assert {r.kind for r in got.values()} == {"jobspy", "usajobs"}


def test_without_a_key_it_is_added_and_says_what_is_missing():
    with _client() as c:
        r = c.post("/sources/add/boards", headers=HX, data={
            "title": "Analyst", "board_usajobs": "1", "usajobs_key": "", "usajobs_email": ""})
        assert "needs your API key and email" in json.loads(r.headers["HX-Trigger"])["k-toast"][
            "message"]
    assert [r.kind for r in _sources().values()] == ["usajobs"]


def test_a_bad_email_adds_nothing_and_the_sheet_says_why():
    with _client() as c:
        r = c.post("/sources/add/boards", headers=HX, data={**FORM, "usajobs_email": "nope"})
    assert 'data-testid="add-error"' in r.text and "email address" in r.text
    assert KEY not in r.text and _sources() == {} and KEY not in _env()


def test_the_same_usajobs_search_is_not_added_twice():
    with _client() as c:
        c.post("/sources/add/boards", headers=HX, data=FORM)
        again = c.post("/sources/add/boards", headers=HX, data=FORM)
    assert "already one of your sources" in again.text and len(_sources()) == 1


def test_the_card_saves_key_and_email_and_updates_every_usajobs_search():
    with _client() as c:
        card = c.get("/sources").text.split('data-testid="usajobs-card"', 1)[1].split(
            "<!-- /usajobs -->", 1)[0]
        assert "Needs key and email" in card and "Get a free USAJobs key" in card
        assert 'type="password"' in card and "your search words and place" in card
        c.post("/sources/add/boards", headers=HX, data={
            "title": "Analyst", "board_usajobs": "1", "usajobs_email": "old@example.com"})
        assert c.post("/sources/usajobs/key", data={}).text.count("Paste your key or type") == 1
        r = c.post("/sources/usajobs/key", data={"api_key": KEY, "email": EMAIL})
        assert "Saved on this computer." in r.text and KEY not in r.text
        assert "Ready" in r.text and "Key saved: ********" in r.text
        bad = c.post("/sources/usajobs/key", data={"email": "x"})
        assert "does not look like an email" in bad.text
    assert next(iter(_sources().values())).config["contact_email"] == EMAIL
    assert f"USAJOBS_API_KEY={KEY}" in _env()


def test_a_source_row_without_a_key_says_so_plainly():
    from kestrel.web import source_view as sv

    async def go(s):
        src = Source(track="employment", kind="usajobs", name="Analysts", enabled=True,
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
    assert v.type_line == "USAJobs · Analyst"
