"""Gold-set regeneration (SPEC.md 8.6, SPEC-MODULES.md 5.5).

`kestrel bench` ships two synthetic placeholder files, `evals/goldset.employment.jsonl` and
`evals/goldset.consulting.jsonl`, each a clearly-flagged (`"placeholder": true`) stand-in gold set.
**The real gold set is the operator's own accumulated labels.** This module turns those labels into
the same JSONL shape the bench harness reads, so model/prompt-version comparisons get progressively
more honest as the operator reviews more opportunities -- and it NEVER overwrites the shipped
placeholder file, writing a `.regenerated.jsonl` sibling instead.

Labels come from `kestrel.learning.capture.labeled_examples`, the same unification of `label` and
`verdict_override` rows (most-recent-judgment-wins) that calibration and the reranker already
consume, so "the operator's real judgment for this opportunity" has exactly one definition across
the whole learning loop.

Hard rule 6 (CLAUDE.md): a sample under 5 labels is still written -- every real label is signal
worth keeping -- but the thinness is logged, never presented as if it were a solid gold set.
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel import paths
from kestrel.config import get_settings
from kestrel.db.enums import TRACKS
from kestrel.db.models import Opportunity
from kestrel.learning.capture import LabeledExample, labeled_examples
from kestrel.pipeline.scoring import verdict_from_score

log = structlog.get_logger()

# Below this many labels, the regenerated set is real but not yet a reliable comparison baseline
# (constraint 6: sample sizes under five are always stated as insufficient, never solid).
MIN_LABELS_FOR_SOLID_SAMPLE = 5

_REMOTE_TYPES = ("remote", "hybrid", "onsite", "unknown")


def _remote_type(value: str | None) -> str:
    return value if value in _REMOTE_TYPES else "unknown"


def _gold_verdict(track: str, ex: LabeledExample) -> str:
    """The operator's own verdict when they gave one; otherwise the score-threshold verdict the
    system itself would derive (never a hallucinated or defaulted-to-'consider' guess)."""
    if ex.verdict:
        return ex.verdict
    settings = get_settings()
    return verdict_from_score(ex.operator_score, track, settings)


def _gold_entry(track: str, opp: Opportunity, ex: LabeledExample) -> dict:
    return {
        "track": track,
        "title": opp.title or "",
        "org": opp.org_name or "",
        "description_md": opp.description_md or "",
        "location": opp.location_raw or "",
        "comp_min": opp.comp_min,
        "comp_max": opp.comp_max,
        "remote_type": _remote_type(opp.remote_type),
        "gold_verdict": _gold_verdict(track, ex),
        "gold_score": round(float(ex.operator_score), 2),
        "placeholder": False,
    }


async def regenerate_goldset(
    session: AsyncSession, track: str, *, out_path: Path | None = None
) -> Path:
    """Read the operator's real labels for `track`, join each to its Opportunity, and write one
    gold entry per opportunity to `evals/goldset.<track>.regenerated.jsonl` (or `out_path`, for
    tests). Returns the path written. Never touches the shipped `goldset.<track>.jsonl` file."""
    if track not in TRACKS:
        raise ValueError(f"unknown track {track!r}; expected one of {TRACKS}")

    examples = await labeled_examples(session, track)
    rows: list[dict] = []
    for ex in examples:
        opp = await session.get(Opportunity, ex.opportunity_id)
        if opp is None:  # the opportunity was deleted since the label was recorded
            continue
        rows.append(_gold_entry(track, opp, ex))

    dest = out_path or (paths.evals_dir() / f"goldset.{track}.regenerated.jsonl")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")

    if len(rows) < MIN_LABELS_FOR_SOLID_SAMPLE:
        log.warning(
            "goldset.regenerate.insufficient_sample",
            track=track,
            n_labels=len(rows),
            path=str(dest),
            note="fewer than 5 labels; written but not a solid comparison baseline yet",
        )
    else:
        log.info("goldset.regenerate.written", track=track, n_labels=len(rows), path=str(dest))

    return dest
