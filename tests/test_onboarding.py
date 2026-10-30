"""The onboarding wizard (D-UI-30, D-UI-155) and Settings > System with Restart onboarding
(D-UI-31).

Six steps: step persistence and resume (also from the old ten-step state), Back and Next, the model
step for Online with the provider mocked by respx, About you with the tracks, the resume skip, What
you want per track, the searches step listing the dry-run summaries, Edit coming back to the
searches, and Start searching enabling the generated sources, queuing a scan and opening Today;
the first-run redirect, /setup's redirects, the System tab, and both restart choices on the
per-test data home. Ollama is faked as down and the hardware as a 4080; no live network
anywhere."""

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
    m = re.search(r'id="ob-step"[^>]*data-step="(\w+)"', body)
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
    assert _step(body) == "welcome" and "Step 1 of 6" in body
    assert 'data-testid="ob-help"' in body and "What happens next" in body
    assert "never applies" in body.lower() or "Never applies" in body
    assert "Never logs in to a site as you" in body  # the spec's third never
    assert "you are not the product" in body
    assert 'data-testid="ob-back"' not in body and 'data-testid="ob-next"' in body
    assert "About 5 minutes" in body
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
    assert _step(r.text) == "models" and "Step 2 of 6" in r.text
    assert _state().step == "models" and "welcome" in _state().visited
    # The app is killed and started again on the same data home: same step.
    c2 = _app(wipe=False)
    body = c2.get("/onboarding").text
    assert _step(body) == "models"
    assert _progress(body)["welcome"] == "ok"
    assert "Picked up where you left off" in body


def test_the_old_ten_step_state_resumes_at_the_nearest_of_the_six():
    state = ob.OnboardingState.from_value({
        "step": "location", "visited": ["welcome", "tracks", "employment", "done", "zzz"],
        "skipped": ["consulting"], "completed": False,
    })
    assert state.step == "wants" and state.visited == ["welcome", "about", "wants", "searches"]
    assert state.skipped == ["wants"]
    assert ob.current_key("tracks", ("employment",)) == "about"
    assert [s.key for s in ob.steps_for(("employment", "consulting"))] == [
        "welcome", "models", "about", "resume", "wants", "searches"]


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
    for key in ("about", "resume", "wants", "searches"):
        _jump(key)
        body = c.get("/onboarding").text
        assert _step(body) == key
        assert 'data-testid="ob-back"' in body, key
        # D-UI-65: the step moves on through its one primary or a quiet Skip for now.
        assert ('data-testid="ob-next"' in body or 'data-testid="ob-skip"' in body
                or 'data-testid="ob-start-searching"' in body), key
        assert 'data-testid="ob-help"' in body, key


def test_every_wizard_step_has_one_primary_button_and_no_second_save():
    """D-UI-65: each step shows ONE primary button (a "Save ... and continue" where it saves),
    a quiet Skip for now where the step can wait, and never a second Save beside it."""
    c = _app()
    labels = {"welcome": "Get started", "models": "Save and continue", "about": "Save and continue",
              "wants": "Save and continue", "searches": "Start searching"}
    for key in ("welcome", "models", "about", "resume", "wants", "searches"):
        _jump(key)
        body = c.get("/onboarding").text
        assert _step(body) == key
        # Only the step's own area counts: the help cards and flyouts hold no buttons.
        area = body.split('class="ob-body"')[1].split("</aside>")[0] + body.split(
            'data-testid="ob-foot"')[1].split('<div class="ob-start"')[0]
        assert len(re.findall(r"k-btn--primary", area)) <= 1, key
        assert ">Save</button>" not in area, key
        if key in labels:
            assert f">{labels[key]}</button>" in area, key
    _jump("resume")
    body = c.get("/onboarding").text
    assert 'data-testid="ob-skip">Skip for now<' in body  # the only step that can wait
    for key in ("about", "wants", "models"):
        _jump(key)
        assert 'data-testid="ob-skip"' not in c.get("/onboarding").text, key


def test_the_primary_is_disabled_with_a_hint_until_the_step_has_what_it_needs():
    c = _app()
    _jump("models", visited=["welcome"])
    body = c.get("/onboarding").text
    assert re.search(r'data-testid="ob-next" disabled', body)
    assert "Choose how Kestrel should think first" in body
    _jump("about")
    body = c.get("/onboarding").text
    assert re.search(r'data-ob-gate="about" disabled', body) and "Add your first name first" in body
    _jump("wants")
    body = c.get("/onboarding").text
    assert re.search(r'data-ob-gate="wants" disabled', body) and "Add a job title first" in body


def test_skip_for_now_leaves_a_waiting_step_and_marks_it_skipped():
    c = _app()
    _jump("resume", visited=["welcome", "models", "about"])
    r = c.post("/onboarding/next", data={"step": "resume", "skip": "1"}, headers=HX)
    assert _step(r.text) == "wants"
    assert "resume" in _state().skipped and _progress(r.text)["resume"] == "skipped"
    assert ">Skipped<" in r.text
    # Your name cannot be skipped: the check still runs.
    _jump("about")
    r = c.post("/onboarding/next", data={"step": "about", "skip": "1"}, headers=HX)
    assert _step(r.text) == "about" and 'data-testid="ob-nav-problem"' in r.text


def test_progress_links_only_to_steps_already_reached():
    c = _app()
    _jump("about", visited=["welcome", "models"])
    body = c.get("/onboarding").text
    assert 'href="/onboarding?step=welcome"' in body
    assert 'href="/onboarding?step=searches"' not in body
    # Asking for a step not reached yet stays where the operator is.
    assert _step(c.get("/onboarding?step=searches").text) == "about"
    assert _step(c.get("/onboarding?step=welcome").text) == "welcome"


# ------------------------------------------------------------------------------ model step
def test_model_step_needs_a_choice_then_connects_hosted_with_a_mocked_verify(monkeypatch):
    monkeypatch.setenv("KESTREL_USE_SYSTEM_KEYS", "")
    c = _app()
    _jump("models", visited=["welcome"])
    body = c.get("/onboarding").text
    assert "Our pick: On this computer" in body and 'data-testid="ob-mode-hosted"' in body
    assert "Online model" in body and 'data-testid="ob-hardware"' in body
    r = c.post("/onboarding/next", data={"step": "models"}, headers=HX)
    assert _step(r.text) == "models" and 'data-testid="ob-nav-problem"' in r.text

    r = c.post("/onboarding/models/mode", data={"mode": "hosted"}, headers=HX)
    assert _step(r.text) == "models"
    assert 'data-testid="ob-byok"' in r.text and "Bring your own key" in r.text
    for url in ("openrouter.ai/settings/keys", "platform.openai.com/api-keys"):
        assert url in r.text
    assert _progress(r.text)["models"] == "todo"
    assert "No provider connected" in r.text
    # Online needs a connected key before Next, in the hint and on the server.
    assert "Connect your key first" in r.text
    r2 = c.post("/onboarding/next", data={"step": "models"}, headers=HX)
    assert _step(r2.text) == "models" and 'data-testid="ob-nav-problem"' in r2.text
    # The consent states what is sent, from the tier sends, never hand-written.
    sends = r.text.split('data-testid="ob-sends"')[1].split("</details>")[0]
    assert "Tier 1 sends" in sends and "Tier 2 sends" in sends
    assert "I understand what gets sent" in r.text

    # No acknowledgement: refused, nothing saved.
    r = c.post("/onboarding/models/connect",
               data={"provider": "openrouter", "api_key": "sk-or-test"}, headers=HX)
    assert 'data-testid="ob-connect-message"' in r.text and 'data-connect-ok="false"' in r.text
    assert "understand" in r.text.split('data-connect-ok="false"')[1][:400].lower()
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
    assert _step(r.text) == "about"


async def _rows(s) -> dict:
    return dict((await s.execute(select(Setting.key, Setting.value))).all())


def test_model_step_local_shows_ollama_and_the_build_steps():
    c = _app()
    _jump("models")
    r = c.post("/onboarding/models/mode", data={"mode": "local"}, headers=HX)
    assert 'data-testid="ob-local"' in r.text
    assert 'data-step="ollama"' in r.text and 'data-step="pull"' in r.text
    assert 'data-step="hosted"' not in r.text  # hosted is not part of a Local choice
    assert 'data-testid="ob-online-card"' not in r.text and 'data-testid="ob-flow"' not in r.text
    assert 'hx-post="/onboarding/models/action/pull"' in r.text
    assert "Local models not ready yet" in r.text
    # Local chosen is enough to move on; the models can finish later.
    assert _step(c.post("/onboarding/next", data={"step": "models"}, headers=HX).text) == "about"
    # The poll of a finished (or never started) action asks the whole step to reload.
    poll = c.get("/onboarding/models/local?poll=1")
    assert poll.headers.get("HX-Trigger") == "ob-refresh"


def test_both_shows_the_local_box_the_online_card_and_where_each_step_runs():
    c = _app()
    _jump("models")
    r = c.post("/onboarding/models/mode", data={"mode": "both"}, headers=HX)
    for tid in ("ob-local-card", "ob-online-card", "ob-flow"):
        assert f'data-testid="{tid}"' in r.text, tid
    assert "Quick sort" in r.text and "Deep read" in r.text
    # Both needs the key too before Next.
    assert "Connect your key first" in r.text
    mode_both = r.text.split('data-testid="ob-mode-both"')[0].rsplit("<button", 1)[-1]
    assert 'aria-checked="true"' in mode_both


def test_a_low_spec_machine_gets_the_warning_and_online_as_our_pick(monkeypatch):
    from kestrel.llm.profiles import Hardware

    weak = Hardware(os="windows", gpu_vendor=None, vram_gb=None, ram_gb=8)
    monkeypatch.setattr(profiles, "detect_hardware", lambda: weak)
    ss.forget_hardware()
    c = _app()
    _jump("models")
    body = c.get("/onboarding").text
    assert 'data-testid="low-spec-warning"' in body and "Our pick: Online model" in body
    assert "Not found" in body  # no graphics card found


# ------------------------------------------------------------------------------- about you
def test_about_you_asks_for_a_first_name_and_what_you_are_looking_for():
    c = _app()
    _jump("about", visited=["welcome", "models"])
    body = c.get("/onboarding").text
    assert _step(body) == "about" and "Step 3 of 6" in body
    for name in ("first_name", "last_name", "email", "location", "headline", "phone"):
        assert f'name="{name}"' in body, name
    assert "Required" in body and "Add more details" in body
    assert "Kestrel does not keep a LinkedIn link yet" in body  # no back end: shown disabled
    # A new install is jobs only (release concern 10): consulting is an opt-in.
    assert re.search(r'value="employment"\s+data-ob-track="jobs"[^>]*checked', body)
    assert not re.search(r'value="consulting"[^>]*checked', body)
    assert "Jobs only. The consulting section is left out." in body
    assert "independent consultant or contractor in your own field" in body  # plain words
    assert "CIO" not in body and "CTO" not in body  # no field assumed (release review 11)
    assert "Step 3 of 6" in body and 'data-ob-step="consulting"' not in body


def test_about_you_is_required_to_advance_and_saves_for_every_track():
    c = _app()
    _jump("about", visited=["welcome", "models"])
    r = c.post("/onboarding/next", data={"step": "about"}, headers=HX)
    assert _step(r.text) == "about" and "Add your first name" in r.text
    # Saved without a name: stays, says why, keeps what was typed.
    r = c.post("/onboarding/save/about",
               data={"email": "pat@example.com", "track": ["employment"]}, headers=HX)
    assert _step(r.text) == "about" and "Add your first name" in r.text
    assert 'value="pat@example.com"' in r.text
    # No track chosen: stays too.
    r = c.post("/onboarding/save/about", data={"first_name": "Pat"}, headers=HX)
    assert _step(r.text) == "about" and "Choose at least one" in r.text
    # Saved with a name and both tracks: the next step, About you ticked, both tracks written.
    r = c.post("/onboarding/save/about",
               data={"first_name": "Pat", "last_name": "Example", "email": "pat@example.com",
                     "location": "Columbus, OH", "headline": "Operations lead",
                     "track": ["employment", "consulting"]}, headers=HX)
    assert _step(r.text) == "resume" and _progress(r.text)["about"] == "ok"
    assert "Step 4 of 6" in r.text
    import yaml

    data = yaml.safe_load((paths.config_dir() / "profile.yml").read_text(encoding="utf-8"))
    for track in ("employment", "consulting"):
        assert data[track]["first_name"] == "Pat" and data[track]["last_name"] == "Example"
        assert data[track]["full_name"] == "Pat Example"
        assert data[track]["location"] == "Columbus, OH"
    assert _db(tracks_svc.load_enabled) == tracks_svc.BOTH
    assert "about" in _state().visited


def test_about_you_leaves_the_fields_it_does_not_ask_for_as_they_were():
    """A restart that kept the operator's details must not wipe must-have, work authorization
    or the sponsorship flag when the short About you form is saved again (D-UI-155)."""
    c = _app()
    _write_profile(
        "employment:\n  first_name: Pat\n  last_name: Example\n  must_have: [Mentoring]\n"
        "  must_avoid: [Night shifts]\n  work_auth: US citizen\n  requires_sponsorship: true\n"
    )
    _jump("about")
    r = c.post("/onboarding/save/about",
               data={"first_name": "Pat", "email": "pat@example.com", "track": ["employment"]},
               headers=HX)
    assert _step(r.text) == "resume"
    import yaml

    block = yaml.safe_load((paths.config_dir() / "profile.yml").read_text("utf-8"))["employment"]
    assert block["must_have"] == ["Mentoring"] and block["must_avoid"] == ["Night shifts"]
    assert block["work_auth"] == "US citizen" and block["requires_sponsorship"] is True
    assert block["email"] == "pat@example.com"
    assert block["last_name"] == "Example"  # not posted: kept


# ---------------------------------------------------------------------------------- resume
def test_resume_can_be_skipped_with_a_clear_note():
    c = _app()
    _jump("resume", visited=["welcome", "models", "about"])
    body = c.get("/onboarding").text
    assert 'data-testid="ob-resume-skip-note"' in body
    assert "No tailored resumes until you add one" in body
    assert 'hx-post="/api/documents/import/read"' in body and 'id="import-proposal"' in body
    # The footer always shows the primary, disabled until a resume exists (onboarding-mockup-locked
    # NEXTL[4], paintFoot), beside the quiet Skip for now.
    assert re.search(r'data-testid="ob-next" disabled', body) and 'data-testid="ob-skip"' in body
    for label in ("Choose a file", "Paste text", "Read my resume"):
        assert label in body, label
    r = c.post("/onboarding/next", data={"step": "resume"}, headers=HX)
    assert _step(r.text) == "resume" and "Add your resume first" in r.text  # not skippable by Next
    r = c.post("/onboarding/next", data={"step": "resume", "skip": "1"}, headers=HX)
    assert _step(r.text) == "wants"
    assert _progress(r.text)["resume"] == "skipped" and "resume" in _state().skipped


def test_an_imported_resume_shows_continue_and_ticks_the_step():
    c = _app()
    _db(lambda s: _add_master(s))
    _jump("resume", visited=["welcome", "models", "about"])
    body = c.get("/onboarding").text
    assert 'data-testid="ob-masters"' in body and "Imported resume" in body
    assert ">Save resume and continue</button>" in body and 'data-testid="ob-skip"' not in body
    assert _progress(body)["resume"] == "ok"
    r = c.post("/onboarding/next", data={"step": "resume"}, headers=HX)
    assert _step(r.text) == "wants"


async def _add_master(s):
    s.add(DocMaster(track="employment", name="Imported resume", doc_kind="resume"))


# ------------------------------------------------------------------------------- what you want
def _two_tracks_and_a_name(c, tracks=("employment",)):
    _db(lambda s: tracks_svc.save_enabled(s, list(tracks)))
    c.post("/onboarding/save/about", data={"first_name": "Pat", "track": list(tracks)},
           headers=HX)


def test_what_you_want_shows_a_section_per_track_in_use():
    c = _app()
    _two_tracks_and_a_name(c)
    _jump("wants")
    body = c.get("/onboarding").text
    assert 'data-testid="ob-sec-jobs"' in body and 'data-testid="ob-sec-where"' in body
    assert 'data-testid="ob-sec-consulting"' not in body
    for text in ("Job titles", "Pay minimum", "Work style",
                 "Keep roles a little under your minimum", "How far under", "Add more details",
                 "Deal-breakers", "Include remote roles", "matches places by name, not distance"):
        assert text in body, text
    # The same control as Profile: Exact is not on the switch's list, 2, 5, 10 and 15 are.
    assert re.search(r'name="comp_floor_margin_pct" value="5"', body)
    assert 'hx-get="/profile/title-suggestions"' in body  # the suggestions slot (D-TTL-3)
    _two_tracks_and_a_name(c, tracks=("employment", "consulting"))
    _jump("wants")
    body = c.get("/onboarding").text
    assert 'data-testid="ob-sec-consulting"' in body and "Day-rate minimum" in body
    assert "Project length" in body and "A longest length is not recorded yet" in body
    # Consulting only leaves the jobs section out.
    _db(lambda s: tracks_svc.save_enabled(s, ["consulting"]))
    body = c.get("/onboarding").text
    assert 'data-testid="ob-sec-jobs"' not in body and 'data-testid="ob-sec-consulting"' in body


def test_what_you_want_starts_blank_and_never_uses_example_values():
    c = _app()
    _two_tracks_and_a_name(c)
    _jump("wants")
    body = c.get("/onboarding").text
    assert "Chief Information Officer" not in body and "CIO" not in body
    assert not re.search(r'name="comp_floor_usd" inputmode="numeric" value="\d', body)


def test_saving_what_you_want_writes_the_terms_and_moves_on():
    c = _app()
    _two_tracks_and_a_name(c)
    _jump("wants")
    # Needs a title and a place or remote; the server says which.
    r = c.post("/onboarding/save/wants", data={"remote_ok": "on"}, headers=HX)
    assert _step(r.text) == "wants" and "Add a job title first" in r.text
    r = c.post("/onboarding/save/wants", data={"target_titles": "Operations Analyst"}, headers=HX)
    assert _step(r.text) == "wants" and "Add a place or turn on remote first" in r.text
    assert 'value="Operations Analyst"' not in r.text and "Operations Analyst" in r.text
    r = c.post(
        "/onboarding/save/wants",
        data={"target_titles": "Operations Analyst\nProcess Analyst", "comp_floor_usd": "65,000",
              "mode_remote": "on", "mode_hybrid": "on", "under": "on",
              "comp_floor_margin_pct": "10", "seniority_floor": "",
              "onsite_metros": "Austin, TX\nRound Rock, TX", "remote_ok": "on",
              "must_avoid": "Requires travel"},
        headers=HX,
    )
    assert _step(r.text) == "searches" and _progress(r.text)["wants"] == "ok"
    import yaml

    block = yaml.safe_load((paths.config_dir() / "profile.yml").read_text("utf-8"))["employment"]
    terms = block["employment_terms"]
    assert terms["target_titles"] == ["Operations Analyst", "Process Analyst"]
    assert terms["comp_floor_usd"] == 65000 and terms["work_modes"] == ["remote", "hybrid"]
    assert terms["comp_floor_margin_pct"] == 10
    geo = block["geo_rules"]
    assert [m["name"] for m in geo["onsite_metros"]] == ["Austin, TX", "Round Rock, TX"]
    assert geo["remote_ok"] is True and geo["remote_country_allowlist"] == ["US"]
    assert block["must_avoid"] == ["Requires travel"]
    # Saved again with the switch off: Exact (0), and the page reads it back.
    _jump("wants")
    r = c.post("/onboarding/save/wants",
               data={"target_titles": "Operations Analyst", "onsite_metros": "Austin, TX"},
               headers=HX)
    assert _step(r.text) == "searches"
    block = yaml.safe_load((paths.config_dir() / "profile.yml").read_text("utf-8"))["employment"]
    assert block["employment_terms"]["comp_floor_margin_pct"] == 0
    assert block["geo_rules"]["remote_ok"] is False
    _jump("wants")
    body = c.get("/onboarding").text
    assert "Operations Analyst" in body and "Austin, TX" in body


def test_consulting_terms_save_weeks_as_billed_days_and_keep_what_is_not_asked():
    c = _app()
    _write_profile(
        "consulting:\n  first_name: Pat\n  consulting_terms:\n    target_sectors: [Retail]\n"
        "    day_rate_target: 900\n    max_concurrent: 2\n"
    )
    _db(lambda s: tracks_svc.save_enabled(s, ["consulting"]))
    _jump("wants")
    r = c.post("/onboarding/save/wants",
               data={"service_lines": "Process audits\nInterim lead", "day_rate_floor": "650",
                     "engagement_weeks": "4", "excluded_sectors": "Gambling", "remote_ok": "on"},
               headers=HX)
    assert _step(r.text) == "searches"
    import yaml

    terms = yaml.safe_load((paths.config_dir() / "profile.yml").read_text("utf-8"))[
        "consulting"]["consulting_terms"]
    assert terms["service_lines"] == ["Process audits", "Interim lead"]
    assert terms["day_rate_floor"] == 650 and terms["min_engagement_days"] == 20
    assert terms["excluded_sectors"] == ["Gambling"]
    assert terms["target_sectors"] == ["Retail"] and terms["day_rate_target"] == 900
    _jump("wants")
    body = c.get("/onboarding").text
    assert 'value="4"' in body and "Process audits" in body


def test_a_step_with_unsaved_terms_cannot_be_passed_with_next():
    c = _app()
    _two_tracks_and_a_name(c)
    _jump("wants", visited=["welcome", "models", "about", "resume"])
    r = c.post("/onboarding/next", data={"step": "wants"}, headers=HX)
    assert _step(r.text) == "wants" and "example" in r.text


# ------------------------------------------------------------------------------ searches
def test_searches_step_says_which_step_to_fill_when_there_is_nothing():
    c = _app()
    _jump("searches")
    body = c.get("/onboarding").text
    assert 'data-testid="ob-no-searches"' in body
    assert 'data-testid="ob-search-gaps"' in body and "Add the jobs you want" in body
    assert re.search(r'data-testid="ob-start-searching" disabled', body)
    assert "Nothing is searching yet" in body and "LinkedIn is never searched" in body
    assert "Indeed and Glassdoor" in body and 'data-testid="ob-companies"' in body


def _ready_to_search(c):
    c.post("/onboarding/models/mode", data={"mode": "local"}, headers=HX)
    _db(lambda s: tracks_svc.save_enabled(s, ["employment"]))
    c.post("/onboarding/save/about", data={"first_name": "Pat", "track": ["employment"]},
           headers=HX)
    r = c.post("/onboarding/save/wants",
               data={"target_titles": "Chief Information Officer\nVP Technology",
                     "seniority_floor": "director", "onsite_metros": "Austin: Round Rock",
                     "remote_ok": "on"},
               headers=HX)
    assert _step(r.text) == "searches"


def test_start_searching_enables_generated_sources_queues_a_scan_and_opens_today():
    c = _app()
    _ready_to_search(c)
    body = c.get("/onboarding").text
    assert 'data-testid="ob-search-list"' in body
    assert "Jobs near Austin" in body and "within 50 miles of Austin" in body
    assert "Remote jobs" in body and "Start searching" in body
    assert "Nothing is searching yet" in body

    async def generated(s):
        rows = (await s.execute(select(Source).where(Source.origin == "generated"))).scalars()
        return [(r.name, r.enabled) for r in rows]

    before = _db(generated)
    assert before and not any(on for _n, on in before)  # nothing scrapes until the press
    r = c.post("/onboarding/searches/start", headers=HX)
    assert r.status_code == 200 and r.headers["HX-Redirect"] == "/?tour=1"
    assert all(on for _n, on in _db(generated))

    async def scrapes(s):
        return (await s.execute(
            select(func.count()).select_from(Task).where(Task.type == "scrape")
        )).scalar()

    assert _db(scrapes) == len(before)
    state = _state()
    assert state.completed and state.completed_at
    body = c.get("/onboarding").text
    assert _progress(body)["searches"] == "ok" and "Searching is on" in body
    # Next on this step is the same single control; without htmx it redirects.
    r = c.post("/onboarding/next", data={"step": "searches"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?tour=1"


def test_start_searching_with_nothing_to_search_stays_and_says_so():
    c = _app()
    _jump("searches")
    # The model step and your name are checked again on the server before scraping turns on.
    r = c.post("/onboarding/searches/start", headers=HX)
    assert _step(r.text) == "searches" and "Choose how Kestrel should think" in r.text
    c.post("/onboarding/models/mode", data={"mode": "local"}, headers=HX)
    r = c.post("/onboarding/searches/start", headers=HX)
    assert _step(r.text) == "searches" and "Add your first name" in r.text
    _db(lambda s: tracks_svc.save_enabled(s, ["employment"]))
    c.post("/onboarding/save/about", data={"first_name": "Pat", "track": ["employment"]},
           headers=HX)
    _jump("searches")
    r = c.post("/onboarding/searches/start", headers=HX)
    assert _step(r.text) == "searches" and "nothing to search yet" in r.text
    assert not _state().completed


def test_picked_up_where_you_left_off_is_only_for_a_resumed_page_load():
    c = _app()
    c.get("/onboarding")
    r = c.post("/onboarding/next", data={"step": "welcome"}, headers=HX)
    assert "ob-resumed" not in r.text and "Progress saves as you go" in r.text
    assert 'data-testid="ob-resumed"' in c.get("/onboarding").text


def test_the_online_consent_is_true_for_each_mode_and_the_resume_read_says_where_it_runs(
        monkeypatch):
    from kestrel.web.pages import documents

    c = _app()
    _jump("models")
    both = c.post("/onboarding/models/mode", data={"mode": "both"}, headers=HX).text
    sends = both.split('data-testid="ob-sends"')[1].split("</details>")[0]
    assert 'data-testid="ob-sends-both"' in sends and "stay on this computer" in sends
    assert "automatically" not in sends  # hosted mode's wording is not true for Both
    hosted = c.post("/onboarding/models/mode", data={"mode": "hosted"}, headers=HX).text
    assert "automatically" in hosted.split('data-testid="ob-sends"')[1].split("</details>")[0]
    _jump("resume")

    async def online():
        return "b/strong (hosted)"

    monkeypatch.setattr(documents, "_import_model_label", online)
    body = c.get("/onboarding").text
    assert 'data-testid="ob-import-where"' in body and "Reading runs online" in body
    assert "b/strong (hosted)" in body and "whole resume text is sent" in body

    async def local():
        return "m (local)"

    monkeypatch.setattr(documents, "_import_model_label", local)
    c.post("/onboarding/models/mode", data={"mode": "local"}, headers=HX)
    assert "Your resume does not leave it" in c.get("/onboarding").text


def test_edit_goes_to_the_step_and_comes_back_to_the_searches_after_the_save():
    c = _app()
    _ready_to_search(c)
    r = c.post("/onboarding/edit/wants", headers=HX)
    assert _step(r.text) == "wants" and _state().ret == "searches"
    assert "Save and go back to searches" in r.text and "Back to searches" in r.text
    r = c.post("/onboarding/save/wants",
               data={"target_titles": "Operations Analyst", "onsite_metros": "Austin: Round Rock",
                     "remote_ok": "on"},
               headers=HX)
    assert _step(r.text) == "searches" and _state().ret is None
    assert "Operations Analyst" in r.text
    # Back from an edit also returns to the searches, and an unknown step is refused.
    c.post("/onboarding/edit/resume", headers=HX)
    r = c.post("/onboarding/back", data={"step": "resume"}, headers=HX)
    assert _step(r.text) == "searches" and _state().ret is None
    assert c.post("/onboarding/edit/searches", headers=HX).status_code == 404


def test_the_page_uses_the_design_system_and_drops_the_legacy_sheet():
    from pathlib import Path

    c = _app()
    body = c.get("/onboarding").text
    assert "/static/css/kestrel.css" not in body and "/static/css/shell.css" in body
    assert "/static/css/pages/onboarding.css" in body and "/static/js/onboarding.js" in body
    static = Path(paths.web_static_dir())
    for rel in ("css/pages/onboarding.css", "js/onboarding.js"):
        text = (static / rel).read_text(encoding="utf-8")
        assert not re.search(r"#[0-9A-Fa-f]{3,8}\b", text), rel
        assert not re.search(r"\b(?:rgb|rgba|hsl|hsla)\(", text), rel
        assert "—" not in text
    templates = Path(paths.web_templates_dir())
    names = ["onboarding.html"] + [
        "partials/" + p.name for p in (templates / "partials").glob("onboarding_*.html")]
    for name in names:
        text = (templates / name).read_text(encoding="utf-8")
        assert "—" not in text, name
        for style in re.findall(r'style="([^"]*)"', text):
            assert "color" not in style and "background" not in style, (name, style)


# -------------------------------------------------------------------------- Settings > System
def test_system_tab_renders_with_restart_onboarding():
    c = _app()
    page = c.get("/settings").text
    assert 'data-testid="st-nav-computer"' in page and "restart" in page.lower()
    assert 'hx-get="/settings/computer/checklist"' in page
    assert "system: ['computer']" in c.get("/static/js/settings.js").text  # old #system link
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
    page = c.get("/onboarding").text
    assert 'value="Pat"' in page and 'value="Example"' in page


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
    assert r.status_code == 303 and "msg=reset_confirm_needed" in r.headers["location"]
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
    assert body.index('id="ob-connect-btn"') < body.index('data-testid="ob-connect-message"')
    assert body.index('data-testid="ob-key-links"') < body.index('data-testid="ob-connect-message"')
    status = _status_box(body)
    assert "ob-note--warn" not in status.split("|")[0] and "Hosted provider connected" in status
    assert "Tier 1 will use a/cheap and Tier 2 will use b/strong" in status
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


def test_a_refused_connect_keeps_what_was_ticked(monkeypatch):
    # Free-only without the training opt-in is refused. The redraw keeps the consent, the free
    # tick and an open "More options", so the form never looks as if the click did nothing.
    monkeypatch.setenv("KESTREL_USE_SYSTEM_KEYS", "")
    c = _app()
    _jump("models", visited=["welcome"])
    c.post("/onboarding/models/mode", data={"mode": "hosted"}, headers=HX)
    r = c.post("/onboarding/models/connect",
               data={"provider": "openrouter", "api_key": "sk-or-test-key", "ack": "on",
                     "free_only": "on"}, headers=HX)
    body = r.text
    assert 'data-connect-ok="false"' in body and "Allow providers that may train" in body
    assert re.search(r'id="ob-ack"[^>]*\bchecked', body)
    assert re.search(r'name="free_only"[^>]*\bchecked', body)
    assert not re.search(r'name="allow_training"[^>]*\bchecked', body)
    assert re.search(r'<details class="ob-details" data-ob-more open', body)
    assert not paths.dotenv_path().exists()
