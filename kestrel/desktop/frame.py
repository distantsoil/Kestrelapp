"""The frameless window (D-UI-88, SPEC-INTERFACES 1.7): the JS API the page's window controls
call, and the per-platform pieces pywebview's `frameless=True` leaves out.

What pywebview 6.2 does with `frameless=True` (read from its platform backends):
  - Windows (winforms): FormBorderStyle None. No caption, no border, and so no edge resize, no
    snap on drag and a maximize that covers the taskbar. Its drag region moves the window from
    JavaScript (pywebviewMoveWindow), which skips Aero Snap.
  - macOS (cocoa): a full-size content view under a transparent title bar with the three
    standard buttons HIDDEN. The window stays resizable from its edges (NSResizableWindowMask).
  - Linux (gtk): set_decorated(False). No title bar and no resize border.

So this module adds, per platform:
  - Windows: drag and edge resize hand the gesture to Windows itself (ReleaseCapture, then
    WM_NCLBUTTONDOWN with HTCAPTION or the edge's hit-test code, posted on the window's own
    thread, from the press point), so the move and size loops, snap and Aero shake are the
    system's. The frame styles come back with no visible frame (install_resizable_frame), so a
    maximize is the system's own and stops at the work area.
  - macOS: the real traffic lights are shown again over the transparent title bar (pyobjc), so
    close, minimize and the full-screen tile menu are native. Nothing is drawn by the page.
  - Linux: edge resize through GTK's begin_resize_drag; drag stays pywebview's drag region.

Every call is best effort: a failing native call is logged and the window keeps working.
Everything here can only be verified on real hardware (a local-required gate)."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

import structlog

log = structlog.get_logger()

# Windows hit-test codes for WM_NCLBUTTONDOWN (winuser.h).
HT = {"caption": 2, "w": 10, "e": 11, "n": 12, "nw": 13, "ne": 14, "s": 15, "sw": 16, "se": 17}
WM_NCLBUTTONDOWN = 0x00A1
EDGES = ("n", "s", "e", "w", "ne", "nw", "se", "sw")


def os_key(platform: str | None = None) -> str:
    """'win', 'mac' or 'linux': what the page's controls are drawn for."""
    p = platform or sys.platform
    if p == "darwin":
        return "mac"
    if p.startswith("linux"):
        return "linux"
    return "win"


class WindowApi:
    """The JS API on the window (`window.pywebview.api`). pywebview exposes every public method to
    the page and nothing that starts with an underscore, so all state is private. The splash
    page's buttons (Try again, Open logs folder, Quit) call it too.

    get_window returns the pywebview window (or None before it exists); close_intent says what a
    close would do ('tray' or 'quit'); close runs the shell's close (the same path as the OS
    close button: close-to-tray, or a graceful quit); the splash callbacks are the shell's."""

    def __init__(
        self,
        get_window: Callable[[], Any],
        *,
        close_intent: Callable[[], str],
        close: Callable[[], None],
        retry: Callable[[], None] | None = None,
        open_logs: Callable[[], None] | None = None,
        quit_app: Callable[[], None] | None = None,
        remember_theme: Callable[[str], None] | None = None,
        platform: str | None = None,
    ) -> None:
        self._get_window = get_window
        self._close_intent = close_intent
        self._close = close
        self._retry = retry
        self._open_logs = open_logs
        self._quit = quit_app
        self._remember_theme = remember_theme
        self._os = os_key(platform)
        self._strip_rgb: tuple[int, int, int] | None = None
        self._maximized = False

    # ------------------------------------------------------------------- state from the shell
    def _set_maximized(self, value: bool) -> None:
        self._maximized = bool(value)

    def _native(self) -> Any:
        w = self._get_window()
        return getattr(w, "native", None) if w is not None else None

    # ------------------------------------------------------------------------- the JS API
    def _is_maximized(self) -> bool:
        """Windows asks the window itself (Win+Up, a drag to the top and Snap change it without
        telling pywebview); elsewhere the shell's maximized and restored events keep the flag."""
        if self._os == "win":
            zoomed = _win_is_zoomed(self._native())
            if zoomed is not None:
                self._maximized = zoomed
        return self._maximized

    def window_info(self) -> dict[str, Any]:
        return {"os": self._os, "maximized": self._is_maximized()}

    def window_minimize(self) -> None:
        w = self._get_window()
        if w is not None:
            w.minimize()

    def window_toggle_maximize(self) -> bool:
        """Maximize or restore; returns the new state for the button's icon."""
        w = self._get_window()
        if w is None:
            return self._maximized
        target = not self._is_maximized()
        if self._os == "win":
            # The same system command Win+Up and a caption double click send, so all three share
            # one path and WM_GETMINMAXINFO sizes it. pywebview's maximize() set WinForms'
            # WindowState, which did nothing on this frameless form (hardware check, round 5).
            if _win_syscommand(self._native(), SC_MAXIMIZE if target else SC_RESTORE):
                self._maximized = target
                return target
        if target:
            w.maximize()
        else:
            w.restore()
        self._maximized = target
        return target

    def window_close_intent(self) -> str:
        try:
            return "quit" if self._close_intent() == "quit" else "tray"
        except Exception as exc:  # the page must still get an answer
            log.warning("close_intent_failed", error=str(exc))
            return "tray"

    def window_close(self) -> None:
        self._close()

    def window_set_theme(self, theme: str) -> None:
        """The page's light or dark choice, kept for the next launch's first frame."""
        if self._remember_theme and theme in ("light", "dark", "auto"):
            self._remember_theme(theme)
        # A second path to the strip color: the page also sends its computed --frame-strip, but
        # if that call is lost the strip kept the launch color (a light ring on a dark window).
        if self._os == "win" and theme in ("light", "dark"):
            dark = theme == "dark"
            self.window_set_frame_color("rgb(9, 13, 32)" if dark else "rgb(255, 255, 255)")

    def window_set_frame_color(self, css_color: str) -> None:
        """D-UI-196: the resize strip's color, the page's --frame-strip as computed CSS
        ("rgb(r, g, b)"), so it follows the theme and scheme. Windows only; ignored elsewhere."""
        rgb = parse_rgb(css_color)
        if rgb is None or self._os != "win":
            return
        form = self._native()
        if form is None:
            return
        self._strip_rgb = rgb
        _win_invoke(form, lambda: _set_back_color(form, rgb), "frame_color")
        if not getattr(self, "_strip_hooked", False):
            # Windows repaints the border in its own color when the window gains or loses focus
            # (a white edge on a dark window, owner round 13), so the color goes back on each time.
            self._strip_hooked = True

            def again(sender: Any = None, args: Any = None) -> None:
                if self._strip_rgb is None:
                    return
                try:  # on the UI thread: an exception here would take the window down
                    _set_back_color(form, self._strip_rgb)
                except Exception as exc:  # noqa: BLE001 - logged, never raised on the UI thread
                    log.warning("frame_ui_call_failed", what="frame_color_again", error=str(exc))

            def hook() -> None:
                from System import EventHandler

                form.Activated += EventHandler(again)
                form.Deactivate += EventHandler(again)

            _win_invoke(form, hook, "frame_color_hook")

    def window_start_drag(self, x: float | None = None, y: float | None = None) -> None:
        """Start a native move; x and y are the press point in physical screen pixels."""
        if self._os == "win":
            press = (int(round(x)), int(round(y))) if x is not None and y is not None else None
            _win_nc_button_down(self._native(), HT["caption"], press)

    def window_start_resize(self, edge: str) -> None:
        if edge not in EDGES or self._is_maximized():
            return
        if self._os == "win":
            _win_nc_button_down(self._native(), HT[edge])
        elif self._os == "linux":
            _gtk_begin_resize(self._native(), edge)

    # ------------------------------------------------------------- the splash page's buttons
    def splash_retry(self) -> None:
        if self._retry:
            self._retry()

    def splash_open_logs(self) -> None:
        if self._open_logs:
            self._open_logs()

    def splash_quit(self) -> None:
        if self._quit:
            self._quit()


# ------------------------------------------------------------------------------------ Windows
def _user32() -> Any:
    """user32 with explicit prototypes: an HWND is pointer-sized, so a 64-bit handle must not be
    squeezed through ctypes' default int conversion."""
    import ctypes
    from ctypes import wintypes

    u = ctypes.WinDLL("user32", use_last_error=True)
    u.ReleaseCapture.argtypes = []
    u.ReleaseCapture.restype = wintypes.BOOL
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.PostMessageW.restype = wintypes.BOOL
    u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    u.GetCursorPos.restype = wintypes.BOOL
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.GetWindowRect.restype = wintypes.BOOL
    u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, wintypes.UINT]
    u.SetWindowPos.restype = wintypes.BOOL
    return u


WM_SYSCOMMAND = 0x0112
SC_MAXIMIZE = 0xF030
SC_RESTORE = 0xF120


def _win_hwnd(form: Any) -> int | None:
    try:
        return int(form.Handle.ToInt64()) if form is not None else None
    except Exception as exc:  # a disposed form or no pythonnet
        log.warning("frame_hwnd_failed", error=f"{type(exc).__name__}: {exc}")
        return None


def _win_is_zoomed(form: Any) -> bool | None:
    """Whether Windows has the window maximized; None when it cannot be asked."""
    hwnd = _win_hwnd(form)
    if hwnd is None:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        u = ctypes.WinDLL("user32", use_last_error=True)
        u.IsZoomed.argtypes = [wintypes.HWND]
        u.IsZoomed.restype = wintypes.BOOL
        return bool(u.IsZoomed(hwnd))
    except (OSError, AttributeError) as exc:
        log.warning("frame_is_zoomed_failed", error=str(exc))
        return None


def _win_syscommand(form: Any, command: int) -> bool:
    """Post a window-menu command (maximize, restore) to the window; True when posted."""
    hwnd = _win_hwnd(form)
    if hwnd is None:
        return False
    try:
        return bool(_user32().PostMessageW(hwnd, WM_SYSCOMMAND, command, 0))
    except (OSError, AttributeError) as exc:
        log.warning("frame_syscommand_failed", command=command, error=str(exc))
        return False


def point_lparam(x: int, y: int) -> int:
    """MAKELPARAM for a screen point: x in the low word, y in the high word, each a signed 16-bit
    value (a display left of or above the primary one has negative coordinates)."""
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def _win_invoke(form: Any, fn: Callable[[], None], what: str) -> None:
    """Run fn on the form's own (UI) thread: capture, posting and bounds belong to it. Whatever
    fn raises there is logged; an exception on the UI thread would take the window down."""
    from System import Action  # pythonnet, present with pywebview's winforms backend

    def safe() -> None:
        try:
            fn()
        except Exception as exc:
            log.warning("frame_ui_call_failed", what=what, error=f"{type(exc).__name__}: {exc}")

    form.BeginInvoke(Action(safe))


def _win_nc_button_down(form: Any, hit: int, press: tuple[int, int] | None = None) -> None:
    """Hand a drag or an edge resize to Windows: the system's own move or size loop runs, so
    snap, Aero shake and the resize cursor are native."""
    if form is None:
        return
    try:
        def go() -> None:
            import ctypes
            from ctypes import wintypes

            user32 = _user32()
            hwnd = int(form.Handle.ToInt64())
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            if press is not None:
                # A move starts only after the pointer travels a few pixels (so double-clicks
                # reach the page). Windows anchors its move loop at the cursor's position when
                # the loop starts, whatever point we pass (hardware rounds 7 and 8: the drag lost
                # that travel), so first shift the window by it: the grab point stays under the
                # cursor.
                dx, dy = pt.x - press[0], pt.y - press[1]
                if dx or dy:
                    rect = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    swp_flags = 0x0001 | 0x0004 | 0x0010  # NOSIZE | NOZORDER | NOACTIVATE
                    user32.SetWindowPos(hwnd, None, rect.left + dx, rect.top + dy, 0, 0,
                                        swp_flags)
            user32.ReleaseCapture()
            # The press point, in screen coordinates: the move loop measures the drag from it.
            # A zero here made Windows start the drag at the screen's origin, so the window
            # jumped to another display (hardware check, rounds 1 and 3).
            user32.PostMessageW(hwnd, WM_NCLBUTTONDOWN, hit, point_lparam(pt.x, pt.y))

        _win_invoke(form, go, "nc_button_down")
    except Exception as exc:  # best effort; the window keeps working without it
        log.warning("frame_nc_button_down_failed", hit=hit, error=f"{type(exc).__name__}: {exc}")


# The resizable frame on Windows. WinForms' borderless form has no WS_THICKFRAME, so Windows
# treats it as fixed: Win+Left/Right snap does nothing and a size loop handed to it is refused.
# Adding the frame styles back, and answering WM_NCCALCSIZE with "the client is the whole
# window", gives Windows a resizable, snappable window that still draws no border or caption.
GWL_STYLE = -16
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_SYSMENU = 0x00080000
WM_NCCALCSIZE = 0x0083
WM_GETMINMAXINFO = 0x0024
WM_NCHITTEST = 0x0084
WM_SIZE = 0x0005
SIZE_MAXIMIZED = 2
# The resize strip (D-UI-88, hardware round 6): the form keeps a border this many logical pixels
# wide around the WebView2 control, painted in the window's background color. The WebView2 child
# window covers everything else, so only this strip reaches the form's own hit test, where it
# answers with the edges and corners and Windows draws the cursors and runs a native resize.
BORDER_PX = 5


def edge_hit(x: int, y: int, rect: tuple[int, int, int, int], border: int) -> int | None:
    """The WM_NCHITTEST answer for a screen point: an HT edge or corner code inside the border
    strip of rect (left, top, right, bottom), None elsewhere. Corners take twice the strip along
    each edge so they are easy to grab."""
    left, top, right, bottom = rect
    on_w, on_e = x < left + border, x >= right - border
    on_n, on_s = y < top + border, y >= bottom - border
    if not (on_w or on_e or on_n or on_s):
        return None
    corner = 2 * border
    near_w, near_e = x < left + corner, x >= right - corner
    near_n, near_s = y < top + corner, y >= bottom - corner
    v = "n" if near_n else "s" if near_s else ""
    h = "w" if near_w else "e" if near_e else ""
    if v and h:
        return HT[v + h]
    return HT["n" if on_n else "s" if on_s else "w" if on_w else "e"]


def signed_point(lparam: int) -> tuple[int, int]:
    """GET_X_LPARAM / GET_Y_LPARAM: signed 16-bit screen coordinates (a display left of or above
    the primary one is negative)."""
    x, y = lparam & 0xFFFF, (lparam >> 16) & 0xFFFF
    return (x - 0x10000 if x & 0x8000 else x), (y - 0x10000 if y & 0x8000 else y)
MONITOR_DEFAULTTONEAREST = 2


def max_rect(
    monitor: tuple[int, int, int, int], work: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    """The maximized window for one monitor, as WM_GETMINMAXINFO wants it: (x, y, width, height)
    with the position relative to that monitor's top-left (its own origin, even when the monitor
    sits left of or above the primary one) and the size of its work area (the taskbar excluded).
    Rectangles are (left, top, right, bottom) in screen pixels."""
    return (work[0] - monitor[0], work[1] - monitor[1], work[2] - work[0], work[3] - work[1])
# SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
SWP_FLAGS = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020
_SUBCLASS_ID = 0x4B53  # "KS"
_subclass_proc: Any = None  # held for the life of the window: Windows calls back into it


def frame_style(style: int) -> int:
    """The window style with the frame bits a snappable, resizable window needs."""
    return style | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU


def install_resizable_frame(window: Any) -> None:
    """Give the frameless WinForms window back its frame styles with no visible frame."""
    global _subclass_proc
    form = getattr(window, "native", None)
    if form is None:
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        comctl = ctypes.WinDLL("comctl32", use_last_error=True)
        LRESULT = ctypes.c_ssize_t
        proto = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                   wintypes.LPARAM, ctypes.c_size_t, ctypes.c_size_t)
        comctl.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                           wintypes.LPARAM]
        comctl.DefSubclassProc.restype = LRESULT
        comctl.SetWindowSubclass.argtypes = [wintypes.HWND, proto, ctypes.c_size_t,
                                             ctypes.c_size_t]
        comctl.SetWindowSubclass.restype = wintypes.BOOL
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.SetWindowPos.restype = wintypes.BOOL

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class MINMAXINFO(ctypes.Structure):
            _fields_ = [("ptReserved", POINT), ("ptMaxSize", POINT), ("ptMaxPosition", POINT),
                        ("ptMinTrackSize", POINT), ("ptMaxTrackSize", POINT)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT), ("rcWork", RECT),
                        ("dwFlags", wintypes.DWORD)]

        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.MonitorFromWindow.restype = wintypes.HANDLE
        user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
        user32.GetMonitorInfoW.restype = wintypes.BOOL

        def proc(hwnd, msg, wparam, lparam, _uid, _data):  # noqa: ANN001, ANN202
            try:
                if msg == WM_NCCALCSIZE and wparam:
                    return 0  # the whole window is client area: no border, no caption
                if msg == WM_NCHITTEST and not user32.IsZoomed(hwnd):
                    r = RECT()
                    if user32.GetWindowRect(hwnd, ctypes.byref(r)):
                        x, y = signed_point(lparam)
                        hit = edge_hit(x, y, (r.left, r.top, r.right, r.bottom), border)
                        if hit is not None:
                            return hit
                if msg == WM_SIZE:
                    # No strip while maximized: the page reaches the screen's edges.
                    pad = 0 if wparam == SIZE_MAXIMIZED else border
                    _win_invoke(form, lambda: _set_padding(form, pad), "frame_padding")
                if msg == WM_GETMINMAXINFO:
                    # A window with no caption maximizes over the whole monitor, taskbar and
                    # all (5134x1454 at -7,-7 on the hardware check). Every maximize path (the
                    # button, a double click, Win+Up, a drag to the top) reads this, so pin it
                    # to the work area of the monitor the window is on.
                    result = comctl.DefSubclassProc(hwnd, msg, wparam, lparam)
                    mon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
                    info = MONITORINFO()
                    info.cbSize = ctypes.sizeof(MONITORINFO)
                    if mon and user32.GetMonitorInfoW(mon, ctypes.byref(info)):
                        m, w = info.rcMonitor, info.rcWork
                        x, y, cx, cy = max_rect((m.left, m.top, m.right, m.bottom),
                                                (w.left, w.top, w.right, w.bottom))
                        mmi = MINMAXINFO.from_address(lparam)
                        mmi.ptMaxPosition.x, mmi.ptMaxPosition.y = x, y
                        mmi.ptMaxSize.x, mmi.ptMaxSize.y = cx, cy
                    return result
            except Exception as exc:  # a window procedure must never raise into Windows
                log.warning("frame_wndproc_failed", msg=msg, error=str(exc))
            return comctl.DefSubclassProc(hwnd, msg, wparam, lparam)

        user32.IsZoomed.argtypes = [wintypes.HWND]
        user32.IsZoomed.restype = wintypes.BOOL
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        try:
            user32.GetDpiForWindow.argtypes = [wintypes.HWND]
            user32.GetDpiForWindow.restype = wintypes.UINT
        except AttributeError:  # before Windows 10 1607
            pass
        border = BORDER_PX

        def go() -> None:
            global _subclass_proc
            nonlocal border
            hwnd = int(form.Handle.ToInt64())
            dpi = 96
            if hasattr(user32, "GetDpiForWindow"):
                dpi = user32.GetDpiForWindow(hwnd) or 96
            border = max(BORDER_PX, round(BORDER_PX * dpi / 96))
            _subclass_proc = proto(proc)
            if not comctl.SetWindowSubclass(hwnd, _subclass_proc, _SUBCLASS_ID, 0):
                log.warning("frame_subclass_failed")
                return
            style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
            user32.SetWindowLongPtrW(hwnd, GWL_STYLE, frame_style(style))
            user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_FLAGS)
            _set_padding(form, 0 if user32.IsZoomed(hwnd) else border)
            log.info("frame_resizable_installed", border=border)

        _win_invoke(form, go, "install_resizable_frame")
    except Exception as exc:
        log.warning("frame_resizable_failed", error=f"{type(exc).__name__}: {exc}")


def parse_rgb(css_color: Any) -> tuple[int, int, int] | None:
    """(r, g, b) from a computed CSS color ("rgb(9, 13, 32)" or "rgba(...)"); None otherwise.
    Never a regex on untrusted shapes beyond this fixed form; out-of-range parts are clamped."""
    if not isinstance(css_color, str):
        return None
    text = css_color.strip()
    for prefix in ("rgba(", "rgb("):
        if text.startswith(prefix) and text.endswith(")"):
            parts = [p.strip() for p in text[len(prefix) : -1].split(",")]
            if len(parts) < 3:
                return None
            try:
                r, g, b = (max(0, min(255, round(float(p)))) for p in parts[:3])
            except ValueError:
                return None
            return r, g, b
    return None


def _set_back_color(form: Any, rgb: tuple[int, int, int]) -> None:
    """The resize strip's color, and Windows 11's own one-pixel window border in the same color,
    so a dark window has no light ring around it (owner round 11). The border call fails quietly
    before Windows 11 (the attribute does not exist there)."""
    from System.Drawing import Color

    form.BackColor = Color.FromArgb(*rgb)
    form.Invalidate()
    try:
        import ctypes

        colorref = ctypes.c_uint(rgb[0] | (rgb[1] << 8) | (rgb[2] << 16))
        hwnd = ctypes.c_void_p(int(form.Handle.ToInt64()))
        ctypes.windll.dwmapi.DwmSetWindowAttribute(  # DWMWA_BORDER_COLOR
            hwnd, 34, ctypes.byref(colorref), ctypes.sizeof(colorref))
    except (AttributeError, OSError, ValueError) as exc:
        log.debug("frame_border_color_unavailable", error=str(exc))
    log.debug("frame_color_set", rgb=list(rgb))


def _set_padding(form: Any, pad: int) -> None:
    """Inset the docked WebView2 control by pad pixels; the strip shows the form's BackColor
    (pywebview sets it from the window's background color, the theme's ground)."""
    from System.Windows.Forms import Padding

    if form.Padding.All != pad:
        form.Padding = Padding(pad)


# -------------------------------------------------------------------------------------- Linux
def _gtk_begin_resize(gtk_window: Any, edge: str) -> None:
    if gtk_window is None:
        return
    try:
        import gi

        gi.require_version("Gdk", "3.0")
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gdk, GLib, Gtk

        edges = {
            "n": Gdk.WindowEdge.NORTH, "s": Gdk.WindowEdge.SOUTH, "e": Gdk.WindowEdge.EAST,
            "w": Gdk.WindowEdge.WEST, "ne": Gdk.WindowEdge.NORTH_EAST,
            "nw": Gdk.WindowEdge.NORTH_WEST, "se": Gdk.WindowEdge.SOUTH_EAST,
            "sw": Gdk.WindowEdge.SOUTH_WEST,
        }

        if "Wayland" in type(Gdk.Display.get_default()).__name__:
            # begin_resize_drag needs the button event's serial on Wayland, which the WebKit view
            # does not hand back; edge resize there is not supported (D-UI-188's fallback).
            log.info("frame_resize_unsupported_on_wayland")
            return

        def go() -> bool:
            try:
                seat = Gdk.Display.get_default().get_default_seat()
                _screen, x, y = seat.get_pointer().get_position()
                gtk_window.begin_resize_drag(edges[edge], 1, x, y, Gtk.get_current_event_time())
            except Exception as exc:
                log.warning("frame_gtk_resize_failed", edge=edge, error=str(exc))
            return False

        GLib.idle_add(go)
    except Exception as exc:
        log.warning("frame_gtk_resize_failed", edge=edge, error=f"{type(exc).__name__}: {exc}")


# -------------------------------------------------------------------------------------- macOS
def show_traffic_lights(window: Any) -> None:
    """pywebview hides the standard buttons of a frameless Cocoa window; show them again over the
    transparent, full-size title bar (D-UI-88), on the main thread AppKit requires."""
    native = getattr(window, "native", None)
    if native is None:
        return
    try:
        import AppKit
        from PyObjCTools import AppHelper

        def go() -> None:
            try:
                for kind in (AppKit.NSWindowCloseButton, AppKit.NSWindowMiniaturizeButton,
                             AppKit.NSWindowZoomButton):
                    button = native.standardWindowButton_(kind)
                    if button is not None:
                        button.setHidden_(False)
            except Exception as exc:
                log.warning("frame_traffic_lights_failed", error=str(exc))

        AppHelper.callAfter(go)
    except Exception as exc:
        log.warning("frame_traffic_lights_failed", error=f"{type(exc).__name__}: {exc}")
