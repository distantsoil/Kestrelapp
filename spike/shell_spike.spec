# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-folder --windowed spec for the Phase -1 spike.

Encodes the SPEC-INTERFACES.md 1.4 packaging traps. The set here is the STARTING hypothesis;
FINDINGS.md records what the build actually needed after iterating on runtime failures.
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

HERE = Path(SPECPATH)

datas = []
binaries = []
hiddenimports = []

# --- Playwright: driver dir (node.exe + package/) is NOT collected by default (1.4) -----------
pw_datas, pw_binaries, pw_hidden = collect_all("playwright")
datas += pw_datas
binaries += pw_binaries
hiddenimports += pw_hidden

# --- pywebview + pythonnet: the fragile EdgeChromium backend (1.4) ----------------------------
for pkg in ("webview", "clr_loader"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
hiddenimports += ["clr"]  # pythonnet; hook-clr bundles Python.Runtime.dll

# --- scikit-learn + scipy: reranker; explicit compiled submodules (1.4) -----------------------
hiddenimports += collect_submodules("sklearn")
hiddenimports += [
    "sklearn.utils._typedefs",
    "sklearn.utils._heap",
    "sklearn.utils._sorting",
    "sklearn.utils._vector_sentinel",
    "scipy.special.cython_special",
]

# --- web/db runtime submodules PyInstaller's static graph misses ------------------------------
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("sqlalchemy")
hiddenimports += collect_submodules("alembic")

# --- tiktoken encoding plugin: a namespace-package plugin, invisible to the module graph ------
hiddenimports += ["tiktoken_ext", "tiktoken_ext.openai_public"]

# --- our own package data --------------------------------------------------------------------
# alembic scripts as DATA (not analyzed): ScriptDirectory imports versions/*.py from disk (1.4)
# vendored tiktoken cl100k_base cache so first use is offline
datas += [
    (str(HERE / "alembic"), "alembic"),
    (str(HERE / "vendor" / "tiktoken_cache"), "vendor/tiktoken_cache"),
]

a = Analysis(
    [str(HERE / "shell_spike.py")],
    pathex=[str(HERE)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="shell_spike",
    console=False,  # --windowed: sys.stdout/stderr are None at runtime (handled in shell_spike)
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,  # UPX trips antivirus heuristics; one-folder is already the quiet path
    name="shell_spike",
)
