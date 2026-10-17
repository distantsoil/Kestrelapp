"""The document engine (SPEC-MODULES.md 1). Parametric documents for both tracks (resume and
capability statement): sections, slots, variants, layouts, render configs. THE invariant: the
renderer emits doc_variant.text byte-for-byte; no model output ever reaches a PDF (hard rule 2)."""

from __future__ import annotations
