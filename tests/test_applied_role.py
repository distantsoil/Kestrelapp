"""I applied from the Role page (issue #129): the server refuses a future date, a role never
shortlisted can be marked applied, the follow-up date and Edit date work, Undo puts back exactly
what was there, and a backdated application says its follow-up is overdue. Nothing here touches
the network or a model."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kestrel import paths
from kestrel.db.engine import make_async_engine
from kestrel.db.models import Pursuit, StatusEvent, Task
from kestrel.services import pursuit_undo
from tests import role_seed

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


@pytest.fixture
def ids():
    return asyncio.run(role_seed.seed_async())


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _db(fn):
    async def run():
        from sqlalchemy.ext.asyncio import async_sessionmaker

        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            out = await fn(s)
            await s.commit()
        await engine.dispose()
        return out

    return asyncio.run(run())


def _pursuit(opp_id: int):
    async def go(s):
        p = (await s.execute(select(Pursuit).where(Pursuit.opportunity_id == opp_id))
             ).scalar_one_or_none()
        return None if p is None else {k: getattr(p, k) for k in pursuit_undo.FIELDS} | {
            "id": p.id}

    return _db(go)


def _events(pid: int) -> list[tuple[str | None, str]]:
    async def go(s):
        rows = (await s.execute(select(StatusEvent).where(StatusEvent.pursuit_id == pid)
                                .order_by(StatusEvent.id))).scalars().all()
        return [(r.from_status, r.to_status) for r in rows]

    return _db(go)


def _question_tasks() -> int:
    async def go(s):
        return len((await s.execute(select(Task).where(Task.type == "interview_questions")
                                    )).scalars().all())

    return _db(go)


def _day(delta: int) -> str:
    return (date.today() + timedelta(days=delta)).isoformat()


def _applied(c, opp_id: int, **data):
    return c.post(f"/opportunities/{opp_id}/role/applied", data=data)


def test_a_future_date_is_refused_by_the_server_and_nothing_is_recorded(ids):
    """The input's max attribute is not enough: a forged post is refused with a plain sentence
    on a never-shortlisted role (no pursuit is made) and on a shortlisted one (no date stored)."""
    with _client() as c:
        r = _applied(c, ids["second"], applied_on=_day(1))
        assert r.headers.get("hx-retarget") == "#applied-error"
        assert "That date is in the future. Choose today or an earlier date." in r.text
        assert _pursuit(ids["second"]) is None  # not even a shortlist entry
        c.post(f"/opportunities/{ids['strong']}/role/shortlist")
        before = _pursuit(ids["strong"])
        n_events = len(_events(before["id"]))
        for path, data in (
            (f"/opportunities/{ids['strong']}/role/applied", {"applied_on": _day(30)}),
            (f"/pursuits/{before['id']}/act/applied/quick", {"applied_on": _day(2)}),
            (f"/pursuits/{before['id']}/act/applied", {"applied_on": _day(2)}),
        ):
            c.post(path, data=data)
        assert _pursuit(ids["strong"]) == before
        assert len(_events(before["id"])) == n_events
        assert _question_tasks() == 0


def test_a_role_never_shortlisted_is_marked_applied_and_undo_takes_it_back(ids):
    past = _day(-3)
    with _client() as c:
        r = _applied(c, ids["second"], applied_on=past)
        assert r.status_code == 200 and 'id="rl-side"' in r.text
        p = _pursuit(ids["second"])
        assert p["status"] == "applied" and p["applied_at"].startswith(past)
        assert p["next_action_at"] == _day(4)  # a week after the applied date
        assert _events(p["id"])[-1] == ("shortlisted", "applied")
        assert 'data-testid="applied-undo"' in r.text
        undo = c.post(f"/opportunities/{ids['second']}/role/applied/undo",
                      headers={"HX-Request": "true", "HX-Target": "rl-side"})
        assert undo.status_code == 200 and 'data-testid="add-to-list"' in undo.text
        assert _pursuit(ids["second"]) is None  # as it was before the click


def test_undo_restores_the_previous_status_dates_and_follow_up_exactly(ids):
    """The real case: a shortlisted card that already carries a next step."""
    with _client() as c:
        c.post(f"/opportunities/{ids['strong']}/role/shortlist")

        async def give_a_next_step(s):
            p = (await s.execute(select(Pursuit).where(Pursuit.opportunity_id == ids["strong"])
                                 )).scalar_one()
            p.next_action, p.next_action_at = "Check the careers page", _day(5)
            p.notes_md = "Kept from before"

        _db(give_a_next_step)
        before = _pursuit(ids["strong"])
        r = _applied(c, ids["strong"], applied_on=_day(-10), follow_up=_day(9))
        assert r.status_code == 200
        mid = _pursuit(ids["strong"])
        assert mid["status"] == "applied" and mid["next_action_at"] == _day(9)
        assert mid["applied_at"].startswith(_day(-10))
        out = c.post(f"/opportunities/{ids['strong']}/role/applied/undo")  # the toast's Undo
        assert out.status_code == 200 and "data-undo-msg" not in out.text
        after = _pursuit(ids["strong"])
        for key in ("status", "applied_at", "next_action", "next_action_at", "notes_md"):
            assert after[key] == before[key], key
        assert _events(after["id"])[-1] == ("applied", "shortlisted")  # history is kept
        again = c.post(f"/opportunities/{ids['strong']}/role/applied/undo")
        assert "data-undo-msg" in again.text  # used up: it says so, changes nothing
        assert _pursuit(ids["strong"])["status"] == "shortlisted"


def test_follow_up_defaults_to_a_week_after_the_applied_date_and_can_be_chosen(ids):
    with _client() as c:
        _applied(c, ids["second"], applied_on=_day(-2), follow_up="")
        assert _pursuit(ids["second"])["next_action_at"] == _day(5)
        _applied(c, ids["strong"], applied_on=_day(0), follow_up=_day(3))
        assert _pursuit(ids["strong"])["next_action_at"] == _day(3)
        bad = _applied(c, ids["maybe"], follow_up="nonsense")
        assert "follow-up date is not a valid date" in bad.text
        assert _pursuit(ids["maybe"]) is None


def test_the_follow_up_date_reads_in_the_same_style_as_the_applied_date(ids):
    """Applied on and Follow up on use the person's date style, never one of each (0.7.6 review:
    "Applied on 2026-11-05. Follow up Nov 12")."""
    with _client() as c:
        saved = _applied(c, ids["strong"], applied_on=_day(0), follow_up=_day(3))
        assert f"Applied on <b>{_day(0)}</b>" in saved.text
        assert f'Follow up on <span class="oa-nowrap">{_day(3)}</span>' in saved.text


def test_a_backdated_application_whose_follow_up_has_passed_says_overdue(ids):
    with _client() as c:
        late = _applied(c, ids["second"], applied_on=_day(-30))
        assert 'data-testid="applied-follow"' in late.text
        assert "Follow-up overdue" in late.text
        page = c.get(f"/opportunities/{ids['second']}").text
        assert "Follow-up overdue" in page
        _applied(c, ids["strong"], applied_on=_day(0))
        fresh = c.get(f"/opportunities/{ids['strong']}").text
        assert "Follow-up overdue" not in fresh
        assert f'Follow up on <span class="oa-nowrap">{_day(7)}</span>' in fresh
        board = c.get("/pursuits").text
        assert "Follow-up overdue" in board  # the same words where the card shows its next step


def test_edit_date_changes_the_dates_only_and_undo_still_goes_back_to_before(ids):
    with _client() as c:
        c.post(f"/opportunities/{ids['strong']}/role/shortlist")
        before = _pursuit(ids["strong"])
        _applied(c, ids["strong"], applied_on=_day(-1))
        events = _events(before["id"])
        r = c.post(f"/opportunities/{ids['strong']}/role/applied/edit",
                   data={"applied_on": _day(-6), "follow_up": _day(2)})
        assert r.status_code == 200 and 'id="rl-side"' in r.text
        edited = _pursuit(ids["strong"])
        assert edited["status"] == "applied" and edited["applied_at"].startswith(_day(-6))
        assert edited["next_action_at"] == _day(2)
        assert _events(before["id"]) == events  # a date edit is not a status change
        refused = c.post(f"/opportunities/{ids['strong']}/role/applied/edit",
                         data={"applied_on": _day(1)})
        assert "in the future" in refused.text
        assert _pursuit(ids["strong"]) == edited
        c.post(f"/opportunities/{ids['strong']}/role/applied/undo")
        back = _pursuit(ids["strong"])
        assert back["status"] == "shortlisted" and back["applied_at"] is None
        assert back["next_action_at"] == before["next_action_at"]


def test_a_second_application_is_refused_and_a_hidden_role_cannot_be_marked(ids):
    with _client() as c:
        _applied(c, ids["second"], applied_on=_day(-1))
        first = _pursuit(ids["second"])
        again = _applied(c, ids["second"], applied_on=_day(-4))
        assert "already recorded" in again.text
        assert _pursuit(ids["second"]) == first
        c.post(f"/opportunities/{ids['maybe']}/role/not-for-me?reason=later")
        hidden = _applied(c, ids["maybe"], applied_on=_day(0))
        assert "Put the role back" in hidden.text
        assert _pursuit(ids["maybe"]) is None


def test_marking_applied_queues_the_questions_once_and_never_asks_nothing_online(
    ids, monkeypatch
):
    """The follow-on task keeps its ask-first rule: with the model online and no confirmation,
    marking applied queues nothing; offline it queues exactly one task."""
    with _client() as c:
        _applied(c, ids["second"], applied_on=_day(0))
        assert _question_tasks() == 1
        monkeypatch.setattr("kestrel.services.online_consent.tier_goes_online",
                            lambda settings, tier: True)
        _applied(c, ids["strong"], applied_on=_day(0))
        assert _pursuit(ids["strong"])["status"] == "applied"
        assert _question_tasks() == 1  # nothing was queued for the online-held role


def test_the_form_offers_the_quick_picks_and_the_follow_up_field(ids):
    with _client() as c:
        html = c.get(f"/opportunities/{ids['second']}").text
    for needle in ('data-testid="pick-today"', 'data-testid="pick-yesterday"',
                   'name="follow_up"', f'value="{_day(7)}"', f'data-oa-pick="{_day(-1)}"'):
        assert needle in html, needle
    assert 'style="' not in html.split('data-testid="applied-form"')[1].split("</form>")[0]


def test_undo_stays_after_the_worker_writes_the_questions_and_edit_date(ids):
    """Issue #129 with the worker's effect present: after I applied on a role never shortlisted,
    the worker drains the interview-question task and stores a set for the new pursuit. Edit date
    then still offers Undo, and Undo takes the role back to before the click: no pursuit, no set,
    nothing left on the queue."""
    import json as _json

    from kestrel.config import get_settings
    from kestrel.db.models import InterviewQuestionSet
    from kestrel.interview import question_task as qt
    from kestrel.llm.client import ChatResult

    class _Model:
        settings = get_settings()

        async def complete(self, *, tier, messages, schema=None, **kw):
            body = {"questions": [{"question": "How would you plan the first month?",
                                   "category": "role_specific", "why_asked": "Tests scope.",
                                   "story_id": None, "proof_point_key": None}]}
            return ChatResult(content=_json.dumps(body), prompt_tokens=1, completion_tokens=1,
                              latency_ms=1, model=self.settings.llm.tiers[tier].model)

    def drain():
        async def run():
            from sqlalchemy.ext.asyncio import async_sessionmaker

            engine = make_async_engine(paths.db_path())
            sm = async_sessionmaker(engine, expire_on_commit=False)
            done = await qt.drain_interview_questions(sm, _Model(), get_settings())
            await engine.dispose()
            return done

        return asyncio.run(run())

    def sets() -> int:
        async def go(s):
            return len((await s.execute(select(InterviewQuestionSet))).scalars().all())

        return _db(go)

    with _client() as c:
        assert _pursuit(ids["second"]) is None
        _applied(c, ids["second"], applied_on=_day(-1))
        assert drain() == 1 and sets() == 1  # the worker wrote the list for this application
        r = c.post(f"/opportunities/{ids['second']}/role/applied/edit",
                   data={"applied_on": _day(-3), "follow_up": _day(4)})
        assert 'data-testid="applied-undo"' in r.text
        assert 'data-testid="applied-undo"' in c.get(f"/opportunities/{ids['second']}").text
        out = c.post(f"/opportunities/{ids['second']}/role/applied/undo",
                     headers={"HX-Request": "true", "HX-Target": "rl-side"})
        assert "Undone." in out.headers.get("hx-trigger", "")
        assert 'data-testid="add-to-list"' in out.text
    assert _pursuit(ids["second"]) is None and sets() == 0


def test_undo_before_the_worker_runs_cancels_the_waiting_question_task(ids):
    """Undo of a fresh application also takes back its queued question task while it waits, so
    the next application of the role gets its own list (D-UI-252)."""

    def task_states() -> list[str]:
        async def go(s):
            rows = (await s.execute(select(Task).where(Task.type == "interview_questions")
                                    .order_by(Task.id))).scalars().all()
            return [t.status for t in rows]

        return _db(go)

    with _client() as c:
        _applied(c, ids["second"], applied_on=_day(0))
        assert task_states() == ["pending"]
        c.post(f"/opportunities/{ids['second']}/role/applied/undo")
        assert _pursuit(ids["second"]) is None
        assert task_states() == ["cancelled"]
        _applied(c, ids["second"], applied_on=_day(0))
        assert task_states() == ["cancelled", "pending"]  # a fresh task for the new pursuit


def test_undo_leaves_a_question_task_the_worker_already_leased(ids):
    """The cancel is conditional on the status: a task the worker leased meanwhile is not
    touched (it finds the pursuit gone and completes as skipped)."""
    from sqlalchemy import update

    def lease_it():
        async def go(s):
            await s.execute(update(Task).where(Task.type == "interview_questions")
                            .values(status="leased", worker_id="pipeline-questions"))

        _db(go)

    def states() -> list[str]:
        async def go(s):
            rows = (await s.execute(select(Task.status).where(Task.type == "interview_questions")
                                    )).scalars().all()
            return list(rows)

        return _db(go)

    with _client() as c:
        _applied(c, ids["second"], applied_on=_day(0))
        lease_it()
        c.post(f"/opportunities/{ids['second']}/role/applied/undo")
        assert _pursuit(ids["second"]) is None
        assert states() == ["leased"]


# ------------------------------------------------------------- Resume used (issue #129, D-UI-253)
def test_resume_used_is_chosen_on_the_role_page_stored_and_shown_on_the_role_and_applications(
    ids,
):
    """The form lists the documents made for this role (labeled by master and date, never the
    file name), the one picked is stored on the application, and the role page and the
    Applications panel both name it."""
    with _client() as c:
        html = c.get(f"/opportunities/{ids['strong']}").text
        pick = html.split('data-testid="applied-resume-pick"')[1].split("</select>")[0]
        assert f'value="{ids["render"]}"' in pick
        assert "Rivera_Larkspur" not in pick  # a PDF name can carry an organization name
        r = _applied(c, ids["strong"], applied_on=_day(-2), document_id=str(ids["render"]))
        assert r.status_code == 200 and "applied-error" not in r.headers.get("hx-retarget", "")
        assert _pursuit(ids["strong"])["document_id"] == ids["render"]
        assert 'data-testid="applied-resume"' in r.text and "Resume used: " in r.text
        page = c.get(f"/opportunities/{ids['strong']}").text
        shown = page.split('data-testid="applied-resume"')[1].split("</div>")[0]
        assert "Resume used: " in shown and "Rivera_Larkspur" not in shown
        pid = _pursuit(ids["strong"])["id"]
        panel = c.get(f"/pursuits/{pid}/panel").text
        assert f'href="/renders/{ids["render"]}"' in panel  # Applications: Documents used
        out = c.post(f"/opportunities/{ids['strong']}/role/applied/undo")
        assert out.status_code == 200
        assert _pursuit(ids["strong"]) is None  # never shortlisted: the whole application goes


def _add_labels(opp_id: int) -> int:
    from kestrel.db.models import Setting, VerdictOverride
    from kestrel.web.pages.opportunity_actions import ADD_REASONS

    async def go(s):
        labels = (await s.execute(select(VerdictOverride).where(
            VerdictOverride.opportunity_id == opp_id, VerdictOverride.reason.in_(ADD_REASONS))
        )).scalars().all()
        snaps = (await s.execute(select(Setting).where(
            Setting.key.like(pursuit_undo.PREFIX + "%")))).scalars().all()
        return len(labels) + len(snaps)

    return _db(go)


def test_resume_used_refuses_another_roles_document_and_a_cover_letter(ids):
    """Only a resume for this role (or a general one) can be named; nothing is recorded on a
    refusal, not even a shortlist entry for a role never shortlisted."""
    from kestrel.db.models import DocRender

    def add_letter() -> int:
        async def go(s):
            row = DocRender(opportunity_id=ids["second"], doc_master_id=ids["master"],
                            template="cover_letter", config={}, filename="letter.pdf")
            s.add(row)
            await s.flush()
            return row.id

        return _db(go)

    letter = add_letter()
    with _client() as c:
        other = _applied(c, ids["second"], applied_on=_day(0), document_id=str(ids["render"]))
        assert "does not belong to this role" in other.text
        assert _pursuit(ids["second"]) is None
        bad = _applied(c, ids["second"], applied_on=_day(0), document_id=str(letter))
        assert "not a cover letter" in bad.text
        assert _pursuit(ids["second"]) is None
        assert _add_labels(ids["second"]) == 0  # the shortlist label went with the pursuit
        html = c.get(f"/opportunities/{ids['second']}").text
        pick = html.split('data-testid="applied-resume-pick"')[1].split("</select>")[0]
        assert f'value="{letter}"' not in pick and f'value="{ids["render"]}"' not in pick


# ----------------------------------------------------- the Pipeline side panel (issue #129)
_PANEL = {"HX-Request": "true", "HX-Target": "pl-panel"}


def test_the_pipeline_panel_has_the_same_i_applied_control_and_route(ids):
    """Owner, 2026-11-03: the same form, the same route, the same Undo. From the panel the answer
    is the panel itself."""
    with _client() as c:
        panel = c.get(f"/opportunities/{ids['second']}/panel").text
        block = panel.split('data-testid="panel-applied"')[1]
        form = block.split('data-testid="applied-form"')[0].rsplit("<form", 1)[1]
        assert f'hx-post="/opportunities/{ids["second"]}/role/applied"' in form
        assert 'hx-target="#pl-panel"' in form and 'hx-swap="innerHTML"' in form
        for needle in ('data-testid="pick-today"', 'data-testid="pick-yesterday"',
                       'name="follow_up"', 'name="document_id"'):
            assert needle in block, needle
        refused = c.post(f"/opportunities/{ids['second']}/role/applied",
                         data={"applied_on": _day(2)}, headers=_PANEL)
        assert "in the future" in refused.text and _pursuit(ids["second"]) is None
        done = c.post(f"/opportunities/{ids['second']}/role/applied",
                      data={"applied_on": _day(-1)}, headers=_PANEL)
        assert 'data-testid="panel-applied"' in done.text  # the panel, not the role column
        assert 'id="rl-side"' not in done.text
        assert 'data-testid="applied-on"' in done.text and 'data-testid="applied-undo"' in done.text
        assert _pursuit(ids["second"])["status"] == "applied"
        edit = c.post(f"/opportunities/{ids['second']}/role/applied/edit",
                      data={"applied_on": _day(-4), "follow_up": _day(3)}, headers=_PANEL)
        assert 'data-testid="panel-applied"' in edit.text
        assert _pursuit(ids["second"])["applied_at"].startswith(_day(-4))
        undo = c.post(f"/opportunities/{ids['second']}/role/applied/undo", headers=_PANEL)
        assert 'data-testid="panel-applied"' in undo.text
        assert 'data-testid="applied-form"' in undo.text
        assert _pursuit(ids["second"]) is None  # never shortlisted, as before the click


# --------------------------------------------------------------- re-posts (issue #129, SPEC 5.6)
def test_the_role_page_says_re_posts_are_hidden_with_the_switch(ids):
    with _client() as c:
        before = c.get(f"/opportunities/{ids['second']}").text
        assert 'data-testid="repost-note"' not in before  # nothing applied yet
        r = _applied(c, ids["second"], applied_on=_day(0))
        assert "Re-posts of a role you applied to are hidden." in r.text
        pid = _pursuit(ids["second"])["id"]
        note = r.text.split('data-testid="repost-note"')[1].split("</div>")[0]
        assert f'hx-post="/pursuits/{pid}/reposts"' in note and "checked" not in note
        on = c.post(f"/pursuits/{pid}/reposts", data={"allow": "on"})
        assert on.status_code == 204 and "will show" in on.headers.get("hx-trigger", "")
        assert _pursuit(ids["second"])["allow_reposts"] is True
        page = c.get(f"/opportunities/{ids['second']}").text
        assert "checked" in page.split('data-testid="repost-note"')[1].split("</div>")[0]
        panel = c.get(f"/opportunities/{ids['second']}/panel").text
        assert 'data-testid="repost-note"' in panel


def test_a_copy_of_a_role_applied_from_the_role_page_is_hidden_on_arrival(ids):
    """The real path: a posting ingested by a source, marked applied on the Role page, then a
    second copy of it arrives from another board. The copy is hidden as a re-post; with the
    switch on, the next copy shows."""
    from kestrel.db.models import Opportunity, Source
    from kestrel.ingest.base import RawPosting
    from kestrel.ingest.normalize import normalize_and_store

    def ingest(external_id: str, url: str):
        async def go(s):
            src = (await s.execute(select(Source).where(Source.name == "board-z"))
                   ).scalar_one_or_none()
            if src is None:
                src = Source(name="board-z", kind="greenhouse", track="employment",
                             enabled=True, config={})
                s.add(src)
                await s.flush()
            res = await normalize_and_store(
                s,
                RawPosting(external_id=external_id, url=url, title="Fleet Routing Coordinator",
                           org_name="Quillfield Freight",
                           description_md="Plan daily routes for a regional fleet. " * 20),
                source=src,
            )
            return res.opportunity.id, res.suppressed

        return _db(go)

    def hidden(opp_id: int):
        async def go(s):
            o = await s.get(Opportunity, opp_id)
            return o.archived_at is not None, o.archive_reason, o.stage_reason

        return _db(go)

    first, suppressed = ingest("qf-1", "https://boards.example/quillfield/1")
    assert suppressed is False
    with _client() as c:
        r = _applied(c, first, applied_on=_day(-1))
        assert r.status_code == 200 and _pursuit(first)["status"] == "applied"
        pid = _pursuit(first)["id"]
        copy, suppressed = ingest("qf-2", "https://other-board.example/quillfield-routing")
        assert suppressed is True and copy != first
        assert hidden(copy) == (True, "applied", f"Re-post of pursuit #{pid}")
        c.post(f"/pursuits/{pid}/reposts", data={"allow": "on"})
    third, suppressed = ingest("qf-3", "https://third-board.example/quillfield")
    assert suppressed is False and hidden(third)[0] is False


# ------------------------- 0.7.6 review item 3: Undo on a shortlisted role takes back the task
# Invariant: Undo of I applied restores exactly what the action changed. On a role that was
# already shortlisted, the application queued an interview-question task (and the worker may
# have written a set); after Undo the role is shortlisted again, so neither may remain.
def _question_task_states() -> list[str]:
    async def go(s):
        rows = (await s.execute(select(Task).where(Task.type == "interview_questions")
                                .order_by(Task.id))).scalars().all()
        return [t.status for t in rows]

    return _db(go)


def _question_sets(pid: int) -> int:
    from kestrel.db.models import InterviewQuestionSet

    async def go(s):
        return len((await s.execute(select(InterviewQuestionSet).where(
            InterviewQuestionSet.pursuit_id == pid))).scalars().all())

    return _db(go)


def _drain_questions() -> int:
    import json as _json

    from kestrel.config import get_settings
    from kestrel.interview import question_task as qt
    from kestrel.llm.client import ChatResult

    class _Model:
        settings = get_settings()

        async def complete(self, *, tier, messages, schema=None, **kw):
            body = {"questions": [{"question": "What would the first quarter look like?",
                                   "category": "role_specific", "why_asked": "Tests scope.",
                                   "story_id": None, "proof_point_key": None}]}
            return ChatResult(content=_json.dumps(body), prompt_tokens=1, completion_tokens=1,
                              latency_ms=1, model=self.settings.llm.tiers[tier].model)

    async def run():
        from sqlalchemy.ext.asyncio import async_sessionmaker

        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        done = await qt.drain_interview_questions(sm, _Model(), get_settings())
        await engine.dispose()
        return done

    return asyncio.run(run())


def test_undo_on_a_shortlisted_role_cancels_the_waiting_question_task(ids):
    with _client() as c:
        c.post(f"/opportunities/{ids['strong']}/role/shortlist")
        _applied(c, ids["strong"], applied_on=_day(0))
        assert _question_task_states() == ["pending"]
        c.post(f"/opportunities/{ids['strong']}/role/applied/undo")
        assert _pursuit(ids["strong"])["status"] == "shortlisted"
        assert _question_task_states() == ["cancelled"]
        assert _drain_questions() == 0  # nothing left for the worker to write
        _applied(c, ids["strong"], applied_on=_day(0))
        assert _question_task_states() == ["cancelled", "pending"]


def test_undo_on_a_shortlisted_role_removes_the_set_the_application_made(ids):
    with _client() as c:
        c.post(f"/opportunities/{ids['strong']}/role/shortlist")
        pid = _pursuit(ids["strong"])["id"]
        _applied(c, ids["strong"], applied_on=_day(-1))
        assert _drain_questions() == 1 and _question_sets(pid) == 1
        out = c.post(f"/opportunities/{ids['strong']}/role/applied/undo",
                     headers={"HX-Request": "true", "HX-Target": "rl-side"})
        assert "Undone." in out.headers.get("hx-trigger", "")
        assert _pursuit(ids["strong"])["status"] == "shortlisted"
        assert _question_sets(pid) == 0


def test_undo_on_a_shortlisted_role_leaves_a_leased_question_task(ids):
    from sqlalchemy import update

    with _client() as c:
        c.post(f"/opportunities/{ids['strong']}/role/shortlist")
        _applied(c, ids["strong"], applied_on=_day(0))

        async def lease(s):
            await s.execute(update(Task).where(Task.type == "interview_questions")
                            .values(status="leased", worker_id="pipeline-questions"))

        _db(lease)
        c.post(f"/opportunities/{ids['strong']}/role/applied/undo")
        assert _pursuit(ids["strong"])["status"] == "shortlisted"
        assert _question_task_states() == ["leased"]


# ---------------- 0.7.6 review item 4: a database failure on a fresh role leaves nothing behind
def test_a_database_failure_while_applying_a_fresh_role_leaves_no_shortlist_or_labels(
    ids, monkeypatch,
):
    from sqlalchemy.exc import OperationalError

    from kestrel.db.models import Setting, VerdictOverride
    from kestrel.services import pursuit_board
    from kestrel.web.pages.opportunity_actions import ADD_REASONS

    async def broken(*args, **kwargs):
        raise OperationalError("UPDATE pursuit", {}, Exception("database is locked"))

    def labels() -> int:
        async def go(s):
            return len((await s.execute(select(VerdictOverride).where(
                VerdictOverride.opportunity_id == ids["second"],
                VerdictOverride.reason.in_(ADD_REASONS)))).scalars().all())

        return _db(go)

    def undo_rows() -> int:
        async def go(s):
            return len((await s.execute(select(Setting).where(
                Setting.key.like(pursuit_undo.PREFIX + "%")))).scalars().all())

        return _db(go)

    labels_before, undo_before = labels(), undo_rows()
    monkeypatch.setattr(pursuit_board, "apply_verb", broken)
    with _client() as c:
        r = _applied(c, ids["second"], applied_on=_day(0))
    assert r.status_code == 200 and r.headers.get("hx-retarget") == "#applied-error"
    assert _pursuit(ids["second"]) is None  # not left on the shortlist
    assert labels() == labels_before  # the Add's learning labels went with it
    assert undo_rows() == undo_before
