"""Write packaging/version.iss with the single-source __version__ so the installer, pyproject, the
window title, /health and /about all agree (SPEC-INTERFACES 1.5). Run before compiling the .iss.

    uv run python packaging/gen_version_iss.py
"""

from __future__ import annotations

from pathlib import Path

from kestrel import __version__

out = Path(__file__).resolve().parent / "version.iss"
out.write_text(f'#define AppVersion "{__version__}"\n', encoding="utf-8")
print(f"wrote {out} -> AppVersion {__version__}")
