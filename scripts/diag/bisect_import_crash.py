"""Find which earlier imports make `import jobspy` crash natively in PyInstaller's dependency scan.

PyInstaller's find_binary_dependencies imports every collected package into ONE child process;
on Windows that child died with 0x80000003 at `jobspy` (2026-10-15), while each package imported
alone is fine. This replays the scan's import order in fresh processes and narrows it (delta
debugging) to the smallest set of earlier imports that still crashes. Diagnostic only: it imports
modules and changes nothing.

    uv run python scripts/diag/bisect_import_crash.py
"""

from __future__ import annotations

import subprocess
import sys

TARGET = "jobspy"
# The scan's import order before `jobspy`, from the failing build's own error message.
PREFIX = """
zipfile zipfile._path urllib importlib importlib.metadata importlib.resources logging email
xmlrpc xml.parsers xml http xml.sax ctypes multiprocessing.dummy _distutils_hack setuptools
asyncio concurrent.futures concurrent setuptools._distutils.compat unittest jaraco.functools
setuptools._distutils setuptools._vendor.tomli tomllib setuptools.compat more_itertools json
setuptools._distutils.command html pydoc_data packaging setuptools._vendor.wheel
setuptools._vendor.jaraco.text jaraco.context setuptools._vendor.backports.tarfile
setuptools._vendor.backports.tarfile.compat setuptools._vendor.backports
setuptools.config._validate_pyproject packaging.licenses setuptools._vendor.packaging
setuptools.config setuptools.command kestrel.ingest httpx httpcore httpcore._backends anyio
exceptiongroup anyio.streams anyio.abc anyio._core _pytest _pytest._io colorama pygments.filters
pygments.formatters pygments.styles pygments numpy.typing numpy numpy.testing numpy._utils
numpy.testing._private numpy.matrixlib numpy.ma numpy.random numpy.polynomial numpy.fft
numpy.linalg numpy.lib ctypes.macholib numpy.compat numpy.core yaml numpy.array_api
numpy._typing xml.etree PIL _pytest.mark _pytest.assertion _pytest.config iniconfig _pytest._py
_pytest._code pluggy anyio._backends idna httpcore._sync h11 certifi httpcore._async
rich._unicode_data rich attr markdown_it markdown_it.rules_core markdown_it.rules_inline mdurl
markdown_it.common markdown_it.rules_block markdown_it.presets markdown_it.helpers
pygments.lexers click httpx._transports structlog greenlet dotenv pydantic_settings
pydantic_settings.sources typing_inspection pydantic.v1 email_validator dns dns.rdtypes
dns.rdtypes.ANY dns.rdtypes.IN dns.rdtypes.CH dns.quic zoneinfo tzdata annotated_types
pydantic.deprecated pydantic.plugin pydantic._internal pydantic_core
pydantic_settings.sources.providers pydantic pydantic.experimental bs4 soupsieve
charset_normalizer bs4.builder lxml lxml.isoschematron lxml.html kestrel.ingest.signal_sources
kestrel.web kestrel.db starlette jinja2 markupsafe starlette.middleware sqlite3 kestrel.services
kestrel.rules tiktoken regex markdownify tokenizers tokenizers.implementations
tokenizers.trainers tokenizers.processors tokenizers.pre_tokenizers tokenizers.normalizers
tokenizers.models tokenizers.decoders onnxruntime onnxruntime.transformers onnxruntime.capi
kestrel.ingest.adapters pandas dateutil dateutil.parser dateutil.tz dateutil.zoneinfo
pandas.core.dtypes pandas.core pandas.core.ops pandas.util.version pandas.util pandas.core.util
pandas.core.computation pandas.core.strings pandas.core.groupby pandas.core.window
pandas.core.tools pandas.core.arrays.arrow pandas.core.indexers pandas.core.array_algos
pandas.compat.numpy pandas.api.extensions pandas.core.reshape scipy.stats scipy._lib
scipy.special scipy.integrate scipy.sparse scipy.sparse.linalg scipy.linalg scipy.fft
scipy.optimize scipy.spatial scipy.constants scipy.interpolate scipy.ndimage
scipy.sparse.csgraph cffi pycparser scipy pandas.core.indexes pandas.io.formats pandas.io.excel
pandas.io.parsers pandas.api.types pytz pandas.core.arrays pandas.core.arrays.sparse
pandas.core.methods pandas.core.internals pandas.io.clipboard pandas.io.json xml.dom fsspec
urllib3 urllib3.contrib cryptography.hazmat cryptography.hazmat.backends.openssl
cryptography.hazmat.backends cryptography.hazmat.primitives.ciphers
cryptography.hazmat.bindings.openssl cryptography.hazmat.bindings
cryptography.hazmat.primitives.asymmetric cryptography.hazmat.primitives.serialization
cryptography.hazmat.primitives cryptography.x509 cryptography urllib3.http2 urllib3.util
requests fsspec.implementations tqdm pandas.core.interchange pandas._libs.window
pandas._libs.tslibs pandas.io.sas pandas._libs pandas._testing pandas.plotting pandas.io
pandas.errors pandas.arrays pandas.api pandas.api.typing pandas.api.interchange
pandas.api.indexers pandas.tseries pandas._config pandas.compat""".split()

_CHILD = f"""
import importlib, sys, warnings
warnings.simplefilter("ignore")
for name in sys.argv[1:]:
    try:
        importlib.import_module(name)
    except Exception:
        pass  # a module that fails to import is not the native crash we are looking for
importlib.import_module({TARGET!r})
print("ok")
"""


def crashes(mods: list[str]) -> bool:
    r = subprocess.run([sys.executable, "-c", _CHILD, *mods], capture_output=True, text=True,
                       timeout=600)
    return r.returncode != 0 and "Traceback" not in r.stderr  # native death, not a Python error


def main() -> int:
    print(f"replaying {len(PREFIX)} imports, then {TARGET} ...", flush=True)
    if not crashes(PREFIX):
        print("RESULT: no crash outside PyInstaller. The scan's own process setup is the cause, "
              "not the import combination.")
        return 0
    mods, n = list(PREFIX), 2
    while len(mods) >= 2:
        size = max(1, len(mods) // n)
        chunks = [mods[i:i + size] for i in range(0, len(mods), size)]
        for i in range(len(chunks)):
            rest = [m for j, c in enumerate(chunks) if j != i for m in c]
            if crashes(rest):
                mods, n = rest, max(n - 1, 2)
                print(f"  still crashes with {len(mods)} imports", flush=True)
                break
        else:
            if n >= len(mods):
                break
            n = min(len(mods), n * 2)
    print("RESULT: these imports, in this order, then `import jobspy`, crash:")
    for m in mods:
        print("   ", m)
    return 0


if __name__ == "__main__":
    sys.exit(main())
