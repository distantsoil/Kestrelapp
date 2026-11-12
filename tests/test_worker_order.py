"""D-WORKER-1: in each worker cycle, what the person asked for runs before background work.

Story builds and then letter drafts come after scoring and before the background lane and the
scheduled jobs (likely questions, titles, company summaries, research). Read from the loop's own
code, in the order the calls appear, so a later edit that moves them fails here."""

from __future__ import annotations

import ast
import inspect
import textwrap

from kestrel.web import app as app_mod


def _call_order() -> list[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(app_mod._worker_loop)))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    calls.sort(key=lambda n: (n.lineno, n.col_offset))
    names = []
    for c in calls:
        f = c.func
        names.append(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return names


def _first(names: list[str], name: str) -> int:
    assert name in names, f"{name} is no longer called in _worker_loop"
    return names.index(name)


def test_stories_then_letters_run_before_the_background_lane_and_scheduled_jobs():
    names = _call_order()
    stories = _first(names, "drain_story_builds")
    letters = _first(names, "run_letter_drafts")
    assert stories < letters
    tier2 = _first(names, "drain_tier")  # scoring the person's new roles stays first
    assert tier2 < stories
    for later in ("release_lane", "drain_interview_questions", "drain_title_suggestions",
                  "drain_org_summaries", "drain_research"):
        assert letters < _first(names, later), f"{later} now runs before letter drafts"


def test_stories_and_letters_each_run_once_a_cycle():
    names = _call_order()
    assert names.count("drain_story_builds") == 1
    assert names.count("run_letter_drafts") == 1
