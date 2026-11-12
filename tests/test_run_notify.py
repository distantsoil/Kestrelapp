"""Issues #110 and #111: what a finished run tells the person.

Invariants: one outcome notification per run, never one per role; at most one "worth a look" alert
per run, naming the role when there is one; a role is not announced twice; a switch turned off
creates no delivery. Each test seeds real rows (a run, roles, evaluations, quarantine rows) and
drives the real notify function, then reads the stored notifications and, for the switches, the
real delivery cycle and the bell."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kestrel.db.base import utcnow_iso
from kestrel.db.models import (
    DocMaster,
    DocSection,
    Evaluation,
    Notification,
    Opportunity,
    Quarantine,
    Run,
    Setting,
    Source,
)
from kestrel.services.delivery import deliver_pending
from kestrel.services.run_notify import notify_run_finished
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web import notifications_view as nv
from kestrel.web import settings_view as sv

# Fixed instants (the code under test only compares them with each other).
RUN1 = ("2026-11-05T10:00:00+00:00", "2026-11-05T10:20:00+00:00")
IN_RUN1 = "2026-11-05T10:05:00+00:00"
RUN2 = ("2026-11-05T11:00:00+00:00", "2026-11-05T11:20:00+00:00")
IN_RUN2 = "2026-11-05T11:05:00+00:00"
RUN3 = ("2026-11-05T12:00:00+00:00", "2026-11-05T12:20:00+00:00")
IN_RUN3 = "2026-11-05T12:05:00+00:00"
RUN4 = ("2026-11-05T13:00:00+00:00", "2026-11-05T13:20:00+00:00")
IN_RUN4 = "2026-11-05T13:05:00+00:00"

# How each fit word is stored: (verdict, score, weighted score, needs_review).
FIT = {
    "strong": ("apply", 4.8, 4.8, False),
    "good": ("apply", 4.2, 4.2, False),
    "your_call": ("apply", 4.8, 2.0, True),
    "not_fit": ("pass", 1.5, 1.5, False),
}


async def _source(sm) -> int:
    async with sm() as s:
        src = Source(track="employment", kind="greenhouse", name="emp", enabled=True)
        s.add(src)
        await s.commit()
        return src.id


async def _run(sm, window: tuple[str, str]) -> int:
    async with sm() as s:
        run = Run(name="pipeline", status="done", started_at=window[0], finished_at=window[1],
                  counts={})
        s.add(run)
        await s.commit()
        return run.id


async def _role(sm, src: int, title: str, org: str | None, first_seen: str,
                fit: str | None = None, scored_at: str | None = None) -> int:
    async with sm() as s:
        opp = Opportunity(
            track="employment", opp_type="job_posting", source_id=src, title=title, org_name=org,
            url=f"https://example.test/{title.replace(' ', '-')}", stage="t2_pass", seen_count=1,
            ghost_score=0.0, first_seen_at=first_seen, version=1,
            description_md="Own reporting for three distribution centers." + " Details." * 30,
        )
        s.add(opp)
        await s.flush()
        if fit:
            verdict, score, weighted, review = FIT[fit]
            s.add(Evaluation(
                opportunity_id=opp.id, tier=2, track="employment", verdict=verdict, score=score,
                weighted_score=weighted, needs_review=review, created_at=scored_at or first_seen,
            ))
        await s.commit()
        return opp.id


async def _score(sm, opp_id: int, fit: str, at: str) -> None:
    verdict, score, weighted, review = FIT[fit]
    async with sm() as s:
        s.add(Evaluation(opportunity_id=opp_id, tier=2, track="employment", verdict=verdict,
                         score=score, weighted_score=weighted, needs_review=review, created_at=at))
        await s.commit()


async def _fail(sm, opp_id: int, at: str) -> None:
    async with sm() as s:
        s.add(Quarantine(opportunity_id=opp_id, tier=2, error="the model could not finish",
                         attempts=3, created_at=at))
        await s.commit()


async def _finish(sm, run_id: int, rated: int = 0) -> None:
    await notify_run_finished(sm, run_id=run_id, rated=rated)


async def _notes(sm, kind: str | None = None) -> list[Notification]:
    async with sm() as s:
        q = select(Notification).order_by(Notification.id)
        if kind:
            q = q.where(Notification.kind == kind)
        return list((await s.execute(q)).scalars())


# ====================================================================== #111 roles worth a look
async def test_no_qualifying_role_raises_no_alert(sm):
    src = await _source(sm)
    run = await _run(sm, RUN1)
    await _role(sm, src, "Warehouse Clerk", "Acme", IN_RUN1, "not_fit")
    await _finish(sm, run, rated=1)
    assert await _notes(sm, "high_score") == []
    assert [n.title for n in await _notes(sm, "run_finished")] == ["Run finished"]


async def test_one_role_is_named_with_its_company_and_links_to_the_role(sm):
    src = await _source(sm)
    run = await _run(sm, RUN1)
    opp = await _role(sm, src, "Operations Lead", "Acme Freight", IN_RUN1, "good")
    await _finish(sm, run, rated=1)
    (note,) = await _notes(sm, "high_score")
    assert note.title == "1 new role worth a look: Operations Lead at Acme Freight"
    assert note.opportunity_id == opp
    assert nv.context_action(note.kind, note.opportunity_id)["href"] == f"/opportunities/{opp}"


async def test_one_role_without_a_company_is_named_by_its_title(sm):
    src = await _source(sm)
    run = await _run(sm, RUN1)
    await _role(sm, src, "Operations Lead", None, IN_RUN1, "good")
    await _finish(sm, run)
    assert (await _notes(sm, "high_score"))[0].title == "1 new role worth a look: Operations Lead"


async def test_several_roles_are_one_alert_with_the_strong_count_and_a_filtered_link(sm):
    src = await _source(sm)
    run = await _run(sm, RUN1)
    for title, fit in (("A", "strong"), ("B", "good"), ("C", "your_call"), ("D", "strong")):
        await _role(sm, src, f"Role {title}", "Acme", IN_RUN1, fit)
    await _role(sm, src, "Role E", "Acme", IN_RUN1, "not_fit")  # never counts
    await _finish(sm, run, rated=5)
    (note,) = await _notes(sm, "high_score")  # one alert, not one per role
    assert note.title == "4 new roles worth a look (2 strong)"
    assert note.opportunity_id is None
    action = nv.context_action(note.kind, note.opportunity_id)
    assert action["href"] == "/opportunities?new_today=1" and action["label"] == "See the new roles"


async def test_no_strong_role_means_no_breakdown(sm):
    src = await _source(sm)
    run = await _run(sm, RUN1)
    await _role(sm, src, "Role A", "Acme", IN_RUN1, "good")
    await _role(sm, src, "Role B", "Acme", IN_RUN1, "your_call")
    await _finish(sm, run)
    assert (await _notes(sm, "high_score"))[0].title == "2 new roles worth a look"


async def test_a_not_a_fit_role_beside_one_good_role_does_not_change_the_count(sm):
    src = await _source(sm)
    run = await _run(sm, RUN1)
    await _role(sm, src, "Role A", "Acme", IN_RUN1, "good")
    await _role(sm, src, "Role B", "Acme", IN_RUN1, "not_fit")
    await _finish(sm, run)
    assert (await _notes(sm, "high_score"))[0].title.startswith("1 new role worth a look: ")


async def test_a_role_rescored_in_a_later_run_is_not_announced_again(sm):
    src = await _source(sm)
    run1 = await _run(sm, RUN1)
    opp = await _role(sm, src, "Operations Lead", "Acme", IN_RUN1, "good")
    await _finish(sm, run1)
    assert len(await _notes(sm, "high_score")) == 1
    run2 = await _run(sm, RUN2)
    await _score(sm, opp, "strong", IN_RUN2)  # Score again lifted it; it is not new to this run
    await _finish(sm, run2)
    assert len(await _notes(sm, "high_score")) == 1


async def test_a_role_first_seen_before_the_run_is_not_new(sm):
    src = await _source(sm)
    run = await _run(sm, RUN2)
    await _role(sm, src, "Old Role", "Acme", IN_RUN1, "strong", scored_at=IN_RUN2)
    await _finish(sm, run)
    assert await _notes(sm, "high_score") == []


async def test_confidential_mode_hides_the_current_employer_in_the_stored_alert(sm):
    src = await _source(sm)
    run = await _run(sm, RUN1)
    async with sm() as s:
        dm = DocMaster(track="employment", name="resume", doc_kind="resume")
        s.add(dm)
        await s.flush()
        s.add(DocSection(doc_master_id=dm.id, section_kind="role", org_label="SecretCo",
                         org_label_redacted="[current employer]"))
        s.add(Setting(key="confidential_mode", value=True))
        await s.commit()
    await _role(sm, src, "Operations Lead", "SecretCo", IN_RUN1, "good")
    await _finish(sm, run)
    (note,) = await _notes(sm, "high_score")
    assert "SecretCo" not in note.title + (note.body or "")
    assert note.title.startswith("1 new role worth a look: Operations Lead at ")


async def _deliver(sm, **events: bool) -> tuple[int, int]:
    async with sm() as s:
        settings = await load_settings_with_db(s)
        settings.notify.enabled = True
        settings.notify.toast = False
        settings.notify.sound = False
        settings.notify.ntfy_topic = None
        settings.notify.webhook_url = None
        settings.notify.quiet_hours = {"enabled": False, "start": "21:00", "end": "07:00"}
        settings.notify.events.update(events)
        result = await deliver_pending(s, settings)
        return result.sent, result.suppressed


async def test_the_switch_is_renamed_and_keeps_its_stored_keys():
    label, blurb, kinds = sv.NOTIFY_KINDS["matches"]
    assert label == "New roles worth a look" and "Your call" in blurb
    assert kinds == ("new_strong_match", "high_score")  # a saved choice still applies


async def test_a_switch_saved_off_before_the_rename_still_silences_the_alert(sm):
    src = await _source(sm)
    run = await _run(sm, RUN1)
    await _role(sm, src, "Role A", "Acme", IN_RUN1, "good")
    await _finish(sm, run)
    sent, suppressed = await _deliver(sm, high_score=False, new_strong_match=False)
    assert (await _notes(sm, "high_score"))[0].delivered_channels == ["suppressed"]
    assert (await _notes(sm, "run_finished"))[0].delivered_channels != ["suppressed"]
    assert suppressed == 1 and sent == 1


@pytest.mark.usefixtures("_isolate_data_home")
def test_the_settings_page_shows_the_new_name():
    from kestrel.db import migrate
    from kestrel.web.app import build_app

    migrate.run_migrations()
    with TestClient(build_app(worker=False)) as c:
        page = c.get("/settings/section/preferences").text
    assert "New roles worth a look" in page and "New strong matches" not in page


# ================================================================== #110 roles needing another try
async def test_one_failed_role_is_one_warning_in_the_attention_group(sm):
    src = await _source(sm)
    run = await _run(sm, RUN1)
    opp = await _role(sm, src, "Operations Lead", "Acme", IN_RUN1)
    await _fail(sm, opp, IN_RUN1)
    await _finish(sm, run, rated=4)
    notes = await _notes(sm)
    assert [(n.kind, n.severity, n.title) for n in notes] == [
        ("roles_need_retry", "warning", "Run finished: 1 role needs another try")
    ]
    assert "roles_need_retry" in sv.NOTIFY_KINDS["attention"][2]
    assert nv.context_action("roles_need_retry", None) == {
        "label": "Open Needs another try", "href": "/quarantine", "external": False}


async def test_several_failed_roles_are_still_one_warning_with_plural_wording(sm):
    src = await _source(sm)
    run = await _run(sm, RUN1)
    for n in range(3):
        opp = await _role(sm, src, f"Role {n}", "Acme", IN_RUN1)
        await _fail(sm, opp, IN_RUN1)
    await _finish(sm, run)
    assert [n.title for n in await _notes(sm)] == ["Run finished: 3 roles need another try"]


async def test_no_failure_is_only_the_plain_summary(sm):
    src = await _source(sm)
    run = await _run(sm, RUN1)
    await _role(sm, src, "Operations Lead", "Acme", IN_RUN1, "not_fit")
    await _finish(sm, run, rated=1)
    notes = await _notes(sm)
    assert [(n.kind, n.severity, n.title) for n in notes] == [
        ("run_finished", "info", "Run finished")]
    assert await _notes(sm, "roles_need_retry") == []


async def test_a_role_waiting_on_online_consent_is_not_announced_as_failed(sm):
    """A scoring task held for the operator's online re-confirmation carries its hold marker in
    last_error, exactly like a failure, but waiting is not failing (issue #153's backoff branch
    must not sweep it in): no retry warning, and the seen-record is not spent on the role, which
    could have suppressed the announcement of a real failure later."""
    from kestrel.db.models import Task
    from kestrel.services.online_consent import APPROVAL_HOLD

    src = await _source(sm)
    run = await _run(sm, RUN1)
    opp = await _role(sm, src, "Operations Lead", "Acme", IN_RUN1)
    async with sm() as s:
        s.add(Task(type="t1", status="pending", payload={"opportunity_id": opp},
                   last_error=APPROVAL_HOLD, updated_at=IN_RUN1))
        await s.commit()
    await _finish(sm, run, rated=1)
    assert await _notes(sm, "roles_need_retry") == []
    assert [n.title for n in await _notes(sm, "run_finished")] == ["Run finished"]


async def test_a_role_awaiting_its_retry_wait_is_announced_with_its_run(sm):
    """A scoring task that failed this run and is waiting out its retry backoff (issue #153) is
    a failure of this run: announced with the run that failed it, not only after it gives up."""
    from kestrel.db.models import Task

    src = await _source(sm)
    run = await _run(sm, RUN1)
    opp = await _role(sm, src, "Operations Lead", "Acme", IN_RUN1)
    async with sm() as s:
        s.add(Task(type="t1", status="pending", attempts=1,
                   payload={"opportunity_id": opp}, last_error="the model could not finish",
                   updated_at=IN_RUN1))
        await s.commit()
    await _finish(sm, run, rated=1)
    notes = await _notes(sm)
    assert [(n.kind, n.severity, n.title) for n in notes] == [
        ("roles_need_retry", "warning", "Run finished: 1 role needs another try")]


async def test_a_role_that_fails_again_on_a_later_run_does_not_notify_again(sm):
    src = await _source(sm)
    first = await _run(sm, RUN1)
    opp = await _role(sm, src, "Operations Lead", "Acme", IN_RUN1)
    await _fail(sm, opp, IN_RUN1)
    await _finish(sm, first)
    second = await _run(sm, RUN2)
    await _fail(sm, opp, IN_RUN2)  # it failed again; it never left Needs another try
    await _finish(sm, second)
    assert len(await _notes(sm, "roles_need_retry")) == 1
    summary = (await _notes(sm, "run_finished"))[0]
    assert "1 still needs another try" in (summary.body or "")
    third = await _run(sm, RUN3)
    other = await _role(sm, src, "Second Role", "Acme", IN_RUN3)
    await _fail(sm, other, IN_RUN3)  # a role new to the list is announced
    await _finish(sm, third)
    retry = await _notes(sm, "roles_need_retry")
    assert len(retry) == 2 and retry[1].title == "Run finished: 1 role needs another try"


async def test_a_role_that_left_the_list_and_fails_again_is_new_to_it_again(sm):
    src = await _source(sm)
    first = await _run(sm, RUN1)
    opp = await _role(sm, src, "Operations Lead", "Acme", IN_RUN1)
    await _fail(sm, opp, IN_RUN1)
    await _finish(sm, first)
    second = await _run(sm, RUN2)
    await _score(sm, opp, "good", IN_RUN2)  # retried and scored: it left Needs another try
    await _finish(sm, second)
    third = await _run(sm, RUN3)
    await _fail(sm, opp, IN_RUN3)
    await _finish(sm, third)
    assert len(await _notes(sm, "roles_need_retry")) == 2


@pytest.mark.usefixtures("_isolate_data_home")
def test_the_warning_shows_in_the_bell_with_its_link():
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.db.engine import make_async_engine
    from kestrel.web.app import build_app

    migrate.run_migrations()

    async def seed() -> None:
        engine = make_async_engine(paths.db_path())
        try:
            sm = async_sessionmaker(engine, expire_on_commit=False)
            src = await _source(sm)
            run = await _run(sm, RUN1)
            opp = await _role(sm, src, "Operations Lead", "Acme", IN_RUN1)
            await _fail(sm, opp, IN_RUN1)
            await _finish(sm, run)
        finally:
            await engine.dispose()

    asyncio.run(seed())
    with TestClient(build_app(worker=False)) as c:
        html = c.get("/notifications/list?tab=attention").text
    assert "Run finished: 1 role needs another try" in html
    assert "Open Needs another try" in html and 'href="/quarantine"' in html


async def test_the_attention_switch_decides_the_warning_not_the_run_finished_one(sm):
    src = await _source(sm)
    run = await _run(sm, RUN1)
    opp = await _role(sm, src, "Operations Lead", "Acme", IN_RUN1)
    await _fail(sm, opp, IN_RUN1)
    await _finish(sm, run)
    sent, suppressed = await _deliver(sm, run_finished=False)  # the other switch: no effect
    assert (sent, suppressed) == (1, 0)
    assert (await _notes(sm))[0].delivered_channels != ["suppressed"]


async def test_the_warning_is_silenced_by_the_attention_switch(sm):
    src = await _source(sm)
    run = await _run(sm, RUN1)
    opp = await _role(sm, src, "Operations Lead", "Acme", IN_RUN1)
    await _fail(sm, opp, IN_RUN1)
    await _finish(sm, run)
    sent, suppressed = await _deliver(sm, **dict.fromkeys(sv.NOTIFY_KINDS["attention"][2], False))
    assert (sent, suppressed) == (0, 1)
    assert (await _notes(sm))[0].delivered_channels == ["suppressed"]


async def test_an_attention_switch_saved_off_before_the_new_kind_existed_still_silences_it(sm):
    src = await _source(sm)
    run = await _run(sm, RUN1)
    opp = await _role(sm, src, "Operations Lead", "Acme", IN_RUN1)
    await _fail(sm, opp, IN_RUN1)
    await _finish(sm, run)
    legacy = ("source_disabled", "liveness_closed", "update", "model_problem", "backup_problem")
    sent, suppressed = await _deliver(sm, **dict.fromkeys(legacy, False))
    assert (sent, suppressed) == (0, 1)


async def test_the_run_finished_switch_silences_the_plain_summary(sm):
    src = await _source(sm)
    run = await _run(sm, RUN1)
    await _role(sm, src, "Operations Lead", "Acme", IN_RUN1, "not_fit")
    await _finish(sm, run)
    sent, suppressed = await _deliver(sm, run_finished=False)
    assert (sent, suppressed) == (0, 1)


# =============================================== the desktop loop's own run (the app, not the CLI)
@pytest.mark.parametrize("fails", [0, 1])
async def test_the_desktop_loops_run_raises_the_notifications_too(sm, fails):
    """Before this, only the command line's `run` raised them: a run that finished inside the app
    told nobody. The loop's run (CycleRun.finish) now does, once."""
    from kestrel.pipeline.runlog import CycleRun

    src = await _source(sm)
    cycle = CycleRun(sm, mode="local")
    await cycle.ensure_open()  # the run starts now; the rows below land inside its window
    await cycle.record("tier2", {"completed": 2, "errors": 0}, did_work=True)
    opp = await _role(sm, src, "Operations Lead", "Acme", utcnow_iso(), "good")
    if fails:
        other = await _role(sm, src, "Second Role", "Acme", utcnow_iso())
        await _fail(sm, other, utcnow_iso())
    await cycle.finish("done")
    kinds = sorted(n.kind for n in await _notes(sm))
    assert kinds == sorted(["high_score", "roles_need_retry" if fails else "run_finished"])
    assert (await _notes(sm, "high_score"))[0].opportunity_id == opp


async def test_a_stopped_or_failed_loop_run_raises_nothing(sm):
    from kestrel.pipeline.runlog import CycleRun

    cycle = CycleRun(sm, mode="local")
    await cycle.ensure_open()
    await cycle.record("tier2", {"completed": 1, "errors": 0}, did_work=True)
    await cycle.finish("stopping")
    assert await _notes(sm) == []


async def test_a_role_rated_by_both_tiers_counts_once_in_the_run_summary(sm):
    """The owner's run: 1 role went through the quick screen and the full review, and the
    notification said "2 roles rated". Each role counts once."""
    from kestrel.pipeline.runlog import CycleRun

    src = await _source(sm)
    cycle = CycleRun(sm, mode="local")
    await cycle.ensure_open()
    opp = await _role(sm, src, "Operations Lead", "Acme", utcnow_iso(), "not_fit")
    await cycle.record("tier1", {"completed": 1, "errors": 0}, did_work=True, roles={opp})
    await cycle.record("tier2", {"completed": 1, "errors": 0}, did_work=True, roles={opp})
    await cycle.finish("done")
    (note,) = await _notes(sm, "run_finished")
    assert note.body == "Your search run is complete. 1 role rated."


async def test_a_loop_run_recorded_without_roles_never_adds_the_tiers(sm):
    from kestrel.pipeline.runlog import CycleRun

    await _source(sm)
    cycle = CycleRun(sm, mode="local")
    await cycle.ensure_open()
    await cycle.record("tier1", {"completed": 2, "errors": 0}, did_work=True)
    await cycle.record("tier2", {"completed": 1, "errors": 0}, did_work=True)
    await cycle.finish("done")
    (note,) = await _notes(sm, "run_finished")
    assert note.body == "Your search run is complete. 2 roles rated."
