"""The onboarding wizard (D-UI-30) and Settings > System with Restart onboarding (D-UI-31).

Step persistence and resume, Back and Next, the model step for Hosted with the provider mocked
by respx, the tracks step removing the consulting step, About you required to advance, the
resume skip, the searches step listing the dry-run summaries and Start searching enabling the
generated sources and queuing a scan, the first-run redirect, /setup's redirects, the System
tab, and both restart choices on the per-test data home. Ollama is faked as down and the
hardware as a 4080; no live network anywhere."""

from __future__ import annotations

import asyncio
import re

import httpx
import pytest
import respx
from httpx import Response
from sqlalchemy import func, select

from kestrel import paths
from kestrel.db.models import DocMaster, Setting, Source, Task
from kestrel.llm import profiles
from kestrel.services import onboarding as ob
from kestrel.services import setup_state as ss
from kestrel.services import tracks as tracks_svc
from kestrel.services.reset import backup_dir
from tests.test_setup_page import HW_4080, FakeOllama

HX = {"HX-Request": "true"}
_OR = "https://openrouter.ai/api/v1"
_MODELS = {
    "data": [
        {
            "id": "a/cheap",
            "context_length": 32000,
            "pricing": {"prompt": "0.0000001", "completion": "0.0000002"},
            "supported_parameters": ["structured_outputs"],
        },
        {
            "id": "b/strong",
            "context_length": 128000,
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            "supported_parameters": ["structured_outputs"],
        },
    ]
}


@pytest.fixture(autouse=True)
def machine(monkeypatch):
    """A 4080 with Ollama down; any stray httpx.get is a test failure."""

    def no_network(*a, **k):
        raise AssertionError("network call from a test")

    monkeypatch.setattr(httpx, "get", no_network)
    monkeypatch.setattr(profiles, "detect_hardware", lambda: HW_4080)
    monkeypatch.setattr(ss, "OllamaClient", FakeOllama)
    FakeOllama.state = {"up": False, "tags": []}
    ss.forget_hardware()
    ss.reset_actions()
    tracks_svc.forget_cache()
    yield
    ss.reset_actions()
    ss.forget_hardware()
    tracks_svc.forget_cache()


def _app(*, wipe: bool = True):
    from starlette.testclient import TestClient

    from kestrel.db import migrate
    from kestrel.db.engine import reset_engine
    from kestrel.web.app import build_app

    if wipe:
        for suffix in ("", "-wal", "-shm"):
            paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations(paths.db_path())
    asyncio.run(reset_engine())
    return TestClient(build_app(worker=False))


def _db(fn):
    """Run fn(session) on a private engine against the per-test database."""

    async def _go():
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from kestrel.db.engine import make_async_engine

        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                out = await fn(s)
                await s.commit()
                return out
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _jump(key: str, **fields) -> None:
    """Put the wizard on a step directly, as a resumed session would find it."""

    async def go(s):
        state = await ob.load(s) or ob.OnboardingState()
        state.step = key
        for k, v in fields.items():
            setattr(state, k, v)
        await ob.save(s, state)

    _db(go)


def _state() -> ob.OnboardingState | None:
    return _db(ob.load)


def _step(body: str) -> str:
    m = re.search(r'id="ob-step" data-step="(\w+)"', body)
    assert m, body[:2000]
    return m.group(1)


def _progress(body: str) -> dict[str, str]:
    return dict(re.findall(r'data-ob-step="(\w+)" data-status="(\w+)"', body))


def _write_profile(text: str) -> None:
    p = paths.config_dir() / "profile.yml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# ----------------------------------------------------------------------- first run, redirects
def test_fresh_install_opens_the_onboarding(monkeypatch):
    from kestrel.web.pages import setup as setup_page

    monkeypatch.setattr(setup_page, "FIRST_RUN_REDIRECT", True)
    c = _app()
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/onboarding"
    body = c.get("/onboarding").text
    assert _step(body) == "welcome" and "Step 1 of 9" in body
    assert 'data-testid="ob-help"' in body and "What happens next" in body
    assert "never applies" in body.lower() or "never applies" in body
    assert 'data-testid="ob-back"' not in body and 'data-testid="ob-next"' in body
    assert "—" not in body  # no em dashes in UI copy


def test_onboarded_install_is_not_redirected(monkeypatch):
    from kestrel.web.pages import setup as setup_page

    monkeypatch.setattr(setup_page, "FIRST_RUN_REDIRECT", True)
    c = _app()
    c.post("/setup/mode", data={"mode": "local"})
    _write_profile("employment: {full_name: Pat Example}\n")
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 200 and "<title>Today" in r.text
    assert 'id="finish-setup"' not in r.text
    # /setup goes to the System tab for an onboarded install.
    r = c.get("/setup", follow_redirects=False)
    assert r.headers["location"] == "/settings#system"


def test_unfinished_onboarding_is_not_redirected_but_gets_the_banner(monkeypatch):
    from kestrel.web.pages import setup as setup_page

    monkeypatch.setattr(setup_page, "FIRST_RUN_REDIRECT", True)
    c = _app()
    _write_profile("employment: {full_name: Pat Example}\n")  # not fresh: a profile exists
    body = c.get("/").text
    assert "<title>Today" in body and 'href="/onboarding"' in body and "Finish setup" in body
    assert c.get("/setup", follow_redirects=False).headers["location"] == "/onboarding"


# -------------------------------------------------------------------------- persistence, nav
def test_step_persists_and_resumes_after_a_restart():
    c = _app()
    c.get("/onboarding")
    r = c.post("/onboarding/next", data={"step": "welcome"}, headers=HX)
    assert _step(r.text) == "models" and "Step 2 of 9" in r.text
    assert _state().step == "models" and "welcome" in _state().visited
    # The app is killed and started again on the same data home: same step.
    c2 = _app(wipe=False)
    body = c2.get("/onboarding").text
    assert _step(body) == "models"
    assert _progress(body)["welcome"] == "ok"


def test_back_and_next_on_every_step():
    c = _app()
    _jump("models")
    body = c.get("/onboarding").text
    assert 'data-testid="ob-back"' in body and 'data-testid="ob-next"' in body
    r = c.post("/onboarding/back", data={"step": "models"}, headers=HX)
    assert _step(r.text) == "welcome" and _state().step == "welcome"
    # Without htmx Back and Next answer 303 to /onboarding, which shows the stored step.
    r = c.post("/onboarding/next", data={"step": "welcome"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/onboarding"
    assert _state().step == "models"
    for key in ("tracks", "resume", "searches", "done"):
        _jump(key)
        body = c.get("/onboarding").text
        assert _step(body) == key
        assert 'data-testid="ob-back"' in body, key
        # D-UI-65: the step moves on through its one primary or a quiet Skip for now.
        assert 'data-testid="ob-next"' in body or 'data-testid="ob-skip"' in body, key
        assert 'data-testid="ob-help"' in body, key


def test_every_wizard_step_has_one_primary_button_and_no_second_save():
    """D-UI-65: each step shows ONE primary button (a "Save ... and continue" where it saves),
    a quiet Skip for now where the step can wait, and never a second Save beside it."""
    import re

    c = _app()
    keys = ("welcome", "models", "tracks", "about", "resume", "employment", "location",
            "searches", "done")
    for key in keys:
        _jump(key)
        body = c.get("/onboarding").text
        assert _step(body) == key
        main = body.split('class="ob-main"')[1].split('class="ob-help')[0]
        primaries = re.findall(r'class="btn primary"', main)
        assert len(primaries) <= 1, (key, len(primaries))
        assert ">Save</button>" not in main, key
        if key in ("employment", "location"):
            assert 'data-testid="ob-skip">Skip for now<' in main, key
            label = {"employment": "Save job terms and continue",
                     "location": "Save places and continue"}[key]
            assert f">{label}</button>" in main, key
    # About you: the primary waits for a name, and says so.
    _jump("about")
    about = c.get("/onboarding").text
    assert ':disabled="!named"' in about and "Add your full name first" in about
    assert 'data-testid="ob-skip"' not in about.split('class="ob-main"')[1].split("ob-help")[0]


def test_skip_for_now_leaves_a_waiting_step_and_marks_it_skipped():
    c = _app()
    _jump("employment", visited=["welcome", "models", "tracks", "about", "resume"])
    r = c.post("/onboarding/next", data={"step": "employment", "skip": "1"}, headers=HX)
    assert _step(r.text) == "location"
    assert "employment" in _state().skipped
    # Your name cannot be skipped: the check still runs.
    _jump("about")
    r = c.post("/onboarding/next", data={"step": "about", "skip": "1"}, headers=HX)
    assert _step(r.text) == "about" and 'data-testid="ob-nav-problem"' in r.text


def test_progress_links_only_to_steps_already_reached():
    c = _app()
    _jump("tracks", visited=["welcome", "models"])
    body = c.get("/onboarding").text
    assert 'href="/onboarding?step=welcome"' in body
    assert 'href="/onboarding?step=searches"' not in body
    # Asking for a step not reached yet stays where the operator is.
    assert _step(c.get("/onboarding?step=searches").text) == "tracks"
    assert _step(c.get("/onboarding?step=welcome").text) == "welcome"


# ------------------------------------------------------------------------------ model step
def test_model_step_needs_a_choice_then_connects_hosted_with_a_mocked_verify(monkeypatch):
    monkeypatch.setenv("KESTREL_USE_SYSTEM_KEYS", "")
    c = _app()
    _jump("models", visited=["welcome"])
    body = c.get("/onboarding").text
    assert "We recommend Local here" in body and 'data-testid="ob-mode-hosted"' in body
    r = c.post("/onboarding/next", data={"step": "models"}, headers=HX)
    assert _step(r.text) == "models" and 'data-testid="ob-nav-problem"' in r.text

    r = c.post("/onboarding/models/mode", data={"mode": "hosted"}, headers=HX)
    assert _step(r.text) == "models"
    assert 'data-testid="ob-byok"' in r.text and "Bring your own key" in r.text
    for url in ("openrouter.ai/settings/keys", "platform.openai.com/api-keys"):
        assert url in r.text
    assert _progress(r.text)["models"] == "todo"
    assert "No provider connected" in r.text

    # No acknowledgement: refused, nothing saved.
    r = c.post("/onboarding/models/connect",
               data={"provider": "openrouter", "api_key": "sk-or-test"}, headers=HX)
    assert "I understand" in r.text and 'data-testid="ob-connect-message"' in r.text
    assert not paths.dotenv_path().exists()

    with respx.mock as router:
        router.get(f"{_OR}/key").mock(
            return_value=Response(200, json={"data": {"label": "k", "is_free_tier": False}})
        )
        router.get(f"{_OR}/models").mock(return_value=Response(200, json=_MODELS))
        r = c.post(
            "/onboarding/models/connect",
            data={"provider": "openrouter", "api_key": "sk-or-test-key", "ack": "on"},
            headers=HX,
        )
    assert "Connected to OpenRouter" in r.text
    assert _progress(r.text)["models"] == "ok"  # the indicator moved with the save
    assert "Hosted provider connected" in r.text
    rows = _db(lambda s: _rows(s))
    assert rows["llm.mode"] == "hosted" and rows["setup.run_choice"] == "hosted"
    assert "OPENROUTER_API_KEY" in paths.dotenv_path().read_text(encoding="utf-8")
    r = c.post("/onboarding/next", data={"step": "models"}, headers=HX)
    assert _step(r.text) == "tracks"


async def _rows(s) -> dict:
    return dict((await s.execute(select(Setting.key, Setting.value))).all())


def test_model_step_local_shows_ollama_and_the_build_steps():
    c = _app()
    _jump("models")
    r = c.post("/onboarding/models/mode", data={"mode": "local"}, headers=HX)
    assert 'data-testid="ob-local"' in r.text
    assert 'data-step="ollama"' in r.text and 'data-step="pull"' in r.text
    assert 'data-step="hosted"' not in r.text  # hosted is not part of a Local choice
    assert 'hx-post="/onboarding/models/action/pull"' in r.text
    assert "Local models not ready yet" in r.text
    # Local chosen is enough to move on; the models can finish later.
    assert _step(c.post("/onboarding/next", data={"step": "models"}, headers=HX).text) == "tracks"
    # The poll of a finished (or never started) action asks the whole step to reload.
    poll = c.get("/onboarding/models/local?poll=1")
    assert poll.headers.get("HX-Trigger") == "ob-refresh"


# ------------------------------------------------------------------------------ tracks step
def test_employment_only_removes_the_consulting_step():
    c = _app()
    _jump("tracks")
    body = c.get("/onboarding").text
    # A new install is employment only (release concern 10): consulting is an opt-in.
    assert 'data-ob-step="consulting"' not in body and "Step 3 of 9" in body
    assert re.search(r'value="employment"\s+checked', body)
    assert not re.search(r'value="consulting"\s+checked', body)
    assert "independent consultant or contractor in your own field" in body  # plain words
    assert "CIO" not in body and "CTO" not in body  # no field assumed (release review 11)
    r = c.post("/onboarding/next", data={"step": "tracks"}, headers=HX)
    assert _step(r.text) == "tracks" and "Choose at least one" in r.text
    r = c.post("/onboarding/next", data={"step": "tracks", "track": ["employment"]}, headers=HX)
    assert _step(r.text) == "about" and "Step 4 of 9" in r.text
    assert 'data-ob-step="consulting"' not in r.text
    assert _db(tracks_svc.load_enabled) == ("employment",)
    # Both again: the consulting step comes back.
    _jump("tracks")
    r = c.post("/onboarding/next", data={"step": "tracks",
                                        "track": ["employment", "consulting"]}, headers=HX)
    assert 'data-ob-step="consulting"' in r.text and "Step 4 of 10" in r.text


# ------------------------------------------------------------------------------- about you
def test_about_you_is_required_to_advance_and_saves_for_every_track():
    c = _app()
    _db(lambda s: tracks_svc.save_enabled(s, tracks_svc.BOTH))
    _jump("about")
    r = c.post("/onboarding/next", data={"step": "about"}, headers=HX)
    assert _step(r.text) == "about" and "Add your full name" in r.text
    body = c.get("/onboarding").text
    assert 'hx-post="/onboarding/save/about"' in body and "Save and continue" in body
    # Saved without a name and Save and continue: stays, says why.
    r = c.post("/onboarding/save/about", data={"email": "pat@example.com", "_next": "1"},
               headers=HX)
    assert _step(r.text) == "about" and "Add your full name" in r.text
    # Saved with a name (plain Save): the whole step re-renders with About you ticked.
    r = c.post("/onboarding/save/about", data={"full_name": "Pat Example",
                                              "email": "pat@example.com"}, headers=HX)
    assert _step(r.text) == "about" and _progress(r.text)["about"] == "ok"
    assert 'data-testid="profile-saved"' in r.text
    import yaml

    data = yaml.safe_load((paths.config_dir() / "profile.yml").read_text(encoding="utf-8"))
    assert data["employment"]["full_name"] == "Pat Example"
    assert data["consulting"]["full_name"] == "Pat Example"  # both tracks are in use
    r = c.post("/onboarding/save/about", data={"full_name": "Pat Example", "_next": "1"},
               headers=HX)
    assert _step(r.text) == "resume"


def test_resume_can_be_skipped_with_a_clear_note():
    c = _app()
    _jump("resume")
    body = c.get("/onboarding").text
    assert 'data-testid="ob-resume-skip-note"' in body and "cannot tailor documents" in body
    assert 'hx-post="/api/documents/import/read"' in body and 'id="import-proposal"' in body
    r = c.post("/onboarding/next", data={"step": "resume", "skip": "1"}, headers=HX)
    assert _step(r.text) == "employment"
    assert _progress(r.text)["resume"] == "skipped" and "resume" in _state().skipped


def test_term_steps_must_be_saved_before_moving_on():
    c = _app()
    _db(lambda s: tracks_svc.save_enabled(s, tracks_svc.BOTH))
    _jump("employment")
    r = c.post("/onboarding/next", data={"step": "employment"}, headers=HX)
    assert _step(r.text) == "employment" and "example" in r.text
    r = c.post(
        "/onboarding/save/employment",
        data={"target_titles": "Chief Information Officer", "seniority_floor": "director",
              "_next": "1"},
        headers=HX,
    )
    assert _step(r.text) == "consulting"  # both tracks are on in this test


# ------------------------------------------------------------------------------ searches
def test_searches_step_says_which_step_to_fill_when_there_is_nothing():
    c = _app()
    _jump("searches")
    body = c.get("/onboarding").text
    assert 'data-testid="ob-no-searches"' in body
    assert 'data-testid="ob-search-gaps"' in body and "Add the titles you want" in body
    assert 'data-testid="ob-start-searching"' not in body


def test_start_searching_enables_generated_sources_and_queues_a_scan():
    c = _app()
    _db(lambda s: tracks_svc.save_enabled(s, ["employment"]))
    _jump("about")
    c.post("/onboarding/save/about", data={"full_name": "Pat Example"}, headers=HX)
    c.post("/onboarding/save/employment",
           data={"target_titles": "Chief Information Officer\nVP Technology",
                 "seniority_floor": "director"}, headers=HX)
    c.post("/onboarding/save/location",
           data={"remote_ok": "on", "remote_country_allowlist": "US",
                 "onsite_metros": "Austin: Round Rock"}, headers=HX)
    _jump("searches")
    body = c.get("/onboarding").text
    assert 'data-testid="ob-search-list"' in body
    assert "Jobs near Austin" in body and "within 50 miles of Austin" in body
    assert "Remote jobs" in body and "Start searching" in body

    async def generated(s):
        rows = (await s.execute(select(Source).where(Source.origin == "generated"))).scalars()
        return [(r.name, r.enabled) for r in rows]

    before = _db(generated)
    assert before and not any(on for _n, on in before)  # nothing scrapes until the press
    r = c.post("/onboarding/searches/start", headers=HX)
    assert "Searching is on" in r.text and _progress(r.text)["searches"] == "ok"
    assert all(on for _n, on in _db(generated))

    async def scrapes(s):
        return (await s.execute(
            select(func.count()).select_from(Task).where(Task.type == "scrape")
        )).scalar()

    assert _db(scrapes) == len(before)
    r = c.post("/onboarding/next", data={"step": "searches"}, headers=HX)
    assert _step(r.text) == "done" and _state().completed
    r = c.post("/onboarding/next", data={"step": "done"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


# -------------------------------------------------------------------------- Settings > System
def test_system_tab_renders_with_restart_onboarding():
    c = _app()
    page = c.get("/settings").text
    assert "tab==='system'" in page and 'hx-get="/settings/system"' in page
    assert "'#system': 'system'" in page
    panel = c.get("/settings/system").text
    assert "1. How will Kestrel run?" in panel and 'data-step="ollama"' in panel
    assert 'data-testid="restart-onboarding"' in panel
    assert "Keep my details" in panel and "Clear everything and start over" in panel
    assert 'name="keep_keys" value="on" checked' in panel and 'name="confirm"' in panel
    nav = page.split('<aside class="side"')[1].split("</aside>")[0]
    assert 'href="/setup"' not in nav  # Setup left the sidebar


def _onboarded(c) -> None:
    c.post("/setup/mode", data={"mode": "local"})
    _write_profile("employment: {full_name: Pat Example, email: pat@example.com}\n")
    _jump("done", completed=True, visited=list(ob.STEP_KEYS))


def test_restart_keep_my_details_reopens_step_one_prefilled():
    c = _app()
    _onboarded(c)
    r = c.post("/onboarding/restart", data={"choice": "keep"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/onboarding"
    state = _state()
    assert state.step == "welcome" and not state.completed
    assert (paths.config_dir() / "profile.yml").exists()
    _jump("about")
    assert 'value="Pat Example"' in c.get("/onboarding").text


def _seed_operator_data(c) -> None:
    _onboarded(c)
    paths.dotenv_path().write_text("OPENROUTER_API_KEY=sk-or-keep-me\n", encoding="utf-8")

    async def add(s):
        s.add(Source(track="employment", kind="greenhouse", name="Mine", config={},
                     origin="custom", enabled=True))
        s.add(Task(type="scrape", payload={"source_id": 1}))
        s.add(DocMaster(track="employment", name="Imported resume", doc_kind="resume"))
        s.add(Setting(key="ui.scheme", value="teal"))

    _db(add)
    docs = paths.data_dir() / "documents"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "old.pdf").write_bytes(b"%PDF-1.4")


def _backups() -> set[str]:
    d = backup_dir()
    return {p.name for p in d.glob("*.zip")} if d.exists() else set()


def test_restart_clear_needs_reset_typed_then_clears_and_keeps_keys():
    from kestrel.web.app import is_paused

    c = _app()
    _seed_operator_data(c)
    r = c.post("/onboarding/restart", data={"choice": "clear", "confirm": "reset",
                                            "keep_keys": "on"}, follow_redirects=False)
    assert r.status_code == 303 and "Nothing%20was%20cleared" in r.headers["location"]
    assert (paths.config_dir() / "profile.yml").exists()  # nothing touched

    before = _backups()
    r = c.post("/onboarding/restart", data={"choice": "clear", "confirm": "RESET",
                                            "keep_keys": "on"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/onboarding"
    new = _backups() - before
    assert len(new) == 1  # a backup, written outside the data home
    assert paths.data_home() not in backup_dir().parents
    assert paths.dotenv_path().exists()  # keys kept
    assert not (paths.config_dir() / "profile.yml").exists()
    assert (paths.config_dir() / "settings.yml").exists()  # config re-seeded
    assert not (paths.data_dir() / "documents").exists()
    assert not is_paused()  # the worker is back the way it was

    async def counts(s):
        out = {}
        for model in (Task, DocMaster):
            out[model.__tablename__] = (
                await s.execute(select(func.count()).select_from(model))
            ).scalar()
        names = (await s.execute(select(Source.name, Source.origin))).all()
        out["custom"] = [n for n, o in names if o == "custom"]
        out["shipped"] = [n for n, o in names if o == "shipped"]
        out["settings"] = dict((await s.execute(select(Setting.key, Setting.value))).all())
        return out

    after = _db(counts)
    assert after["task"] == 0 and after["doc_master"] == 0 and after["custom"] == []
    assert after["shipped"]  # the shipped sources are seeded again, all off
    assert "llm.mode" not in after["settings"] and "ui.scheme" not in after["settings"]
    body = c.get("/onboarding").text
    assert _step(body) == "welcome"


def test_restart_clear_without_keep_keys_removes_the_env_file():
    c = _app()
    _seed_operator_data(c)
    r = c.post("/onboarding/restart", data={"choice": "clear", "confirm": "RESET"},
               follow_redirects=False)
    assert r.status_code == 303
    assert not paths.dotenv_path().exists()
    assert not (paths.config_dir() / "profile.yml").exists()
    # A fresh install again: / opens the wizard.
    assert _db(ob.is_fresh_install)


# ------------------------------------------------------ D-UI-40: an unambiguous Connect result
def _connect(c, router, key_status: int = 200, api_key: str = "sk-or-test-key"):
    router.get(f"{_OR}/key").mock(
        return_value=Response(key_status, json={"data": {"label": "k", "is_free_tier": False}})
    )
    router.get(f"{_OR}/models").mock(return_value=Response(200, json=_MODELS))
    return c.post(
        "/onboarding/models/connect",
        data={"provider": "openrouter", "api_key": api_key, "ack": "on"},
        headers=HX,
    )


def _status_box(body: str) -> str:
    m = re.search(r'<div class="([^"]+)" data-testid="ob-models-status"[^>]*>(.*?)</div>', body,
                  re.S)
    assert m, "no models status box"
    return m.group(1) + "|" + m.group(2)


def test_hosted_connect_success_is_one_clear_sentence_and_the_status_agrees(monkeypatch):
    from structlog.testing import capture_logs

    monkeypatch.setenv("KESTREL_USE_SYSTEM_KEYS", "")
    c = _app()
    _jump("models", visited=["welcome"])
    c.post("/onboarding/models/mode", data={"mode": "hosted"}, headers=HX)
    with capture_logs() as logs, respx.mock as router:
        r = _connect(c, router)
    body = r.text
    assert 'data-connect-ok="true"' in body
    assert ("Connected to OpenRouter. Tier 1 will use a/cheap and Tier 2 will use b/strong."
            in body)
    # The picks are named as defaults, with the way to override them (D-UI-42).
    assert 'data-testid="ob-connect-message-defaults"' in body
    assert 'href="/models#hosted-overrides"' in body
    # The result sits under the button that produced it, not above the key links.
    assert body.index("Check the key and connect") < body.index('data-testid="ob-connect-message"')
    status = _status_box(body)
    assert status.startswith("panel|") and "Hosted provider connected" in status
    assert "Tier 1 will use a/cheap and Tier 2 will use b/strong" in status
    assert "warnbox" not in status.split("|")[0]
    events = [e for e in logs if e.get("event") == "hosted_connect_result"]
    assert events and events[-1]["ok"] is True and events[-1]["tier1"] == "a/cheap"
    assert "sk-or-test-key" not in repr(logs)  # the key is never logged


def test_a_failed_connect_says_not_connected_and_the_status_names_the_earlier_one(monkeypatch):
    from structlog.testing import capture_logs

    monkeypatch.setenv("KESTREL_USE_SYSTEM_KEYS", "")
    c = _app()
    _jump("models", visited=["welcome"])
    c.post("/onboarding/models/mode", data={"mode": "hosted"}, headers=HX)
    with respx.mock as router:
        _connect(c, router)
    with capture_logs() as logs, respx.mock(assert_all_called=False) as router:
        r = _connect(c, router, key_status=401, api_key="sk-or-bad-key")
    body = r.text
    assert 'data-connect-ok="false"' in body and "Not connected to OpenRouter." in body
    assert "rejected the key" in body and "Connected to OpenRouter." not in body
    status = _status_box(body)
    assert "using the earlier connection; this attempt changed nothing" in status
    events = [e for e in logs if e.get("event") == "hosted_connect_result"]
    assert events and events[-1]["ok"] is False and events[-1]["reason"] == "key_rejected"
    assert "sk-or-bad-key" not in repr(logs)


def test_hosted_choice_never_probes_ollama(monkeypatch):
    from kestrel.config import get_settings

    def boom(*a, **k):
        raise AssertionError("Ollama probed under the Hosted choice")

    monkeypatch.setattr(ss, "probe_ollama", boom)
    st = ss.assess(get_settings(), {ss.CHOICE_KEY: "hosted"})
    assert st.choice == "hosted" and st.ollama_version is None
    with pytest.raises(AssertionError, match="Ollama probed"):
        ss.assess(get_settings(), {ss.CHOICE_KEY: "local"})
