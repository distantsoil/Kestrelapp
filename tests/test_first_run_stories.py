"""The first run builds interview stories once (issue 176, D-STORY-700 to D-STORY-703).

INVARIANT: nothing is sent to an online model without the confirmed dialog. With Tier 2 online the
first run queues no story build and the notice never promises one. Also: at most one automatic
build per install, the flag lives in the database, and a run with no deep read does not spend it."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from kestrel.config import get_settings
from kestrel.db.models import Evaluation, Opportunity, Setting, Task
from kestrel.interview.first_run import queue_first_run_stories
from kestrel.services import onboarding as ob


@pytest.fixture
def migrated(_isolate_data_home):
    from kestrel.db import migrate
    from kestrel.db.engine import reset_engine

    asyncio.run(reset_engine())
    migrate.run_migrations()
    yield
    asyncio.run(reset_engine())


def _run(fn):
    from kestrel.db.engine import get_sessionmaker

    async def go():
        async with get_sessionmaker()() as s:
            out = await fn(s)
            await s.commit()
            return out

    return asyncio.run(go())


def _online_settings():
    st = get_settings().model_copy(deep=True)
    st.confidential_mode = False
    st.llm.tiers["tier2"] = st.llm.tiers["tier2"].model_copy(
        update={"backend": "frontier", "model": "vendor/model-a"})
    hm = st.llm.hosted_mode
    hm.enabled = True
    hm.acknowledged_at = "2026-10-01T00:00:00Z"
    hm.acknowledged_terms = hm.disclosure_terms()
    return st


def _go_online(monkeypatch):
    from kestrel.services import setting_overrides

    async def load(session, **_kw):
        return _online_settings()

    monkeypatch.setattr(setting_overrides, "load_settings_with_db", load)


async def _arm(s):
    assert await ob.mark_first_run_break(s)


async def _add_deep_read(s, track="employment", tier=2):
    opp = Opportunity(track="employment", opp_type="job_posting", title="Planner",
                      org_name="Example Co", url="https://example.test/1")
    s.add(opp)
    await s.flush()
    s.add(Evaluation(opportunity_id=opp.id, tier=tier, track=track, blocks={}))


async def _builds(s):
    return (await s.execute(
        select(func.count()).select_from(Task).where(Task.type == "build_stories"))).scalar_one()


def _queue(produced=True):
    from kestrel.db.engine import get_sessionmaker

    return asyncio.run(queue_first_run_stories(get_sessionmaker(), produced_tier2=produced))


def test_one_build_is_queued_after_the_first_deep_reads_and_never_again(migrated):
    _run(_arm)
    _run(_add_deep_read)
    assert _queue() is True
    assert _run(_builds) == 1
    assert _run(ob.first_run_stories_due) is False
    # The task finishes; a later run asks nothing more.
    _run(lambda s: s.execute(Task.__table__.update().values(status="done")))
    assert _queue() is False
    assert _run(_builds) == 1


def test_a_run_without_a_deep_read_leaves_the_build_due(migrated):
    _run(_arm)
    assert _queue(produced=False) is False
    assert _queue(produced=True) is False  # no tier 2 employment read exists yet
    _run(lambda s: _add_deep_read(s, track="consulting"))
    assert _queue() is False
    _run(lambda s: _add_deep_read(s, tier=1))
    assert _queue() is False
    assert _run(ob.first_run_stories_due) is True
    assert _run(_builds) == 0
    _run(_add_deep_read)
    assert _queue() is True and _run(_builds) == 1


def test_online_tier_two_queues_nothing_and_spends_the_flag(migrated, monkeypatch):
    """The consent invariant: no confirmed dialog, no build, and no later retry behind the
    person's back."""
    _run(_arm)
    _run(_add_deep_read)
    _go_online(monkeypatch)
    assert _queue() is False
    assert _run(_builds) == 0
    assert _run(ob.first_run_stories_due) is False
    assert _queue() is False
    assert _run(_builds) == 0


def test_the_flag_survives_a_restart_and_a_second_arming_never_happens(migrated):
    _run(_arm)
    # A new session (a restarted app) still finds it due, and closing the notice keeps it due.
    assert _run(ob.first_run_stories_due) is True
    _run(ob.dismiss_first_run_break)
    assert _run(ob.first_run_stories_due) is True
    assert _run(ob.first_run_break_pending) is False
    _run(_add_deep_read)
    assert _queue() is True
    assert _run(ob.mark_first_run_break) is False  # restarting onboarding never re-arms it
    assert _run(ob.first_run_stories_due) is False


def test_a_kill_before_the_commit_leaves_no_build_and_a_due_flag(migrated, monkeypatch):
    """The task row and the flag share one transaction: a crash at the commit loses both, and the
    next run queues exactly one."""
    from kestrel.db.engine import get_sessionmaker

    _run(_arm)
    _run(_add_deep_read)
    real = ob.finish_first_run_stories

    async def die(s):
        await real(s)
        raise RuntimeError("killed before the commit")

    monkeypatch.setattr(ob, "finish_first_run_stories", die)
    with pytest.raises(RuntimeError):
        asyncio.run(queue_first_run_stories(get_sessionmaker(), produced_tier2=True))
    assert _run(_builds) == 0 and _run(ob.first_run_stories_due) is True
    monkeypatch.setattr(ob, "finish_first_run_stories", real)
    assert _queue() is True and _run(_builds) == 1


def test_a_build_that_ran_after_a_crash_is_never_queued_a_second_time(migrated):
    """The review's scenario: whatever happened, once a build exists the flag is spent with it,
    so a finished build is not followed by another."""
    _run(_arm)
    _run(_add_deep_read)
    assert _queue() is True
    _run(lambda s: s.execute(Task.__table__.update().values(status="done")))
    assert _run(ob.first_run_stories_due) is False
    assert _queue() is False
    assert _run(_builds) == 1


def test_a_restart_of_onboarding_leaves_no_build_due(migrated):
    _run(ob.restart)
    assert _run(ob.first_run_stories_due) is False
    _run(_add_deep_read)
    assert _queue() is False
    assert _run(_builds) == 0


def test_an_install_from_before_the_flag_existed_has_none(migrated):
    _run(lambda s: s.merge(Setting(key=ob.BREAK_KEY, value={"pending": True})))
    assert _run(ob.first_run_stories_due) is False


def _today_html(first_stories: bool) -> str:
    from kestrel.web.deps import templates

    return templates().env.get_template("partials/first_run_break.html").render(
        first_stories=first_stories, tour=True, static_url=lambda p: "/static/" + p)


def test_step_four_and_the_note_clause_show_only_when_the_build_is_coming(migrated, monkeypatch):
    local = _today_html(True)
    assert "Drafting interview stories from your best matches" in local
    assert "your stories appear in Interview prep when they are ready" in local
    three = _today_html(False)
    assert "Drafting interview stories" not in three and "stories appear" not in three
    assert three.count('class="sf-break__n"') == 3 and local.count('class="sf-break__n"') == 4

    # The page asks the same helper the queueing uses.
    _run(_arm)
    assert _run(lambda s: ob.first_run_stories_expected(s, get_settings())) is True
    assert _run(lambda s: ob.first_run_stories_expected(s, _online_settings())) is False
    _run(_add_deep_read)
    _go_online(monkeypatch)
    _queue()
    assert _run(lambda s: ob.first_run_stories_expected(s, get_settings())) is False  # spent


def test_the_notice_carries_no_inline_script_handler_or_style():
    import re
    from importlib.resources import files

    html = _today_html(True)
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert not re.search(r"\son[a-z]+\s*=|\sstyle\s*=|javascript:", html)
    assert "first-run-break.js" in html
    assert files("kestrel.web").joinpath("static/js/first-run-break.js").is_file()
