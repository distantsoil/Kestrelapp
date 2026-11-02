"""Kestrel — a local-LLM career operations engine."""

from __future__ import annotations

import os
import sys

# Single source of truth for the version (SPEC-INTERFACES.md 1.4): pyproject reads this via
# hatch, and it feeds /health, the window title, and the About page.
__version__ = "0.7.3"


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

# D-PKG-2 (amended): this module used to preload JobSpy's tls_client Go DLL on Windows, because
# loading it after onnxruntime, scipy.stats and cryptography killed the process (0x80000003).
# JobSpy 1.3.0 dropped tls_client for curl_cffi (libcurl-impersonate, a C extension, no Go
# runtime). Measured 2026-10-18: the same combination loads cleanly in either order, so no
# preload is warranted and none is kept. tests/test_native_load_order.py guards that.
