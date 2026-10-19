"""D-PKG-2 (amended): JobSpy 1.3.0 uses curl_cffi, a C extension with no Go runtime. The old
tls_client crash (0x80000003 when its Go DLL loaded after onnxruntime, scipy.stats and
cryptography) does not reproduce, so kestrel no longer preloads anything native. These tests pin
that the combination still coexists, in the order that used to kill the process, and that the
package import stays free of native side effects."""

from __future__ import annotations

import subprocess
import sys

_NATIVE_FIRST = (
    "import onnxruntime.capi, scipy.stats, cryptography.x509\n"
    "import kestrel, jobspy\n"
    "from curl_cffi import requests as cr\n"
    "from curl_cffi.curl import Curl\n"
    "Curl()  # loads libcurl-impersonate itself, not just the Python wrapper\n"
    "s = cr.Session(impersonate='chrome')\n"
    "s.close()\n"
    "print('ok')\n"
)


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          timeout=300)


def test_curl_cffi_loads_after_the_other_native_runtimes():
    r = _run(_NATIVE_FIRST)
    assert r.returncode == 0 and r.stdout.strip() == "ok", (r.returncode, r.stderr[-2000:])


def test_curl_cffi_loads_before_the_other_native_runtimes():
    r = _run(
        "import curl_cffi.requests, jobspy\n"
        "from curl_cffi.curl import Curl; Curl()\n"
        "import onnxruntime.capi, scipy.stats, cryptography.x509\n"
        "print('ok')\n"
    )
    assert r.returncode == 0 and r.stdout.strip() == "ok", (r.returncode, r.stderr[-2000:])


def test_importing_kestrel_loads_no_http_native_library():
    r = _run("import sys; sys.platform = 'win32'; import kestrel; "
             "print(sorted(m for m in ('tls_client', 'curl_cffi') if m in sys.modules))")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "[]"
