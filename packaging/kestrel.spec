# PyInstaller one-folder spec for Kestrel (SPEC-INTERFACES 1.4). ONE-FOLDER, --windowed, ONE spec
# for all three platforms. Every hidden import and data entry below is what the Phase -1 spike
# (spike/FINDINGS.md F4) and the Follow-on 4 build (the private decision log) proved is load-bearing. Do NOT
# bundle Chromium, Ollama, or any Ollama model. The ONE bundled model is the in-process embedding
# model (D-EMBED-2): fetch it first with `uv run python scripts/fetch_embed_model.py`.
#
# Build:   uv run pyinstaller packaging/kestrel.spec --noconfirm
# Output:  Windows  dist/Kestrel/Kestrel.exe (windowed) + dist/Kestrel/kestrel-cli.exe (console)
#          macOS    dist/Kestrel.app (BUNDLE over the same one-folder; the CLI is
#                   Kestrel.app/Contents/MacOS/Kestrel with arguments)
#          Linux    dist/Kestrel/kestrel (packaging/build_appimage.sh wraps it in an AppImage)
#
# One entry point everywhere: kestrel/desktop/__main__.py opens the window with no arguments and
# dispatches to the `kestrel` CLI when given any. Windows alone gets a second EXE from the same
# Analysis because a windowed EXE has no console to print the CLI's output to, and NTFS is case-
# insensitive, so it cannot be called kestrel.exe next to Kestrel.exe.
#
# noqa / undefined names (Analysis, PYZ, EXE, COLLECT, BUNDLE) are injected by PyInstaller at spec
# exec time.

import importlib.util
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

ROOT = Path(SPECPATH).parent  # packaging/ -> repo root
IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

# The single-source version (kestrel/__init__.py feeds pyproject, /health, /about, the window title
# and the installer). Read, not imported: importing the package inside the spec would run its
# frozen-only side effects in the build process.
VERSION = re.search(
    r'^__version__\s*=\s*"([^"]+)"',
    (ROOT / "kestrel" / "__init__.py").read_text(encoding="utf-8"),
    re.MULTILINE,
).group(1)
BUNDLE_ID = "com.kestrel.desktop"

datas = []
binaries = []
hiddenimports = []

# The fragile hooks the spike confirmed (F4): pywebview (EdgeChromium via pythonnet on Windows,
# Cocoa on macOS, GTK/Qt on Linux when present), Playwright (the msedge driver, NOT Chromium), and
# clr_loader. collect_all bundles their data. pythonnet and clr_loader are Windows-only dependencies
# (pyproject markers), so they are collected only where they exist; a missing package would make
# collect_all abort the whole build on a macOS or Linux runner.
collect_all_pkgs = ["webview", "playwright"]
if importlib.util.find_spec("clr_loader") is not None:
    collect_all_pkgs.append("clr_loader")
    hiddenimports += ["clr"]  # pythonnet's runtime import, invisible to the static graph
# Third-party test suites and backends Kestrel never loads are filtered out of every submodule
# sweep (D-PKG-5): they bloated the bundle and each failed import was a warning in the build log
# (webview.platforms.android, sklearn's torch shim, alembic.testing.suite). A build-log warning
# is a defect here, not noise.
_SKIP_PARTS = {"tests", "testing", "conftest", "android", "torch", "cupy", "dask"}


def _keep(name: str) -> bool:
    return not _SKIP_PARTS.intersection(name.split("."))


for pkg in collect_all_pkgs:
    d, b, h = collect_all(pkg, filter_submodules=_keep)
    datas += d
    binaries += b
    hiddenimports += h

# JobSpy 1.3.0 talks to the boards through curl_cffi (libcurl-impersonate), which replaced the
# tls_client Go library of 1.1.x (D-PKG-4, amended). curl_cffi is a cffi extension (_wrapper) plus
# a delvewheel-repaired sibling folder `curl_cffi.libs` holding libcurl-impersonate; its __init__
# adds that folder, resolved as ../curl_cffi.libs next to the package, to the DLL search path.
# pyinstaller-hooks-contrib 2026.6 has no curl_cffi hook, and the folder is not a Python package,
# so collect_all("curl_cffi") alone would miss it and every packaged job search would fail to load
# the library. The folder goes to the bundle root, which is where ../curl_cffi.libs resolves.
if importlib.util.find_spec("curl_cffi") is not None:
    _curl_d, _curl_b, _curl_h = collect_all("curl_cffi", filter_submodules=_keep)
    datas += _curl_d
    binaries += _curl_b
    hiddenimports += _curl_h + ["_cffi_backend"]
    _curl_libs = Path(importlib.util.find_spec("curl_cffi").origin).parent.parent / "curl_cffi.libs"
    if _curl_libs.is_dir():
        binaries += [(str(f), "curl_cffi.libs") for f in _curl_libs.iterdir() if f.is_file()]

# Starlette imports its multipart backend by string at request time, so the static graph misses it
# and form posts fail only in the frozen app. Both names ship: the package renamed itself to
# python_multipart but still installs the legacy `multipart` alias, and which one Starlette reaches
# for depends on its version.
hiddenimports += ["python_multipart", "multipart"]

# tiktoken resolves cl100k_base through a namespace-package plugin the static graph misses (F2).
hiddenimports += ["tiktoken_ext", "tiktoken_ext.openai_public"]

# sklearn/scipy: the exact list the spike trained an HGBR with in the frozen app (F4 / §1.4).
hiddenimports += collect_submodules("sklearn", filter=_keep)
hiddenimports += [
    "sklearn.utils._typedefs",
    "sklearn.utils._heap",
    "sklearn.utils._sorting",
    "sklearn.utils._vector_sentinel",
    "scipy.special.cython_special",
]

# Dynamic autoloaders — insurance per F4 (trimming these is a size optimization, not correctness).
for pkg in ("uvicorn", "sqlalchemy", "alembic", "aiosqlite"):
    hiddenimports += collect_submodules(pkg, filter=_keep)

# Kestrel's own STRING-IMPORTED modules. Not insurance: without these the app cannot start.
# There are exactly two places Kestrel resolves its own modules by name at runtime, and the static
# graph cannot see either:
#   * kestrel/web/app.py `_PAGE_MODULES` -> importlib.import_module for all 18 page routers, and
#     most of the application (documents, research, interview, negotiation, tier 3) is reachable
#     only through those pages;
#   * kestrel/ingest/signal_sources/__init__.py -> __import__ per adapter.
# Without them the previous build shipped no kestrel/web/pages/ directory at all (86 of 164 kestrel
# modules, zero pages), so the frozen app raised ModuleNotFoundError on the first page import,
# before uvicorn served and before the window appeared.
#
# Deliberately NOT collect_submodules("kestrel"). That pulls every kestrel module into the set
# PyInstaller imports in ONE isolated subprocess during binary-dependency analysis, which drags in
# jobspy, pandas, the whole sklearn test tree, scipy, fastmcp and beartype together and crashes it
# ("Isolated subprocess crashed while importing package 'jobspy'"). Everything else in kestrel is
# statically reachable from the entry point, including function-local imports, which modulegraph
# follows. (the private decision log, Follow-on 4.)
hiddenimports += collect_submodules("kestrel.web.pages")
hiddenimports += collect_submodules("kestrel.ingest.signal_sources")

# Kestrel's own package data: config YAML, prompts, derived-model Modelfiles, web static +
# templates, and filters.yml — all loaded via importlib.resources, so they must ship as datas.
datas += collect_data_files("kestrel", includes=[
    "resources/**/*", "web/static/**/*", "web/templates/**/*", "docs/templates/**/*",
    "**/*.yml", "**/*.md",
])
# tests/test_packaging.py::test_every_package_data_file_is_shipped keeps this list honest: the
# PDF template (docs/templates) was missing until 0.3.0 and every preview and render in the
# packaged app failed with TemplateNotFound (D-PKG-3).

# Third-party notices (D-LIC-1): the license text of every bundled library, generated from the
# build environment's metadata so it matches exactly what is shipped. Read by /about/notices.
# Loaded by file path, not imported, for the same reason VERSION is read above: notices.py imports
# nothing from kestrel, so the package's __init__ never runs in the build process.
_ns_spec = importlib.util.spec_from_file_location(
    "kestrel_build_notices", ROOT / "kestrel" / "services" / "notices.py"
)
_ns = importlib.util.module_from_spec(_ns_spec)
_ns_spec.loader.exec_module(_ns)
_closure = _ns.runtime_closure()
if len(_closure) < 50:  # 118 on 2026-10-13; a near-empty closure means kestrel is not installed
    raise SystemExit(
        f"third-party notices: only {len(_closure)} bundled packages found; run `uv sync` so the "
        "kestrel distribution and its dependencies are installed in the build environment"
    )
# The in-process embedding model (D-EMBED-2, SPEC.md 8.1.1): ~90 MB of ONNX weights plus the
# tokenizer, fetched and SHA-256 verified by scripts/fetch_embed_model.py into
# kestrel/resources/embed_model/ and shipped by the collect_data_files call above. A build without
# it would leave every hosted_only (BYOK) install with no embeddings at all, so it is refused here
# rather than discovered by an operator. The names mirror inprocess_embed.BUNDLED.files plus the
# license NOTICE (a test keeps them in step).
EMBED_MODEL_DIR = ROOT / "kestrel" / "resources" / "embed_model"
EMBED_MODEL_FILES = ("model.onnx", "tokenizer.json", "config.json", "NOTICE.txt")
_missing_embed = [n for n in EMBED_MODEL_FILES if not (EMBED_MODEL_DIR / n).is_file()]
if _missing_embed:
    raise SystemExit(
        f"in-process embedding model missing from {EMBED_MODEL_DIR} ({', '.join(_missing_embed)}); "
        "run `uv run python scripts/fetch_embed_model.py` before building"
    )
_notices = _ns.write_notices(
    ROOT / "build" / "notices" / _ns.NOTICES_FILENAME,
    model_notice=EMBED_MODEL_DIR / "NOTICE.txt",
)
datas.append((str(_notices), "kestrel/resources"))

# Alembic migration modules ship as DATA (not analyzed): env.py resolves them via sys._MEIPASS and
# command.upgrade() imports them at runtime (F4 / §1.4). They MUST keep the kestrel/ prefix, because
# paths.packaged_migrations() loads them via files("kestrel.db.migrations") -> _MEIPASS/kestrel/db/
# migrations; shipping to db/migrations would leave that dir empty and abort frozen DB init.
mig = ROOT / "kestrel" / "db" / "migrations"
for pattern in ("*.py", "*.mako"):
    for f in mig.rglob(pattern):
        datas.append((str(f), str(f.parent.relative_to(ROOT))))

# The vendored tiktoken cl100k_base cache (F2, exact SHA-1 filename) already lives in the package at
# kestrel/resources/tiktoken_cache and is collected above; kestrel.ingest.tokens sets
# TIKTOKEN_CACHE_DIR to it, so the frozen app tokenizes offline with no separate vendor copy.

a = Analysis(
    [str(ROOT / "kestrel" / "desktop" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # F7: benign; keep the bundle lean. authlib and numpy.array_api are only optional imports of
    # dependencies (FastMCP's OAuth proxy, huggingface_hub's login, numpy's experimental API) that
    # Kestrel never loads; bundling them printed deprecation warnings on every build (D-PKG-5).
    # tests/test_packaging.py proves the app's import graph never reaches them.
    excludes=["tkinter", "pytest", "torch", "jax", "authlib", "numpy.array_api"],
    noarchive=False,
    # LGPL libraries ship as plain .py files in _internal, not inside the PYZ archive, so a user
    # can replace them with their own build as the LGPL requires (D-LIC-1): pystray (LGPLv3,
    # the tray icon) and python-xlib (LGPLv2+, its Linux backend).
    module_collection_mode={"pystray": "py", "Xlib": "py"},
)
pyz = PYZ(a.pure)

# --------------------------------------------------------------------------- per-platform icon
# Windows embeds an .ico in the EXE; macOS puts an .icns in the bundle; Linux executables carry no
# icon (the .desktop file in the AppImage points at the PNG instead). Each is optional so a checkout
# without the generated assets still builds (scripts/make_icons.py regenerates all of them).
def _optional(p: Path):
    return str(p) if p.exists() else None


if IS_WINDOWS:
    ICON = _optional(ROOT / "packaging" / "kestrel.ico")
elif IS_MACOS:
    ICON = _optional(ROOT / "packaging" / "kestrel.icns")
else:
    ICON = None

# The desktop executable. console=False everywhere: on Windows that is the --windowed subsystem (F3:
# stdout/stderr are None and __main__ repairs them); on macOS it marks the bundle as a GUI app; on
# Linux the flag is inert and the binary still prints to the terminal it was started from.
EXE_NAME = "Kestrel" if (IS_WINDOWS or IS_MACOS) else "kestrel"
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=EXE_NAME,
    console=False,
    icon=ICON,
)
exes = [exe]

if IS_WINDOWS:
    # The console twin for `kestrel-cli.exe setup|doctor|...`: same scripts, same PYZ, same
    # _internal folder, so it costs a few hundred KB, not a second bundle.
    exes.append(EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name="kestrel-cli",
        console=True,
        icon=ICON,
    ))

coll = COLLECT(*exes, a.binaries, a.datas, name="Kestrel")  # ONE-FOLDER, never one-file (§1.4)

if IS_MACOS:
    app = BUNDLE(
        coll,
        name="Kestrel.app",
        icon=ICON,
        bundle_identifier=BUNDLE_ID,
        version=VERSION,
        info_plist={
            "CFBundleName": "Kestrel",
            "CFBundleDisplayName": "Kestrel",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "CFBundleIdentifier": BUNDLE_ID,
            "NSHighResolutionCapable": True,
            # Ventura: the first macOS whose bundled WebKit pywebview 6 targets, and the floor of
            # the pyobjc wheels in uv.lock.
            "LSMinimumSystemVersion": "13.0",
            "LSApplicationCategoryType": "public.app-category.productivity",
            # The window is the UI; the dock icon stays. Set LSUIElement only if the tray-only
            # mode ever becomes the default on macOS.
            "NSSupportsAutomaticGraphicsSwitching": True,
        },
    )
