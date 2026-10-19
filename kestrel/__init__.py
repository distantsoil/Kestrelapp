"""Kestrel — a local-LLM career operations engine."""

from __future__ import annotations

import os
import sys

# Single source of truth for the version (SPEC-INTERFACES.md 1.4): pyproject reads this via
# hatch, and it feeds /health, the window title, and the About page.
__version__ = "0.6.0"


def _pin_native_thread_pools() -> None:
    """Cap OpenMP/BLAS thread fan-out for HGBR and PCA in the frozen app (AFTERACTION 3.8).

    This lives in the ROOT package __init__ on purpose. These variables are read once, when the
    native runtime loads: OMP at the first sklearn import, and OPENBLAS/MKL at the first numpy
    import. numpy is imported at module level well outside kestrel.learning (kestrel/llm/
    embeddings.py, kestrel/intelligence/search.py, kestrel/interview/stories.py), so pinning inside
    kestrel/learning/__init__.py only worked when import order happened to reach learning first.
    No kestrel submodule can be imported without executing this module, so this is the one place
    that is ordering-proof.

    Performance guard only, not the loky relaunch bug (already avoided via n_jobs=1). Two limits:
    only when frozen (unfrozen, the operator's cores should be used, and pinning would slow every
    dev run and every test), and never overriding a value the operator set.
    """
    if not getattr(sys, "frozen", False):
        return
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(var, "1")


_pin_native_thread_pools()


def _preload_tls_client() -> None:
    """Load JobSpy's Go TLS library before any other native runtime, on Windows (D-PKG-2).

    Measured on Windows 11 (2026-10-15): with onnxruntime, scipy.stats and cryptography already
    loaded, loading tls_client's Go DLL kills the process with 0x80000003, in the running app (a
    JobSpy scrape after an in-process embedding) and in PyInstaller's dependency scan alike.
    Loaded FIRST, the same four coexist. JobSpy imports it lazily, so nothing else guarantees the
    order; this module runs before any kestrel submodule, which makes it the one place that does.
    Windows only (no crash elsewhere), and best effort: a missing or broken tls_client only
    disables JobSpy sources, which already report their own import errors.
    """
    if sys.platform != "win32":
        return
    try:
        import tls_client  # noqa: F401
    except (ImportError, OSError):  # not installed, or its DLL will not load: JobSpy says so
        pass


_preload_tls_client()
