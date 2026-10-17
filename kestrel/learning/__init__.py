"""The learning loop (SPEC-MODULES.md 5): capture, weight recalibration, the learned reranker,
active-learning queue, and outcome-driven calibration. All machinery is built up front; the models
it trains come online as the operator's labels accumulate."""

from __future__ import annotations

# Native thread-pool pinning for HGBR/PCA (AFTERACTION 3.8) lives in kestrel/__init__.py, not here:
# OPENBLAS/MKL are read at the first numpy import, which happens outside this package, so pinning
# here would be too late. See kestrel._pin_native_thread_pools.
