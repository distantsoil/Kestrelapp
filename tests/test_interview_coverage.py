"""Theme coverage: a deliberately missing behavioral theme is reported as a gap, and the total is
stated against the 8-12 target (constraint 6). Fully offline: reads canonical stories only."""

from __future__ import annotations

from kestrel.db.models import Story
from kestrel.interview.coverage import COMMON_THEMES, theme_coverage


async def test_missing_theme_is_reported_as_a_gap(sm):
    async with sm() as s:
        # Cover three themes on purpose; leave "failure" (and others) uncovered.
        s.add(Story(title="Turnaround", themes=["turnaround"], is_canonical=True))
        s.add(Story(title="Conflict", themes=["conflict", "difficult_stakeholder"],
                    is_canonical=True))
        s.add(Story(title="Team", themes=["team_building"], is_canonical=True))
        # A non-canonical story must not count toward coverage.
        s.add(Story(title="Draft failure", themes=["failure"], is_canonical=False))
        await s.commit()

    async with sm() as s:
        report = await theme_coverage(s)

    assert "failure" in report.gaps
    assert "turnaround" in report.covered_themes
    assert "conflict" in report.covered_themes
    assert "team_building" in report.covered_themes
    # The draft story's "failure" theme does not lift the gap.
    assert "failure" not in report.covered_themes
    assert report.total_canonical == 3
    assert set(report.gaps).issubset(set(COMMON_THEMES))
    assert "Gaps:" in report.note
    assert "below the target" in report.note  # 3 < 8


async def test_no_stories_reports_every_theme_as_a_gap(sm):
    async with sm() as s:
        report = await theme_coverage(s)
    assert report.total_canonical == 0
    assert set(report.gaps) == set(COMMON_THEMES)
