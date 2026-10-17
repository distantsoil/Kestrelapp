"""D-PKG-2: on Windows, JobSpy's Go TLS library must load before onnxruntime, scipy.stats and
cryptography, or the process dies with 0x80000003 (measured 2026-10-15). kestrel/__init__.py
preloads it; these tests pin that, and the Windows CI job proves the real combination."""

from __future__ import annotations

import subprocess
import sys

import pytest


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          timeout=300)


def test_importing_kestrel_on_windows_loads_tls_client_first():
    """Simulated platform: the preload is attempted at package import, before any other native
    runtime. (Off Windows, faking the platform makes tls_client reach for its Windows DLL and
    fail, which the preload tolerates; so the test records the attempt and its order.)"""
    r = _run(
        "import sys\n"
        "seen = []\n"
        "class Spy:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name in ('tls_client', 'onnxruntime', 'scipy', 'cryptography'):\n"
        "            seen.append(name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, Spy())\n"
        "sys.platform = 'win32'\n"
        "import kestrel\n"
        "print(seen)\n"
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "['tls_client']"  # attempted, and nothing native before it


def test_no_preload_off_windows():
    r = _run("import sys; sys.platform = 'linux'; import kestrel; "
             "print('tls_client' in sys.modules)")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "False"


@pytest.mark.skipif(sys.platform != "win32", reason="the crash is Windows-only")
def test_the_crashing_combination_survives_once_kestrel_is_imported_first():
    """The bisected combination (scripts/diag/bisect_import_crash.py) that killed the app."""
    r = _run("import kestrel, onnxruntime.capi, scipy.stats, cryptography.x509; import jobspy; "
             "print('ok')")
    assert r.returncode == 0 and r.stdout.strip() == "ok", (r.returncode, r.stderr[-2000:])
