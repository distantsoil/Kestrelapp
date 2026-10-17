"""Theme coverage and gaps (SPEC-MODULES 3.1). Gap analysis is the point: knowing you have no good
failure story is more useful than having forty mediocre ones. Coverage reports, for each common
behavioral theme, whether the operator has a strong (canonical) story for it and, first and loudest,
which themes are a gap."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Story

# The common behavioral themes an interview loop draws on (SPEC-MODULES 3.1). This is the coverage
# axis; the P8 theme enum is wider, but coverage is reported against the themes interviewers
# actually probe, so a story tagged only with a niche theme still leaves these gaps visible.
COMMON_THEMES: tuple[str, ...] = (
    "conflict",
    "failure",
    "influence_without_authority",
    "turnaround",
    "prioritization",
    "difficult_stakeholder",
    "technical_judgment",
    "team_building",
)
TARGET_MIN = 8
TARGET_MAX = 12


@dataclass
class ThemeCoverage:
    theme: str
    story_ids: list[int]
    covered: bool


@dataclass
class CoverageReport:
    total_canonical: int
    themes: list[ThemeCoverage]
    gaps: list[str]
    note: str

    @property
    def covered_themes(self) -> list[str]:
        return [t.theme for t in self.themes if t.covered]


async def theme_coverage(session: AsyncSession) -> CoverageReport:
    """Map every common theme to the canonical stories that legitimately answer it, flag the ones
    with no story as gaps, and state the total against the 8-12 target (constraint 6: a small bank
    is labeled as such, not presented as if it were solid coverage)."""
    stories = (
        (await session.execute(select(Story).where(Story.is_canonical.is_(True)))).scalars().all()
    )
    by_theme: dict[str, list[int]] = {t: [] for t in COMMON_THEMES}
    for s in stories:
        for theme in s.themes or []:
            if theme in by_theme:
                by_theme[theme].append(s.id)

    themes = [
        ThemeCoverage(theme=t, story_ids=by_theme[t], covered=bool(by_theme[t]))
        for t in COMMON_THEMES
    ]
    gaps = [t.theme for t in themes if not t.covered]
    total = len(stories)

    if total < TARGET_MIN:
        note = (
            f"{total} canonical stories, below the target of {TARGET_MIN}-{TARGET_MAX}. "
            "Treat coverage as provisional until the bank fills out."
        )
    elif total > TARGET_MAX:
        note = (
            f"{total} canonical stories, above the target of {TARGET_MIN}-{TARGET_MAX}. "
            "Consider retiring the weakest so the strong ones are the ones you reach for."
        )
    else:
        note = f"{total} canonical stories, within the {TARGET_MIN}-{TARGET_MAX} target."
    if gaps:
        note = f"Gaps: {', '.join(gaps)}. " + note

    return CoverageReport(total_canonical=total, themes=themes, gaps=gaps, note=note)
