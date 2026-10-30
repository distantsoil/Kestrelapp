"""Path and packaged-resource resolution. This module is imported before everything else and has
no Kestrel dependencies of its own (config.py depends on it, never the reverse).

Rules it enforces (CLAUDE.md, SPEC.md 2, SPEC-INTERFACES.md 1.4):
  - Nothing writable lives beside the executable. All writes go under the per-OS data home:
      Windows  %LOCALAPPDATA%\\Kestrel (the packaged desktop app's home, unchanged)
      macOS    ~/Library/Application Support/Kestrel
      Linux    $XDG_DATA_HOME/kestrel, else ~/.local/share/kestrel
    KESTREL_DATA_HOME overrides all three (tests, and an operator who wants it elsewhere).
  - Packaged defaults (config/*.yml, models/Modelfile.*) are accessed via importlib.resources,
    never __file__-relative arithmetic, so the frozen read-only install works identically to dev.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import as_file, files
from importlib.resources.abc import Traversable
from pathlib import Path

APP_NAME = "Kestrel"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def data_home() -> Path:
    """The writable root, per OS (see the module docstring), overridable with KESTREL_DATA_HOME."""
    override = os.environ.get("KESTREL_DATA_HOME")
    if override:
        return Path(override)
    return default_data_home()


def default_data_home() -> Path:
    """The per-OS default data home, ignoring KESTREL_DATA_HOME (the single-instance lock compares
    against it, and the installer's AppMutex belongs to this folder only)."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(local) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / APP_NAME.lower()


def data_dir() -> Path:
    return data_home() / "data"


def config_dir() -> Path:
    return data_home() / "config"


def logs_dir() -> Path:
    return data_home() / "logs"


def dotenv_path() -> Path:
    return data_home() / ".env"


def runtime_json() -> Path:
    return data_home() / "runtime.json"


def window_state_json() -> Path:
    """The desktop window's own remembered state (its light or dark theme), read before the
    loading page paints so the first frame matches (D-UI-89)."""
    return data_home() / "window.json"


def db_path() -> Path:
    return data_dir() / "kestrel.db"


def sounds_dir() -> Path:
    """Where Kestrel keeps the alert sounds it renders for itself (kestrel.services.alert_sound):
    one small WAV per sound and volume. A cache: deleting it only makes the next alert draw them
    again."""
    return data_home() / "cache" / "sounds"


def ml_dir() -> Path:
    """Where the reranker and its fitted PCA are persisted (SPEC-MODULES 5.4). Lives under the
    writable data dir, never beside the read-only packaged app."""
    return data_dir() / "ml"


def evals_dir() -> Path:
    """Where the bench-harness gold sets live (SPEC.md 8.6). The shipped placeholder files
    (`evals/goldset.<track>.jsonl`) are checked into the repo root, one level above the `kestrel`
    package -- a dev-time source file, not packaged resource data. A frozen install has no writable
    repo tree beside the executable, so regeneration there lands under the writable data dir
    instead; the current working directory (where `kestrel goldset regen` is actually run from in
    dev) resolves the repo root without any `__file__` arithmetic."""
    d = data_dir() / "evals" if is_frozen() else Path.cwd() / "evals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def exports_dir() -> Path:
    """Kestrel's own folder for files the operator asked it to save, used when the Downloads
    folder cannot be found or written (D-UI-41)."""
    return data_home() / "exports"


def _windows_downloads() -> Path | None:
    """The Downloads Known Folder (FOLDERID_Downloads), which follows a Downloads folder the
    operator moved to another drive; None when the shell call is unavailable or fails."""
    import ctypes

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    # {374DE290-123F-4565-9164-39C4925E467B}
    folder_id = _GUID(
        0x374DE290, 0x123F, 0x4565,
        (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B),
    )
    out = ctypes.c_wchar_p()
    try:
        windll = ctypes.windll  # type: ignore[attr-defined]
        hr = windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(out)
        )
        try:
            return Path(out.value) if hr == 0 and out.value else None
        finally:
            windll.ole32.CoTaskMemFree(out)
    except (OSError, AttributeError, ValueError):
        return None


def downloads_dir() -> Path:
    """Where the desktop app saves a file the operator asked for, such as the diagnostics zip
    (D-UI-41): the user's Downloads folder (on Windows the Known Folder, so a relocated
    Downloads is honored), else Kestrel's own exports folder under the data home."""
    if sys.platform == "win32":
        found = _windows_downloads()
        if found is not None and found.is_dir():
            return found
    home_downloads = Path.home() / "Downloads"
    if home_downloads.is_dir():
        return home_downloads
    return exports_dir()


def ensure_dirs() -> None:
    for d in (data_dir(), config_dir(), logs_dir()):
        d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- packaged resources
def _resources() -> Traversable:
    return files("kestrel.resources")


def packaged_config() -> Traversable:
    return _resources() / "config"


def packaged_models() -> Traversable:
    return _resources() / "models"


def packaged_icons() -> Traversable:
    """The rasterized tray icons, generated from docs/images by scripts/make_icons.py."""
    return _resources() / "icons"


def packaged_app_icon() -> Traversable:
    """The indigo app icon (.ico, the installer's and Kestrel.exe's), for the Windows
    notification identity (shell.register_notification_icon)."""
    return _resources() / "icons" / "kestrel.ico"


def packaged_embed_model() -> Traversable:
    """The bundled in-process embedding model (kestrel.llm.inprocess_embed). Written at build time
    by scripts/fetch_embed_model.py and never committed; absent in a fresh source checkout."""
    return _resources() / "embed_model"


def packaged_migrations() -> Traversable:
    """The Alembic migrations directory (env.py + versions/), shipped as package data."""
    return files("kestrel.db.migrations")


def web_templates_dir() -> str:
    """The Jinja templates directory as a real path (Jinja2Templates needs a directory string).
    One-folder installs and dev checkouts are unzipped, so files() is already a real directory."""
    return str(files("kestrel.web") / "templates")


def web_static_dir() -> str:
    """The vendored static assets directory (HTMX, Alpine, prebuilt CSS) as a real path. No CDN at
    runtime; the packaged app serves these from disk (SPEC-INTERFACES 1.4)."""
    return str(files("kestrel.web") / "static")


def tiktoken_cache_dir() -> str:
    """The vendored cl100k_base cache dir, as a real path. Set as TIKTOKEN_CACHE_DIR before first
    use so tokenization is offline (the packaged app never downloads the encoding)."""
    return str(_resources() / "tiktoken_cache")


@contextmanager
def resource_path(traversable: Traversable) -> Iterator[Path]:
    """Yield a real filesystem path for a packaged resource (for tools like `ollama create -f`).
    In dev and one-folder installs this is the resource itself; as_file extracts if it is zipped."""
    with as_file(traversable) as p:
        yield p


# ------------------------------------------------------------------------------ config seeding
CONFIG_FILENAMES = ("settings.yml", "filters.yml", "sources.yml", "profile.example.yml")


def config_file(name: str) -> Path | None:
    """Resolve a config file: the operator's seeded copy under config_dir wins; otherwise fall
    back to the packaged default (so a fresh, unseeded install still loads)."""
    seeded = config_dir() / name
    if seeded.exists():
        return seeded
    packaged = packaged_config() / name
    return Path(str(packaged)) if packaged.is_file() else None


def seed_config() -> list[str]:
    """On first run, copy packaged config defaults into config_dir when it holds no *.yml yet.
    Returns the list of filenames written. profile.example.yml is seeded; the operator copies it
    to profile.yml (which is never shipped and never committed)."""
    ensure_dirs()
    existing = list(config_dir().glob("*.yml"))
    if existing:
        return []
    written: list[str] = []
    src_dir = packaged_config()
    for name in CONFIG_FILENAMES:
        src = src_dir / name
        if src.is_file():
            (config_dir() / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            written.append(name)
    return written
