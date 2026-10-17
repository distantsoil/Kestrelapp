"""Settings > System, formerly the Setup page (D-UI-3, D-UI-31): setup_state.assess() for each
machine state (Ollama down, models missing, everything present and verified, hosted-only), the
System panel and its cards, POST /setup/mode writing `llm.mode`, the pull and build buttons (task
rows run through a respx-mocked Ollama HTTP API, release round R1; tests/test_model_tasks.py covers
progress, resume and cancel in depth), the verify thread, and the Finish setup banner on / that
points to the onboarding until it is complete. OllamaClient is faked and httpx.get is poisoned:
no Ollama, no provider, no network."""

from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest
import respx
from sqlalchemy import select

from kestrel import doctor as doctor_mod
from kestrel import paths
from kestrel.config import get_settings
from kestrel.db.models import Setting
from kestrel.llm import profiles
from kestrel.services import model_tasks as mt
from kestrel.services import setup_state as ss

HW_4080 = profiles.Hardware(os="windows", gpu_vendor="nvidia", vram_gb=16.0, ram_gb=128.0)
BASE = ["qwen3:4b", "qwen3:14b", "nomic-embed-text"]
DERIVED = [f"kestrel-{t}" for t in profiles.TIERS]


class FakeOllama:
    """Stands in for kestrel.llm.client.OllamaClient; never touches the network."""

    state: dict = {"up": False, "tags": []}

    def __init__(self, *args, **kwargs) -> None:
        pass

    def version(self, timeout: float = 5.0) -> str:
        if not self.state["up"]:
            raise httpx.ConnectError("refused")
        return "0.9.0"

    def list_models(self, timeout: float = 10.0) -> list[str]:
        return list(self.state["tags"])


@pytest.fixture
def quiet(monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("network call from a test")

    monkeypatch.setattr(httpx, "get", no_network)
    monkeypatch.setattr(profiles, "detect_hardware", lambda: HW_4080)
    monkeypatch.setattr(ss, "OllamaClient", FakeOllama)
    monkeypatch.setattr(mt, "RETRY_BACKOFF_S", 0.0)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENMODEL_API_KEY", raising=False)
    FakeOllama.state = {"up": False, "tags": []}
    ss.forget_hardware()
    ss.reset_actions()
    mt.reset()
    yield
    act = ss.running_action()
    if act is not None:
        act.join(5.0)
    ss.reset_actions()
    mt.reset()
    ss.forget_hardware()


def _drain() -> int:
    """Run every queued pull and build against the (respx-mocked) Ollama, as the setup loop on
    the app's event loop would; returns how many tasks it took."""

    async def _go():
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from kestrel.db.engine import make_async_engine

        engine = make_async_engine(paths.db_path())
        try:
            sm = async_sessionmaker(engine, expire_on_commit=False)
            n = 0
            while await mt.drain_once(sm, get_settings().llm.ollama_native_url):
                n += 1
            return n
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _stream(*objs: dict) -> httpx.Response:
    return httpx.Response(200, content="\n".join(json.dumps(o) for o in objs) + "\n")


def _without_details(html: str) -> str:
    return re.sub(r"<details\b.*?</details>", "", html, flags=re.DOTALL)


PULL_OK = (
    {"status": "pulling manifest"},
    {"status": "pulling abc", "digest": "sha256:abc", "total": 1000, "completed": 120},
    {"status": "pulling abc", "digest": "sha256:abc", "total": 1000, "completed": 1000},
    {"status": "verifying sha256 digest"},
    {"status": "writing manifest"},
    {"status": "success"},
)


def _ollama(up: bool, tags: list[str]) -> None:
    FakeOllama.state = {"up": up, "tags": list(tags)}


def _step(state: ss.SetupState, key: str) -> ss.StepState:
    return next(s for s in state.steps if s.key == key)


def _fresh_app():
    from starlette.testclient import TestClient

    from kestrel.db import migrate
    from kestrel.db.engine import reset_engine
    from kestrel.web.app import build_app

    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations(paths.db_path())
    asyncio.run(reset_engine())
    return TestClient(build_app(worker=False))


def _db_rows() -> dict:
    async def _go():
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from kestrel.db.engine import make_async_engine

        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine)() as s:
                return dict((await s.execute(select(Setting.key, Setting.value))).all())
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _pass_report() -> doctor_mod.DoctorReport:
    rep = doctor_mod.DoctorReport()
    for name in ss.VERIFY_ROWS:
        rep.add(name, doctor_mod.PASS, "fine")
    rep.add("Profile configured", doctor_mod.WARN, "no profile.yml", "copy it")
    return rep


# ------------------------------------------------------------------------------- 1. assess()
def test_assess_with_ollama_down(quiet):
    st = ss.assess(get_settings(), {})
    assert st.hardware_line == "windows, NVIDIA GPU 16 GB VRAM, 128 GB RAM"
    assert st.recommended == "gpu_16gb" and st.profile == "gpu_16gb" and not st.profile_recorded
    assert st.choice == "local" and not st.choice_explicit and st.first_run
    assert st.ollama_version is None
    ollama = _step(st, "ollama")
    # App first (release round R1): a Download Ollama link and a Check again button; the
    # commands stay on the step for Show details only.
    assert ollama.status == "warn" and ollama.action == "check"
    assert ollama.link == "https://ollama.com/download/windows" and ollama.link_external
    assert ollama.link_label == "Download Ollama" and ollama.action_label == "Check again"
    assert ollama.commands == ["winget install --id Ollama.Ollama -e", "ollama serve"]
    pull = _step(st, "pull")
    assert pull.status == "now" and pull.action == "pull" and not pull.action_enabled
    assert pull.detail == "needs Ollama"
    build = _step(st, "build")
    assert build.action == "build" and not build.action_enabled
    assert _step(st, "hosted").pill == "optional"
    # Who the operator is belongs to the onboarding now (D-UI-31): no profile step here.
    assert [s.number for s in st.steps] == [2, 3, 4, 5, 6]
    assert "who" not in {s.key for s in st.steps}


def test_assess_with_models_missing(quiet):
    _ollama(True, ["qwen3:4b"])
    st = ss.assess(get_settings(), {})
    assert st.ollama_version == "0.9.0"
    assert _step(st, "ollama").status == "ok"
    assert st.missing_tags == ["qwen3:14b", "nomic-embed-text"]
    pull = _step(st, "pull")
    assert pull.status == "now" and pull.action_label == "Pull 2 models" and pull.action_enabled
    assert pull.detail == "1 of 3 present"
    assert pull.commands == ["ollama pull qwen3:14b", "ollama pull nomic-embed-text"]
    assert "About 9.6 GB" in pull.explanation
    build = _step(st, "build")
    assert build.status == "todo" and not build.action_enabled  # bases first
    assert st.missing_derived == DERIVED


def test_assess_with_everything_present_and_verified(quiet):
    _ollama(True, BASE + DERIVED)
    profiles.save_active_profile("gpu_16gb", "NVIDIA GPU with 16 GB VRAM", HW_4080)
    st = ss.assess(get_settings(), {"llm.mode": "local"})
    assert st.profile_recorded and not st.first_run
    assert _step(st, "pull").status == "ok" and _step(st, "pull").detail == "3 of 3 present"
    build = _step(st, "build")
    assert build.status == "now" and build.action == "verify" and build.detail == "not verified yet"
    assert build.commands == ["kestrel doctor"]

    ss.save_report(_pass_report())
    st = ss.assess(get_settings(), {"llm.mode": "local"})
    build = _step(st, "build")
    assert build.status == "ok" and build.detail.startswith("verified ")
    assert build.action == "verify" and build.action_label == "Verify again"
    assert _step(st, "hosted").status == "now"  # optional, but the next thing with a link

    failed = _pass_report()
    failed.add("needle", doctor_mod.FAIL, "lost at 12000 tokens", "rebuild", blocking=True)
    failed.results = [r for r in failed.results if not (r.name == "needle" and r.status == "PASS")]
    ss.save_report(failed)
    st = ss.assess(get_settings(), {})
    assert st.verified == "failed" and _step(st, "build").status == "warn"
    assert "needle failed" in _step(st, "build").detail

    (paths.config_dir() / "profile.yml").write_text("employment: {}\n", encoding="utf-8")
    st = ss.assess(get_settings(), {})
    assert st.profile_yml and "who" not in {s.key for s in st.steps}


def _hosted_settings(monkeypatch, *, ack: bool = True, key: bool = True):
    if key:
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    s = get_settings()
    s.llm.hosted_mode.enabled = ack
    s.llm.hosted_mode.acknowledged_at = "2026-10-12T10:00:00+00:00" if ack else None
    s.llm.hosted_mode.acknowledged_terms = s.llm.hosted_mode.disclosure_terms() if ack else None
    if ack:
        for tier in ("tier1", "tier2"):
            s.llm.hosted_tiers[tier] = s.llm.tiers["frontier"].model_copy()
    return s


def test_assess_hosted_only(quiet, monkeypatch):
    s = _hosted_settings(monkeypatch)
    st = ss.assess(s, {"llm.mode": "hosted", "setup.run_choice": "hosted"})
    assert st.choice == "hosted" and st.choice_explicit and not st.local_needed
    assert st.ollama_version is None  # Ollama down and nobody cares
    for key in ("ollama", "profile", "pull", "build"):
        step = _step(st, key)
        assert step.status == "skip" and step.pill == "skipped: Hosted"
        assert step.action is None
    hosted = _step(st, "hosted")
    assert hosted.status == "ok" and hosted.pill == "ok"
    assert hosted.link == "/settings#hosted"
    assert st.hosted_keys == {
        "frontier": True,
        "openmodel": False,
        "anthropic": False,
        "openai": False,
    }


HW_LAPTOP = profiles.Hardware(os="windows", gpu_vendor=None, vram_gb=None, ram_gb=8.0)


def test_local_on_a_no_gpu_machine_falls_back_to_the_cpu_profile(quiet, monkeypatch):
    """Hosted is the recommendation, but the operator chose Local: the page must not drive a
    local run into the hosted profile (nothing to pull, a step text starting with '. ')."""
    monkeypatch.setattr(profiles, "detect_hardware", lambda: HW_LAPTOP)
    ss.forget_hardware()
    for choice in ("local", "both"):
        st = ss.assess(get_settings(), {"setup.run_choice": choice})
        assert st.recommended == "hosted_only"  # the card row still says Hosted is recommended
        assert st.profile == "cpu_8gb" and not st.profile_recorded
        assert st.profile_reason == ss.LOCAL_FALLBACK_REASON
        prof = _step(st, "profile")
        assert prof.title == "Hardware profile: cpu_8gb"
        assert not prof.explanation.startswith(".")
        assert "Hosted is recommended" in prof.explanation
        pull = _step(st, "pull")
        assert pull.action_label.startswith("Pull ") and "Pull 0" not in pull.action_label
        assert st.base_tags and st.missing_tags == st.base_tags
    # Under Hosted the recommendation stands.
    st = ss.assess(get_settings(), {"setup.run_choice": "hosted"})
    assert st.profile == "hosted_only"


def test_recorded_hosted_profile_under_local_says_nothing_to_pull(quiet):
    profiles.save_active_profile("hosted_only", "chosen on the Setup page", HW_4080)
    st = ss.assess(get_settings(), {"setup.run_choice": "local"})
    assert st.profile == "hosted_only"
    pull = _step(st, "pull")
    assert pull.action_label == "Nothing to pull" and not pull.action_enabled
    prof = _step(st, "profile")
    assert prof.explanation == "Recorded: chosen on the Setup page."


def test_run_choice_is_derived_when_no_row_exists(quiet, monkeypatch):
    assert ss.run_choice(get_settings(), {}) == ("local", False)
    assert ss.run_choice(get_settings(), {"llm.mode": "hosted"}) == ("hosted", False)
    both = _hosted_settings(monkeypatch)
    assert ss.run_choice(both, {"llm.mode": "local"}) == ("both", False)
    assert ss.run_choice(both, {"setup.run_choice": "local"}) == ("local", True)
    # Hosted chosen but not yet configured: step 6 is required and names what is missing.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    st = ss.assess(_hosted_settings(monkeypatch, ack=False, key=False), {"llm.mode": "hosted"})
    hosted = _step(st, "hosted")
    assert hosted.status == "now" and hosted.pill == "required" and hosted.link_label == "Add a key"
    assert hosted.detail == "no key yet"


def test_is_first_run(quiet, monkeypatch):
    s = get_settings()
    assert ss.is_first_run(s, {})
    assert not ss.is_first_run(s, {"llm.mode": "local"})
    assert not ss.is_first_run(s, {"setup.run_choice": "both"})
    assert not ss.is_first_run(_hosted_settings(monkeypatch), {})
    profiles.save_active_profile("gpu_16gb", "test", HW_4080)
    assert not ss.is_first_run(s, {})


# ------------------------------------------------------------------------------ 2. the page
HX = {"HX-Request": "true"}


def test_system_panel_renders_cards_and_steps(quiet):
    _ollama(True, ["qwen3:4b"])
    client = _fresh_app()
    body = client.get("/settings/system").text
    assert "1. How will Kestrel run?" in body
    for card in ("<b>Local</b>", "<b>Hosted</b>", "<b>Both</b>"):
        assert card in body
    assert body.count('aria-pressed="true"') == 1
    assert "Detected: windows, NVIDIA GPU 16 GB VRAM, 128 GB RAM." in body
    assert "Local is recommended on this machine" in body
    assert 'data-step="ollama" data-status="ok"' in body
    assert 'data-step="pull" data-status="now"' in body
    assert "Pull 2 models" in body and "ollama pull qwen3:14b" in body
    assert 'data-step="build" data-status="todo"' in body
    assert 'data-step="hosted"' in body and 'href="/settings#hosted"' in body
    assert 'data-step="who"' not in body  # the profile is the onboarding's job now
    assert 'hx-get="/setup/steps"' not in body  # nothing running: no polling
    assert 'data-testid="restart-onboarding"' in body and "Restart onboarding" in body
    assert "kestrel setup --hosted" in body
    assert "—" not in body  # no em dashes in UI copy


def test_setup_mode_writes_the_choice_and_llm_mode_only_when_hosted_can_run(quiet, monkeypatch):
    client = _fresh_app()
    # Hosted chosen with no key and no acknowledgement: the choice is stored, llm.mode is NOT
    # written (hosted_mode_problem is the one rule), and step 1 says when it will apply.
    r = client.post("/setup/mode", data={"mode": "hosted"}, headers=HX)
    assert r.status_code == 200 and 'id="system-panel"' in r.text
    rows = _db_rows()
    assert rows["setup.run_choice"] == "hosted" and "llm.mode" not in rows
    active = profiles.load_active_profile()
    assert active is not None and active.profile == "hosted_only"
    body = r.text
    assert "Hosted mode will apply once step 6 is complete" in body
    assert 'data-testid="hosted-pending"' in body
    assert 'data-step="pull" data-status="skip"' in body and "skipped: Hosted" in body
    assert 'data-step="hosted" data-status="now"' in body

    # Step 6 done: the next visit to the page writes the pending mode row.
    monkeypatch.setattr(ss, "hosted_mode_problem", lambda settings: None)
    body = client.get("/settings/system").text
    rows = _db_rows()
    assert rows["llm.mode"] == "hosted" and rows["setup.run_choice"] == "hosted"
    assert 'data-testid="hosted-pending"' not in body
    # Choosing Hosted when it can run writes the mode at once.
    r = client.post("/setup/mode", data={"mode": "hosted"}, headers=HX)
    assert "Kestrel runs on hosted models." in r.text and _db_rows()["llm.mode"] == "hosted"

    r = client.post("/setup/mode", data={"mode": "both"}, headers=HX)
    rows = _db_rows()
    assert rows["llm.mode"] == "local" and rows["setup.run_choice"] == "both"
    assert 'data-step="pull" data-status="now"' in r.text
    assert profiles.load_active_profile().profile == "hosted_only"  # an existing record is kept

    assert client.post("/setup/mode", data={"mode": "cloud"}).status_code == 400
    # Without htmx (no script) the form lands back on the System tab with the message.
    r = client.post("/setup/mode", data={"mode": "local"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/settings?msg=")
    assert r.headers["location"].endswith("#system")


def test_set_run_mode_hosted_leaves_an_existing_local_mode_row_until_hosted_can_run(quiet):
    client = _fresh_app()
    client.post("/setup/mode", data={"mode": "local"})
    assert _db_rows()["llm.mode"] == "local"
    r = client.post("/setup/mode", data={"mode": "hosted"}, headers=HX)
    rows = _db_rows()
    assert rows["llm.mode"] == "local" and rows["setup.run_choice"] == "hosted"
    assert "until then runs use local models" in r.text


def test_setup_profile_records_a_catalog_profile(quiet):
    client = _fresh_app()
    r = client.post("/setup/profile", data={"profile": "gpu_24gb"}, headers=HX)
    assert r.status_code == 200
    assert profiles.load_active_profile().profile == "gpu_24gb"
    assert 'data-step="profile" data-status="ok"' in r.text
    assert client.post("/setup/profile", data={"profile": "gpu_99gb"}).status_code == 400


# ---------------------------------------------------------------------------- 3. the actions
def test_pull_button_queues_tasks_that_run_through_the_ollama_api(quiet):
    _ollama(True, ["qwen3:4b"])
    client = _fresh_app()
    r = client.post("/setup/action/pull")
    assert r.status_code == 200
    assert 'hx-trigger="every 2s"' in r.text  # polling while the tasks are live
    assert 'data-testid="setup-progress-pull" data-live="true"' in r.text
    assert 'data-testid="setup-cancel-pull"' in r.text
    # A second start while they wait: 200 (htmx 2 never swaps a 4xx by default), the partial
    # says why, and the root carries the busy marker.
    for name in ("build", "pull"):
        busy = client.post(f"/setup/action/{name}")
        assert busy.status_code == 200
        assert 'data-setup-status="busy"' in busy.text
        assert "Not started: pull is still running." in busy.text
    assert client.post("/setup/action/nope").status_code == 404

    url = get_settings().llm.ollama_native_url.rstrip("/")
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{url}/api/pull").mock(return_value=_stream(*PULL_OK))
        assert _drain() == 2
    sent = [json.loads(c.request.content) for c in route.calls]
    assert sent == [
        {"model": "qwen3:14b", "stream": True},
        {"model": "nomic-embed-text", "stream": True},
    ]
    body = client.get("/setup/steps").text
    assert 'data-live="false"' in body and 'hx-trigger="every 2s"' not in body
    assert body.count('data-task-status="done"') == 2
    # Finished: the button works again (nothing is missing in the fake now, so it says so).
    _ollama(True, BASE)
    assert "already downloaded" in client.post("/setup/action/pull").text


def test_build_button_creates_each_derived_model_and_records_the_profile(quiet):
    _ollama(True, BASE)
    client = _fresh_app()
    assert client.post("/setup/action/build").status_code == 200
    url = get_settings().llm.ollama_native_url.rstrip("/")
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{url}/api/create").mock(return_value=_stream(
            {"status": "reading model metadata"}, {"status": "writing manifest"},
            {"status": "success"},
        ))
        assert _drain() == 4
    bodies = [json.loads(c.request.content) for c in route.calls]
    assert [b["model"] for b in bodies] == DERIVED
    first = bodies[0]
    assert first["from"] == "qwen3:4b" and first["stream"] is True
    assert isinstance(first["parameters"]["num_ctx"], int)
    assert first["modelfile"].startswith("# kestrel-tier1")
    for mf in (paths.config_dir() / "models").iterdir():  # the Show details commands stay true
        assert mf.name.startswith("Modelfile.")
    active = profiles.load_active_profile()
    assert active is not None and active.profile == "gpu_16gb"
    _ollama(True, BASE + DERIVED)
    body = client.get("/setup/steps").text
    assert 'data-step="profile" data-status="ok"' in body


def test_setup_steps_show_buttons_and_keep_commands_under_details(quiet):
    _ollama(False, [])
    client = _fresh_app()
    body = client.get("/setup/steps").text
    assert 'data-testid="download-ollama"' in body and 'target="_blank"' in body
    assert "https://ollama.com/download/windows" in body
    assert 'hx-post="/setup/action/check"' in body and "Check again" in body
    outside = _without_details(body)
    for cmd in ("winget install", "ollama serve", "ollama pull", "ollama create", "curl -fsSL"):
        assert cmd not in outside, cmd
    assert "ollama pull qwen3:14b" in body  # under Show details
    assert "<summary>Show details</summary>" in body
    # Check again asks Ollama now: it comes up and the step turns green.
    _ollama(True, [])
    again = client.post("/setup/action/check").text
    assert 'data-step="ollama" data-status="ok"' in again


def _mock_doctor_probes(monkeypatch) -> None:
    """The REAL run_doctor with only the Ollama model probes, the browser probe and the ATS
    adapter faked: the settings layer, the database check and the ATS source query still run
    against the database, which is the point (they must not touch the app's engine)."""
    from types import SimpleNamespace

    from kestrel.ingest import adapters as adapters_mod
    from kestrel.ingest import http as http_mod

    def ok(name):
        def probe(rep, *a, **k):
            rep.add(name, doctor_mod.PASS, "fine")

        return probe

    def ollama_up(rep, client):
        rep.add("Ollama reachable", doctor_mod.PASS, "fake")
        return True

    monkeypatch.setattr(doctor_mod, "_check_ollama", ollama_up)
    monkeypatch.setattr(doctor_mod, "_check_models_present", lambda rep, c, s: set(DERIVED))
    monkeypatch.setattr(doctor_mod, "check_model_fit", lambda rep, s, c: None)
    for fn, name in (
        ("_check_num_ctx", "num_ctx"),
        ("_check_needle", "needle"),
        ("_check_thinking", "thinking off"),
        ("_check_schema", "schema honored"),
        ("_check_browser", "Browser (PDF)"),
    ):
        monkeypatch.setattr(doctor_mod, fn, ok(name))

    class FakeHttp:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class FakeAdapter:
        async def fetch(self, config, http):
            return [
                SimpleNamespace(
                    title="VP of IT", url="https://x/1", external_id="1",
                    description_md="body", description_html=None,
                )
            ]

    monkeypatch.setattr(http_mod, "HttpClient", FakeHttp)
    monkeypatch.setattr(adapters_mod, "get_adapter", lambda kind: FakeAdapter())


def _add_ats_source() -> None:
    async def _go():
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from kestrel.db.engine import make_async_engine
        from kestrel.db.models import Source

        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine)() as s:
                s.add(Source(track="employment", kind="greenhouse", name="board", enabled=True,
                             config={"board_token": "x"}))
                await s.commit()
        finally:
            await engine.dispose()

    asyncio.run(_go())


def test_verify_action_runs_the_real_doctor_without_touching_the_app_engine(quiet, monkeypatch):
    """The Verify action calls run_doctor() in its own thread while the app's engine lives on the
    server loop. Doctor's async probes (settings layer, ATS shapes) must open and dispose their
    own engine: disposing the app's from a foreign loop (the old reset_engine() call) left every
    later request with cross-loop aiosqlite errors."""
    from kestrel.db import engine as engine_mod

    _ollama(True, BASE + DERIVED)
    _mock_doctor_probes(monkeypatch)
    client = _fresh_app()
    _add_ats_source()
    assert client.get("/setup/steps").status_code == 200  # the app engine now exists
    app_engine = engine_mod.get_engine()
    resets: list[str] = []

    async def spy_reset():
        resets.append("called")

    monkeypatch.setattr(engine_mod, "reset_engine", spy_reset)
    assert client.post("/setup/action/verify").status_code == 200
    ss.get_action("verify").join(30.0)
    act = ss.get_action("verify")
    assert act.status == "done", act.lines
    assert resets == []
    assert engine_mod.get_engine() is app_engine  # neither reset nor replaced
    report = ss.load_report()
    assert report is not None
    names = {r["name"] for r in report["results"]}
    assert "Settings layer" not in names  # setting-table rows applied, no fallback row
    assert "ATS board" in names and "Database" in names
    # The app engine is still usable afterwards: this request queries the setting table.
    body = client.get("/setup/steps").text
    assert 'data-action="verify" data-action-status="done"' in body
    assert 'data-step="build" data-status="ok"' in body and "verified 20" in body
    assert "[ OK ]  needle" in body and "[ OK ]  ATS board" in body


def test_run_doctor_full_leaves_an_existing_app_engine_usable(quiet, monkeypatch):
    """Outside the page too: run_doctor(quick=False) on a thread beside a live app engine."""
    from sqlalchemy import text

    from kestrel.db import engine as engine_mod
    from kestrel.db import migrate

    _mock_doctor_probes(monkeypatch)
    migrate.run_migrations(paths.db_path())
    _add_ats_source()
    resets: list[str] = []

    async def spy_reset():
        resets.append("called")

    monkeypatch.setattr(engine_mod, "reset_engine", spy_reset)

    async def app_side():
        sm = engine_mod.get_sessionmaker()  # the app engine, bound to this loop
        async with sm() as s:
            assert (await s.execute(text("select 1"))).scalar_one() == 1
        rep = await asyncio.to_thread(doctor_mod.run_doctor, quick=False)
        async with sm() as s:  # still usable: the doctor disposed only its own engine
            n = (await s.execute(text("select count(*) from source"))).scalar_one()
        await engine_mod.get_engine().dispose()
        return rep, n

    rep, n = asyncio.run(app_side())
    rows = {r.name: r for r in rep.results}
    assert n == 1 and resets == []
    assert rows["ATS board"].status == doctor_mod.PASS
    assert "Settings layer" not in rows


def test_pull_with_ollama_down_says_why_and_queues_nothing(quiet):
    _ollama(False, [])
    client = _fresh_app()
    r = client.post("/setup/action/pull")
    assert r.status_code == 200 and 'data-setup-status="error"' in r.text
    assert "Ollama is not running" in r.text and "Check again" in r.text
    assert _drain() == 0
    assert ss.running_action() is None


# ------------------------------------------------------------------------- 4. first-run banner
def test_first_run_banner_on_the_dashboard(quiet):
    # The conftest switches the fresh-install redirect off, so / is the dashboard with the
    # banner; tests/test_onboarding.py covers the redirect itself.
    client = _fresh_app()
    body = client.get("/").text
    assert 'id="finish-setup"' in body and 'href="/onboarding"' in body and "Finish setup" in body
    assert "<title>Today" in body  # the dashboard itself, not a redirect
    assert body.index("<main") < body.index('id="finish-setup"')  # inside the content column


def test_finish_setup_banner_stays_until_onboarding_is_complete(quiet):
    client = _fresh_app()
    assert 'id="finish-setup"' in client.get("/").text
    client.post("/setup/mode", data={"mode": "local"})  # a mode alone is not set up
    assert 'id="finish-setup"' in client.get("/").text
    # An install from before the wizard with a profile and a stored mode counts as onboarded.
    (paths.config_dir() / "profile.yml").write_text(
        "employment: {full_name: Pat}\n", encoding="utf-8"
    )
    body = client.get("/").text
    assert 'id="finish-setup"' not in body and "<title>Today" in body


def test_setup_redirects_to_onboarding_then_to_settings_system(quiet):
    client = _fresh_app()
    r = client.get("/setup", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/onboarding"
    client.post("/setup/mode", data={"mode": "local"})
    (paths.config_dir() / "profile.yml").write_text(
        "employment: {full_name: Pat}\n", encoding="utf-8"
    )
    r = client.get("/setup", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings#system"
