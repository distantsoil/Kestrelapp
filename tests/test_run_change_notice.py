"""Issue #103: a setting changed while a run works says so, with a Stop button, and the rail and
run card keep showing what the run is really using until it ends.

Invariants: the run is never re-pointed (a change applies to the next run, so a run confirmed for
an online model is not silently moved), the notice appears only while a run is working and only
once per distinct change, and Stop reuses /runs/stop (nothing new starts after it)."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db.engine import make_async_engine
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web import app as app_mod
from kestrel.web.app import build_app, set_paused
from tests import runs_seed

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

HX = {"HX-Request": "true"}


def _run(fn):
    async def go():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                out = await fn(s)
                await s.commit()
                return out
        finally:
            await engine.dispose()

    return asyncio.run(go())


@pytest.fixture
def working(monkeypatch):
    """An app whose worker is mid-run: an open run row, and the cycle's settings held. The mode a
    loop in an earlier test recorded is cleared (monkeypatch puts it back), so the card reads the
    settings this test holds."""
    monkeypatch.setitem(app_mod._run_mode, "mode", None)
    runs_seed.seed(active=True)
    with TestClient(build_app(worker=False)) as c:
        run_settings = _run(load_settings_with_db)
        app_mod._hold_run(run_settings)
        yield c, run_settings
    app_mod._release_run()
    set_paused(False)


@pytest.fixture
def idle():
    runs_seed.seed()
    app_mod._release_run()
    with TestClient(build_app(worker=False)) as c:
        yield c


def _notice(response) -> dict | None:
    header = response.headers.get("HX-Trigger")
    if not header:
        return None
    return json.loads(header).get("k-run-notice") if header.startswith("{") else None


def _speed(c, tier1: int, tier2: int = 2):
    return c.post("/models/speed", data={"tier1": str(tier1), "tier2": str(tier2)}, headers=HX)


def test_a_change_during_a_run_carries_the_notice_with_a_stop_button(working):
    c, run_settings = working
    got = _notice(_speed(c, 9))
    assert got is not None
    tier2 = run_settings.llm.tiers["tier2"].model
    assert got["message"] == (
        f"This run is still using {tier2} on this computer, as confirmed. Your change applies "
        "to the next run. Stop the run if you do not want it to continue."
    )
    assert got["undoUrl"] == "/runs/stop" and got["undoLabel"] == "Stop the run"


def test_the_notice_names_an_online_run_as_online(working):
    c, run_settings = working
    hosted = run_settings.model_copy(deep=True)
    hosted.llm.tiers["tier2"].backend = "frontier"
    hosted.llm.tiers["tier2"].model = "some/model:free"
    app_mod._hold_run(hosted)
    got = _notice(_speed(c, 9))
    assert "some/model:free online, as confirmed" in got["message"]


def test_no_notice_when_nothing_is_running(idle):
    assert _notice(_speed(idle, 9)) is None


def test_no_notice_when_the_save_changes_nothing_the_run_uses(working):
    c, run_settings = working
    same = _speed(c, run_settings.concurrency.tier1, run_settings.concurrency.tier2)
    assert _notice(same) is None


def test_the_notice_comes_once_per_change_and_again_for_a_different_one(working):
    c, run_settings = working
    assert _notice(_speed(c, 9)) is not None
    assert _notice(_speed(c, 9)) is None  # the same change, saved again: already said
    assert _notice(c.post("/runs/pause", headers=HX)) is None
    assert _notice(_speed(c, 11)) is None  # same field still differs: the same sentence
    changed_back = _speed(c, run_settings.concurrency.tier1, run_settings.concurrency.tier2)
    assert _notice(changed_back) is None
    assert _notice(_speed(c, 5)) is not None  # back to the run's own, then a new change


def test_the_notice_keeps_the_route_s_own_toast(working):
    c, _ = working
    r = c.post("/runs/start", headers=HX)  # a /runs/ path never carries the notice
    assert _notice(r) is None


def test_stop_ends_the_run_s_hold_and_nothing_more_is_announced(working):
    c, _ = working
    notice = _notice(_speed(c, 9))
    stop = c.post(notice["undoUrl"])
    assert stop.status_code == 204
    assert app_mod.confirmed_run_settings() is None
    assert _notice(_speed(c, 4)) is None


def _online_run(run_settings):
    """The settings of a run the person confirmed for an online model."""
    online = run_settings.model_copy(deep=True)
    online.llm.mode = "hosted"
    online.llm.tiers["tier2"].backend = "frontier"
    online.llm.tiers["tier2"].model = "some/model:free"
    online.llm.hosted_tiers["tier2"] = online.llm.tiers["tier2"].model_copy()
    return online


def test_the_rail_keeps_showing_the_online_run_after_the_person_switches_to_local(working):
    """The run was confirmed online; the stored choice is local. Until the run ends the rail says
    online (the model row and the mode), and its tooltip says the change waits for the next run."""
    c, run_settings = working
    app_mod._hold_run(_online_run(run_settings))
    assert run_settings.llm.mode == "local"  # what is stored
    api = c.get("/api/status").json()
    assert api["settings_mode"] == "hosted" and api["run_confirmed"]["diverged"] is True
    assert api["model"]["label"] == "Hosted mode · some/model:free"
    assert "some/model:free online" in api["run_confirmed"]["text"]
    rail = c.get("/status/rail").text
    assert "Your change applies to the next run" in rail
    # When the run ends the rail follows the stored choice again.
    app_mod._release_run()
    api = c.get("/api/status").json()
    assert api["settings_mode"] == "local" and api["run_confirmed"] is None
    assert "Hosted mode" not in (api["model"] or {}).get("label", "")


def test_the_run_card_keeps_the_runs_mode_until_it_ends(working):
    c, run_settings = working
    app_mod._hold_run(_online_run(run_settings))
    assert asyncio.run(_card_words(run_settings)) == "Online model, free"
    app_mod._release_run()
    assert asyncio.run(_card_words(run_settings)) == "Model on this computer"


async def _card_words(stored):
    from kestrel.web.pages.today import schedule_card

    engine = make_async_engine(paths.db_path())
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            return (await schedule_card(s, stored))["mode_words"]
    finally:
        await engine.dispose()


def test_only_a_save_from_the_page_carries_the_notice(working):
    """A request that is not the page's own htmx save (the tray, a script) gets no toast header,
    and neither does an answer that is an error."""
    c, _ = working
    assert _notice(c.post("/models/speed", data={"tier1": "9", "tier2": "2"})) is None
    refused = c.post("/models/change/zzz/undo", headers=HX)
    assert refused.status_code == 404 and _notice(refused) is None
    # The change is still unannounced: the next save from the page says so.
    assert _notice(_speed(c, 9)) is not None
