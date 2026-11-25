"""Which fetched postings are stored (D-SRC-13, replacing the D-SRC-8 background lane). A board
brings in every opening at that company, so a title-only filter would skip many roles the
operator would have applied for. A posting is KEPT when its level is unknown or at or above the
operator's floor, AND its field words overlap the field words of their target titles (with
their phrasings) or of at least MIN_PURSUITS of their own pursuits. A false result skips the
posting before storage and is counted on its scrape run. Searches and all company boards use
the same rule; manual paste bypasses it.

Since D-SCORE-914 (issue #250, the owner's choice) one shared word is enough only when it names a
field. A shared word on the generic list (`title_filter.generic_words` in filters.yml:
operations, systems, business and the like) keeps a posting only together with a second shared
word, or when it is the only field word of one of the operator's own target titles (they search
"Operations Manager": the word is theirs). Nothing already stored is touched.

Pure decision in `keep`; `build_context` reads the learned words from the database and the
generic list from filters.yml once per run (never cached globally, so a profile edit, a new
pursuit or an edited list applies on the next run)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

import structlog
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel import paths
from kestrel.db.models import Opportunity, Pursuit
from kestrel.rules import helpers, titles

log = structlog.get_logger()

FILTERS_FILE = "filters.yml"
GENERIC_SECTION, GENERIC_KEY = "title_filter", "generic_words"
MIN_PURSUITS = 2  # a field word seen in this many of the operator's pursuits counts as theirs
_STOPWORDS = frozenset({
    "of", "and", "the", "for", "to", "in", "a", "&", "global", "onsite", "remote", "role",
    "not", "stated",
})


@dataclass(frozen=True)
class FilterContext:
    floor_rank: int  # -1 when no floor is set: the level test always passes
    tokens: frozenset[str]  # field words the operator cares about
    # Words that do not name a field (D-SCORE-914): one of them shared is not enough alone.
    generic: frozenset[str] = frozenset()
    # Generic words that are all one target title names: for this operator they count alone.
    alone: frozenset[str] = frozenset()

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


class _UnreadableList(ValueError):
    """filters.yml, or its generic-words list, is not in a shape the filter can use."""


def _generic_in(text: str) -> frozenset[str] | None:
    """The generic words a filters.yml text holds, or None when the file simply has no list (one
    seeded before the list existed). Raises _UnreadableList for a file that is not a mapping or
    a list that is not a list of words, so the caller falls back and says so, never guesses."""
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise _UnreadableList("the file is not a mapping")
    if GENERIC_SECTION not in data:
        return None
    section = data[GENERIC_SECTION]
    if not isinstance(section, dict):
        raise _UnreadableList("the title filter section is not a mapping")
    if GENERIC_KEY not in section:
        return None
    entries = section[GENERIC_KEY]
    if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
        raise _UnreadableList("the generic words are not a list of words")
    out: set[str] = set()
    for entry in entries:
        out |= field_tokens(entry)
    return frozenset(out)


def shipped_generic_words() -> frozenset[str]:
    """The generic words of the packaged filters.yml."""
    text = (paths.packaged_config() / FILTERS_FILE).read_text(encoding="utf-8")
    return _generic_in(text) or frozenset()


def generic_words() -> frozenset[str]:
    """The generic words in force: the operator's filters.yml when it has the list (an empty
    list switches the rule off), else the shipped list. A filters.yml seeded before the list
    existed has none and gets the shipped list silently. A file or list that cannot be read is
    never a crash and never a reason to keep nothing or everything: it gets the shipped list
    and a warning in the log. That covers a file that cannot be opened (no permission, a folder
    by that name), one that is not UTF-8 (UTF-16 from a Windows editor, a binary file), an
    empty file, broken YAML and a wrong shape. UTF-8 with a byte order mark is a readable file:
    the YAML reader accepts the mark and the list in it is used."""
    seeded = paths.config_dir() / FILTERS_FILE
    try:
        # `exists`, not `is_file`: a folder or anything else by that name is reported below
        # like any other file that cannot be read, never passed over in silence.
        own = _generic_in(seeded.read_text(encoding="utf-8")) if seeded.exists() else None
    except (OSError, ValueError, yaml.YAMLError) as exc:
        # ValueError holds UnicodeDecodeError and _UnreadableList. Only the kind of error is
        # logged, never the file's text.
        log.warning("title_filter_list_unreadable", error_type=type(exc).__name__)
        own = None
    return own if own is not None else shipped_generic_words()


def alone_tokens(target_titles: Any, generic: frozenset[str]) -> frozenset[str]:
    """Generic words that are the only field word of a target title or of one of its phrasings:
    the operator searches that word itself, so it keeps a posting without a partner."""
    out: set[str] = set()
    for t in target_titles or []:
        if not isinstance(t, str) or not t.strip():
            continue
        for spelling in (t, *titles.phrasings(t)):
            words = field_tokens(spelling)
            if len(words) == 1 and words <= generic:
                out |= words
    return frozenset(out)


def keep(title: str | None, ctx: FilterContext) -> bool:
    """True when the posting should be scored in the normal flow. Uncertainty keeps (hard rule
    4): a blank title, an unknown level, a title with no field words, or nothing to compare to.
    Otherwise it shares a field word with the operator's words; a single shared word that is
    generic is enough only when it is the operator's own (D-SCORE-914)."""
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
    shared = words & ctx.tokens
    if len(shared) != 1:
        return bool(shared)  # none: unrelated. Two or more: related, generic or not.
    return not shared <= ctx.generic or shared <= ctx.alone


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
    """The filter inputs for one track, read now: the profile's floor and target titles, the
    words learned from the operator's pursuits, and the generic words of filters.yml."""
    terms = getattr(profile, "employment_terms", None) if profile is not None else None
    floor = getattr(terms, "seniority_floor", None) if terms is not None else None
    targets = getattr(terms, "target_titles", None) if terms is not None else None
    floor_rank = helpers.seniority_rank(floor) if isinstance(floor, str) and floor.strip() else -1
    tokens = profile_tokens(targets) | await learned_tokens(session, track)
    generic = generic_words()
    return FilterContext(floor_rank=floor_rank, tokens=tokens, generic=generic,
                         alone=alone_tokens(targets, generic))
