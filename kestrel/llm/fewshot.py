"""Few-shot example selection (SPEC-MODULES.md 5.3): same track, ranked by embedding cosine to the
current description, and DELIBERATELY DIVERSIFIED across the verdict range so the model is not shown
three positives and taught to inflate. Gated at settings.learning.min_labels_for_few_shot; below
that, zero-shot (return []). The caller filters to the same track and records the chosen ids in
evaluation.few_shot_ids."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from kestrel.llm.embeddings import cosine


@dataclass
class FewShotExample:
    id: int
    embedding: list[float] | np.ndarray
    verdict: str
    holistic: float
    summary: str = ""
    rationale: str = ""


def select_few_shot(
    query_embedding: list[float] | np.ndarray,
    examples: list[FewShotExample],
    *,
    k: int,
    min_labels: int,
) -> list[FewShotExample]:
    """Return up to k examples, diversified across verdicts. Empty (zero-shot) below min_labels."""
    if len(examples) < min_labels or k <= 0:
        return []

    scored = [(cosine(query_embedding, ex.embedding), ex) for ex in examples]

    # Bucket by verdict; sort each bucket by similarity descending.
    buckets: dict[str, list[tuple[float, FewShotExample]]] = defaultdict(list)
    for sim, ex in scored:
        buckets[ex.verdict].append((sim, ex))
    for b in buckets.values():
        b.sort(key=lambda t: t[0], reverse=True)

    # Order verdict buckets by their best similarity, then round-robin so the selection spreads
    # across verdicts rather than stacking the k most-similar (which are often all one verdict).
    order = sorted(buckets.keys(), key=lambda v: buckets[v][0][0], reverse=True)
    cursor = {v: 0 for v in order}
    selected: list[FewShotExample] = []
    while len(selected) < k and any(cursor[v] < len(buckets[v]) for v in order):
        for v in order:
            if cursor[v] < len(buckets[v]):
                selected.append(buckets[v][cursor[v]][1])
                cursor[v] += 1
                if len(selected) >= k:
                    break
    return selected
