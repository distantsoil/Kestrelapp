"""Which postings from a found company board are worth scoring now (D-SRC-8). A company board
brings in every opening at that company, so a title-only filter would skip many roles the
operator would have applied for. A posting is KEPT when its level is unknown or at or above the
operator's floor, AND its field words overlap the field words of their target titles (with
their phrasings) or of at least MIN_PURSUITS of their own pursuits. Everything else is not
rejected (hard rule 4): it goes to the background lane (kestrel.pipeline.lane) and is scored when
Kestrel is idle. Only boards found in a search use this; hand-added boards and searches do not.

Pure decision in `keep`; `build_context` reads the learned words from the database once per
run (never cached globally, so a profile edit or a new pursuit applies on the next run)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Opportunity, Pursuit
from kestrel.rules import helpers, titles

MIN_PURSUITS = 2  # a field word seen in this many of the operator's pursuits counts as theirs
_STOPWORDS = frozenset({
    "of", "and", "the", "for", "to", "in", "a", "&", "global", "onsite", "remote", "role",
    "not", "stated",
})


@dataclass(frozen=True)
class FilterContext:
    floor_rank: int  # -1 when no floor is set: the level test always passes
    tokens: frozenset[str]  # field words the operator cares about

    @property
    def can_judge(self) -> bool:
        """With no target titles and no pursuits there is nothing to compare against."""
        return bool(self.tokens)


def field_tokens(title: str | None) -> frozenset[str]:
    """The words of a title that name its field: canonical tokens minus level words, stop words,
    digits and single characters."""
    out = set()
    for tok in titles.normalize(title):
        if tok in titles._LEVEL_TOKENS or tok in _STOPWORDS:
            continue
        if len(tok) < 2 or tok.isdigit():
            continue
        out.add(tok)
    return frozenset(out)


def profile_tokens(target_titles: Any) -> frozenset[str]:
    """Field words of each target title and of its phrasings (IT / Technology, and so on)."""
    out: set[str] = set()
    for t in target_titles or []:
        if not isinstance(t, str) or not t.strip():
            continue
        out |= field_tokens(t)
        for p in titles.phrasings(t):
            out |= field_tokens(p)
    return frozenset(out)


def keep(title: str | None, ctx: FilterContext) -> bool:
    """True when the posting should be scored in the normal flow. Uncertainty keeps (hard rule
    4): a blank title, an unknown level, a title with no field words, or nothing to compare to."""
    if not isinstance(title, str) or not title.strip():
        return True
    rank = helpers.seniority_rank(title)
    if rank >= 0 and ctx.floor_rank >= 0 and rank < ctx.floor_rank:
        return False
    if not ctx.can_judge:
        return True
    words = field_tokens(title)
    if not words:
        return True
    return bool(words & ctx.tokens)


async def learned_tokens(
    session: AsyncSession, track: str, *, min_pursuits: int = MIN_PURSUITS
) -> frozenset[str]:
    """Field words found in the titles of at least `min_pursuits` of the operator's pursuits."""
    rows = (
        await session.execute(
            select(Opportunity.title)
            .join(Pursuit, Pursuit.opportunity_id == Opportunity.id)
            .where(Pursuit.track == track)
        )
    ).scalars().all()
    seen: Counter[str] = Counter()
    for t in rows:
        seen.update(field_tokens(t))
    return frozenset(w for w, n in seen.items() if n >= min_pursuits)


async def build_context(session: AsyncSession, track: str, profile: Any) -> FilterContext:
    """The filter inputs for one track, read now: the profile's floor and target titles plus the
    words learned from the operator's pursuits."""
    terms = getattr(profile, "employment_terms", None) if profile is not None else None
    floor = getattr(terms, "seniority_floor", None) if terms is not None else None
    targets = getattr(terms, "target_titles", None) if terms is not None else None
    floor_rank = helpers.seniority_rank(floor) if isinstance(floor, str) and floor.strip() else -1
    tokens = profile_tokens(targets) | await learned_tokens(session, track)
    return FilterContext(floor_rank=floor_rank, tokens=tokens)
