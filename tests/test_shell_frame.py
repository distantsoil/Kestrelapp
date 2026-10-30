"""The frameless window (D-UI-88), the loading page (D-UI-89) and the shell's top row: the JS API
the window controls call, when the controls render, the splash page, and Mark all as read.
Offline; the native calls themselves can only be checked on real Windows and macOS hardware."""

from __future__ import annotations

import asyncio
from pathlib import Path

from kestrel import paths
from kestrel.desktop import frame


class _Win:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.native = None

    def minimize(self) -> None:
        self.calls.append("min")

    def maximize(self) -> None:
        self.calls.append("max")

    def restore(self) -> None:
        self.calls.append("restore")


def _api(win, **kw):
    closed: list[str] = []
    api = frame.WindowApi(lambda: win, close_intent=kw.pop("intent", lambda: "tray"),
                          close=lambda: closed.append("close"),
                          platform=kw.pop("platform", "linux"), **kw)
    return api, closed


def test_window_api_minimize_maximize_restore_and_close():
    win = _Win()
    api, closed = _api(win)
    assert api.window_info() == {"os": "linux", "maximized": False}
    api.window_minimize()
    assert api.window_toggle_maximize() is True
    assert api.window_toggle_maximize() is False
    assert win.calls == ["min", "max", "restore"]
    api.window_close()
    assert closed == ["close"]


def test_window_api_close_intent_is_tray_or_quit_and_never_raises():
    api, _ = _api(_Win(), intent=lambda: "quit")
    assert api.window_close_intent() == "quit"

    def boom() -> str:
        raise RuntimeError("no settings yet")

    api, _ = _api(_Win(), intent=boom)
    assert api.window_close_intent() == "tray"


def test_window_api_exposes_only_its_own_methods_to_the_page():
    api, _ = _api(_Win())
    public = sorted(n for n in dir(api) if not n.startswith("_"))
    assert public == ["splash_open_logs", "splash_quit", "splash_retry", "window_close",
                      "window_close_intent", "window_info", "window_minimize",
                      "window_set_frame_color", "window_set_theme", "window_start_drag",
                      "window_start_resize",
                      "window_toggle_maximize"]


def test_resize_ignores_unknown_edges_and_a_maximized_window(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(frame, "_gtk_begin_resize", lambda w, e: seen.append(("gtk", e)))
    monkeypatch.setattr(frame, "_win_nc_button_down",
                        lambda w, ht, press=None: seen.append(("win", ht)))
    api, _ = _api(_Win())
    api.window_start_resize("se")
    api.window_start_resize("middle")
    api.window_start_drag()  # Linux drags through pywebview's drag region, not here
    api._set_maximized(True)
    api.window_start_resize("n")
    assert seen == [("gtk", "se")]
    win_api, _ = _api(_Win(), platform="win32")
    win_api.window_start_drag()
    win_api.window_start_resize("ne")
    assert seen[1:] == [("win", frame.HT["caption"]), ("win", frame.HT["ne"])]


def test_a_windows_drag_is_anchored_at_the_press_point(monkeypatch):
    # Round 7: the move started after a 3 px threshold from the cursor's current spot, so the
    # window ended ~10 px short of the pointer. The press point now anchors the native move.
    seen: list[tuple] = []
    monkeypatch.setattr(frame, "_win_nc_button_down",
                        lambda w, ht, press=None: seen.append((ht, press)))
    win_api, _ = _api(_Win(), platform="win32")
    win_api.window_start_drag(812.4, 96.6)
    win_api.window_start_drag()
    assert seen == [(frame.HT["caption"], (812, 97)), (frame.HT["caption"], None)]


def test_os_key():
    assert frame.os_key("win32") == "win"
    assert frame.os_key("darwin") == "mac"
    assert frame.os_key("linux") == "linux"


def test_splash_page_is_built_self_contained():
    """The loading page shows before any server exists, so its tokens and mark are inlined (its
    behavior is covered in a real browser: test_shell_frame_browser.py)."""
    from kestrel.desktop.shell import splash_html

    html = splash_html()
    assert "/*K-TOKENS*/" not in html and "/*K-MARK*/" not in html
    assert "/static/" not in html  # nothing it would have to fetch


def _client():
    from fastapi.testclient import TestClient

    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _fresh():
    from kestrel.db import migrate
    from kestrel.db.engine import reset_engine

    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()


def test_window_controls_render_only_with_the_desktop_shell_and_start_hidden():
    from kestrel.web import desktop_bridge

    _fresh()
    with _client() as c:
        plain = c.get("/help").text
    assert 'data-k-wctl' not in plain and "data-k-rsz" not in plain  # `kestrel serve`
    desktop_bridge.set_quit_handler(lambda: None)
    desktop_bridge.set_window_frame(True)
    with _client() as c:
        app = c.get("/help").text
    side = app.split('<aside class="side"')[1].split("</aside>")[0]
    # Inside the sidebar, at its top, hidden until the page finds the window's JS API.
    assert side.index("data-k-wctl") < side.index('class="k-brand"')
    assert 'id="k-wctl" data-k-wctl hidden' in side
    assert "data-k-rsz=" not in side
    # The resize handles sit at body level, never inside .k-win: its container query is a
    # containing block for position: fixed, which put the bottom handles below the window
    # (hardware check, round 5).
    edges = app.split("<body")[1].split('<div class="k-win"')[0]
    assert edges.count("data-k-rsz=") == 8 and edges.count('data-k-rsz="n" hidden') == 1
    for wc in ("min", "max", "close"):
        assert f'data-wc="{wc}"' in side
    # The top row carries the power button (D-UI-197), after the avatar, with an in-window tip.
    assert 'data-testid="quit-app"' in app and 'data-k-tip="Quit Kestrel"' in app
    assert "kestrelQuit.open(this)" in app
    # The empty top of the row is the drag region.
    assert 'class="k-tools" data-k-drag' in app


def test_top_row_has_search_bell_theme_and_avatar_and_no_page_title():
    _fresh()
    with _client() as c:
        html = c.get("/settings").text
    tools = html.split('<header class="k-tools"')[1].split("</header>")[0]
    assert 'class="k-search"' in tools and "Search or jump to..." in tools
    assert 'class="k-icon k-bell"' in tools and 'id="bellcount" class="k-icon__n"' in tools
    assert 'data-testid="theme-toggle"' in tools and 'class="k-avatar' in tools
    assert 'class="title"' not in html and "&#9681;" not in tools and "◑" not in tools
    assert "\U0001f514" not in tools  # the bell is the drawn icon, not an emoji


def test_mark_all_as_read_clears_the_bell():
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel.db.engine import forget_engine, make_async_engine
    from kestrel.db.models import Notification

    _fresh()

    async def seed():
        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine)() as s:
            for i in range(3):
                s.add(Notification(kind="run_done", title=f"n{i}", severity="info"))
            await s.commit()
        await engine.dispose()
        forget_engine()

    asyncio.run(seed())
    with _client() as c:
        assert c.get("/notifications/bell").text == "3"
        r = c.post("/notifications/read-all", data={"tab": "attention"})
        assert r.status_code == 200 and 'id="bellcount"' in r.text
        assert 'data-tab="attention"' in r.text  # the panel stays on the tab it was on
        assert c.get("/notifications/bell").text == ""
    # The button swaps into the panel body, which exists before the list has loaded, and sends
    # the current tab with it.
    with _client() as c:
        page = c.get("/help").text
    button = page.split('data-testid="notif-read-all"')[0].rsplit("<button", 1)[1]
    assert 'hx-target="#k-np-body"' in button and "tab:" in button
    assert 'id="k-np-body"' in page


# --------------------------------------------------------------------------- command palette
def _palette_js(snippet: str):
    import json
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    src = Path(paths.web_static_dir(), "js", "palette.js")
    code = f"const P = require({json.dumps(str(src))});\nconsole.log(JSON.stringify({snippet}));"
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_palette_groups_recent_and_suggestions_when_empty():
    acts = ('[{id:"run",g:"Actions",t:"Run a search now",s:"x",suggest:true},'
            '{id:"cal",g:"Actions",t:"Open Calibrate",s:"y",suggest:true},'
            '{id:"dark",g:"Actions",t:"Turn on dark mode",s:"z"}]')
    recent = '[{g: "Roles", t: "Analyst", s: "Co"}]'
    got = _palette_js(f'P.results("", {{actions: {acts}, recent: {recent}}})')
    assert [r.get("h") for r in got["rows"] if "h" in r] == ["RECENT", "ACTIONS"]
    assert got["count"] == 3
    dark = _palette_js(f'P.results("dark", {{actions: {acts}, dark: true}}).items.map(i => i.t)')
    assert "Turn off dark mode" in dark  # the action reads Turn off when dark (D-UI-91)


def test_palette_finds_settings_by_synonyms_and_says_when_nothing_matches():
    for word, expected in (("ollama", "Models"), ("backup", "Your data"),
                           ("linkedin", "Sources"), ("dates", "Preferences")):
        titles = _palette_js(f'P.results({word!r}, {{}}).items.map(i => i.t)')
        assert expected in titles, (word, titles)
    groups = _palette_js('P.results("data", {roles: [{t: "Data Analyst", s: "Acme"}],'
                         ' companies: [{t: "Data Co", s: "1 role"}]}).rows.filter(r => r.h)'
                         '.map(r => r.h)')
    assert groups == ["ROLES", "COMPANIES", "SETTINGS"]
    assert _palette_js('P.results("zzqx", {}).count') == 0
    assert _palette_js('P.highlight("<b>Models</b>", "mod")') == (
        '&lt;b&gt;<mark class="k-pal__m">Mod</mark>els&lt;/b&gt;')


def test_palette_search_returns_roles_and_companies_and_never_hidden_ones():
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel.db.base import utcnow_iso
    from kestrel.db.engine import forget_engine, make_async_engine
    from kestrel.db.models import Opportunity, Org, Source

    _fresh()

    async def seed():
        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine)() as s:
            src = Source(track="employment", kind="greenhouse", name="b", enabled=True)
            seen = Org(name="Larkspur Logistics", normalized_name="larkspur logistics")
            gone = Org(name="Larkspur Hidden", normalized_name="larkspur hidden",
                       hidden_at=utcnow_iso())
            s.add_all([src, seen, gone])
            await s.flush()
            for org, title in ((seen, "Senior Operations Analyst"), (gone, "Operations Lead")):
                s.add(Opportunity(track="employment", opp_type="job_posting", org_id=org.id,
                                  source_id=src.id, title=title, org_name=org.name,
                                  url=f"https://x/{org.id}"))
            # The same role found on a second board: one row, not two, in any count.
            s.add(Opportunity(track="employment", opp_type="job_posting", org_id=seen.id,
                              source_id=src.id, title="Senior Operations Analyst",
                              org_name=seen.name, url="https://y/1", is_canonical=False))
            await s.commit()
        await engine.dispose()
        forget_engine()

    asyncio.run(seed())
    with _client() as c:
        body = c.get("/palette/search", params={"q": "operations"}).json()
        assert [r["t"] for r in body["roles"]] == ["Senior Operations Analyst"]
        assert body["roles"][0]["href"].startswith("/opportunities/")
        orgs = c.get("/palette/search", params={"q": "larkspur"}).json()["companies"]
        assert [o["t"] for o in orgs] == ["Larkspur Logistics"]
        assert orgs[0]["s"] == "1 role in your Pipeline"  # canonical, visible roles only
        assert c.get("/palette/search", params={"q": "o"}).json() == {"roles": [], "companies": []}


def test_every_page_without_the_sidebar_still_renders_the_window_controls():
    """The setup wizard hides the sidebar; in the desktop window its controls stay (hardware
    check: a first-run user could only close the window with Alt+F4)."""
    import re

    from kestrel.web import desktop_bridge

    _fresh()
    desktop_bridge.set_quit_handler(lambda: None)
    desktop_bridge.set_window_frame(True)
    with _client() as c:
        page = c.get("/onboarding").text
    assert 'id="k-wctl" data-k-wctl hidden' in page
    for wc in ("min", "max", "close"):
        assert f'data-wc="{wc}"' in page
    assert re.search(r'class="ob-top[^"]*" data-k-drag', c.get("/onboarding/step").text) or \
        'data-k-drag' in page.split('class="ob-top')[1].split(">")[0]
    css = Path(paths.web_static_dir(), "css", "pages", "onboarding.css").read_text("utf-8")
    assert ".side > :not(.k-wctl):not(.k-rsz)" in css  # the wizard hides all but the controls
    shell_css = Path(paths.web_static_dir(), "css", "components.css").read_text("utf-8")
    assert "html[data-k-frame] .ob-top { padding-top: 46px; }" in shell_css


def test_the_first_frame_matches_the_saved_theme(monkeypatch):
    from kestrel.desktop import shell

    monkeypatch.setattr(shell, "_os_dark", lambda: False)
    paths.window_state_json().unlink(missing_ok=True)
    assert shell._launch_theme() == ("auto", False)
    shell._remember_theme("dark")
    assert shell._launch_theme() == ("dark", True)
    dark, light = shell._token("sky-top", True), shell._token("sky-top", False)
    css = Path(paths.web_static_dir(), "css", "tokens.css").read_text("utf-8")
    assert dark and light and dark != light and f"--sky-top:{dark}" in css
    assert '<html lang="en" data-theme="dark">' in shell.splash_html("dark")
    assert "data-theme" not in shell.splash_html("auto").split("<head>")[0]
    shell._remember_theme("purple")  # never stored
    assert shell._saved_theme() == "dark"
    paths.window_state_json().unlink(missing_ok=True)


def test_a_busy_database_is_a_plain_retryable_answer_never_a_500():
    import json
    import sqlite3

    from fastapi import APIRouter
    from fastapi.testclient import TestClient
    from sqlalchemy.exc import OperationalError

    from kestrel.web.app import DB_BUSY_MESSAGE, build_app

    _fresh()
    app = build_app(worker=False)
    router = APIRouter()

    @router.post("/busy-sa")
    async def busy_sa():
        raise OperationalError("UPDATE setting", {}, sqlite3.OperationalError("database is locked"))

    @router.post("/busy-raw")
    async def busy_raw():
        raise sqlite3.OperationalError("database is locked")

    @router.post("/broken")
    async def broken():
        raise OperationalError("SELECT", {}, sqlite3.OperationalError("no such table: x"))

    app.include_router(router)
    with TestClient(app, raise_server_exceptions=False) as c:
        for path in ("/busy-sa", "/busy-raw"):
            r = c.post(path, headers={"HX-Request": "true"})
            assert r.status_code == 503 and r.text == DB_BUSY_MESSAGE
            toast = json.loads(r.headers["HX-Trigger"])["k-toast"]
            assert toast["tone"] == "warn" and "try again" in toast["message"]
        assert c.post("/broken").status_code == 500  # any other database error is still one


def test_a_drag_starts_from_the_press_point_on_any_display():
    """WM_NCLBUTTONDOWN carries the press point; a zero there made Windows start the move at the
    screen origin and throw the window onto another display."""
    assert frame.point_lparam(100, 200) == (200 << 16) | 100
    # A display left of or above the primary has negative coordinates (16-bit two's complement).
    assert frame.point_lparam(-1, -1232) == ((-1232 & 0xFFFF) << 16) | 0xFFFF
    assert frame.point_lparam(1449, -1232) & 0xFFFF == 1449


def test_maximize_fills_exactly_the_work_area_of_its_own_monitor():
    """Hardware check, round 4: a caption-less window maximized over the whole monitor (taskbar
    covered). WM_GETMINMAXINFO gets the monitor's work area, relative to that monitor."""
    # 5120x1440 primary with a 48 px taskbar at the bottom.
    assert frame.max_rect((0, 0, 5120, 1440), (0, 0, 5120, 1392)) == (0, 0, 5120, 1392)
    # A second 2560x1440 display above the primary (negative offset), no taskbar on it.
    assert frame.max_rect((0, -1440, 2560, 0), (0, -1440, 2560, 0)) == (0, 0, 2560, 1440)
    # A display to the left with its taskbar on the left edge.
    assert frame.max_rect((-2560, 0, 0, 1440), (-2512, 0, 0, 1440)) == (48, 0, 2512, 1440)


def test_maximize_button_double_click_and_win_up_share_the_system_command(monkeypatch):
    """Hardware check, round 5: the button and a double click did nothing (pywebview's
    maximize() sets WinForms' WindowState, inert on this form) while Win+Up worked. On Windows
    the toggle now posts the same WM_SYSCOMMAND Win+Up does, deciding from IsZoomed, the
    window's own state, so a Win+Up maximize is restored by the button too."""
    zoomed = {"v": False}
    sent: list[int] = []
    monkeypatch.setattr(frame, "_win_is_zoomed", lambda form: zoomed["v"])
    monkeypatch.setattr(frame, "_win_syscommand", lambda form, cmd: sent.append(cmd) or True)
    win = _Win()
    api, _ = _api(win, platform="win32")
    assert api.window_toggle_maximize() is True
    zoomed["v"] = True
    assert api.window_toggle_maximize() is False
    zoomed["v"] = True  # maximized by Win+Up, behind pywebview's back
    assert api.window_info()["maximized"] is True
    assert api.window_toggle_maximize() is False
    assert sent == [frame.SC_MAXIMIZE, frame.SC_RESTORE, frame.SC_RESTORE]
    assert win.calls == []  # never WinForms' WindowState
    # A maximized window (however it got there) does not start an edge resize.
    seen: list[int] = []
    monkeypatch.setattr(frame, "_win_nc_button_down", lambda w, ht: seen.append(ht))
    api.window_start_resize("e")
    zoomed["v"] = False
    api.window_start_resize("e")
    assert seen == [frame.HT["e"]]


def test_window_geometry_first_launch_restore_offscreen_and_maximized(monkeypatch):
    """D-UI-195: the last place is restored only when it still fits a display's work area."""
    from kestrel.desktop import shell

    paths.ensure_dirs()
    paths.window_state_json().unlink(missing_ok=True)
    areas = [(0, 0, 1920, 1040), (1920, 0, 2560, 1400)]
    # First launch: nothing saved, the D-UI-87 default (centered) is used.
    assert shell._restore_geometry(shell._window_state().get("geometry"), areas) is None
    # Saved through the shell's own writer, merged with the theme rather than replacing it.
    shell._remember_theme("dark")
    shell._update_window_state(geometry={"x": 2000, "y": 100, "width": 1500, "height": 900,
                                         "maximized": False})
    state = shell._window_state()
    assert state["theme"] == "dark"
    assert shell._restore_geometry(state["geometry"], areas) == ((2000, 100, 1500, 900), False)
    # The second display unplugged, or a lower resolution: the default instead.
    assert shell._restore_geometry(state["geometry"], areas[:1]) is None
    assert shell._restore_geometry(state["geometry"], [(1920, 0, 1280, 720)]) is None
    # Maximized: the normal rectangle plus the flag (shown first, then maximized).
    maxed = dict(state["geometry"], maximized=True)
    assert shell._restore_geometry(maxed, areas) == ((2000, 100, 1500, 900), True)
    # Junk and undersized rectangles never restore.
    for bad in (None, "x", {"x": 1}, {"x": 0, "y": 0, "width": 200, "height": 100}):
        assert shell._restore_geometry(bad, areas) is None
    paths.window_state_json().unlink(missing_ok=True)


def test_window_geometry_is_saved_with_the_normal_rect_kept_while_maximized(monkeypatch):
    from kestrel.desktop import shell

    paths.ensure_dirs()
    paths.window_state_json().unlink(missing_ok=True)

    class W:
        x, y, width, height = 120, 80, 1600, 950

    class Api:
        maxed = False

        def _is_maximized(self):
            return self.maxed

    api = Api()
    monkeypatch.setattr(shell, "window", W())
    monkeypatch.setattr(shell, "_frame_api", api)
    monkeypatch.setattr(shell, "_started", True)
    monkeypatch.setattr(shell, "_minimized", False)
    shell._save_geometry()
    assert shell._window_state()["geometry"] == {"x": 120, "y": 80, "width": 1600,
                                                 "height": 950, "maximized": False}
    api.maxed = True
    W.x, W.y, W.width, W.height = 0, 0, 1920, 1040  # the maximized size is never stored
    shell._save_geometry()
    assert shell._window_state()["geometry"] == {"x": 120, "y": 80, "width": 1600,
                                                 "height": 950, "maximized": True}
    paths.window_state_json().unlink(missing_ok=True)


def test_the_windows_border_strip_answers_edges_and_corners():
    """Hardware check, round 6: page handles never got a resize cursor. The form keeps a strip
    around the WebView2 control and its hit test names the edge, so Windows resizes natively."""
    ht = frame.HT
    rect, b = (100, 50, 1100, 850), 5
    assert frame.edge_hit(100, 400, rect, b) == ht["w"]
    assert frame.edge_hit(1099, 400, rect, b) == ht["e"]
    assert frame.edge_hit(600, 50, rect, b) == ht["n"]
    assert frame.edge_hit(600, 849, rect, b) == ht["s"]
    assert frame.edge_hit(100, 50, rect, b) == ht["nw"]
    assert frame.edge_hit(1099, 52, rect, b) == ht["ne"]
    assert frame.edge_hit(1095, 849, rect, b) == ht["se"]  # corners reach 2x the strip
    assert frame.edge_hit(101, 845, rect, b) == ht["sw"]
    assert frame.edge_hit(600, 400, rect, b) is None  # the page itself
    assert frame.edge_hit(105, 400, rect, b) is None  # just inside the strip
    # A display left of and above the primary one has negative coordinates.
    assert frame.signed_point(frame.point_lparam(-1800, -20)) == (-1800, -20)
    assert frame.signed_point(frame.point_lparam(2400, 900)) == (2400, 900)
    neg = (-1920, -100, -900, 700)
    assert frame.edge_hit(*frame.signed_point(frame.point_lparam(-1920, 300)), neg, b) == ht["w"]


def test_the_border_strip_is_installed_by_the_window_procedure():
    src = Path(frame.__file__).read_text(encoding="utf-8")
    start = src.index("def proc(")
    proc = src[start : src.index("def go()", start)]
    assert "WM_NCHITTEST" in proc and "IsZoomed" in proc and "edge_hit(" in proc
    assert "WM_SIZE" in proc and "SIZE_MAXIMIZED" in proc  # no strip while maximized
    assert "_set_padding(form" in src[src.index("def go()", start) :]


def test_the_strip_color_follows_the_page_and_only_on_windows(monkeypatch):
    """D-UI-196: the strip takes the page's --frame-strip, sent as computed CSS."""
    assert frame.parse_rgb("rgb(9, 13, 32)") == (9, 13, 32)
    assert frame.parse_rgb("rgba(255, 255, 255, 0.5)") == (255, 255, 255)
    assert frame.parse_rgb("rgb(300, -4, 1.6)") == (255, 0, 2)
    for bad in ("", "#fff", "rgb(1,2)", "rgb(a,b,c)", None, 7):
        assert frame.parse_rgb(bad) is None
    seen: list[str] = []
    monkeypatch.setattr(frame, "_win_invoke", lambda form, fn, what: seen.append(what))
    win = _Win()
    win.native = object()
    api, _ = _api(win, platform="win32")
    api.window_set_frame_color("rgb(9, 13, 32)")
    api.window_set_frame_color("not a color")
    linux, _ = _api(win)
    linux.window_set_frame_color("rgb(9, 13, 32)")
    # The color, then (once) the hook that puts it back on each focus change (D-UI-203).
    assert seen == ["frame_color", "frame_color_hook"]


def test_every_theme_block_defines_the_strip_color():
    css = Path(paths.web_static_dir(), "css", "tokens.css").read_text(encoding="utf-8")
    assert css.count("--side-bg:") == css.count("--frame-strip:")
