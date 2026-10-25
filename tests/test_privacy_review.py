"""Privacy review fixes (D-UI-159): S1 Suggest again, S2 interview questions and story builds,
S3 run-now, S5 applications export, N1 photo size, N2 narrative model, N3 summary model, N4
diagnostics. Offline: scripted models, no network. B1 and S4 have their own files."""

from __future__ import annotations

import asyncio
import json
import struct
import threading
import zlib

import pytest
from sqlalchemy import func, select

from kestrel.config import HOSTED_TIER_SENDS, HostedModeError, get_settings
from kestrel.db.models import InterviewQuestionSet, Task, TitleSuggestion
from kestrel.interview import question_task as qt
from kestrel.services import title_suggest as ts
from tests.test_interview_questions import _apply, _count, _seed, _seed_web
from tests.test_title_suggestions import _Client, _db, _seed_master

MODEL = "vendor/model-a"


def _dest(model: str = MODEL) -> str:
    """The destination `_online` configures: the first external backend, with `model`."""
    name, backend = next((n, b) for n, b in get_settings().llm.backends.items() if b.external)
    return f"{name}|{backend.base_url}|{model}"


@pytest.fixture
def web(_isolate_data_home):
    """A TestClient over a freshly migrated app database."""
    from fastapi.testclient import TestClient

    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.db.engine import forget_engine, reset_engine
    from kestrel.web.app import build_app

    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()
    return TestClient(build_app(worker=False))


def _online(monkeypatch, *, tier="tier2", model=MODEL, employer="off"):
    """Settings as every ask-first surface reads them: `tier` on an online backend with Hosted
    mode acknowledged. employer: off | hidden | held (what confidential mode would do)."""
    from kestrel.services import setting_overrides as so

    orig = so.load_settings_with_db

    async def patched(session, **kw):
        try:
            base = await orig(session, **kw)
        except HostedModeError:
            base = await orig(session)
        st = base.model_copy(deep=True)
        hm = st.llm.hosted_mode
        hm.enabled = True
        hm.acknowledged_at = "2026-10-01T00:00:00Z"
        hm.acknowledged_terms = hm.disclosure_terms()
        ext = next(n for n, b in st.llm.backends.items() if b.external)
        st.llm.tiers[tier] = st.llm.tiers[tier].model_copy(update={"backend": ext, "model": model})
        return st

    async def state(_session, _settings):
        return employer

    monkeypatch.setattr(so, "load_settings_with_db", patched)
    monkeypatch.setattr("kestrel.web.routes_control.load_settings_with_db", patched)
    monkeypatch.setattr("kestrel.web.routes_pages.load_settings_with_db", patched)
    monkeypatch.setattr("kestrel.services.online_consent.employer_state", state)


def _tier2_line() -> str:
    first = HOSTED_TIER_SENDS["tier2"].split("{")[0]
    return first[:1].upper() + first[1:]


async def _tasks(sm, kind):
    async with sm() as s:
        return list((await s.execute(select(Task).where(Task.type == kind))).scalars())


# ------------------------------------------------------------------------------ S1
def test_suggest_again_online_asks_first_and_the_server_rechecks(web, monkeypatch):
    async def seed(sm):
        await _seed_master(sm)

    _db(seed)
    _online(monkeypatch)
    page = web.get("/profile/title-suggestions").text
    assert 'data-testid="title-suggest-ask"' in page and MODEL in page
    assert _tier2_line() in page.replace("&gt;", ">")  # from the acknowledged disclosure
    assert "your resume text" in page and 'name="confirmed"' in page

    async def n(sm):
        return len(await _tasks(sm, ts.TASK_TYPE))

    none = web.post("/title-suggestions/refresh")
    other = web.post("/title-suggestions/refresh", data={"confirmed": "1", "model": "x/y"})
    elsewhere = web.post("/title-suggestions/refresh", data={
        "confirmed": "1", "model": MODEL, "dest": f"other|https://elsewhere.example/v1|{MODEL}"})
    assert "Nothing was sent" in none.text and "Nothing was sent" in other.text
    assert "Nothing was sent" in elsewhere.text  # same model name, different service
    assert _db(n) == 0
    ok = web.post(
        "/title-suggestions/refresh", data={"confirmed": "1", "model": MODEL, "dest": _dest()}
    )
    assert "Queued" in ok.text and _db(n) == 1

    async def payload(sm):
        return (await _tasks(sm, ts.TASK_TYPE))[0].payload

    assert _db(payload)["consented_model"] == MODEL
    assert _db(payload)["consented_destination"] == _dest()


def test_suggest_again_is_held_in_confidential_mode_without_an_employer(web, monkeypatch):
    _db(_seed_master)
    _online(monkeypatch, employer="held")
    page = web.get("/profile/title-suggestions").text
    assert "This send will be held" in page
    r = web.post(
        "/title-suggestions/refresh", data={"confirmed": "1", "model": MODEL, "dest": _dest()}
    )
    assert "Nothing was sent" in r.text and "Choose your employer" in r.text


def test_suggest_again_stays_a_plain_button_on_a_local_model(web):
    _db(_seed_master)
    page = web.get("/profile/title-suggestions").text
    assert 'data-testid="title-suggest-ask"' not in page
    assert "Queued" in web.post("/title-suggestions/refresh").text


async def test_an_automatic_title_task_is_held_when_tier2_went_online(sm, monkeypatch):
    await _seed_master(sm)
    async with sm() as s:  # queued by the cadence while Tier 2 was local: no confirmation
        await ts.request_title_suggestions(s, "weekly")
        await s.commit()
    _online(monkeypatch)
    client = _Client(settings=get_settings())
    await ts.drain_title_suggestions(sm, client, get_settings())
    assert client.calls == []  # nothing was sent
    (task,) = await _tasks(sm, ts.TASK_TYPE)
    assert task.status == "done"
    async with sm() as s:
        assert (await s.execute(select(func.count()).select_from(TitleSuggestion))).scalar() == 0


async def test_a_confirmation_for_another_model_does_not_send(sm, monkeypatch):
    await _seed_master(sm)
    async with sm() as s:
        await ts.request_title_suggestions(s, "manual", consent={"consented_model": "old/model"})
        await s.commit()
    _online(monkeypatch)
    client = _Client(settings=get_settings())
    await ts.drain_title_suggestions(sm, client, get_settings())
    assert client.calls == []


# ------------------------------------------------------------------------------ S2
async def test_marking_applied_sends_nothing_while_tier2_is_online(sm, monkeypatch):
    _opp, pid = await _seed(sm)
    _online(monkeypatch)
    await _apply(sm, pid)
    assert await _count(sm, Task, Task.type == qt.TASK_TYPE) == 0


async def test_a_queued_question_task_is_held_if_tier2_went_online(sm, monkeypatch):
    _opp, pid = await _seed(sm)
    await _apply(sm, pid)  # queued while local
    assert await _count(sm, Task, Task.type == qt.TASK_TYPE) == 1
    _online(monkeypatch)
    client = _Client(settings=get_settings())
    await qt.drain_interview_questions(sm, client, get_settings())
    assert client.calls == [] and await _count(sm, InterviewQuestionSet) == 0
    assert (await _tasks(sm, qt.TASK_TYPE))[0].status == "done"


def test_regenerate_online_asks_first_and_the_server_rechecks(web, monkeypatch):
    opp, _pid = _seed_web("applied")
    _online(monkeypatch)
    page = web.get(f"/opportunities/{opp}").text
    assert 'data-testid="questions-ask"' in page and MODEL in page
    assert _tier2_line() in page.replace("&gt;", ">") and 'name="confirmed"' in page
    none = web.post(f"/opportunities/{opp}/questions/regenerate")
    other = web.post(f"/opportunities/{opp}/questions/regenerate",
                        data={"confirmed": "1", "model": "x/y"})
    assert "Nothing was sent" in none.text and "Nothing was sent" in other.text

    async def n(sm):
        return await _count(sm, Task, Task.type == qt.TASK_TYPE)

    assert _db(n) == 0
    ok = web.post(f"/opportunities/{opp}/questions/regenerate",
                     data={"confirmed": "1", "model": MODEL, "dest": _dest()})
    assert "Queued" in ok.text and _db(n) == 1


def test_building_stories_online_asks_first_and_the_server_rechecks(web, monkeypatch):
    _online(monkeypatch)
    page = web.get("/profile/title-suggestions").text  # any page: the route is the check
    assert page
    none = web.post("/stories/build")
    assert "Nothing was sent" in json.loads(none.headers["HX-Trigger"])["k-toast"]["message"]

    async def n(sm):
        return len(await _tasks(sm, "build_stories"))

    assert _db(n) == 0
    other = web.post("/profile/stories/build", data={"confirmed": "1", "model": "x/y"})
    assert "Nothing was sent" in other.text and _db(n) == 0
    ok = web.post("/stories/build", data={"confirmed": "1", "model": MODEL, "dest": _dest()})
    assert "Building stories" in json.loads(ok.headers["HX-Trigger"])["k-toast"]["message"]
    assert _db(n) == 1


async def test_a_queued_story_build_is_held_if_tier2_went_online(sm, monkeypatch):
    from kestrel.interview import stories as stories_mod
    from kestrel.interview import story_task

    async with sm() as s:
        assert await story_task.request_build(s)  # queued while local
    _online(monkeypatch)
    called = []

    async def fake(session, client, settings):
        called.append(1)

    monkeypatch.setattr(stories_mod, "consolidate_stories", fake)
    await story_task.drain_story_builds(sm, object(), get_settings())
    assert called == []
    assert (await _tasks(sm, story_task.TASK_TYPE))[0].status == "done"
    async with sm() as s:
        last = await story_task.last_build(s)
    assert last and last["ok"] is False and "held" in last["error"]


# ------------------------------------------------------------------------------ S3
def test_api_run_now_hosted_needs_the_confirmation(web, monkeypatch):
    r = web.post("/api/run/now?mode=hosted")
    assert r.status_code == 400 and "confirm" in r.json()["reason"]
    assert web.get("/api/status").json()["pending_mode"] is None
    r = web.post("/runs/now", data={"mode": "hosted"})
    assert r.status_code == 400 and "confirm" in r.text
    assert web.get("/api/status").json()["pending_mode"] is None


def test_api_run_now_hosted_is_held_in_confidential_mode_without_an_employer(web, monkeypatch):
    _online(monkeypatch, employer="held")
    approval = json.dumps({"tier2": [_dest()]})
    r = web.post("/api/run/now?mode=hosted&confirmed=1", data={"online_approval": approval})
    assert r.status_code == 409 and "no current employer" in r.json()["reason"]
    r = web.post(
        "/runs/now", data={"mode": "hosted", "confirmed": "1", "online_approval": approval}
    )
    assert r.status_code == 409 and "no current employer" in r.text
    assert web.get("/api/status").json()["pending_mode"] is None


def test_api_run_now_local_needs_no_confirmation(web):
    r = web.post("/api/run/now?mode=local")
    assert r.status_code == 200 and r.json() == {"queued": True, "mode": "local"}


# ------------------------------------------------------------------------------ S5
def test_applications_csv_is_refused_in_confidential_mode_without_an_employer(web):
    from tests.test_analytics_page import _write_setting

    _write_setting("confidential_mode", True)
    r = web.get("/analytics/export/applications.csv?track=employment")
    assert r.status_code == 409
    assert "Confidential mode is on but no employer is set" in r.text
    assert "text/csv" not in r.headers["content-type"]


def test_applications_csv_still_exports_when_confidential_mode_is_off(web):
    r = web.get("/analytics/export/applications.csv?track=employment")
    assert r.status_code == 200 and "text/csv" in r.headers["content-type"]


# ------------------------------------------------------------------------------ N1
def _bomb_png(width: int, height: int) -> bytes:
    """A few dozen bytes whose header declares a huge canvas: no pixel data at all."""
    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def test_a_small_file_declaring_a_huge_canvas_is_refused_before_decoding(_isolate_data_home):
    from kestrel.services import profile_photo as pp

    data = _bomb_png(30000, 30000)
    assert len(data) < 200
    with pytest.raises(pp.PhotoError, match="too large"):
        pp.save(data)
    assert pp.current() is None


def test_a_normal_photo_still_saves(_isolate_data_home):
    import io

    from PIL import Image

    from kestrel.services import profile_photo as pp

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(buf, "PNG")
    assert pp.save(buf.getvalue()).is_file()


async def test_the_photo_resize_runs_off_the_event_loop(_isolate_data_home, monkeypatch):
    from kestrel.services import profile_photo as pp

    seen = []
    monkeypatch.setattr(pp, "save", lambda data: seen.append(threading.get_ident()))
    await pp.save_async(b"x")
    assert seen and seen[0] != threading.get_ident()


def test_both_upload_routes_resize_through_the_thread_helper(web, monkeypatch):
    from kestrel.services import profile_photo as pp

    calls = []

    async def fake(data):
        calls.append(len(data))
        raise pp.PhotoError("stop here")

    monkeypatch.setattr(pp, "save_async", fake)
    web.post("/profile/photo", files={"photo": ("a.png", _bomb_png(2, 2), "image/png")})
    web.post("/settings/prefs/avatar/photo",
                files={"photo": ("a.png", _bomb_png(2, 2), "image/png")})
    assert len(calls) == 2


# ------------------------------------------------------------------------------ N2
def test_an_online_rewrite_needs_the_model_that_was_shown(web, monkeypatch):
    from tests.test_analytics_page import _hosted_tier2, _spy_summary

    _hosted_tier2(monkeypatch)
    calls = _spy_summary(monkeypatch)
    r = web.post("/analytics/narrative?track=employment&range=90",
                    data={"confirmed": "1"})  # confirmed, but no model posted
    assert "Nothing was sent" in r.text
    r = web.post("/analytics/narrative?track=employment&range=90",
                    data={"confirmed": "1", "model": "some/other-model"})
    assert "Nothing was sent" in r.text
    assert [c for c in calls if c is not None] == []
    dialog = web.get("/analytics/narrative/online?track=employment&range=90").text
    import html

    assert '"model": "vendor/model-a"' in html.unescape(dialog)


# ------------------------------------------------------------------------------ N3
async def test_a_summary_confirmation_is_bound_to_the_model_shown(sm, monkeypatch):
    from kestrel.research import org_summary as osum
    from tests.test_org_summary import _Client as SummaryClient
    from tests.test_org_summary import _org

    org_id = await _org(sm)
    shown = {"model": "vendor/model-a"}

    async def online(_session):
        return True

    async def model(_session):
        return shown["model"]

    monkeypatch.setattr(osum, "_runs_online", online)
    monkeypatch.setattr(osum, "_configured_model", model)
    async with sm() as s:
        assert await osum.refresh_summary(s, org_id)
        await s.commit()
    (task,) = await _tasks(sm, osum.TASK_TYPE)
    assert task.payload["confirmed_model"] == "vendor/model-a"
    shown["model"] = "vendor/model-b"  # the operator changed Tier 2 after the dialog

    async def acquire(session, org, settings):
        raise AssertionError("must not fetch or call a model")

    client = SummaryClient()
    await osum.drain_org_summaries(sm, client, get_settings(), acquire=acquire)
    assert client.calls == []
    async with sm() as s:
        assert await osum.is_held(s, org_id)


# ------------------------------------------------------------------------------ N4
async def test_diagnostics_is_conservative_when_confidential_mode_has_no_employer(
    _isolate_data_home,
):
    from kestrel.web.pages import about
    from tests.test_about_page import _seed as about_seed

    await about_seed(confidential=True)
    # remove the only thing that defines the employer map
    import sqlite3

    from kestrel import paths

    con = sqlite3.connect(paths.db_path())
    con.execute("DELETE FROM doc_section")
    con.commit()
    con.close()
    _settings, redaction, conservative = await about._effective()
    assert redaction is None and conservative is True


async def test_diagnostics_is_conservative_when_the_database_cannot_answer(monkeypatch):
    from sqlalchemy.exc import OperationalError

    from kestrel.web.pages import about

    def broken():
        raise OperationalError("select", {}, Exception("no db"))

    monkeypatch.setattr(about, "sessionmaker", broken)
    _settings, redaction, conservative = await about._effective()
    assert redaction is None and conservative is True


def test_a_conservative_bundle_leaves_out_the_quarantine_excerpts(_isolate_data_home):
    import io
    import zipfile

    from kestrel.services import diagnostics
    from tests import test_about_page as ap

    asyncio.run(ap._seed(confidential=True))
    from kestrel import paths

    out = paths.exports_dir()
    archive = diagnostics.build_bundle(get_settings(), out_dir=out, conservative=True)
    with zipfile.ZipFile(io.BytesIO(archive.read_bytes())) as z:
        raw = z.read("quarantine.json")
    quarantine = json.loads(raw)
    row = quarantine["rows"][0]
    assert row["raw_response"] == diagnostics.WITHHELD
    assert ap._EMPLOYER.encode() not in raw and ap._POSTING.encode() not in raw
    assert row["error"] == diagnostics.WITHHELD
