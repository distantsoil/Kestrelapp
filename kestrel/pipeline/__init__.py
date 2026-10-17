"""The tiered pipeline (SPEC.md 6): Tier 0 deterministic rules, Tier 1 fast triage, Tier 2 deep
evaluation, Tier 3 escalation, and the tier-draining worker that runs them. Both tracks."""

from __future__ import annotations
