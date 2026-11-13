"""The MCP switch on Settings > LAN (D-MCP-1): app first, off by default, saved as a setting row,
started and stopped live in the running app with the server's security unchanged. Loopback
only; no external endpoint is called."""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import Setting
from kestrel.mcp import runtime


def _fresh() -> None:
    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _row() -> object:
    async def go():
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            row = await s.get(Setting, "mcp.enabled")
        await engine.dispose()
        return None if row is None else row.value

    return asyncio.run(go())


@pytest.fixture(autouse=True)
def _mcp_runtime_state_is_fresh():
    """The MCP runtime keeps its state in one module-level dict (live flag, server task, stop
    event, port, stored failure reason and detail). A test that leaves a stored failure behind
    would leak it into the next test in the same worker, so the whole dict is put back to its
    defaults before and after every test."""
    from kestrel.mcp import runtime as _rt

    def _reset() -> None:
        _rt._state.clear()
        _rt._state.update(live=False, task=None, stop=None, port=None, bind_all=False,
                          reason=None, recorded=None, detail=None)

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _not_in_safe_mode(monkeypatch):
    """The worker app starts MCP only outside safe mode, and safe mode is a process environment
    variable (startup_health.SAFE_MODE_ENV), so these tests state that it is off (D-TEST-24)."""
    from kestrel import startup_health

    monkeypatch.delenv(startup_health.SAFE_MODE_ENV, raising=False)


@pytest.fixture(autouse=True)
def _not_live():
    runtime.set_live(False)
    yield
    runtime.set_live(False)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_switch_is_off_by_default_and_shows_the_port_and_address():
    _fresh()
    with _client() as c:
        page = c.get("/settings").text
        panel = c.get("/settings/mcp").text
    assert 'data-testid="st-mcp"' in page  # the card is part of Privacy and access
    assert 'data-on="0"' in panel and 'data-testid="mcp-status">Off<' in panel
    assert "http://127.0.0.1:9110/sse" in panel
    assert 'data-testid="mcp-secret-note"' not in panel  # loopback only: no secret needed
    assert "—" not in panel


def test_turning_it_on_saves_the_row_and_a_ui_only_process_says_restart():
    _fresh()
    with _client() as c:
        on = c.post("/settings/mcp", data={"enabled": "1"}).text
        assert _row() is True
        assert 'data-on="1"' in on and "Restart Kestrel to apply." in on
        off = c.post("/settings/mcp", data={}).text
    assert _row() is False and 'data-on="0"' in off


def test_in_the_running_app_the_switch_starts_and_stops_it_live(monkeypatch):
    _fresh()
    calls: list = []

    async def fake_start(port, *, bind_all=False):
        calls.append(("start", port, bind_all))
        return None

    async def fake_stop():
        calls.append(("stop",))

    monkeypatch.setattr(runtime, "start", fake_start)
    monkeypatch.setattr(runtime, "stop", fake_stop)
    runtime.set_live(True)
    with _client() as c:
        on = c.post("/settings/mcp", data={"enabled": "1"}).text
        off = c.post("/settings/mcp", data={}).text
    # bind_all comes from the same place the web app's own guard reads it (YAML/env), never a
    # setting row, so the MCP port is never more exposed than the web port.
    assert calls == [("start", 9110, False), ("stop",)]
    assert "On. The MCP server is running." in on and "Off. The MCP server has stopped." in off


def test_bind_all_shows_the_shared_secret_note(monkeypatch):
    from kestrel import config as config_mod

    _fresh()
    real = config_mod.get_settings()
    lan = real.model_copy(update={"server": real.server.model_copy(update={"bind_all": True})})
    monkeypatch.setattr(config_mod, "get_settings", lambda: lan)
    with _client() as c:
        panel = c.get("/settings/mcp").text
    note = panel.split('data-testid="mcp-secret-note"')[1].split("</p>")[0]
    assert "X-Kestrel-Key" in note and "Make the key first" in note


def test_runtime_serves_with_the_host_guard_and_stops():
    """The live start uses sse_app unchanged: a foreign Host is refused (DNS rebinding), and stop
    frees the port."""
    port = _free_port()

    async def go():
        assert await runtime.start(port) is None
        assert runtime.running()
        assert await runtime.start(port) is None  # idempotent
        async with httpx.AsyncClient() as client:
            for _ in range(50):
                try:
                    r = await client.post(
                        f"http://127.0.0.1:{port}/messages/?session_id=x",
                        json={"jsonrpc": "2.0", "method": "ping"},
                        headers={"Host": "evil.example"},
                    )
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.05)
        await runtime.stop()
        return r.status_code

    # D-SEC-10: a loopback peer with a foreign Host is a remote visitor now, so the key check
    # refuses it (401) before the Host guard would (421); either way it is refused.
    assert asyncio.run(go()) in (401, 421)
    assert not runtime.running() and runtime.port_free(port)


def test_a_busy_port_is_refused_with_a_reason_not_an_exit():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        problem = asyncio.run(runtime.start(port))
    assert problem and f"Port {port} is in use" in problem and not runtime.running()


# ----------------------------------------------- Kestrel starts whatever happens to MCP (#130)
def _stub_worker_side_effects(monkeypatch):
    """The worker app's lifespan with every loop and network step idle; the MCP start is real."""
    from kestrel.web import app as app_mod

    async def idle(stop, *a, **k):
        await stop.wait()

    async def noop(*a, **k):
        return None

    for name in ("_worker_loop", "_delivery_loop", "_stall_monitor", "_setup_models_loop",
                 "_update_loop"):
        monkeypatch.setattr(app_mod, name, idle)
    for name in ("_seed_shipped_sources", "_rebuild_generated_sources", "_seed_shipped_layouts",
                 "_apply_keep_awake", "_mark_update_seen"):
        monkeypatch.setattr(app_mod, name, noop)
    monkeypatch.setattr(app_mod, "_graceful_worker_shutdown", noop)


def _broken_server_module(exc: BaseException):
    import types

    mod = types.ModuleType("kestrel.mcp.server")

    def _raise(name):
        raise exc

    mod.__getattr__ = _raise  # type: ignore[attr-defined]
    return mod


@pytest.mark.parametrize("exc", [
    __import__("importlib.metadata").metadata.PackageNotFoundError("fastmcp"),
    ImportError("no module named fastmcp"),
    OSError(13, "denied C:/secret/path"),
    RuntimeError("anything at all"),
])
def test_kestrel_starts_when_mcp_is_on_and_its_server_cannot_load(monkeypatch, exc):
    """INVARIANT: Kestrel always starts, whatever happens to MCP. The switch is saved on, the
    server import raises; the lifespan still completes, the panel says plainly that it is not
    running (no class name, no path), the technical text is under Show details only, and the
    failure is logged with context."""
    import sys

    import structlog

    _fresh()
    with _client() as c:  # a UI-only process saves the switch row
        c.post("/settings/mcp", data={"enabled": "1"})
    assert _row() is True
    _stub_worker_side_effects(monkeypatch)
    monkeypatch.setitem(sys.modules, "kestrel.mcp.server", _broken_server_module(exc))
    from kestrel.web.app import build_app

    with structlog.testing.capture_logs() as logs:
        with TestClient(build_app(worker=True)) as c:
            assert c.get("/health").status_code == 200
            panel = c.get("/settings/mcp").text
    assert runtime.is_live() is False  # the lifespan also shut down cleanly
    assert not runtime.running()
    status = panel.split('data-testid="mcp-status"')[1].split("</p>")[0]
    # D-MCP-4: the failed start turned the saved switch off (self-heal), so the next launch is
    # clean, and the card says why in plain words.
    assert _row() is False and 'data-on="0"' in panel
    assert "Off" in status and "Running" not in status
    visible, _, details = panel.partition("Show details")
    plain = ("a part the assistant connection needs is missing from this install"
             if isinstance(exc, ImportError) else "an unexpected problem")
    assert f"MCP was turned off because it could not start: {plain}; the details are in the log. " \
        "Turn it on again to retry." in visible
    assert "Turn it on again to retry." in visible
    assert type(exc).__name__ not in visible and "secret" not in visible
    assert "anything at all" not in visible
    assert "Why it stopped:" in details and type(exc).__name__ in details
    failed = [e for e in logs if e["event"] == "mcp_start_failed"]
    assert len(failed) == 1 and failed[0]["port"] == 9110
    assert type(exc).__name__ in failed[0]["error"]


def test_the_call_site_guard_alone_keeps_kestrel_starting(monkeypatch):
    """Belt and braces: even if runtime.start itself raised, the app's startup step records the
    failure and finishes."""
    _fresh()
    with _client() as c:
        c.post("/settings/mcp", data={"enabled": "1"})
    _stub_worker_side_effects(monkeypatch)

    async def explodes(port, *, bind_all=False):
        raise RuntimeError("boom")

    monkeypatch.setattr(runtime, "start", explodes)
    from kestrel.web.app import build_app

    with TestClient(build_app(worker=True)) as c:
        assert c.get("/health").status_code == 200
        panel = c.get("/settings/mcp").text
    assert _row() is False
    assert ("MCP was turned off because it could not start: an unexpected problem; the details "
            "are in the log. Turn it on again to retry.") in panel


def _lan_client(monkeypatch, ip):
    from kestrel import config as config_mod
    from kestrel.web import auth

    real = config_mod.get_settings()
    lan = real.model_copy(update={"server": real.server.model_copy(update={"bind_all": True})})
    monkeypatch.setattr(config_mod, "get_settings", lambda: lan)
    monkeypatch.setattr(auth, "lan_host_ip", lambda: ip)


def test_lan_off_labels_the_loopback_address_and_points_to_lan_access():
    _fresh()
    with _client() as c:
        c.post("/settings/mcp", data={"enabled": "1"})
        panel = c.get("/settings/mcp").text
    assert "this computer only" in panel.lower() and "http://127.0.0.1:9110/sse" in panel
    assert 'data-testid="mcp-address-lan"' not in panel
    hint = panel.split('data-testid="mcp-lan-hint"')[1].split("</p>")[0]
    assert "To connect from another device" in hint and 'href="/settings#privacy"' in hint


def test_lan_on_shows_both_addresses_each_with_its_own_copy_button(monkeypatch):
    _fresh()
    _lan_client(monkeypatch, "192.168.1.20")
    with _client() as c:
        c.post("/settings/mcp", data={"enabled": "1"})
        panel = c.get("/settings/mcp").text
    visible = panel.partition("Show details")[0]
    assert "http://127.0.0.1:9110/sse" in visible and "http://192.168.1.20:9110/sse" in visible
    assert 'data-testid="mcp-copy-local"' in visible and 'data-testid="mcp-copy-lan"' in visible
    assert 'data-st-copy="http://192.168.1.20:9110/sse"' in visible
    assert "on your network" in visible and 'data-testid="mcp-lan-hint"' not in visible
    assert 'data-testid="mcp-secret-note"' in panel  # the shared-key note stays


def test_lan_on_with_no_clear_address_says_so_instead_of_guessing(monkeypatch):
    _fresh()
    _lan_client(monkeypatch, None)
    with _client() as c:
        c.post("/settings/mcp", data={"enabled": "1"})
        panel = c.get("/settings/mcp").text
    assert "Could not tell which network address to use." in panel
    assert 'data-testid="mcp-address-lan"' not in panel


def test_mcp_binds_loopback_unless_lan_access_is_on_and_a_lan_bind_keeps_the_key(monkeypatch):
    """INVARIANT: loopback unless LAN access is on; a LAN bind still requires the shared key."""
    import uvicorn

    from kestrel.mcp import server as mcp_server

    seen: list[tuple[str, bool]] = []

    class Spy:
        def __init__(self, app, host, **kw):
            seen.append((host, isinstance(app, mcp_server.RequireLanKey)))

    async def go(bind_all):
        stop = asyncio.Event()
        stop.set()
        monkeypatch.setattr(uvicorn, "Config", Spy)
        monkeypatch.setattr(uvicorn, "Server", lambda cfg: type(
            "S", (), {"install_signal_handlers": None, "should_exit": False,
                      "serve": staticmethod(lambda: asyncio.sleep(0))})())
        try:
            await mcp_server.serve_mcp(stop, port=9111, bind_all=bind_all)
        except RuntimeError:
            pass

    asyncio.run(go(False))
    asyncio.run(go(True))
    assert seen == [("127.0.0.1", True), ("0.0.0.0", True)]  # noqa: S104 (key guard always on)
