"""Negotiation and offer analysis (SPEC-MODULES.md 4).

  - valuation.py — pure first-year / four-year value of an offer, shared by the two below.
  - benchmark.py — grounds an offer in the operator's OWN comp corpus (populated since Phase 3), the
    posting's stated band, real open competing offers, and their own history. Every number
    carries an explicit sample size; under five observations it says "insufficient data" rather
    than presenting a precise-looking figure from three points (constraint 6).
  - scripts.py — P11 negotiation scripts. The walk-away number is the operator's and the model never
    proposes, questions, or adjusts it; every justification must rest on a supplied proof point
    or a supplied market data point with its sample size, and a script that rests on neither is
    FLAGGED in code; competing-offer scripts are only generated when a real competing offer exists
    (no bluffs). Drafts only; the system never sends.
  - compare.py — the offer comparison matrix, presented as ANALYSIS to make the trade-offs legible,
    never as a recommendation to accept.
"""

from __future__ import annotations
