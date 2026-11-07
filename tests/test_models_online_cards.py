"""Models page under the Online choice and the Local AI rows on the Health check card
(issues #114, #115, #144; D-115-ONLINE-CARDS). Cloud-safe: TestClient on the real app, a throwaway
data directory, no Ollama and no network (the Ollama probe is patched and counted).

Invariants proved here:
* under the Online choice nothing on the Models page starts a local build, pull or check, whatever
  the page showed (the routes refuse on the server);
* a check that could not run reads "unverified" on the card, never as a pass;
* a stale failed local report never paints an online step or the online-mode cards red."""

from __future__ import annotations

import asyncio
import re

import pytest
from sqlalchemy import select

from kestrel import doctor
from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import Setting, Task
from kestrel.llm.profiles import Hardware
from kestrel.services import model_tasks as mt
from kestrel.services import setup_state as ss
from kestrel.web import models_view as mv
from tests.test_hosted_mode import _fresh_app

GPU = Hardware(os="windows", gpu_vendor="nvidia", vram_gb=12.0, ram_gb=32.0)
NOT_NEEDED = "Not needed while Kestrel runs online."


@pytest.fixture(autouse=True)
def _world(monkeypatch):
    """A GPU machine; every Ollama probe is counted so a test can prove none was made."""
    calls: list[str] = []

    def probe(settings, fresh=False):
        calls.append("probe")
        return ("0.5.0", ["nomic-embed-text:latest"])

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    monkeypatch.setattr(ss, "detected_hardware", lambda: GPU)
    monkeypatch.setattr(ss, "probe_ollama", probe)
    return calls


def _set(key: str, value) -> None:
    async def go():
        async with get_sessionmaker()() as s:
            row = await s.get(Setting, key)
            if row is None:
                s.add(Setting(key=key, value=value))
            else:
                row.value = value
            await s.commit()

    asyncio.run(go())


def _row(key: str):
    async def go():
        async with get_sessionmaker()() as s:
            row = await s.get(Setting, key)
            return None if row is None else row.value

    return asyncio.run(go())


def _task_count() -> int:
    async def go():
        async with get_sessionmaker()() as s:
            return len((await s.execute(select(Task.id))).scalars().all())

    return asyncio.run(go())


def _save_report(*rows):
    rep = doctor.DoctorReport()
    for name, status, detail, *rest in rows:
        rep.add(name, status, detail, *(rest or [""]))
    ss.save_report(rep)


def _stale_failed_local_report():
    """What an earlier local setup left behind: the engine and room-to-read checks failed."""
    _save_report(
        ("Ollama reachable", "FAIL", "not reachable", "Open Ollama, then press Check again."),
        ("Models present", "PASS", "all found"),
        ("num_ctx tier1", "FAIL", "kestrel-tier1: num_ctx 2048 < required 4000"),
        ("num_ctx tier2", "FAIL", "kestrel-tier2: num_ctx 8192 < required 16384"),
        ("needle docsel", "FAIL", "hidden word not found"),
        ("thinking off kestrel-tier2", "FAIL", "think span returned"),
        ("Local AI: backend address", "WARN", "http://192.168.1.9:11434",
         "Your text goes to another address, not this computer."),
    )


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


# ----------------------------------------------------------------------------------- #114 steps
def test_online_steps_with_a_stale_failed_local_check_are_not_too_small():
    client = _fresh_app()
    _stale_failed_local_report()
    _set(ss.CHOICE_KEY, "hosted")
    page = client.get("/models").text
    assert "Too small" not in page
    assert "mo-fix-tier1" not in page and "mo-fix-tier2" not in page and "mo-fix-docsel" not in page
    for key in ("tier1", "tier2", "docsel"):
        cell = page.split(f'data-testid="mo-canread-{key}"')[1].split("</div>")[0]
        assert mv.ONLINE_READS in cell, key
    row = page.split('data-testid="mo-step-tier2"')[0].rsplit("<tr", 1)[1]
    assert "is-bad" not in row  # the row does not turn red either


def test_a_local_step_with_the_same_report_still_reads_too_small():
    """The control: the label is for local steps, so the report itself was not made harmless."""
    client = _fresh_app()
    _stale_failed_local_report()
    _set(ss.CHOICE_KEY, "local")
    page = client.get("/models").text
    assert "Too small: cuts long postings short" in page
    assert 'data-testid="mo-fix-tier2"' in page


def test_the_online_can_read_line_is_true_and_explains_itself():
    client = _fresh_app()
    _set(ss.CHOICE_KEY, "hosted")
    page = client.get("/models").text
    tip = page.split('data-testid="mo-canread-tier2"')[0].rsplit("title=", 1)[1]
    assert "depends on the online model" in tip
    assert "only covers models on this computer" in tip


def test_fix_it_in_a_steps_row_answers_beside_the_click_and_updates_the_card(_world):
    client = _fresh_app()
    _stale_failed_local_report()
    _set(ss.CHOICE_KEY, "local")
    page = client.get("/models").text
    assert 'hx-post="/models/health/fix?step=tier2"' in page
    assert 'hx-target="#mo-fixres-tier2"' in page and 'id="mo-fixres-tier2"' in page
    # Ollama is down: the answer says so at the click, in the row's own fragment.
    import kestrel.services.setup_state as setup_mod

    setup_mod_probe = lambda settings, fresh=False: (None, [])  # noqa: E731
    from _pytest.monkeypatch import MonkeyPatch

    with MonkeyPatch.context() as mp:
        mp.setattr(setup_mod, "probe_ollama", setup_mod_probe)
        r = client.post("/models/health/fix?step=tier2")
    assert r.status_code == 200
    answer = r.text.split("<section", 1)[0]
    assert 'data-testid="mo-fixres-tier2"' in answer and "is-bad" in answer
    assert "Not started: Ollama is not running" in _text(answer)
    assert 'id="mo-health"' in r.text and 'hx-swap-oob="outerHTML"' in r.text


# ----------------------------------------------------------------------------------- #115 cards
def test_online_mode_greys_both_cards_with_one_plain_line_and_no_stale_rows():
    client = _fresh_app()
    _stale_failed_local_report()
    _set(ss.CHOICE_KEY, "hosted")
    page = client.get("/models").text
    assert _text(page).count(NOT_NEEDED) == 2  # one on each card
    health = page.split(' id="mo-health"')[1].split("</section>")[0]
    downloads = page.split(' id="mo-downloads"')[1].split("</section>")[0]
    for card in (health, downloads):
        assert "Fix it" not in card and "Download" not in card
    assert "mo-disabled" in page.split(' id="mo-health"')[0].rsplit("<section", 1)[1]
    assert "mo-disabled" in page.split(' id="mo-downloads"')[0].rsplit("<section", 1)[1]
    # The stale report is hidden, not shown as current: no pass or fail row, no red or amber edge.
    assert 'data-testid="mo-hc-' not in health and 'data-testid="mo-la-' not in health
    assert "mo-edge-bad" not in page.split(' id="mo-health"')[0].rsplit("<section", 1)[1]
    assert "Your last local check was on" in _text(health)
    assert re.search(r'disabled[^>]*data-testid="mo-health-run"', health)
    assert 'data-testid="mo-dl-start"' not in downloads
    assert "AI engine is not running" not in downloads


def test_online_mode_with_no_report_has_no_last_check_line():
    client = _fresh_app()
    _set(ss.CHOICE_KEY, "hosted")
    health = client.get("/models/health").text
    assert NOT_NEEDED in _text(health) and "Your last local check" not in health


def test_mo_disabled_is_defined_from_theme_tokens():
    from importlib.resources import files

    css = (files("kestrel.web") / "static" / "css" / "models.css").read_text(encoding="utf-8")
    rule = re.search(r"\.mo-disabled \{([^}]*)\}", css)
    assert rule, ".mo-disabled has no style"
    assert "var(--" in rule.group(1) and not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb", rule.group(1))


def test_both_mode_says_the_check_covers_local_steps_only():
    client = _fresh_app()
    _set(ss.CHOICE_KEY, "both")
    health = client.get("/models/health").text
    assert 'data-testid="mo-health-both"' in health
    assert "steps on this computer and the matching model only" in _text(health)
    _set(ss.CHOICE_KEY, "local")
    assert 'data-testid="mo-health-both"' not in client.get("/models/health").text


def test_nothing_on_the_models_page_starts_local_work_while_online(monkeypatch, _world):
    """INVARIANT: under the Online choice Run check, Fix it and the download buttons cannot start a
    check, a build, a pull or any Ollama call, even when posted by hand or from a stale page."""
    client = _fresh_app()
    _stale_failed_local_report()
    _set(ss.CHOICE_KEY, "hosted")
    started: list[str] = []
    monkeypatch.setattr(ss, "verify", lambda: started.append("verify"))
    monkeypatch.setattr(ss, "forget_ollama_probe", lambda: started.append("forget"))
    monkeypatch.setattr(mt, "queue_builds", lambda *a, **k: started.append("build"))
    monkeypatch.setattr(mt, "queue_pulls", lambda *a, **k: started.append("pull"))
    _world.clear()
    for path in ("/models/health/run", "/models/health/fix", "/models/health/fix?step=tier2",
                 "/models/download", "/models/download/tune"):
        r = client.post(path)
        assert r.status_code == 200, path
        assert "Nothing was started" in _text(r.text), path
    assert started == [] and _world == [] and _task_count() == 0
    assert asyncio.run(_no_live_tasks())


async def _no_live_tasks() -> bool:
    async with get_sessionmaker()() as s:
        return not await mt.live_tasks(s)


def test_the_same_posts_do_start_work_under_the_local_choice(monkeypatch):
    """The control for the guard: it is the choice that refuses, not the routes."""
    client = _fresh_app()
    _set(ss.CHOICE_KEY, "local")
    started: list[str] = []
    monkeypatch.setattr(ss, "verify", lambda: started.append("verify"))
    monkeypatch.setattr(ss, "forget_ollama_probe", lambda: None)
    client.post("/models/health/run")
    assert started == ["verify"]


# --------------------------------------------------------------- switching back re-runs the check
def _post_mode(client, mode: str):
    return client.post("/settings/computer/mode", data={"mode": mode})


def test_switching_from_online_to_local_or_both_queues_one_new_check():
    client = _fresh_app()
    _set(ss.CHOICE_KEY, "hosted")
    assert _row(ss.RECHECK_OWED_KEY) is None
    assert _post_mode(client, "both").status_code == 200
    assert _row(ss.RECHECK_OWED_KEY) is True


def test_a_switch_that_does_not_leave_online_owes_no_check():
    client = _fresh_app()
    _set(ss.CHOICE_KEY, "local")
    _post_mode(client, "both")
    assert _row(ss.RECHECK_OWED_KEY) is None


def _run_recheck() -> bool:
    async def go():
        return await mt.finish_owed_recheck(get_sessionmaker())

    return asyncio.run(go())


def test_the_owed_check_waits_while_online_then_runs_once(monkeypatch):
    _fresh_app()
    runs: list[int] = []
    monkeypatch.setattr(ss, "verify", lambda: runs.append(1))
    monkeypatch.setattr(ss, "forget_ollama_probe", lambda: None)
    _set(ss.RECHECK_OWED_KEY, True)
    _set(ss.CHOICE_KEY, "hosted")
    assert _run_recheck() is False and runs == []  # nothing runs while online
    assert _row(ss.RECHECK_OWED_KEY) is True  # and it is still owed
    _set(ss.CHOICE_KEY, "local")
    assert _run_recheck() is True and runs == [1]
    assert _row(ss.RECHECK_OWED_KEY) is False
    assert _run_recheck() is False and runs == [1]  # once, not on every idle cycle


def _run_the_setup_loop(monkeypatch, cycles: int) -> None:
    """The app's own setup loop (run_loop), driven for `cycles` idle cycles. The wrapper only
    counts the calls the loop makes to the real check and then stops the loop."""
    stop_box: dict = {}
    seen = {"n": 0}
    real = mt.finish_owed_recheck

    async def counted(sm):
        result = await real(sm)
        seen["n"] += 1
        if result or seen["n"] >= cycles:
            stop_box["stop"].set()
        return result

    async def go():
        stop_box["stop"] = asyncio.Event()

        async def url():
            return "http://127.0.0.1:1"

        await asyncio.wait_for(mt.run_loop(stop_box["stop"], get_sessionmaker(), url), 20.0)

    monkeypatch.setattr(mt, "finish_owed_recheck", counted)
    monkeypatch.setattr(mt, "IDLE_POLL_S", 0.01)
    asyncio.run(go())


def test_the_setup_loop_runs_the_owed_check_once_the_choice_has_left_online(monkeypatch):
    _fresh_app()
    runs: list[int] = []
    monkeypatch.setattr(ss, "verify", lambda: runs.append(1))
    monkeypatch.setattr(ss, "forget_ollama_probe", lambda: None)
    _set(ss.RECHECK_OWED_KEY, True)
    _set(ss.CHOICE_KEY, "local")
    _run_the_setup_loop(monkeypatch, cycles=5)
    assert runs == [1] and _row(ss.RECHECK_OWED_KEY) is False


def test_the_setup_loop_does_not_run_the_owed_check_while_the_choice_is_online(monkeypatch):
    _fresh_app()
    runs: list[int] = []
    monkeypatch.setattr(ss, "verify", lambda: runs.append(1))
    monkeypatch.setattr(ss, "forget_ollama_probe", lambda: None)
    _set(ss.RECHECK_OWED_KEY, True)
    _set(ss.CHOICE_KEY, "hosted")
    _run_the_setup_loop(monkeypatch, cycles=3)
    assert runs == [] and _row(ss.RECHECK_OWED_KEY) is True


def test_the_owed_check_stays_owed_when_another_action_is_running(monkeypatch):
    _fresh_app()

    def busy():
        raise ss.ActionBusy("pull is still running")

    monkeypatch.setattr(ss, "verify", busy)
    monkeypatch.setattr(ss, "forget_ollama_probe", lambda: None)
    _set(ss.RECHECK_OWED_KEY, True)
    _set(ss.CHOICE_KEY, "local")
    assert _run_recheck() is False
    assert _row(ss.RECHECK_OWED_KEY) is True


def test_the_owed_check_waits_for_a_running_download(monkeypatch):
    _fresh_app()
    runs: list[int] = []
    monkeypatch.setattr(ss, "verify", lambda: runs.append(1))
    monkeypatch.setattr(ss, "forget_ollama_probe", lambda: None)
    _set(ss.RECHECK_OWED_KEY, True)
    _set(ss.CHOICE_KEY, "local")

    async def queue():
        async with get_sessionmaker()() as s:
            await mt.queue_pulls(s, ["qwen3:8b"])

    asyncio.run(queue())
    assert _run_recheck() is False and runs == []


# -------------------------------------------------------------------------- #144 rows on the card
def _la(card: str, key: str) -> str:
    return card.split(f'data-testid="mo-la-{key}"')[0].rsplit("<li", 1)[1].split(">", 1)[0]


def test_the_local_ai_rows_show_on_the_health_card_with_their_real_states():
    client = _fresh_app()
    _set(ss.CHOICE_KEY, "local")
    _save_report(
        ("Local AI: backend address", "WARN", "http://192.168.1.9:11434",
         "Your text goes to another address, not this computer."),
        ("Local AI: Ollama bind", "PASS", "OLLAMA_HOST unset (defaults to loopback)"),
        ("Local AI: cloud model tags", "WARN", "gpt-oss:20b-cloud",
         "These models are cloud-hosted: prompts are sent to Ollama's servers."),
        ("Local AI: cloud features", "INFO", "not seen turned off. Ollama's cloud models stay "
         "available if you sign in"),
        ("Local AI: egress", "WARN", "Unverified: the operating system did not let Kestrel list "
         "the connections of 1 of 2 Ollama process(es). This is a spot check."),
        ("Local AI: model digests", "INFO", "unverified: the digests Kestrel tested are not "
         "recorded yet"),
    )
    card = client.get("/models/health").text
    assert 'data-testid="mo-la-title"' in card
    states = {k: _la(card, k) for k in ("backend", "bind", "cloudtags", "cloudoff", "egress",
                                        "digests")}
    assert "is-warn" in states["backend"] and "is-ok" in states["bind"]
    assert "is-warn" in states["cloudtags"] and "is-info" in states["cloudoff"]
    # A check that could not run is its own state, never a pass.
    assert "is-unverified" in states["egress"] and "is-unverified" in states["digests"]
    assert "is-ok" not in states["egress"] + states["digests"] + states["cloudoff"]
    assert "Unverified" in card.split('data-testid="mo-la-egress"')[1].split("</li>")[0]
    assert "not this computer" in _text(card)  # the backend warning in plain words
    assert "mo-edge-bad" in card[:400]  # a warning flags the card


def test_a_connection_row_with_findings_and_a_gap_is_a_warning_not_unverified():
    rows = mv.local_ai_rows({"results": [{
        "name": "Local AI: egress", "status": "WARN",
        "detail": "Unverified: access denied for 1 process. Ollama connected to a public "
                  "address: 203.0.113.9:443 (ollama.exe). This is a spot check."}]})
    egress = next(r for r in rows if r["key"] == "egress")
    assert egress["state"] == "warn" and "203.0.113.9:443" in egress["detail"]


def test_with_ollama_unreachable_the_rows_that_need_it_are_unverified_not_passes():
    rows = mv.local_ai_rows({"results": [
        {"name": "Local AI: checks needing Ollama", "status": "INFO",
         "detail": "unverified: Ollama not reachable"},
        {"name": "Local AI: backend address", "status": "PASS", "detail": "http://127.0.0.1:11434"},
    ]})
    by = {r["key"]: r for r in rows}
    assert by["cloudtags"]["state"] == "unverified" and by["digests"]["state"] == "unverified"
    assert by["backend"]["state"] == "ok"
    assert "ok" not in {by["cloudtags"]["state"], by["digests"]["state"]}


def test_before_any_check_the_local_ai_rows_say_not_checked_yet():
    rows = mv.local_ai_rows(None)
    assert [r["state"] for r in rows] == ["none"] * 6
    client = _fresh_app()
    _set(ss.CHOICE_KEY, "local")
    assert _text(client.get("/models/health").text).count("Not checked yet") >= 12


def test_a_skipped_quick_check_is_not_a_pass():
    rows = mv.local_ai_rows({"results": [
        {"name": "Local AI: egress", "status": "INFO", "detail": "skipped (--quick)"}]})
    assert next(r for r in rows if r["key"] == "egress")["state"] == "none"


def test_the_firewall_tip_is_copyable_text_behind_details_and_changes_nothing():
    client = _fresh_app()
    _set(ss.CHOICE_KEY, "local")
    card = client.get("/models/health").text
    details = card.split('<details class="mo-details">')[1]
    assert "Block Ollama from the internet" in details
    assert "Download your models first" in details
    assert "New-NetFirewallRule" in details and "iptables" in details
    assert "hx-post" not in details  # text to copy, never a button that acts
    assert "—" not in card


# -------------------------------------------------------------------- doctor: Ollama cloud features
def _cloud_row(monkeypatch, tmp_path, *, env=None, content=None, backend="http://127.0.0.1:11434"):
    from kestrel.config import get_settings

    monkeypatch.delenv("OLLAMA_NO_CLOUD", raising=False)
    if env is not None:
        monkeypatch.setenv("OLLAMA_NO_CLOUD", env)
    path = tmp_path / "server.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(doctor, "ollama_server_json_path", lambda: path)
    s = get_settings()
    s.llm.backends["local"].base_url = backend
    rep = doctor.DoctorReport()
    doctor._check_ollama_cloud_off(rep, s)
    return rep.results[0]


def test_cloud_features_off_by_the_settings_file(monkeypatch, tmp_path):
    row = _cloud_row(monkeypatch, tmp_path, content='{"disable_ollama_cloud": true}')
    assert row.status == doctor.PASS and "off" in row.detail


def test_cloud_features_off_by_the_environment(monkeypatch, tmp_path):
    assert _cloud_row(monkeypatch, tmp_path, env="1").status == doctor.PASS


def test_cloud_features_not_seen_off_is_info_never_a_pass(monkeypatch, tmp_path):
    for content in (None, '{"disable_ollama_cloud": false}', "{}"):
        row = _cloud_row(monkeypatch, tmp_path, content=content)
        assert row.status == doctor.INFO and row.detail.startswith("not seen turned off"), content


def test_an_unreadable_settings_file_is_unverified(monkeypatch, tmp_path):
    row = _cloud_row(monkeypatch, tmp_path, content="{not json")
    assert row.status == doctor.INFO and row.detail.startswith("unverified")
    bad = tmp_path / "dir"
    bad.mkdir()
    monkeypatch.setattr(doctor, "ollama_server_json_path", lambda: bad)
    from kestrel.config import get_settings

    rep = doctor.DoctorReport()
    doctor._check_ollama_cloud_off(rep, get_settings())
    assert rep.results[0].detail.startswith("unverified")


def test_ollama_on_another_computer_is_unverified(monkeypatch, tmp_path):
    row = _cloud_row(monkeypatch, tmp_path, content='{"disable_ollama_cloud": true}',
                     backend="http://192.168.1.9:11434")
    assert row.status != doctor.PASS and row.detail.startswith("unverified")


# ---------------------------------------------------------- doctor: sampling during the probe
class _Conn:
    def __init__(self, addr: str, port: int) -> None:
        self.raddr = (addr, port)


class _FakeProc:
    """An Ollama process whose connection list depends on whether the probes are running."""

    def __init__(self, state: dict) -> None:
        self._state = state

    def net_connections(self, kind="inet"):
        self._state["looked"] = self._state.get("looked", 0) + 1
        return [_Conn("192.168.1.77", 8080)] if self._state.get("probing") else []


def test_the_connection_check_samples_while_the_probes_run(monkeypatch):
    """A connection that exists only while a model is being probed is seen (it was missed when the
    sampling happened at rest, #144). No real model: the probe is a stand-in."""
    import psutil

    from kestrel.services import local_ai_egress as egress

    state: dict = {}
    monkeypatch.setattr(egress, "_find_ollama_pids", lambda: [(4242, "ollama.exe")])
    monkeypatch.setattr(psutil, "Process", lambda pid=None: _FakeProc(state))
    watcher = egress.ConnectionWatcher(interval_s=0.01, tail_s=0.05).start()
    deadline_hit = _poll(lambda: state.get("looked", 0) >= 2)
    assert deadline_hit, "the watcher never looked"
    state["probing"] = True  # the probe begins
    assert _poll(lambda: state.get("looked", 0) >= 6)
    state["probing"] = False  # the probe ends; only the short tail remains
    rep = doctor.DoctorReport()
    doctor._check_local_ai_egress(rep, watcher)
    row = rep.results[0]
    assert row.name == "Local AI: egress" and row.status == doctor.WARN
    assert "192.168.1.77:8080" in row.detail


def _poll(cond, tries: int = 400) -> bool:
    import time

    for _ in range(tries):
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_the_doctor_starts_the_watcher_before_the_probes_and_ends_it_after(monkeypatch):
    order: list[str] = []

    class FakeWatcher:
        def finish(self):
            order.append("finish")
            from kestrel.services.local_ai_egress import EgressResult

            return EgressResult()

    monkeypatch.setattr(doctor, "_start_egress_watcher", lambda: (order.append("start"),
                                                                   _watcher(FakeWatcher))[1])
    monkeypatch.setattr(doctor, "_run_probes", lambda *a, **k: order.append("probes"))
    monkeypatch.setattr(doctor, "_check_ollama", lambda rep, client: True)
    monkeypatch.setattr(doctor, "_check_models_present", lambda rep, c, s: set())
    monkeypatch.setattr(doctor, "_check_num_ctx", lambda *a, **k: None)
    monkeypatch.setattr(doctor, "check_model_fit", lambda *a, **k: None)
    monkeypatch.setattr(doctor, "_check_cloud_model_tags", lambda *a, **k: None)
    monkeypatch.setattr(doctor, "_check_model_digests", lambda *a, **k: None)
    monkeypatch.setattr(doctor, "_check_local_ai_settings", lambda *a, **k: None)
    monkeypatch.setattr(doctor, "_check_ollama_cloud_off", lambda *a, **k: None)
    monkeypatch.setattr(doctor, "_check_ats_shapes", lambda *a, **k: None)
    monkeypatch.setattr(doctor, "ollama_needed", lambda s: True)
    monkeypatch.setattr(doctor, "hosted_only", lambda s: False)
    monkeypatch.setattr(
        "kestrel.services.local_ai_egress.ConnectionWatcher", _WatcherBase, raising=True)
    doctor.run_doctor(quick=False)
    assert order == ["start", "probes", "finish"]


class _WatcherBase:
    pass


def _watcher(cls):
    class W(_WatcherBase):
        def finish(self):
            return cls().finish()

    return W()


def test_a_failing_probe_still_ends_the_watcher(monkeypatch):
    finished: list[int] = []

    class W(_WatcherBase):
        def finish(self):
            finished.append(1)
            from kestrel.services.local_ai_egress import EgressResult

            return EgressResult()

    monkeypatch.setattr("kestrel.services.local_ai_egress.ConnectionWatcher", _WatcherBase)
    monkeypatch.setattr(doctor, "_start_egress_watcher", lambda: W())

    def boom(*a, **k):
        raise RuntimeError("probe crashed")

    monkeypatch.setattr(doctor, "_run_probes", boom)
    monkeypatch.setattr(doctor, "_check_ollama", lambda rep, client: True)
    monkeypatch.setattr(doctor, "_check_models_present", lambda rep, c, s: set())
    monkeypatch.setattr(doctor, "_check_num_ctx", lambda *a, **k: None)
    monkeypatch.setattr(doctor, "check_model_fit", lambda *a, **k: None)
    monkeypatch.setattr(doctor, "ollama_needed", lambda s: True)
    monkeypatch.setattr(doctor, "hosted_only", lambda s: False)
    with pytest.raises(RuntimeError):
        doctor.run_doctor(quick=False)
    assert finished == [1]


# --------------------------------------------------------------------- packaging and notices
def test_psutil_is_in_the_packaging_spec_and_the_third_party_notices():
    from kestrel.services import notices
    from tests.test_packaging import PACKAGING

    spec = (PACKAGING / "kestrel.spec").read_text(encoding="utf-8")
    assert re.search(r'hiddenimports \+= \[[^\]]*"psutil"', spec), "psutil missing from the spec"
    names = {d.metadata["Name"].lower() for d in notices.runtime_closure()}
    assert "psutil" in names, "psutil is not a runtime dependency, so no notice would be built"
    text = notices.build_notices()
    assert "psutil " in text and "BSD" in text


def test_the_health_context_itself_has_no_stale_rows_while_online():
    """The context, not only the template, drops the earlier local rows under Online, so no other
    template can show them as current (D-115)."""
    from kestrel.web.pages import models_ui

    _fresh_app()
    _stale_failed_local_report()
    _set(ss.CHOICE_KEY, "hosted")
    ctx = asyncio.run(models_ui.health_ctx())
    assert ctx["hc_online"] is True
    assert ctx["hc_rows"] == [] and ctx["la_rows"] == []
    assert ctx["hc_failed"] is False and ctx["hc_la_warn"] is False and ctx["hc_any_fix"] is False
    _set(ss.CHOICE_KEY, "local")
    local = asyncio.run(models_ui.health_ctx())
    assert local["hc_failed"] is True and local["hc_any_fix"] is True and local["la_rows"]
