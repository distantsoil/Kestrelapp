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

    assert asyncio.run(go()) == 421
    assert not runtime.running() and runtime.port_free(port)


def test_a_busy_port_is_refused_with_a_reason_not_an_exit():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        problem = asyncio.run(runtime.start(port))
    assert problem and f"Port {port} is in use" in problem and not runtime.running()
