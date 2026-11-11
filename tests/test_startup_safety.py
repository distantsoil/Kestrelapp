"""Kestrel always starts (#130, D-MCP-4, D-UI-252): MCP turns itself off after a failed start and
says why, two failed starts in a row offer safe mode, safe mode skips every optional start, and
the MCP card's Start again works. Real routes and the app lifespan; no network, no real window."""

from __future__ import annotations

import asyncio
import re
import sys

import pytest
from fastapi.testclient import TestClient

from kestrel import startup_health
from kestrel.mcp import runtime
from tests.test_mcp_switch import (
    _broken_server_module,
    _client,
    _free_port,
    _fresh,
    _mcp_runtime_state_is_fresh,  # noqa: F401 - autouse fixture, reused
    _not_live,  # noqa: F401 - autouse fixture, reused
    _row,
    _stub_worker_side_effects,
)


@pytest.fixture(autouse=True)
def _clean_startup_state(monkeypatch):
    from kestrel import paths

    paths.startup_health_json().unlink(missing_ok=True)
    monkeypatch.delenv(startup_health.SAFE_MODE_ENV, raising=False)
    monkeypatch.setattr(startup_health, "_offered", False)
    yield
    paths.startup_health_json().unlink(missing_ok=True)


def _save_mcp_on() -> None:
    _fresh()
    with _client() as c:
        c.post("/settings/mcp", data={"enabled": "1"})
    assert _row() is True


# ------------------------------------------------------------------ 1a self-heal
def test_a_failed_start_turns_the_switch_off_and_settings_says_why(monkeypatch):
    _save_mcp_on()
    _stub_worker_side_effects(monkeypatch)
    monkeypatch.setitem(sys.modules, "kestrel.mcp.server",
                        _broken_server_module(ImportError("no module named fastmcp")))
    from kestrel.web.app import build_app

    with TestClient(build_app(worker=True)) as c:
        assert c.get("/health").status_code == 200
        card = c.get("/settings/mcp").text
        page = c.get("/settings").text
    assert _row() is False  # the next launch is clean
    sentence = ("MCP was turned off because it could not start: a part the assistant connection "
                "needs is missing from this install; the details are in the log. "
                "Turn it on again to retry.")
    assert sentence in card and sentence in page
    assert "ImportError" not in card.partition("Show details")[0]


def test_turning_it_on_again_clears_the_sentence(monkeypatch):
    _save_mcp_on()
    _stub_worker_side_effects(monkeypatch)
    monkeypatch.setitem(sys.modules, "kestrel.mcp.server", _broken_server_module(OSError("x")))
    from kestrel.web.app import build_app

    with TestClient(build_app(worker=True)) as c:
        assert "MCP was turned off" in c.get("/settings/mcp").text
        again = c.post("/settings/mcp", data={"enabled": "1"}).text
        later_off = c.post("/settings/mcp", data={}).text  # a stale reason must not come back
    assert "MCP was turned off" not in again and "MCP was turned off" not in later_off


# ------------------------------------------------------------------ 1d Start again
def test_start_again_on_the_mcp_card_retries_and_shows_the_new_state(monkeypatch):
    _fresh()
    port = _free_port()
    _stub_worker_side_effects(monkeypatch)
    from kestrel.web.app import build_app

    with TestClient(build_app(worker=True)) as c:
        c.post("/settings/privacy/mcp/port", data={"port": str(port)})
        monkeypatch.setitem(sys.modules, "kestrel.mcp.server",
                            _broken_server_module(RuntimeError("down")))
        failed = c.post("/settings/mcp", data={"enabled": "1"}).text
        status = failed.split('data-testid="mcp-status"')[1].split("</p>")[0]
        assert "On, stopped:" in status and "Start again" in status
        # The button posts exactly what its own markup says.
        button = re.search(r'data-testid="mcp-restart" hx-post="([^"]+)" hx-vals=\'([^\']+)\'',
                           failed)
        assert button is not None
        import json

        monkeypatch.delitem(sys.modules, "kestrel.mcp.server")  # the install is repaired
        again = c.post(button.group(1), data=json.loads(button.group(2))).text
        status = again.split('data-testid="mcp-status"')[1].split("</p>")[0]
        assert f"Running on port {port}" in status and "Start again" not in status
        assert "On. The MCP server is running." in again
        assert runtime.running()
    assert not runtime.running()


# ------------------------------------------------------------------ 1b counter and offer
def _splash():
    from kestrel.desktop import shell

    return shell._Splash()


def _fail_migrations(monkeypatch):
    from kestrel.db import migrate

    def boom():
        raise RuntimeError("disk")

    monkeypatch.setattr(migrate, "run_migrations", boom)


def _busy() -> int:
    raise OSError("every port busy")


def _fail_port(monkeypatch):
    from kestrel.db import migrate
    from kestrel.desktop import shell

    monkeypatch.setattr(migrate, "run_migrations", lambda: None)
    monkeypatch.setattr(shell, "start_server", _busy)


def test_two_failed_starts_make_the_third_offer_safe_mode_without_starting(monkeypatch):
    from kestrel.desktop import shell

    _fail_port(monkeypatch)  # a failure safe mode can help with (a data failure is not counted)
    for _ in range(2):
        with pytest.raises(shell.StartupError) as err:
            shell._start_backend(_splash())
        assert err.value.code == "no_port"
    assert startup_health.failed_starts() == 2
    started: list[int] = []
    monkeypatch.setattr(shell, "start_server", lambda: started.append(1) or _busy())
    with pytest.raises(shell.StartupError) as err:
        shell._start_backend(_splash())
    assert err.value.code == startup_health.OFFER_CODE
    assert "last two times" in err.value.reason and "optional features off" in err.value.reason
    assert started == []  # the offer does not try the same start a third time
    assert startup_health.failed_starts() == 2  # and is not itself a failed start
    # Try again on the offer card really tries (once offered, in this process).
    with pytest.raises(shell.StartupError) as err:
        shell._start_backend(_splash())
    assert err.value.code == "no_port" and started == [1]


def test_one_failed_start_does_not_offer_and_a_good_start_clears_the_count(monkeypatch):
    from kestrel.db import migrate
    from kestrel.desktop import shell
    from kestrel.web import serve

    _fail_port(monkeypatch)
    with pytest.raises(shell.StartupError):
        shell._start_backend(_splash())
    assert startup_health.failed_starts() == 1
    monkeypatch.undo()
    monkeypatch.setattr(migrate, "run_migrations", lambda: None)
    monkeypatch.setattr(shell, "start_server", lambda: 8765)
    monkeypatch.setattr(serve, "wait_for_health", lambda port, timeout=1.0: True)
    monkeypatch.setattr(serve, "write_runtime", lambda port, pid: None)
    monkeypatch.setattr(shell, "run_preflight", lambda: None)
    assert shell._start_backend(_splash()) == 8765
    assert startup_health.failed_starts() == 0


def test_quitting_during_the_start_is_not_a_failed_start(monkeypatch):
    from kestrel.db import migrate
    from kestrel.desktop import shell

    monkeypatch.setattr(migrate, "run_migrations", lambda: None)

    def start_then_quit():
        monkeypatch.setattr(shell, "_quitting", True)
        return 8765

    monkeypatch.setattr(shell, "start_server", start_then_quit)
    monkeypatch.setattr(shell, "_quitting", False)
    monkeypatch.setattr(shell, "_stop_server", lambda: None)
    monkeypatch.setattr(shell, "window", None)
    shell._boot_steps(busy=False)
    assert startup_health.failed_starts() == 0


def test_a_missing_or_garbled_counter_file_reads_as_zero():
    from kestrel import paths

    assert startup_health.failed_starts() == 0
    paths.startup_health_json().write_text("not json", encoding="utf-8")
    assert startup_health.failed_starts() == 0
    paths.startup_health_json().write_text('{"failed_starts": "many"}', encoding="utf-8")
    assert startup_health.failed_starts() == 0


def test_the_safe_start_button_sets_safe_mode_and_retries(monkeypatch):
    from kestrel.desktop import frame, shell

    calls: list[str] = []
    monkeypatch.setattr(shell, "_retry_start", lambda: calls.append("retry") or True)
    monkeypatch.setattr(shell, "_started", False)
    monkeypatch.setattr(shell, "_quitting", False)
    api = frame.WindowApi(lambda: None, close_intent=lambda: "quit", close=lambda: None,
                          safe_start=shell._safe_start)
    api.splash_safe_start()
    assert calls == ["retry"] and startup_health.safe_mode()
    # In safe mode the offer is not shown again, even with the count still high.
    assert startup_health.take_offer(5) is False


# ------------------------------------------------------------------ 1b/1c lifespan in safe mode
def _recorded_lifespan(monkeypatch):
    """The worker app's lifespan with every optional start replaced by a recorder."""
    from kestrel.web import app as app_mod

    ran: list[str] = []
    # These tests assert the loops started right after the health answer; no delay here.
    monkeypatch.setattr(app_mod, "WORKER_DELAY_S", 0.0)

    def recorder(name):
        async def idle(stop, *a, **k):
            ran.append(name)
            await stop.wait()

        return idle

    async def noop(*a, **k):
        return None

    for name, label in (("_worker_loop", "worker"), ("_delivery_loop", "delivery"),
                        ("_stall_monitor", "stall"), ("_setup_models_loop", "models"),
                        ("_update_loop", "updates"), ("_scheduler_loop", "scheduler")):
        monkeypatch.setattr(app_mod, name, recorder(label))
    for name in ("_seed_shipped_sources", "_rebuild_generated_sources", "_seed_shipped_layouts",
                 "_mark_update_seen"):
        monkeypatch.setattr(app_mod, name, noop)

    async def keep_awake():
        ran.append("keep_awake")

    monkeypatch.setattr(app_mod, "_apply_keep_awake", keep_awake)
    monkeypatch.setattr(app_mod, "_graceful_worker_shutdown", noop)

    async def mcp_start(port, *, bind_all=False):
        ran.append("mcp")

    monkeypatch.setattr(runtime, "start", mcp_start)
    return ran


def test_a_normal_start_runs_every_optional_feature(monkeypatch):
    _save_mcp_on()
    ran = _recorded_lifespan(monkeypatch)
    from kestrel.web.app import build_app

    with TestClient(build_app(worker=True, scheduler=True)) as c:
        assert c.get("/health").status_code == 200
        assert asyncio.run(asyncio.sleep(0.2)) is None
    assert {"worker", "stall", "delivery", "models", "updates", "scheduler", "keep_awake",
            "mcp"} <= set(ran)


def test_safe_mode_starts_the_app_with_every_optional_feature_off(monkeypatch):
    _save_mcp_on()
    ran = _recorded_lifespan(monkeypatch)
    monkeypatch.setenv(startup_health.SAFE_MODE_ENV, "1")
    from kestrel.web.app import build_app

    with TestClient(build_app(worker=True, scheduler=True)) as c:
        assert c.get("/health").status_code == 200
        card = c.get("/settings/mcp").text
        asyncio.run(asyncio.sleep(0.2))
    assert "worker" in ran  # the core stays
    for optional in ("mcp", "delivery", "models", "updates", "scheduler", "keep_awake"):
        assert optional not in ran
    assert _row() is True  # safe mode is for this run only: the saved switch is untouched
    assert "started with optional features off this time" in card
    assert runtime.is_live() is False


# ------------------------------------------------------------------ 1c the rest of the audit
def test_kestrel_starts_when_the_mcp_runtime_itself_cannot_be_imported(monkeypatch):
    _save_mcp_on()
    _stub_worker_side_effects(monkeypatch)
    import kestrel.mcp as mcp_pkg

    monkeypatch.delattr(mcp_pkg, "runtime")
    monkeypatch.setitem(sys.modules, "kestrel.mcp.runtime", None)  # import raises ImportError
    from kestrel.web.app import build_app

    with TestClient(build_app(worker=True)) as c:
        assert c.get("/health").status_code == 200


# ------------- 0.7.6 review item 8: a data failure never leads to the safe-mode offer
# Safe mode still runs the migrations, so it cannot help a start that failed on opening the data
# (a database a newer Kestrel saved, or one the migrations cannot open). Those failures show their
# own message every time and never count toward the offer.
@pytest.mark.parametrize("cause, code", [
    (RuntimeError("disk"), "migrations_failed"),
    (RuntimeError("Can't locate revision identified by '9999'"), "newer_database"),
])
def test_a_data_failure_never_counts_toward_the_safe_mode_offer(monkeypatch, cause, code):
    from kestrel.db import migrate
    from kestrel.desktop import shell

    def boom():
        raise cause

    monkeypatch.setattr(migrate, "run_migrations", boom)
    monkeypatch.setattr(migrate, "newer_than_this_build", lambda: None)
    for _ in range(4):
        with pytest.raises(shell.StartupError) as err:
            shell._start_backend(_splash())
        assert err.value.code == code  # its own message, never the offer
        assert startup_health.failed_starts() == 0


def test_a_data_failure_leaves_an_earlier_streak_as_it_was(monkeypatch):
    from kestrel.db import migrate
    from kestrel.desktop import shell

    startup_health._write(1)  # one earlier start that did not finish
    _fail_migrations(monkeypatch)
    with pytest.raises(shell.StartupError):
        shell._start_backend(_splash())
    assert startup_health.failed_starts() == 1
    monkeypatch.setattr(migrate, "run_migrations", lambda: None)
    assert startup_health.not_counted("migrations_failed")
    assert startup_health.not_counted("newer_database")
    assert not startup_health.not_counted("no_port")
