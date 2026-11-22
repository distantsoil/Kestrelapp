"""Issue #181: Ollama's OLLAMA_NUM_PARALLEL and OLLAMA_KEEP_ALIVE, set from
Settings > This computer.

Invariant: nothing on the person's system changes without the exact button press. The confirm
states which variable gets which value, where, and how to undo it; Undo puts back what was there
before Kestrel's first change. A missing or stale digest changes nothing, a GET never changes
anything, and every command runs as an argument list (no shell) with UTF-8 text.

Every backend's system calls are replaced here: a recorder stands in for the registry, launchctl
and systemctl, and Linux files live under tmp_path. All values are invented. No network.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import types
from pathlib import Path

import pytest

from kestrel import paths
from kestrel.services import ollama_env as oe

_WINDOWS = os.name == "nt"


def _mode_kept(posix_mode: int) -> int:
    """The permission bits a rewrite must keep. Windows has no POSIX bits: os.stat reports 0o666
    for every writable file, so that is what a Windows run checks."""
    return 0o666 if _WINDOWS else posix_mode


def _symlink(link: Path, target: Path) -> None:
    """Make a symlink, or skip the test where the system refuses one (Windows without Developer
    Mode or admin rights raises WinError 1314)."""
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"this system cannot create a symlink: {exc}")


# ------------------------------------------------------------------------- fakes
class FakeBackend(oe.Backend):
    """Holds the two values in a dict and records every write and restart."""

    def __init__(self, values: dict | None = None, *, can_set: bool = True,
                 can_restart: bool = True, mechanism: str = "fake") -> None:
        super().__init__(mechanism=mechanism, where="a test place", can_set=can_set,
                         why_not="not here", can_restart=can_restart,
                         restart_text="restart it", persistence="stays",
                         undo_text="press Undo")
        self.values: dict[str, str | None] = dict.fromkeys(oe.NAMES)
        self.values.update(values or {})
        self.writes: list[dict] = []
        self.restarts = 0

    def owned(self):
        return dict(self.values)

    def write(self, values):
        self.writes.append(dict(values))
        self.values.update(values)

    def commands(self, values):
        return [f"fake set {n} {v}" for n, v in values.items()]

    def restart(self):
        self.restarts += 1


@pytest.fixture
def fake(monkeypatch):
    be = FakeBackend()
    monkeypatch.setattr(oe, "_override", [be])
    monkeypatch.setattr("kestrel.llm.profiles.ollama_needed", lambda s: True)
    return be


def _settings(t1: int = 4, t2: int = 2, embed: int = 1):
    return types.SimpleNamespace(concurrency=types.SimpleNamespace(tier1=t1, tier2=t2,
                                                                   embed=embed))


LOCAL = "http://127.0.0.1:11434"


# ------------------------------------------------------------------------- values
@pytest.mark.parametrize("value,seconds", [
    ("5m", 300.0), ("2h", 7200.0), ("1h30m", 5400.0), ("3600", 3600.0), ("0", 0.0),
    ("-1", float("inf")), ("-1m", float("inf")), ("90s", 90.0), ("1.5h", 5400.0),
])
def test_keep_alive_formats_from_the_faq_are_read(value, seconds):
    assert oe.keep_alive_seconds(value) == seconds


@pytest.mark.parametrize("value", ["", "soon", "2 hours", "h", "5mm"])
def test_an_unreadable_keep_alive_is_none(value):
    assert oe.keep_alive_seconds(value) is None


@pytest.mark.parametrize("name,value,verdict,target", [
    (oe.PARALLEL, None, "unset", "4"),
    (oe.PARALLEL, "2", "low", "4"),
    (oe.PARALLEL, "4", "ok", None),
    (oe.PARALLEL, "8", "ok", None),
    (oe.PARALLEL, "four", "unreadable", "4"),
    (oe.KEEP_ALIVE, None, "unset", "2h"),
    (oe.KEEP_ALIVE, "5m", "short", "2h"),
    (oe.KEEP_ALIVE, "0", "short", "2h"),
    (oe.KEEP_ALIVE, "1h", "ok", None),
    (oe.KEEP_ALIVE, "-1", "ok", None),
    (oe.KEEP_ALIVE, "later", "unreadable", "2h"),
])
def test_each_value_gets_a_verdict_and_a_target(name, value, verdict, target):
    row = oe.judge(name, value, 4)
    assert (row.verdict, row.target) == (verdict, target)


def test_the_need_is_the_largest_configured_concurrency():
    assert oe.need_for(_settings(2, 6, 3)) == 6
    assert oe.need_for(_settings(1, 1, 5)) == 5


# ------------------------------------------------------------------------- service
def test_assess_reads_and_never_writes(fake):
    a = oe.assess(_settings(), None, LOCAL)
    assert a.shown and a.plan is not None
    assert [(c.name, c.old, c.new) for c in a.plan.changes] == [
        (oe.PARALLEL, None, "4"), (oe.KEEP_ALIVE, None, "2h")]
    assert fake.writes == []


def test_nothing_to_offer_when_both_values_are_fine(fake):
    fake.values.update({oe.PARALLEL: "4", oe.KEEP_ALIVE: "2h"})
    assert oe.assess(_settings(), None, LOCAL).plan is None


def test_hidden_when_no_step_runs_on_ollama(fake, monkeypatch):
    monkeypatch.setattr("kestrel.llm.profiles.ollama_needed", lambda s: False)
    a = oe.assess(_settings(), None, LOCAL)
    assert not a.shown and a.backend is None


def test_hidden_when_ollama_is_on_another_computer(fake):
    a = oe.assess(_settings(), None, "http://gpu-box.example:11434")
    assert not a.shown and "gpu-box.example" in a.hidden_reason


@pytest.mark.parametrize("digest", ["", "0" * 32, "not-a-digest"])
def test_apply_without_the_shown_digest_changes_nothing(fake, digest):
    with pytest.raises(oe.StalePlan):
        oe.apply(_settings(), None, digest, LOCAL)
    assert fake.writes == []


def test_apply_after_the_system_changed_changes_nothing(fake):
    shown = oe.assess(_settings(), None, LOCAL).plan.digest
    fake.values[oe.KEEP_ALIVE] = "10m"  # changed elsewhere after the confirm was drawn
    with pytest.raises(oe.StalePlan):
        oe.apply(_settings(), None, shown, LOCAL)
    assert fake.writes == []


def test_apply_with_the_shown_digest_sets_exactly_the_plan(fake):
    fake.values[oe.PARALLEL] = "8"
    plan = oe.assess(_settings(), None, LOCAL).plan
    done, record = oe.apply(_settings(), None, plan.digest, LOCAL)
    assert fake.writes == [{oe.KEEP_ALIVE: "2h"}]
    assert record.previous == {oe.KEEP_ALIVE: None}
    assert record.set_to == {oe.KEEP_ALIVE: "2h"}
    assert done == plan


def test_a_backend_that_cannot_set_writes_nothing(fake):
    fake.can_set = False
    plan = oe.assess(_settings(), None, LOCAL).plan
    with pytest.raises(oe.EnvChangeFailed):
        oe.apply(_settings(), None, plan.digest, LOCAL)
    assert fake.writes == []


def test_undo_restores_what_was_there_before_the_first_change(fake):
    fake.values[oe.KEEP_ALIVE] = "5m"
    plan = oe.assess(_settings(), None, LOCAL).plan
    _p, record = oe.apply(_settings(), None, plan.digest, LOCAL)
    # A second change (concurrency raised) keeps the original undo point.
    fake.values[oe.PARALLEL] = "2"
    stored = record.as_json()
    plan2 = oe.assess(_settings(t1=6), stored, LOCAL).plan
    _p2, record2 = oe.apply(_settings(t1=6), stored, plan2.digest, LOCAL)
    assert record2.previous == {oe.PARALLEL: None, oe.KEEP_ALIVE: "5m"}
    fake.writes.clear()
    oe.undo(record2.as_json(), record2.digest)
    assert fake.writes == [{oe.PARALLEL: None, oe.KEEP_ALIVE: "5m"}]


@pytest.mark.parametrize("digest", ["", "f" * 32])
def test_undo_without_the_record_digest_changes_nothing(fake, digest):
    record = oe.UndoRecord("fake", "a test place", {oe.KEEP_ALIVE: None}, {oe.KEEP_ALIVE: "2h"})
    with pytest.raises(oe.StalePlan):
        oe.undo(record.as_json(), digest)
    assert fake.writes == []


def test_an_undo_record_from_another_mechanism_is_not_offered_or_run(fake):
    record = oe.UndoRecord("other", "elsewhere", {oe.KEEP_ALIVE: None}, {oe.KEEP_ALIVE: "2h"})
    assert oe.assess(_settings(), record.as_json(), LOCAL).undo is None
    with pytest.raises(oe.StalePlan):
        oe.undo(record.as_json(), record.digest)
    assert fake.writes == []


def test_a_stored_record_is_rebuilt_through_an_allowlist():
    # Names outside the two are dropped; a missing earlier value stays None.
    raw = {"mechanism": "fake", "where": "x", "previous": {oe.PARALLEL: None, "PATH": "/evil"},
           "set_to": {oe.PARALLEL: "4", "PATH": "/evil"}}
    rec = oe.UndoRecord.from_json(raw)
    assert rec.previous == {oe.PARALLEL: None}
    assert rec.set_to == {oe.PARALLEL: "4"}
    # A non-text earlier value refuses the whole record (#181 round 5): read as None it would
    # make Undo delete a variable that was set.
    assert oe.UndoRecord.from_json({**raw, "previous": {oe.PARALLEL: 3}}) is None
    assert oe.UndoRecord.from_json({"mechanism": "fake"}) is None
    assert oe.UndoRecord.from_json("text") is None


def test_restart_only_where_the_backend_can(fake):
    fake.can_restart = False
    with pytest.raises(oe.EnvChangeFailed):
        oe.restart()
    assert fake.restarts == 0


# ------------------------------------------------------------------------- the runner
def test_every_command_runs_without_a_shell_as_utf8_text(monkeypatch):
    seen: list[dict] = []

    def fake_run(argv, **kw):
        seen.append({"argv": argv, **kw})
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    oe.run(["launchctl", "getenv", oe.PARALLEL])
    assert seen[0]["argv"] == ["launchctl", "getenv", oe.PARALLEL]
    assert seen[0]["shell"] is False
    assert seen[0]["encoding"] == "utf-8"
    assert seen[0]["text"] is True


class Recorder:
    """Stands in for oe.run: answers from a table, records every argument list."""

    def __init__(self, answers: dict | None = None) -> None:
        self.calls: list[list[str]] = []
        self.answers = answers or {}

    def __call__(self, argv):
        self.calls.append(list(argv))
        rc, out = self.answers.get(tuple(argv), (0, ""))
        return subprocess.CompletedProcess(argv, rc, out, "")


# ------------------------------------------------------------------------- macOS
def test_macos_reads_and_sets_with_launchctl(monkeypatch, tmp_path):
    rec = Recorder({("launchctl", "getenv", oe.KEEP_ALIVE): (0, "5m\n")})
    monkeypatch.setattr(oe, "run", rec)
    be = oe.MacBackend(home=tmp_path)
    assert be.owned() == {oe.PARALLEL: None, oe.KEEP_ALIVE: "5m"}
    rec.calls.clear()
    be.write({oe.PARALLEL: "4", oe.KEEP_ALIVE: None})
    assert rec.calls == [["launchctl", "setenv", oe.PARALLEL, "4"],
                         ["launchctl", "unsetenv", oe.KEEP_ALIVE]]
    assert "restart your Mac" in be.persistence


def test_macos_restart_is_offered_only_with_the_ollama_app(monkeypatch, tmp_path):
    rec = Recorder()
    monkeypatch.setattr(oe, "run", rec)
    monkeypatch.setattr(Path, "exists", lambda self: False)
    be = oe.MacBackend(home=tmp_path)
    assert not be.can_restart
    with pytest.raises(oe.EnvChangeFailed):
        be.restart()
    assert rec.calls == []


class _PgrepRecorder(Recorder):
    """pgrep answers "running" for the first `running_for` asks, then "not running"."""

    def __init__(self, running_for: int) -> None:
        super().__init__()
        self.running_for = running_for

    def __call__(self, argv):
        if argv[:1] == ["pgrep"]:
            self.calls.append(list(argv))
            self.running_for -= 1
            return subprocess.CompletedProcess(argv, 0 if self.running_for >= 0 else 1, "", "")
        return super().__call__(argv)


def test_macos_restart_quits_waits_for_exit_then_opens_the_app(monkeypatch, tmp_path):
    (tmp_path / "Applications" / "Ollama.app").mkdir(parents=True)
    rec = _PgrepRecorder(running_for=3)  # open; still quitting twice; then gone
    monkeypatch.setattr(oe, "run", rec)
    monkeypatch.setattr(oe.time, "sleep", lambda s: None)
    be = oe.MacBackend(home=tmp_path)
    assert be.can_restart
    be.restart()
    rest = [c for c in rec.calls if c[:1] != ["pgrep"]]
    assert rest == [["osascript", "-e", 'tell application "Ollama" to quit'],
                    ["open", "-a", "Ollama"]]
    # open ran only after pgrep had answered "not running" (running_for went below zero).
    assert rec.running_for < 0
    assert sum(c[:1] == ["pgrep"] for c in rec.calls) == 4


def test_macos_restart_leaves_ollama_alone_when_the_app_is_not_open(monkeypatch, tmp_path):
    (tmp_path / "Applications" / "Ollama.app").mkdir(parents=True)
    rec = _PgrepRecorder(running_for=0)
    monkeypatch.setattr(oe, "run", rec)
    with pytest.raises(oe.EnvChangeFailed, match="not open"):
        oe.MacBackend(home=tmp_path).restart()
    assert [c for c in rec.calls if c[:1] != ["pgrep"]] == []


def test_macos_restart_gives_up_when_the_app_does_not_quit(monkeypatch, tmp_path):
    (tmp_path / "Applications" / "Ollama.app").mkdir(parents=True)
    rec = _PgrepRecorder(running_for=10**6)
    monkeypatch.setattr(oe, "run", rec)
    clock = iter(range(0, 10**6, 5))
    monkeypatch.setattr(oe.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(oe.time, "sleep", lambda s: None)
    with pytest.raises(oe.EnvChangeFailed, match="did not quit"):
        oe.MacBackend(home=tmp_path).restart()
    assert ["open", "-a", "Ollama"] not in rec.calls


def test_a_failed_launchctl_raises_a_fixed_sentence(monkeypatch, tmp_path):
    monkeypatch.setattr(oe, "run", Recorder({("launchctl", "setenv", oe.PARALLEL, "4"):
                                             (1, "")}))
    with pytest.raises(oe.EnvChangeFailed, match="launchd environment"):
        oe.MacBackend(home=tmp_path).write({oe.PARALLEL: "4"})


# ------------------------------------------------------------------------- Windows
class FakeRegistry(oe.WindowsBackend):
    def __init__(self) -> None:
        super().__init__()
        self.reg: dict[str, str] = {}
        self.machine: dict[str, str] = {}
        self.kinds: dict[str, int] = {}
        self.broadcasts = 0

    def _reg_get(self, name):
        self._kinds.pop(name, None)
        if name in self.reg:
            self._kinds[name] = self.kinds.get(name, 1)
        return self.reg.get(name)

    def _reg_get_machine(self, name):
        return self.machine.get(name)

    def _reg_set(self, name, value):
        self.reg[name] = value

    def _reg_delete(self, name):
        self.reg.pop(name, None)

    def _broadcast(self):
        self.broadcasts += 1


def test_windows_writes_the_user_environment_and_broadcasts():
    be = FakeRegistry()
    be.reg[oe.KEEP_ALIVE] = "5m"
    be.write({oe.PARALLEL: "4", oe.KEEP_ALIVE: None})
    assert be.reg == {oe.PARALLEL: "4"}
    assert be.broadcasts == 1
    assert "HKEY_CURRENT_USER\\Environment" in be.where
    assert not be.can_restart  # Kestrel never closes Ollama on Windows
    assert be.commands({oe.PARALLEL: "4", oe.KEEP_ALIVE: None}) == [
        f"setx {oe.PARALLEL} 4", f'reg delete "HKCU\\Environment" /v {oe.KEEP_ALIVE} /f']


# ------------------------------------------------------------------------- Linux
def test_linux_prefers_a_user_service_then_a_system_one_then_the_profile(monkeypatch, tmp_path):
    answers = {("systemctl", "--user", "cat", "ollama.service"): True,
               ("systemctl", "cat", "ollama.service"): True}
    monkeypatch.setattr(oe, "_ok", lambda argv: answers.get(tuple(argv), False))
    assert oe.detect("linux", tmp_path).mechanism == "linux_systemd_user"
    answers[("systemctl", "--user", "cat", "ollama.service")] = False
    sys_be = oe.detect("linux", tmp_path)
    assert sys_be.mechanism == "linux_systemd_system" and not sys_be.can_set
    answers[("systemctl", "cat", "ollama.service")] = False
    assert oe.detect("linux", tmp_path).mechanism == "linux_profile"
    assert oe.detect("win32", tmp_path).mechanism == "windows_user_env"
    assert oe.detect("darwin", tmp_path).mechanism == "macos_launchctl"


def test_a_system_service_is_never_written(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(oe, "run", rec)
    be = oe.LinuxSystemServiceBackend()
    with pytest.raises(oe.EnvChangeFailed):
        be.write({oe.PARALLEL: "4"})
    assert not any(c[:1] == ["sudo"] for c in rec.calls)
    assert be.commands({oe.PARALLEL: "4"})[0] == "sudo systemctl edit ollama.service"


def test_linux_user_service_dropin_written_reloaded_and_removed(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    rec = Recorder()
    monkeypatch.setattr(oe, "run", rec)
    be = oe.LinuxUserServiceBackend(home=tmp_path)
    assert be.path == (tmp_path / ".config" / "systemd" / "user" / "ollama.service.d"
                       / "zz-kestrel-ollama-env.conf")
    hints = be.hints()
    assert hints == {"created": True, oe.FOLDER_HINT: 4}  # .config/systemd/user/ollama.service.d
    be.write({oe.PARALLEL: "4", oe.KEEP_ALIVE: "2h"})
    text = be.path.read_text(encoding="utf-8")
    assert "[Service]" in text
    assert f'Environment="{oe.PARALLEL}=4"' in text
    assert f'Environment="{oe.KEEP_ALIVE}=2h"' in text
    assert rec.calls[-1] == ["systemctl", "--user", "daemon-reload"]
    assert be.owned() == {oe.PARALLEL: "4", oe.KEEP_ALIVE: "2h"}
    be.restore({oe.PARALLEL: None, oe.KEEP_ALIVE: None}, hints)
    assert not be.path.exists()
    be.restart()
    assert rec.calls[-1] == ["systemctl", "--user", "restart", "ollama.service"]


def test_linux_user_service_reads_the_effective_environment(monkeypatch, tmp_path):
    out = f"{oe.PARALLEL}=3 OTHER=x {oe.KEEP_ALIVE}=24h\n"
    monkeypatch.setattr(oe, "run", Recorder({
        ("systemctl", "--user", "show", "ollama.service", "--property=Environment", "--value"):
        (0, out)}))
    be = oe.LinuxUserServiceBackend(home=tmp_path)
    assert be.read(oe.PARALLEL) == "3" and be.read(oe.KEEP_ALIVE) == "24h"


def test_linux_profile_block_is_added_and_removed_byte_for_byte(monkeypatch, tmp_path):
    monkeypatch.delenv(oe.PARALLEL, raising=False)
    profile = tmp_path / ".profile"
    original = "# my own lines\nexport EDITOR=nano\n"
    profile.write_text(original, encoding="utf-8")
    profile.chmod(0o640)
    be = oe.LinuxProfileBackend(home=tmp_path)
    be.write({oe.PARALLEL: "4", oe.KEEP_ALIVE: "2h"})
    text = profile.read_text(encoding="utf-8")
    assert text.startswith(original)
    assert f"export {oe.PARALLEL}=4" in text
    assert be.owned() == {oe.PARALLEL: "4", oe.KEEP_ALIVE: "2h"}
    assert profile.stat().st_mode & 0o777 == _mode_kept(0o640)
    be.write({oe.PARALLEL: None, oe.KEEP_ALIVE: None})
    assert profile.read_text(encoding="utf-8") == original
    assert not be.can_restart


def test_linux_profile_is_not_created_for_an_undo_with_nothing_to_remove(tmp_path):
    be = oe.LinuxProfileBackend(home=tmp_path)
    be.write({oe.PARALLEL: None})
    assert not (tmp_path / ".profile").exists()


# ------------------------------------------------------------------------- web
def _client():
    from starlette.testclient import TestClient

    from kestrel.db import migrate
    from kestrel.db.engine import reset_engine
    from kestrel.web.app import build_app

    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations(paths.db_path())
    asyncio.run(reset_engine())
    return TestClient(build_app(worker=False))


def _stored() -> object:
    async def _go():
        from kestrel.db.engine import get_sessionmaker
        from kestrel.db.models import Setting

        async with get_sessionmaker()() as s:
            row = await s.get(Setting, oe.UNDO_KEY)
            return row.value if row is not None else None

    return asyncio.run(_go())


def _digest(html: str, field: str) -> str:
    import re

    m = re.search(rf'name="{field}" value="([0-9a-f]+)"', html)
    assert m, f"no {field} digest on the page"
    return m.group(1)


@pytest.fixture
def web(fake, _isolate_data_home):
    return _client()


def test_the_section_and_its_review_change_nothing(web, fake):
    page = web.get("/setup/ollama-env")
    assert page.status_code == 200
    assert 'data-testid="oe-review"' in page.text and 'data-testid="oe-apply"' not in page.text
    review = web.get("/setup/ollama-env?review=1")
    assert 'data-testid="oe-confirm"' in review.text
    for words in (oe.PARALLEL, oe.KEEP_ALIVE, "<b>4</b>", "<b>2h</b>", "a test place",
                  "To undo it: press Undo", "Ollama must restart"):
        assert words in review.text
    assert "Show details" in review.text and "fake set" in review.text
    assert fake.writes == [] and _stored() is None


@pytest.mark.parametrize("form", [{}, {"plan": ""}, {"plan": "0" * 32}, {"confirmed": "1"}])
def test_an_apply_post_without_the_shown_digest_changes_nothing(web, fake, form):
    r = web.post("/setup/ollama-env/apply", data=form)
    assert r.status_code == 200
    assert "changed since you looked" in r.text
    assert fake.writes == [] and _stored() is None


def test_the_confirmed_apply_sets_stores_undo_and_offers_restart(web, fake):
    digest = _digest(web.get("/setup/ollama-env?review=1").text, "plan")
    r = web.post("/setup/ollama-env/apply", data={"plan": digest})
    assert fake.writes == [{oe.PARALLEL: "4", oe.KEEP_ALIVE: "2h"}]
    assert "restart Ollama now" in r.text
    assert 'data-testid="oe-restart"' in r.text and 'data-testid="oe-undo"' in r.text
    assert _stored()["previous"] == {oe.PARALLEL: None, oe.KEEP_ALIVE: None}
    # Undo needs its own digest; a bare post changes nothing.
    fake.writes.clear()
    web.post("/setup/ollama-env/undo", data={})
    assert fake.writes == [] and _stored() is not None
    r = web.post("/setup/ollama-env/undo", data={"undo": _digest(r.text, "undo")})
    assert fake.writes == [{oe.PARALLEL: None, oe.KEEP_ALIVE: None}]
    assert "Undone" in r.text and _stored() is None


def test_restart_needs_its_own_button_and_waits_for_running_work(web, fake):
    web.post("/setup/ollama-env/restart", data={})
    assert fake.restarts == 0

    async def _lease():
        from kestrel.db.engine import get_sessionmaker
        from kestrel.db.models import Task

        async with get_sessionmaker()() as s:
            s.add(Task(type="tier1", status="leased", payload={}))
            await s.commit()

    asyncio.run(_lease())
    r = web.post("/setup/ollama-env/restart", data={"restart": "ollama"})
    assert fake.restarts == 0 and "using Ollama right now" in r.text


def test_restart_runs_when_idle_and_is_hidden_where_unsafe(web, fake):
    r = web.post("/setup/ollama-env/restart", data={"restart": "ollama"})
    assert fake.restarts == 1 and "restarting" in r.text
    fake.can_restart = False
    r = web.post("/setup/ollama-env/apply", data={
        "plan": _digest(web.get("/setup/ollama-env?review=1").text, "plan")})
    assert 'data-testid="oe-restart"' not in r.text and "restart it" in r.text


def test_a_backend_that_cannot_set_shows_why_and_no_button(web, fake):
    fake.can_set = False
    for url in ("/setup/ollama-env", "/setup/ollama-env?review=1"):
        text = web.get(url).text
        assert 'data-testid="oe-why-not"' in text
        assert 'data-testid="oe-review"' not in text and 'data-testid="oe-apply"' not in text


def test_settings_this_computer_loads_the_section():
    html = (Path(__file__).resolve().parents[1] / "kestrel" / "web" / "templates" / "partials"
            / "st_computer.html").read_text(encoding="utf-8")
    assert 'hx-get="/setup/ollama-env"' in html


# ------------------------------------------------------------------------- review round 1
@pytest.mark.parametrize("value", ["2h\nExecStartPre=/bin/true", 'a"b', "a\\b", "%h", "$HOME",
                                   "`x`", "x" * 65])
def test_an_unsafe_value_is_never_written_or_kept_for_undo(value, tmp_path, monkeypatch):
    monkeypatch.setattr(oe, "run", Recorder())
    assert not oe.safe_value(value)
    for be in (oe.LinuxUserServiceBackend(home=tmp_path), oe.LinuxProfileBackend(home=tmp_path)):
        with pytest.raises(oe.EnvChangeFailed):
            be.write({oe.KEEP_ALIVE: value})
    assert not (tmp_path / ".profile").exists()
    assert not any(tmp_path.rglob("*.conf"))
    raw = {"mechanism": "fake", "where": "x", "previous": {oe.KEEP_ALIVE: value},
           "set_to": {oe.KEEP_ALIVE: "2h"}}
    assert oe.UndoRecord.from_json(raw) is None


def test_every_value_kestrel_sets_is_safe():
    for need in range(1, 17):
        assert oe.safe_value(str(need))
    assert oe.safe_value(oe.KEEP_ALIVE_VALUE)


def test_an_unsafe_current_value_stops_apply_before_any_write(fake):
    fake.values[oe.KEEP_ALIVE] = "5m\nx"
    plan = oe.assess(_settings(), None, LOCAL).plan
    with pytest.raises(oe.EnvChangeFailed, match="Undo"):
        oe.apply(_settings(), None, plan.digest, LOCAL)
    assert fake.writes == []


def test_a_symlinked_profile_keeps_its_link(tmp_path):
    real = tmp_path / "dotfiles" / "profile"
    real.parent.mkdir()
    real.write_text("export EDITOR=nano\n", encoding="utf-8")
    _symlink(tmp_path / ".profile", real)
    be = oe.LinuxProfileBackend(home=tmp_path)
    be.write({oe.PARALLEL: "4"})
    assert (tmp_path / ".profile").is_symlink()
    assert f"export {oe.PARALLEL}=4" in real.read_text(encoding="utf-8")


class _HalfWrite(FakeBackend):
    """Writes the first value, then fails: a registry write that stops halfway."""

    def write(self, values):
        first = next(iter(values))
        self.values[first] = values[first]
        self.writes.append({first: values[first]})
        raise oe.EnvChangeFailed("Could not save your Windows environment variables.")


def test_a_half_finished_apply_still_offers_undo(monkeypatch, _isolate_data_home):
    be = _HalfWrite()
    monkeypatch.setattr(oe, "_override", [be])
    monkeypatch.setattr("kestrel.llm.profiles.ollama_needed", lambda s: True)
    client = _client()
    digest = _digest(client.get("/setup/ollama-env?review=1").text, "plan")
    r = client.post("/setup/ollama-env/apply", data={"plan": digest})
    assert "press Undo to put them back" in r.text
    assert 'data-testid="oe-undo"' in r.text
    # Only the name that was written is in the record: the unwritten one is never offered
    # to Undo, so it is never reported as "changed after Kestrel set it".
    assert _stored()["previous"] == {oe.PARALLEL: None}
    assert _stored()["set_to"] == {oe.PARALLEL: "4"}


# ------------------------------------------------------------------------- review round 2
def test_a_record_from_another_place_is_neither_offered_nor_run(fake):
    record = oe.UndoRecord("fake", "another computer's place", {oe.KEEP_ALIVE: "9m"},
                           {oe.KEEP_ALIVE: "2h"})
    fake.values[oe.KEEP_ALIVE] = "2h"
    assert oe.assess(_settings(), record.as_json(), LOCAL).undo is None
    with pytest.raises(oe.StalePlan):
        oe.undo(record.as_json(), record.digest)
    assert fake.writes == []


def test_a_restored_backup_drops_the_undo_record():
    from kestrel.services import backup

    assert oe.UNDO_KEY in backup.DROPPED_SETTING_KEYS


def test_undo_leaves_a_value_the_person_changed_since(fake):
    plan = oe.assess(_settings(), None, LOCAL).plan
    _p, record = oe.apply(_settings(), None, plan.digest, LOCAL)
    fake.values[oe.PARALLEL] = "16"  # the person raised it themselves afterwards
    fake.writes.clear()
    _r, skipped = oe.undo(record.as_json(), record.digest)
    assert skipped == [oe.PARALLEL]
    assert fake.writes == [{oe.KEEP_ALIVE: None}]
    assert fake.values[oe.PARALLEL] == "16"


def test_windows_shows_the_system_value_ollama_inherits():
    be = FakeRegistry()
    be.machine[oe.PARALLEL] = "8"
    assert be.read(oe.PARALLEL) == "8"
    assert oe.judge(oe.PARALLEL, be.read(oe.PARALLEL), 4).target is None  # never lowered
    be.reg[oe.PARALLEL] = "2"
    assert be.read(oe.PARALLEL) == "2"  # the user value wins


def test_windows_never_overwrites_a_value_undo_could_not_restore(fake, monkeypatch):
    reg = FakeRegistry()
    reg.reg[oe.KEEP_ALIVE] = "5m"
    reg.kinds[oe.KEEP_ALIVE] = 2  # REG_EXPAND_SZ
    monkeypatch.setattr(oe, "_override", [reg])
    plan = oe.assess(_settings(), None, LOCAL).plan
    with pytest.raises(oe.EnvChangeFailed, match="Undo"):
        oe.apply(_settings(), None, plan.digest, LOCAL)
    assert reg.reg == {oe.KEEP_ALIVE: "5m"} and reg.broadcasts == 0


class _Outranked(FakeBackend):
    """Saves the value, but another setting keeps the effective value as it was."""

    def read(self, name):
        return "1" if name == oe.PARALLEL else self.values.get(name)


def test_a_change_another_setting_outranks_is_reported_with_undo(monkeypatch):
    be = _Outranked()
    monkeypatch.setattr(oe, "_override", [be])
    monkeypatch.setattr("kestrel.llm.profiles.ollama_needed", lambda s: True)
    plan = oe.assess(_settings(), None, LOCAL).plan
    with pytest.raises(oe.EnvChangeFailed, match="another setting") as caught:
        oe.apply(_settings(), None, plan.digest, LOCAL)
    assert caught.value.record is not None
    assert caught.value.record.set_to[oe.PARALLEL] == "4"


def test_the_dropin_sorts_after_an_override_file():
    assert sorted(["override.conf", "50-x.conf", "99-y.conf", oe._DROPIN_NAME])[-1] == \
        oe._DROPIN_NAME


def test_a_private_profile_stays_private_and_leaves_no_temp_file(tmp_path):
    profile = tmp_path / ".profile"
    profile.write_text("export EDITOR=nano\n", encoding="utf-8")
    profile.chmod(0o600)
    oe.LinuxProfileBackend(home=tmp_path).write({oe.PARALLEL: "4"})
    assert profile.stat().st_mode & 0o777 == _mode_kept(0o600)
    assert sorted(p.name for p in tmp_path.iterdir()) == [".profile"]


def test_a_key_holder_sees_no_values_and_runs_no_command(web, fake, monkeypatch):
    calls: list = []
    monkeypatch.setattr(oe, "assess", lambda *a, **k: calls.append(a))
    monkeypatch.setattr("kestrel.web.pages.ollama_env.request_is_local", lambda r: False)
    text = web.get("/setup/ollama-env").text
    assert "Only this computer can see or change these." in text
    assert "a test place" not in text and 'data-testid="oe-row"' not in text
    assert calls == []


def test_a_cross_site_post_restarts_nothing(web, fake):
    r = web.post("/setup/ollama-env/restart", data={"restart": "ollama"},
                 headers={"Origin": "https://elsewhere.example"})
    assert r.status_code == 403
    assert fake.restarts == 0


def test_the_buttons_disable_while_their_request_runs():
    html = (Path(__file__).resolve().parents[1] / "kestrel" / "web" / "templates" / "partials"
            / "ollama_env.html").read_text(encoding="utf-8")
    assert html.count('hx-disabled-elt="find button"') == 3


# ------------------------------------------------------------------------- review round 2
_START = oe._BLOCK_START
_END = oe._BLOCK_END
_MINE_BEFORE = "alias ll='ls -l'\n"
_MINE_AFTER = "export PATH=$HOME/bin:$PATH\nalias gs='git status'\n"


@pytest.mark.parametrize("middle", [
    # the end marker deleted by hand: the person's lines after it would be eaten
    f"{_START}\nexport {oe.PARALLEL}=4\n",
    # the start marker deleted by hand
    f"export {oe.PARALLEL}=4\n{_END}\n",
    # a line of the person's own added inside the block
    f"{_START}\nexport {oe.PARALLEL}=4\nexport EDITOR=vim\n{_END}\n",
    # the same name twice
    f"{_START}\nexport {oe.PARALLEL}=4\nexport {oe.PARALLEL}=8\n{_END}\n",
    # a value edited to something outside the allowlist
    f"{_START}\nexport {oe.PARALLEL}=\"4 8\"\n{_END}\n",
    # the markers in the wrong order
    f"{_END}\nexport {oe.PARALLEL}=4\n{_START}\n",
    # two blocks
    f"{_START}\nexport {oe.PARALLEL}=4\n{_END}\n{_START}\nexport {oe.KEEP_ALIVE}=2h\n{_END}\n",
    # a marker line edited (indented)
    f"  {_START}\nexport {oe.PARALLEL}=4\n{_END}\n",
])
def test_a_hand_edited_block_is_never_rewritten(tmp_path, middle):
    profile = tmp_path / ".profile"
    original = _MINE_BEFORE + middle + _MINE_AFTER
    profile.write_text(original, encoding="utf-8")
    be = oe.LinuxProfileBackend(home=tmp_path)
    assert be.blocked()
    assert be.owned() == {oe.PARALLEL: None, oe.KEEP_ALIVE: None}
    for values in ({oe.PARALLEL: "4", oe.KEEP_ALIVE: "2h"}, {oe.PARALLEL: None}):
        with pytest.raises(oe.EnvChangeFailed, match="changed by hand"):
            be.write(values)
        assert profile.read_text(encoding="utf-8") == original


def test_an_exact_block_is_removed_and_the_persons_lines_kept(tmp_path):
    profile = tmp_path / ".profile"
    profile.write_text(_MINE_BEFORE + _MINE_AFTER, encoding="utf-8")
    be = oe.LinuxProfileBackend(home=tmp_path)
    be.write({oe.PARALLEL: "4", oe.KEEP_ALIVE: "2h"})
    assert not be.blocked()
    be.write({oe.PARALLEL: None, oe.KEEP_ALIVE: None})
    assert profile.read_text(encoding="utf-8") == _MINE_BEFORE + _MINE_AFTER


def test_a_damaged_profile_offers_neither_set_nor_undo(fake, monkeypatch, tmp_path):
    (tmp_path / ".profile").write_text(f"{_START}\nexport {oe.PARALLEL}=4\n" + _MINE_AFTER,
                                       encoding="utf-8")
    be = oe.LinuxProfileBackend(home=tmp_path)
    monkeypatch.setattr(oe, "_override", [be])
    monkeypatch.delenv(oe.PARALLEL, raising=False)
    monkeypatch.delenv(oe.KEEP_ALIVE, raising=False)
    record = oe.UndoRecord(be.mechanism, be.where, {oe.PARALLEL: None}, {oe.PARALLEL: "4"})
    a = oe.assess(_settings(), record.as_json(), LOCAL)
    assert "changed by hand" in a.blocked
    with pytest.raises(oe.EnvChangeFailed, match="changed by hand"):
        oe.apply(_settings(), record.as_json(), a.plan.digest, LOCAL)
    with pytest.raises(oe.EnvChangeFailed, match="changed by hand"):
        oe.undo(record.as_json(), record.digest)


def test_undo_removes_a_profile_kestrel_created(tmp_path):
    be = oe.LinuxProfileBackend(home=tmp_path)
    hints = be.hints()
    assert hints["created"] is True
    be.write({oe.PARALLEL: "4"})
    assert (tmp_path / ".profile").exists()
    be.restore({oe.PARALLEL: None}, hints)
    assert not (tmp_path / ".profile").exists()


@pytest.mark.parametrize("value", ["1&x", "1|x", "1;x", "1<x", "1>x", "1'x", "1 x", "/x", "",
                                   "x" * 65])
def test_the_value_allowlist_keeps_shell_characters_out(value):
    assert not oe.safe_value(value)
    raw = {"mechanism": "fake", "where": "x", "previous": {oe.PARALLEL: value},
           "set_to": {oe.PARALLEL: "4"}}
    assert oe.UndoRecord.from_json(raw) is None


@pytest.mark.parametrize("value", ["4", "16", "2h", "-1", "-1m", "1h30m", "1.5h", "24h"])
def test_the_value_allowlist_keeps_every_ordinary_value(value):
    assert oe.safe_value(value)


class _HalfWriteOnce(_HalfWrite):
    """Stops halfway on the first write only; Undo's write then works."""

    failed = False

    def write(self, values):
        if self.failed:
            return FakeBackend.write(self, values)
        self.failed = True
        return super().write(values)


def test_undo_after_a_half_finished_apply_reports_nothing_as_changed_since(
        monkeypatch, _isolate_data_home):
    be = _HalfWriteOnce()
    monkeypatch.setattr(oe, "_override", [be])
    monkeypatch.setattr("kestrel.llm.profiles.ollama_needed", lambda s: True)
    client = _client()
    r = client.post("/setup/ollama-env/apply", data={
        "plan": _digest(client.get("/setup/ollama-env?review=1").text, "plan")})
    r = client.post("/setup/ollama-env/undo", data={"undo": _digest(r.text, "undo")})
    assert "Undone" in r.text
    assert "changed after Kestrel set them" not in r.text
    assert be.values == {oe.PARALLEL: None, oe.KEEP_ALIVE: None}


def test_a_half_finished_apply_that_wrote_nothing_offers_no_undo(fake, monkeypatch):
    def fail(values):
        raise oe.EnvChangeFailed("Could not save.")

    monkeypatch.setattr(fake, "write", fail)
    plan = oe.assess(_settings(), None, LOCAL).plan
    with pytest.raises(oe.EnvChangeFailed) as caught:
        oe.apply(_settings(), None, plan.digest, LOCAL)
    assert caught.value.record is None


def _doctor_settings(url: str = LOCAL):
    s = _settings()
    s.llm = types.SimpleNamespace(ollama_native_url=url)
    return s


def _doctor_rows(s) -> dict:
    from kestrel import doctor as d

    rep = d.DoctorReport()
    d._check_ollama_env(rep, s)
    return {r.name: r for r in rep.results}


def test_the_doctor_reads_where_ollama_reads_and_agrees_with_the_section(fake, monkeypatch):
    from kestrel import doctor as d

    monkeypatch.setenv(oe.PARALLEL, "16")  # Kestrel's own environment: not what Ollama sees
    monkeypatch.setenv(oe.KEEP_ALIVE, "24h")
    rows = _doctor_rows(_doctor_settings())
    assert rows[oe.PARALLEL].status == d.WARN and rows[oe.KEEP_ALIVE].status == d.WARN
    assert "Settings > This computer" in rows[oe.PARALLEL].remediation
    assert f"{oe.PARALLEL}=4" in rows[oe.PARALLEL].remediation
    fake.values.update({oe.PARALLEL: "4", oe.KEEP_ALIVE: "2h"})
    rows = _doctor_rows(_doctor_settings())
    assert rows[oe.PARALLEL].status == d.PASS and rows[oe.KEEP_ALIVE].status == d.PASS


def test_the_doctor_says_where_to_set_them_for_a_remote_ollama(fake):
    from kestrel import doctor as d

    rows = _doctor_rows(_doctor_settings("http://gpu-box.example:11434"))
    assert rows[oe.PARALLEL].status == d.INFO
    assert "gpu-box.example" in rows[oe.PARALLEL].detail


def test_the_models_speed_note_links_to_the_section():
    html = (Path(__file__).resolve().parents[1] / "kestrel" / "web" / "templates" / "partials"
            / "models_speed.html").read_text(encoding="utf-8")
    assert 'href="/settings#computer"' in html and "Ollama speed settings" in html


# ------------------------------------------------------------------------- review round 3
@pytest.mark.parametrize("raw", [b"export EDITOR=\xff\xfe nano\n", b"\x80\x81\x82"])
def test_a_profile_that_is_not_utf8_blocks_and_never_crashes(tmp_path, raw, monkeypatch):
    profile = tmp_path / ".profile"
    profile.write_bytes(raw)
    be = oe.LinuxProfileBackend(home=tmp_path)
    assert "could not read" in be.blocked()
    assert be.owned() == {oe.PARALLEL: None, oe.KEEP_ALIVE: None}
    with pytest.raises(oe.EnvChangeFailed, match="could not read"):
        be.write({oe.PARALLEL: "4"})
    assert profile.read_bytes() == raw


def test_an_unreadable_profile_shows_the_section_and_the_doctor_row(fake, monkeypatch, tmp_path):
    from kestrel import doctor as d

    (tmp_path / ".profile").write_bytes(b"\xff\xfe")
    monkeypatch.setattr(oe, "_override", [oe.LinuxProfileBackend(home=tmp_path)])
    monkeypatch.delenv(oe.PARALLEL, raising=False)
    a = oe.assess(_settings(), None, LOCAL)
    assert a.shown and "could not read" in a.blocked
    rows = _doctor_rows(_doctor_settings())
    assert rows[oe.PARALLEL].status == d.WARN


def test_a_profile_that_cannot_be_opened_blocks(tmp_path, monkeypatch):
    (tmp_path / ".profile").write_text("x\n", encoding="utf-8")

    def denied(self):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_bytes", denied)
    assert "could not read" in oe.LinuxProfileBackend(home=tmp_path).blocked()


@pytest.mark.parametrize("original", [
    b"alias ll='ls -l'\r\nexport A=1\r\n",          # Windows line endings
    b"alias ll='ls -l'\nexport A=1",                 # no final newline
    b"alias ll='ls -l'\r\nexport A=1",               # both
    b"",                                             # an empty profile the person made
])
def test_set_then_undo_restores_the_profile_byte_for_byte(tmp_path, original):
    profile = tmp_path / ".profile"
    profile.write_bytes(original)
    be = oe.LinuxProfileBackend(home=tmp_path)
    hints = be.hints()
    be.write({oe.PARALLEL: "4", oe.KEEP_ALIVE: "2h"})
    after = profile.read_bytes()
    assert after.startswith(original.rstrip(b"\r\n")) and b"export OLLAMA_NUM_PARALLEL=4" in after
    be.restore({oe.PARALLEL: None, oe.KEEP_ALIVE: None}, hints)
    assert profile.exists()
    assert profile.read_bytes() == original


def test_undo_keeps_an_empty_symlinked_dotfile(tmp_path):
    real = tmp_path / "dotfiles" / "profile"
    real.parent.mkdir()
    real.write_bytes(b"")
    _symlink(tmp_path / ".profile", real)
    be = oe.LinuxProfileBackend(home=tmp_path)
    hints = be.hints()
    assert hints["created"] is False
    be.write({oe.PARALLEL: "4"})
    be.restore({oe.PARALLEL: None}, hints)
    assert real.exists() and real.read_bytes() == b""
    assert (tmp_path / ".profile").is_symlink() and (tmp_path / ".profile").exists()


def test_a_read_only_profile_is_never_written(tmp_path, monkeypatch):
    profile = tmp_path / ".profile"
    profile.write_text("export EDITOR=nano\n", encoding="utf-8")
    real_access = os.access
    monkeypatch.setattr(oe.os, "access", lambda p, mode: False if Path(p) == profile.resolve()
                        and mode == os.W_OK else real_access(p, mode))
    be = oe.LinuxProfileBackend(home=tmp_path)
    assert "read-only" in be.blocked()
    with pytest.raises(oe.EnvChangeFailed, match="read-only"):
        be.write({oe.PARALLEL: "4"})
    assert profile.read_text(encoding="utf-8") == "export EDITOR=nano\n"


def test_the_record_keeps_the_hints_through_storage(fake, monkeypatch, tmp_path):
    be = oe.LinuxProfileBackend(home=tmp_path)
    monkeypatch.setattr(oe, "_override", [be])
    monkeypatch.delenv(oe.PARALLEL, raising=False)
    monkeypatch.delenv(oe.KEEP_ALIVE, raising=False)
    plan = oe.assess(_settings(), None, LOCAL).plan
    _p, record = oe.apply(_settings(), None, plan.digest, LOCAL)
    stored = record.as_json()
    assert stored["hints"] == {"created": True, "no_final_newline": False}
    assert oe.UndoRecord.from_json({**stored, "hints": {"created": "yes", "rm": True}}).hints == {}
    oe.undo(stored, oe.UndoRecord.from_json(stored).digest)
    assert not (tmp_path / ".profile").exists()


def test_a_retried_undo_counts_a_restored_name_as_done(fake):
    plan = oe.assess(_settings(), None, LOCAL).plan
    _p, record = oe.apply(_settings(), None, plan.digest, LOCAL)
    fake.values[oe.PARALLEL] = None  # the first Undo got this far, then stopped
    fake.writes.clear()
    _r, skipped = oe.undo(record.as_json(), record.digest)
    assert skipped == []
    assert fake.writes == [{oe.KEEP_ALIVE: None}]


def test_an_outranked_change_shows_one_sentence(monkeypatch, _isolate_data_home):
    be = _Outranked()
    monkeypatch.setattr(oe, "_override", [be])
    monkeypatch.setattr("kestrel.llm.profiles.ollama_needed", lambda s: True)
    client = _client()
    r = client.post("/setup/ollama-env/apply", data={
        "plan": _digest(client.get("/setup/ollama-env?review=1").text, "plan")})
    assert "another setting on this computer" in r.text
    assert "Some values may have changed" not in r.text
    assert 'data-testid="oe-undo"' in r.text


@pytest.mark.parametrize("error", [UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
                                   PermissionError("denied"), ValueError("odd")])
def test_the_doctor_never_fails_on_a_place_it_cannot_read(fake, monkeypatch, error):
    from kestrel import doctor as d

    def broken(*a, **k):
        raise error

    monkeypatch.setattr(oe, "assess", broken)
    rows = _doctor_rows(_doctor_settings())
    assert rows[oe.PARALLEL].status == d.INFO and rows[oe.KEEP_ALIVE].status == d.INFO


def test_a_crlf_profile_gets_a_crlf_block(tmp_path):
    profile = tmp_path / ".profile"
    profile.write_bytes(b"alias ll='ls -l'\r\n")
    oe.LinuxProfileBackend(home=tmp_path).write({oe.PARALLEL: "4"})
    after = profile.read_bytes()
    assert b"\r\n" + oe._BLOCK_START.encode() + b"\r\n" in after
    assert b"\n" not in after.replace(b"\r\n", b"")


# ------------------------------------------------------------------------- review round 4
def _dropin(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(oe, "run", Recorder())
    be = oe.LinuxUserServiceBackend(home=tmp_path)
    be.path.parent.mkdir(parents=True)
    return be


_THEIRS = ("[Service]\nLimitNOFILE=65536\nEnvironment=\"OLLAMA_NUM_PARALLEL=8\"\n")


@pytest.mark.parametrize("content", [
    _THEIRS,                                                           # the person's own file
    "\n".join(oe._DROPIN_HEADER) + "\nLimitNOFILE=65536\n",           # a line added to Kestrel's
    "\n".join(oe._DROPIN_HEADER) + '\nEnvironment="OLLAMA_NUM_PARALLEL=4 8"\n',
    "\n".join(oe._DROPIN_HEADER)
    + '\nEnvironment="OLLAMA_NUM_PARALLEL=4"\nEnvironment="OLLAMA_NUM_PARALLEL=8"\n',
])
def test_a_dropin_kestrel_did_not_write_is_never_rewritten_or_deleted(tmp_path, monkeypatch,
                                                                      content):
    be = _dropin(tmp_path, monkeypatch)
    be.path.write_text(content, encoding="utf-8")
    assert "did not write" in be.blocked()
    assert be.owned() == {oe.PARALLEL: None, oe.KEEP_ALIVE: None}
    for hints in ({}, {"created": True}):
        with pytest.raises(oe.EnvChangeFailed, match="did not write"):
            be.restore({oe.PARALLEL: None, oe.KEEP_ALIVE: None}, hints)
        with pytest.raises(oe.EnvChangeFailed, match="did not write"):
            be.write({oe.PARALLEL: "4"})
    assert be.path.read_text(encoding="utf-8") == content


def test_a_kestrel_dropin_from_before_the_first_change_is_kept_by_undo(tmp_path, monkeypatch):
    be = _dropin(tmp_path, monkeypatch)
    # newline="\n": Windows text mode would write CRLF, which no Kestrel drop-in holds.
    be.path.write_text("\n".join(oe._DROPIN_HEADER) + "\n", encoding="utf-8", newline="\n")
    hints = be.hints()
    assert hints == {"created": False, oe.FOLDER_HINT: 0}
    be.write({oe.PARALLEL: "4"})
    be.restore({oe.PARALLEL: None}, hints)
    assert be.path.read_text(encoding="utf-8") == "\n".join(oe._DROPIN_HEADER) + "\n"


@pytest.mark.parametrize("raw", [b"\xff\xfe[Service]\n", b"[Service]\nEnvironment=\"\x80\"\n"])
def test_a_dropin_that_is_not_utf8_blocks(tmp_path, monkeypatch, raw):
    be = _dropin(tmp_path, monkeypatch)
    be.path.write_bytes(raw)
    assert "could not read" in be.blocked()
    with pytest.raises(oe.EnvChangeFailed, match="could not read"):
        be.write({oe.PARALLEL: "4"})
    assert be.path.read_bytes() == raw


def test_a_dropin_that_cannot_be_opened_blocks(tmp_path, monkeypatch):
    be = _dropin(tmp_path, monkeypatch)
    be.path.write_text("\n".join(oe._DROPIN_HEADER) + "\n", encoding="utf-8")

    def denied(self):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_bytes", denied)
    assert "could not read" in be.blocked()


def test_a_symlink_loop_profile_blocks_and_never_crashes(tmp_path, monkeypatch):
    _symlink(tmp_path / ".profile", tmp_path / "loop")
    _symlink(tmp_path / "loop", tmp_path / ".profile")
    be = oe.LinuxProfileBackend(home=tmp_path)
    assert be.blocked()
    with pytest.raises(oe.EnvChangeFailed):
        be.write({oe.PARALLEL: "4"})
    assert be.hints()["created"] in (True, False)


@pytest.mark.parametrize("error", [RuntimeError("loop"), PermissionError("denied"),
                                   UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
                                   subprocess.TimeoutExpired(["x"], 1)])
def test_no_backend_error_reaches_the_page_or_the_doctor(web, fake, monkeypatch, error):
    from kestrel import doctor as d

    def broken(name):
        raise error

    monkeypatch.setattr(fake, "read", broken)
    for url in ("/setup/ollama-env", "/setup/ollama-env?review=1"):
        r = web.get(url)
        assert r.status_code == 200 and "could not read or change" in r.text
    for path, data in (("/setup/ollama-env/apply", {"plan": "0" * 32}),
                       ("/setup/ollama-env/undo", {"undo": "0" * 32}),
                       ("/setup/ollama-env/restart", {"restart": "ollama"})):
        assert web.post(path, data=data).status_code == 200
    assert fake.writes == []
    assert fake.restarts == 1  # Restart reads nothing, so it still runs on its own button
    rows = _doctor_rows(_doctor_settings())
    assert rows[oe.PARALLEL].status == d.INFO


def test_every_entry_point_is_guarded():
    for fn in (oe.assess, oe.apply, oe.undo, oe.restart):
        assert getattr(fn, "__wrapped__", None) is not None, fn.__name__


# ------------------------------------------------------------------------- review round 5
def _tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_undo_removes_the_folders_kestrel_created_for_the_dropin(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(oe, "run", Recorder())
    before = _tree(tmp_path)
    be = oe.LinuxUserServiceBackend(home=tmp_path)
    hints = be.hints()
    be.write({oe.PARALLEL: "4"})
    assert be.path.exists()
    be.restore({oe.PARALLEL: None}, hints)
    assert _tree(tmp_path) == before == []


def test_undo_keeps_folders_that_were_there_or_are_not_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(oe, "run", Recorder())
    (tmp_path / ".config" / "app").mkdir(parents=True)  # the person's own config folder
    be = oe.LinuxUserServiceBackend(home=tmp_path)
    hints = be.hints()
    be.write({oe.PARALLEL: "4"})
    (be.path.parent / "override.conf").write_text("[Service]\n", encoding="utf-8")
    be.restore({oe.PARALLEL: None}, hints)
    assert (tmp_path / ".config" / "app").is_dir()
    assert (be.path.parent / "override.conf").exists()  # a folder with someone else's file stays
    assert not be.path.exists()


@pytest.mark.parametrize("value", [4, 4.0, True, ["4"], {"v": "4"}])
def test_a_record_with_a_non_text_previous_value_is_refused_whole(value):
    raw = {"mechanism": "fake", "where": "x", "previous": {oe.PARALLEL: value},
           "set_to": {oe.PARALLEL: "8"}}
    assert oe.UndoRecord.from_json(raw) is None


@pytest.mark.parametrize("hints", [{"created_folders": -1}, {"created_folders": 99},
                                   {"created_folders": True}, {"created_folders": "3"}])
def test_folder_hints_outside_the_allowlist_are_dropped(hints):
    raw = {"mechanism": "fake", "where": "x", "previous": {oe.PARALLEL: None},
           "set_to": {oe.PARALLEL: "4"}, "hints": hints}
    assert "created_folders" not in oe.UndoRecord.from_json(raw).hints


def test_a_file_is_written_where_the_system_has_no_fchmod(tmp_path, monkeypatch):
    """Windows before Python 3.13 has no os.fchmod. The release check on Windows stopped on that
    line (D-CI-595); the write must work there instead of raising AttributeError."""
    target = tmp_path / "override.conf"
    monkeypatch.delattr(os, "fchmod", raising=False)
    assert not hasattr(os, "fchmod")
    oe._write_text(target, "[Service]\nEnvironment=A=1\n")
    assert target.read_bytes() == b"[Service]\nEnvironment=A=1\n"
    assert [p.name for p in tmp_path.iterdir()] == ["override.conf"]  # no temporary file left


def test_a_rewrite_keeps_the_permission_bits_where_fchmod_exists(tmp_path):
    if not hasattr(os, "fchmod"):
        pytest.skip("this system has no os.fchmod")
    target = tmp_path / "profile"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o600)
    oe._write_text(target, "new\n")
    assert target.read_text(encoding="utf-8") == "new\n"
    assert target.stat().st_mode & 0o777 == 0o600
