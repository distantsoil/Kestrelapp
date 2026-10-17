"""Intelligence query, aggregation, and presentation (SPEC-MODULES.md 6). The comp corpus and the
ghost score are CAPTURED at ingestion (Phase 3) as pure arithmetic; this package is the layer that
USES that accumulated data:

  - comp.py     — query the comp corpus by (title, geo, size) for median/p25/p75 and, always, the
    sample size; under five observations it reports insufficient rather than a chart from three
    points (constraint 6).
  - ghost.py    — the cross-opportunity aggregate signals the per-opportunity scorer cannot see (an
    org running 20+ simultaneous generic postings; a requisition id reappearing after being marked
    closed), folded into ghost_score, plus the badge presentation.
  - liveness.py — re-fetch shortlisted/pursued URLs on a cadence; a 404/410, a "position filled"
    page, or an ATS that no longer returns the id marks the opportunity closed and notifies.
"""

from __future__ import annotations
