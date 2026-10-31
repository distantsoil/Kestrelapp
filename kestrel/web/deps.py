"""Shared web dependencies: the Jinja environment (with the track-aware helpers the templates use)
and the app sessionmaker. The sessionmaker is the app-wide one from db.engine, created lazily on
uvicorn's loop so every request session lives on that single loop (SPEC-INTERFACES 1.1).

Also the shell's template globals (docs/UI_PROPOSAL.html): the sidebar groups and the color scheme
the server renders on <html> for the first paint."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime

import structlog
from fastapi.templating import Jinja2Templates

from kestrel import paths
from kestrel.config import UI_SCHEMES
from kestrel.date_display import format_display
from kestrel.db.engine import get_sessionmaker
from kestrel.intelligence.liveness import closed_reason_text
from kestrel.web.flash import flash_text
from kestrel.web.words import PLAIN, words

log = structlog.get_logger()


def _iso_to_local(ts: str | None) -> str:
    """Convert an ISO timestamp string to a local date/time string for display.
    Returns empty string for None/empty input. Format: YYYY-MM-DD HH:MM (24-hour)."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        # Convert to local timezone
        local_dt = dt.astimezone()
        return format_display(local_dt, kind="datetime", time_style="24h")
    except (ValueError, TypeError, OverflowError):
        return ts  # fallback to original string on parse error


_templates: Jinja2Templates | None = None


@dataclass(frozen=True)
class NavItem:
    label: str
    href: str
    # Path prefixes that mark this item active; "/" is matched exactly.
    prefixes: tuple[str, ...]
    icon: str
    # Key into the /status/counts payload for the optional count badge, or None.
    count: str | None = None
    # The label while the consulting track is the active one (Pursuits reads Leads there).
    consulting_label: str | None = None

    def label_for(self, track: str = "") -> str:
        """The sidebar label for the active track; consulting-only installs read as consulting."""
        if self.consulting_label and track_is_consulting(track):
            return self.consulting_label
        return self.label


TRACK_COOKIE = "kestrel-track"


def active_track(request) -> str:
    """The track the operator's tab is on (D-UI-57). There is no "both" view: with two tracks in
    use the switch is two tabs, and every track page reads exactly one. An explicit `?track=`
    wins (a link may name a track that is switched off; its data never left), then the
    remembered tab (the `kestrel-track` cookie the switch writes beside its localStorage copy)
    while that track is in use, then the first track in use."""
    from kestrel.services.tracks import enabled_tracks_cached

    enabled = tuple(enabled_tracks_cached())
    if request is not None:
        asked = request.query_params.get("track")
        if asked in ("employment", "consulting"):
            return str(asked)
        remembered = request.cookies.get(TRACK_COOKIE)
        if remembered in enabled:
            return str(remembered)
    return enabled[0]


def track_is_consulting(track: str = "") -> bool:
    """True when `track` is consulting, or when consulting is the only track in use."""
    from kestrel.services.tracks import enabled_tracks_cached

    return track == "consulting" or (not track and enabled_tracks_cached() == ("consulting",))


# The sidebar (docs/UI_PROPOSAL.html, Screen 1): Work, Learn, Setup, then Help and About on their
# own. Runs stays reachable under Setup. Bench left the sidebar (D-DRIFT-11): it is Model check
# now and lives under Models, so /bench highlights Models and the command palette lists it by its
# new name. The command palette lists the same pages. The Setup page itself is folded into
# Settings > System and the onboarding (D-UI-31), so the Setup group holds Sources, Models, Runs
# and Settings; /onboarding is reached from the first-run redirect, the Finish setup banner and
# Settings > System.
NAV_GROUPS: tuple[tuple[str, tuple[NavItem, ...]], ...] = (
    (
        "Work",
        (
            NavItem("Today", "/", ("/",), "today", "shortlist"),
            NavItem(
                "Pipeline", "/opportunities", ("/opportunities", "/orgs"),
                "pipeline", "unrated",
            ),
            NavItem(
                "Applications", "/pursuits", ("/pursuits",), "apps", "followups",
                consulting_label="Leads",
            ),
            NavItem("Profile", "/profile", ("/profile", "/documents", "/renders"), "profile"),
            NavItem("Interview prep", "/stories", ("/stories", "/prep", "/mock"), "prep"),
            NavItem("Offers", "/offers", ("/offers",), "offers"),
        ),
    ),
    (
        "Learn",
        (
            NavItem("Calibrate", "/calibrate", ("/calibrate",), "calibrate", "calibrate"),
            NavItem("Analytics", "/analytics", ("/analytics",), "analytics"),
        ),
    ),
    (
        "Setup",
        (
            NavItem("Sources", "/sources", ("/sources",), "sources"),
            NavItem("Models", "/models", ("/models", "/bench"), "models"),
            # Needs another try (/quarantine) has no nav entry; it sits under Runs (D-UI-104).
            NavItem("Runs", "/runs", ("/runs", "/quarantine"), "runs"),
            NavItem("Settings", "/settings", ("/settings", "/backup"), "settings"),
        ),
    ),
    (
        "",
        (
            NavItem("Help", "/help", ("/help",), "help"),
            NavItem("About", "/about", ("/about",), "about"),
        ),
    ),
)

# The phone bottom bar: the first three Work items; everything else opens from "More".
BOTTOM_BAR: tuple[str, ...] = ("/", "/opportunities", "/pursuits")


def nav_active(item: NavItem, path: str) -> bool:
    return any(path == p if p == "/" else path.startswith(p) for p in item.prefixes)


def nav_title(path: str, track: str = "") -> str:
    """The active page's label for the top bar, or an empty string off the map."""
    for _group, items in NAV_GROUPS:
        for item in items:
            if nav_active(item, path):
                return item.label_for(track)
    return ""


# ------------------------------------------------------------------------------ color scheme
# The scheme row (`ui.scheme` in the setting table) is read synchronously here because a Jinja
# global cannot await, and the attribute has to be on <html> before the stylesheet applies or the
# page flashes the default scheme. One primary-key read on a WAL file is sub-millisecond; it is
# cached and invalidated by the appearance route so a save shows on the very next request.
_SCHEME_TTL_S = 10.0
_scheme_cache: dict[str, object] = {"value": None, "at": 0.0, "db": ""}


def set_ui_scheme_cache(value: str | None) -> None:
    ok = value in UI_SCHEMES
    _scheme_cache["value"] = value if ok else None
    _scheme_cache["at"] = time.monotonic() if ok else 0.0
    _scheme_cache["db"] = str(paths.db_path())


_MISSING = object()


def _read_setting_row(key: str, timeout_s: float = 5.0) -> object:
    """One setting-table value by primary key, synchronously; _MISSING when there is no row, no
    database or table yet, or the read fails."""
    db = paths.db_path()
    if not db.exists():
        return _MISSING
    try:
        conn = sqlite3.connect(str(db), timeout=timeout_s)
        try:
            conn.execute(f"PRAGMA busy_timeout={int(timeout_s * 1000)}")
            row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:  # no table yet (pre-migration), locked beyond the timeout: default
        if "locked" in str(exc):
            log.warning("setting_row_locked", key=key, timeout_s=timeout_s)
        return _MISSING
    if row is None:
        return _MISSING
    try:
        return json.loads(row[0]) if isinstance(row[0], str) else row[0]
    except ValueError:
        return _MISSING


def _read_scheme_row() -> str | None:
    value = _read_setting_row("ui.scheme")
    return value if value in UI_SCHEMES else None


CLOSE_TO_TRAY_KEY = "ui.close_to_tray"


def close_to_tray() -> bool:
    """Whether closing the desktop window hides to the tray (D-UI-8). Read fresh on every close
    (one primary-key read), so a change on Settings > Appearance applies to the next close. The
    row wins; without one, settings.yml or KESTREL_UI__CLOSE_TO_TRAY; default on."""
    # The window waits on this read, so it is bounded to a second; a miss falls back below.
    value = _read_setting_row(CLOSE_TO_TRAY_KEY, timeout_s=1.0)
    if isinstance(value, bool):
        return value
    if value is not _MISSING:
        log.warning("close_to_tray_row_unreadable", value=repr(value))
    import yaml

    from kestrel.config import get_settings

    try:
        return get_settings().ui.close_to_tray
    except (ValueError, OSError, yaml.YAMLError):  # a broken settings file: keep the default
        return True


def ui_scheme() -> str:
    """The stored scheme name for `data-scheme` on <html>; "indigo" when nothing is stored."""
    now = time.monotonic()
    db = str(paths.db_path())  # the data home can move (tests, a restore): never serve a stale file
    cached = _scheme_cache["value"]
    if (
        cached is not None
        and _scheme_cache["db"] == db
        and now - float(_scheme_cache["at"]) < _SCHEME_TTL_S
    ):
        return str(cached)
    value = _read_scheme_row() or UI_SCHEMES[0]
    _scheme_cache.update(value=value, at=now, db=db)
    return value


# ---------------------------------------------------------------------------- preferences
# Settings > Preferences reach every page through ONE helper that base.html and the scene band
# macro call (D-UI-149 follow-up). The setting rows are read fresh on every render (a save shows
# on the very next page); the YAML and environment layer below them is cached for a few seconds.
_UI_KEYS = (
    "ui.scene", "ui.scene_daily", "ui.scene_rotation", "ui.scene_animate", "ui.touches_enabled",
    "ui.touches_off", "ui.touch_groups_off", "ui.avatar", "ui.text_size", "ui.reduce_motion",
    "ui.classic_nav",
)
_ui_layer_cache: dict[str, object] = {"value": None, "at": 0.0, "db": ""}


def _today():
    """Local today; a seam so tests can pick a date inside or outside a touch window."""
    from datetime import date

    return date.today()


def _ui_layer():
    """The `ui` block from settings.yml and the environment (no database row)."""
    import yaml

    from kestrel.config import UI, get_settings

    now = time.monotonic()
    db = str(paths.db_path())
    cached = _ui_layer_cache["value"]
    if cached is not None and _ui_layer_cache["db"] == db and (
        now - float(_ui_layer_cache["at"]) < _SCHEME_TTL_S
    ):
        return cached
    try:
        layer = get_settings().ui
    except (ValueError, OSError, yaml.YAMLError):
        layer = UI()
    _ui_layer_cache.update(value=layer, at=now, db=db)
    return layer


def _read_ui_rows() -> dict[str, object]:
    db = paths.db_path()
    if not db.exists():
        return {}
    marks = ",".join("?" for _ in _UI_KEYS)
    try:
        conn = sqlite3.connect(str(db), timeout=2.0)
        try:
            conn.execute("PRAGMA busy_timeout=2000")
            rows = conn.execute(
                f"SELECT key, value FROM setting WHERE key IN ({marks})", _UI_KEYS
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    out: dict[str, object] = {}
    for key, raw in rows:
        try:
            out[key] = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            continue
    return out


def ui_prefs() -> dict[str, object]:
    """What the shell needs from Settings > Preferences, already validated (a stored value that
    no longer validates reads as the default). Keys: text_size, reduce_motion, avatar, photo
    (URL or ""), initial (a letter or ""), scene (the id to draw today, "none" for no art),
    animate (False while Reduce motion is on), touch (the touch id to draw today or "")."""
    from kestrel import ui_settings as u

    rows = _read_ui_rows()
    layer = _ui_layer()

    def get(name: str, fn, fallback):
        if f"ui.{name}" in rows:
            return u.lenient(fn, fallback)(rows[f"ui.{name}"])
        return getattr(layer, name)

    text_size = get("text_size", u.text_size, "default")
    reduce_motion = bool(get("reduce_motion", u.flag, False))
    classic_nav = bool(get("classic_nav", u.flag, False))  # D-UI-220
    scene = get("scene", u.scene, u.DEFAULT_SCENE)
    daily = bool(get("scene_daily", u.flag, True))
    rotation = list(get(
        "scene_rotation", u.scene_rotation, lambda: list(u.SCENES)
    ))
    today = _today()
    shown_scene = u.scene_for_day(scene, daily, rotation, today)
    animate = bool(get("scene_animate", u.flag, True)) and not reduce_motion
    touch_id = u.touch_for_day(
        today,
        bool(get("touches_enabled", u.flag, True)),
        list(get("touch_groups_off", u.touch_groups_off, list)),
        list(get("touches_off", u.touches_off, list)),
    )
    from kestrel.web.settings_view import TOUCH_JS

    avatar = get("avatar", u.avatar, "initial")
    photo = ""
    initial = ""
    if avatar == "photo":
        from kestrel.services import profile_photo

        version = profile_photo.version()
        if version:
            photo = f"/profile/photo?v={version}"
        else:
            avatar = "initial"
    if avatar == "initial":
        from kestrel.pipeline.profile import load_profile

        for track in ("employment", "consulting"):
            try:
                first = str(getattr(load_profile(track), "first_name", "") or "").strip()
            except (ValueError, OSError):
                first = ""
            if first:
                initial = first[:1].upper()
                break
    return {
        "text_size": text_size, "reduce_motion": reduce_motion, "classic_nav": classic_nav,
        "avatar": avatar,
        "photo": photo, "initial": initial, "scene": shown_scene, "animate": animate,
        "touch": TOUCH_JS.get(touch_id or "", ""),
    }


def tier_label(tier: object) -> str:
    """"Quick screen (Tier 1)" for 1, "1", "tier1" or "t1": the plain label first, the setting
    name beside it. Used only where Tier is a setting name (Models, Bench)."""
    digits = "".join(ch for ch in str(tier) if ch.isdigit())
    key = f"tier{digits}" if digits else str(tier)
    plain = PLAIN.get(key)
    return f"{plain} (Tier {digits})" if plain and digits else str(tier)


def too_few(n: int | None, needed: int = 5) -> str:
    """Hard rule 6 in plain words: a number from fewer than `needed` observations says so."""
    return f"too few to judge ({int(n or 0)} of {needed} needed)"


def _effective_track(track: str | None) -> str:
    """The track whose words a page uses: the one asked for, else consulting only when it is the
    only track in use."""
    if track in ("employment", "consulting"):
        return str(track)
    from kestrel.services.tracks import enabled_tracks_cached

    return "consulting" if tuple(enabled_tracks_cached()) == ("consulting",) else "employment"


_static_token: str | None = None


def static_token() -> str:
    """A fingerprint of the shipped static files (paths, sizes, modified times), computed once.
    Static URLs carry it, so the window caches each file for good within one build and a new
    build or edited file gets a new URL. Without it, a fresh install's new file dates made the
    browser re-check every stylesheet and script with the server on every page change."""
    global _static_token
    if _static_token is None:
        import hashlib
        from pathlib import Path

        root = Path(paths.web_static_dir())
        h = hashlib.sha256()
        for f in sorted(root.rglob("*")):
            try:
                if not f.is_file():
                    continue
                st = f.stat()
            except OSError:  # removed during the scan (a dev edit): the rest still fingerprint
                continue
            h.update(f"{f.relative_to(root).as_posix()}:{st.st_size}:{st.st_mtime_ns};".encode())
        _static_token = h.hexdigest()[:12]
    return _static_token


def static_url(path: str) -> str:
    """The URL of a file under /static/, versioned with static_token()."""
    return f"/static/{path.lstrip('/')}?v={static_token()}"


def templates() -> Jinja2Templates:
    global _templates
    if _templates is None:
        t = Jinja2Templates(directory=paths.web_templates_dir())
        t.env.globals["verdict_class"] = _verdict_class
        t.env.globals["static_url"] = static_url
        t.env.globals["nav_groups"] = NAV_GROUPS
        t.env.globals["bottom_bar"] = BOTTOM_BAR
        t.env.globals["nav_active"] = nav_active
        t.env.globals["nav_title"] = nav_title
        t.env.globals["ui_scheme"] = ui_scheme
        t.env.globals["ui_prefs"] = ui_prefs
        t.env.globals["words"] = words
        t.env.globals["flash_text"] = flash_text
        t.env.globals["closed_reason_text"] = closed_reason_text
        from kestrel.ingest.generated import US_NOTE

        t.env.globals["us_note"] = US_NOTE
        t.env.globals["too_few"] = too_few
        from kestrel.privacy import COMMON_WORD_WARNING, default_names, is_common_word

        # D-PRIV-1: the role editor's names-to-hide chips.
        t.env.globals["is_common_word"] = is_common_word
        t.env.globals["common_word_warning"] = COMMON_WORD_WARNING
        t.env.globals["default_hide_names"] = default_names
        t.env.filters["tier_label"] = tier_label
        # Which tracks are in use (D-UI-26): templates hide consulting when it is off.
        from kestrel.services.tracks import consulting_enabled, enabled_tracks_cached

        t.env.globals["enabled_tracks"] = enabled_tracks_cached
        t.env.globals["active_track"] = active_track
        from kestrel.web.desktop_bridge import can_quit, desktop_window, has_window_frame

        t.env.globals["desktop_can_quit"] = can_quit
        t.env.globals["desktop_has_frame"] = has_window_frame
        t.env.globals["desktop_window"] = desktop_window
        t.env.globals["consulting_enabled"] = consulting_enabled
        t.env.globals["ui_schemes"] = UI_SCHEMES
        from kestrel.services import run_model as _run_model

        # D-UI-72: a run's counts without its one-run model choice, and that choice in words.
        t.env.filters["run_counts"] = lambda c: {
            k: v for k, v in (c or {}).items()
            # D-SRC-17: the dropped-title sample has its own disclosure, never a raw dump.
            if k not in (_run_model.OVERRIDE_KEY, _run_model.LABEL_KEY, "dropped_title_sample")
        }
        t.env.filters["run_model_label"] = lambda c: _run_model.label_of(c)
        t.env.filters["comma"] = lambda n: (
            f"{int(n):,}" if isinstance(n, float) and n.is_integer() else
            f"{n:,}" if isinstance(n, (int, float)) else n
        )
        t.env.filters["money"] = format_money
        t.env.filters["typed_amount"] = format_typed_amount
        t.env.filters["iso_to_local"] = lambda ts: _iso_to_local(ts)
        t.env.globals["date_display"] = format_display
        _templates = t
    return _templates


def format_money(n: object) -> str:
    """Whole dollars with a thousands separator: 210000.0 -> "$210,000". Anything that is not a
    number is returned unchanged. The one money formatter for the Offers page."""
    if isinstance(n, bool) or not isinstance(n, (int, float)):
        return "" if n is None else str(n)
    return f"${round(n):,}"


_PLAIN_AMOUNT = re.compile(r"^\$?\s*(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?$")


def format_typed_amount(text: object) -> str:
    """A figure the operator typed, echoed. A plain amount (digits, optional $, commas, decimal
    part) is shown as money with exactly the digits they typed (no rounding, no "k" expansion);
    anything else comes back verbatim."""
    raw = "" if text is None else str(text)
    m = _PLAIN_AMOUNT.match(raw.strip())
    if not m:
        return raw
    whole = int(m.group(1).replace(",", ""))
    return f"${whole:,}{m.group(2) or ''}"


def sessionmaker():
    return get_sessionmaker()


def _verdict_class(verdict: str | None) -> str:
    return {
        "apply": "apply", "pursue": "pursue", "consider": "consider",
        "pass": "pass", "keep": "apply", "kill": "kill",
    }.get(verdict or "", "")
