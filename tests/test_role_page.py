"""The Role page (D-UI-150): each state of the mockup and each action, against the seeded roles of
tests/role_seed.py. Nothing reaches the network or a model; an online route is faked where a
test needs one."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kestrel import paths
from kestrel.db.engine import make_async_engine
from kestrel.db.models import Label, Opportunity, Pursuit, Story, Task, VerdictOverride
from tests import role_seed

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


@pytest.fixture
def ids():
    return asyncio.run(role_seed.seed_async())


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _rows(model, *where):
    async def run():
        from sqlalchemy.ext.asyncio import async_sessionmaker

        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            out = list((await s.execute(select(model).where(*where))).scalars())
        await engine.dispose()
        return out

    return asyncio.run(run())


def _toast(r) -> dict:
    return json.loads(r.headers["hx-trigger"])["k-toast"]


# ------------------------------------------------------------------------------ the evaluation
def test_the_evaluation_tab_shows_every_part_of_the_mockup(ids):
    with _client() as c:
        html = c.get(f"/opportunities/{ids['strong']}").text
    assert "Senior Operations Analyst" in html and ">4.6<" in html and "Strong fit" in html
    for name in (
        "Role fit",
        "Domain match",
        "Company context",
        "Pay and logistics",
        "Career direction",
    ):
        assert name in html
    assert "Why it is a strong match" in html and "Written by Kestrel's AI" in html
    # Pay: the band, the similar-roles bracket with its count, the one-line read
    assert (
        'class="rl-pband"' in html and "Similar roles · 6" in html and "Above your target" in html
    )
    # Level ladder, reports to, team size; Lead with
    assert (
        "Same level as now" in html
        and "Reports to VP Operations" in html
        and "This role · You" in html
    )
    assert "Lead with" in html and "22% before-and-after" in html
    # Match wheel: 2 met, 1 partly, 2 not met, "2 of 5", clickable chips, a story link per gap
    assert 'data-testid="wheel-center">2 of 5<' in html and "2 not met" in html
    assert html.count("+ I have a story for this") == 3  # the partly met one and both gaps
    assert "In your favor" in html and "Gaps" in html and "Lead with your Lean results" in html
    # Evidence: the quote, the proof, a link to the exact story, "Source not recorded" otherwise
    assert "/stories#story-" in html and "Story · Demand forecast rebuild" in html
    assert "Source not recorded" in html
    assert "+ I have a story that shows something else they ask for" in html
    # What the role is, labeled; tags from facts on file
    assert "Summarized by Kestrel's AI" in html and "Transportation and logistics" in html
    # Your view and the company card
    assert "Kestrel says <b>4.6</b>, strong fit. Agree?" in html
    assert "Austin, TX" in html and "Public sources only. Blank when unknown" in html


def test_fewer_than_five_similar_roles_says_too_few_and_draws_no_bracket(ids):
    with _client() as c:
        html = c.get(f"/opportunities/{ids['second']}").text
    assert "Similar roles: Too few (3)." in html and "rl-pband__sim" not in html


def test_the_unscored_role_says_so_and_offers_scoring(ids):
    with _client() as c:
        html = c.get(f"/opportunities/{ids['unscored']}").text
    assert 'data-testid="not-scored"' in html and "Being scored" in html
    assert "/rescore/choose?opp_ids=" in html and 'data-testid="wheel-center"' not in html


def test_a_lead_reads_in_the_consulting_words(ids):
    with _client() as c:
        html = c.get(f"/opportunities/{ids['lead']}").text
    assert "Engagement fit" in html and "Strategic value" in html and "Add to leads" in html
    assert "Opening move" in html and "Buyer access" in html and "Engagement outline" in html
    assert "PREPARE" in html and "PREPARE YOUR APPLICATION" not in html
    assert "Discovery call prep" in html and "The situation" in html


def test_watch_lines_and_blockers_are_shown(ids):
    with _client() as c:
        second = c.get(f"/opportunities/{ids['second']}").text
        maybe = c.get(f"/opportunities/{ids['maybe']}").text
    assert 'data-testid="watch"' in second and "may not be a real opening" in second
    assert "On-site in Dayton, OH." in maybe


def test_previous_and_next_follow_the_default_pipeline_view(ids):
    with _client() as c:
        first = c.get(f"/opportunities/{ids['strong']}").text
        second = c.get(f"/opportunities/{ids['second']}").text
        part = c.get(f"/opportunities/{ids['second']}", headers={"HX-Request": "true"}).text
    assert (
        'data-testid="rl-pos">1 of 4<' in first
        and f'href="/opportunities/{ids["second"]}"' in first
    )
    assert 'data-testid="rl-pos">2 of 4<' in second
    assert (
        part.lstrip().startswith("{#") is False and "<html" not in part and 'id="rl-body"' in part
    )


# ------------------------------------------------------------------------- Prepare is greyed out
def test_prepare_is_greyed_until_shortlisted_then_switches_on(ids):
    with _client() as c:
        before = c.get(f"/opportunities/{ids['second']}").text
        assert (
            before.count('data-locked="1"') == 3
            and "Available once you shortlist this role" in before
        )
        assert "Why?" in before and 'id="rl-why"' in before and "minutes of your computer" in before
        r = c.post(f"/opportunities/{ids['second']}/role/shortlist")
        assert r.status_code == 200 and 'id="rl-side"' in r.text
        assert 'data-locked="1"' not in r.text and "/renders/new?master_id=" in r.text
        assert f"/prep/{ids['second']}?auto=1" in r.text
        toast = _toast(r)
        assert toast["undoUrl"].startswith(f"/opportunities/{ids['second']}/role/shortlist/undo")
        assert (
            "Shortlisted. Tailor resume, cover letter and prep pack are switched on."
            in toast["message"]
        )
        # Cover letter has no drafting back end: shown, never active, and it says why
        assert 'disabled title="Kestrel does not draft this yet.' in r.text
    assert len(_rows(Pursuit, Pursuit.opportunity_id == ids["second"])) == 1


def test_the_second_click_removes_the_shortlist_with_an_undo(ids):
    with _client() as c:
        c.post(f"/opportunities/{ids['second']}/role/shortlist")
        r = c.post(f"/opportunities/{ids['second']}/role/unshortlist")
        assert "Anything you drafted stays in Documents." in _toast(r)["message"]
        assert not _rows(Pursuit, Pursuit.opportunity_id == ids["second"])
        assert 'data-testid="add-to-list"' in r.text
        undo = _toast(r)["undoUrl"]
        c.post(undo)
    assert len(_rows(Pursuit, Pursuit.opportunity_id == ids["second"])) == 1


def test_undo_of_shortlist_removes_only_that_pursuit_and_never_a_worked_one(ids):
    with _client() as c:
        r = c.post(f"/opportunities/{ids['second']}/role/shortlist")
        undo = _toast(r)["undoUrl"]
        pid = _rows(Pursuit)[0].id

        async def work():
            from sqlalchemy.ext.asyncio import async_sessionmaker

            engine = make_async_engine(paths.db_path())
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                p = await s.get(Pursuit, pid)
                p.status = "applied"
                p.applied_at = "2026-10-20T00:00:00+00:00"
                await s.commit()
            await engine.dispose()

        asyncio.run(work())
        c.post(undo)
        assert len(_rows(Pursuit)) == 1  # it moved on: Undo leaves it alone
        r = c.post(f"/opportunities/{ids['second']}/role/unshortlist")
        assert "moved on in Applications" in _toast(r)["message"] and len(_rows(Pursuit)) == 1
        page = c.get(f"/opportunities/{ids['second']}").text
        assert 'href="/pursuits"' in page and "Remove from my shortlist" in page


# ---------------------------------------------------------------------------------- Not for me
def test_not_for_me_hides_with_a_reason_and_an_undo_and_offers_the_next_role(ids):
    with _client() as c:
        page = c.get(f"/opportunities/{ids['second']}").text
        for reason in ("Not my kind of role", "Pay or location", "Wrong level", "Just not now"):
            assert reason in page
        r = c.post(f"/opportunities/{ids['second']}/role/not-for-me?reason=pay_place")
        assert "Moved to Not a fit in Pipeline." in r.text and 'data-testid="next-role"' in r.text
        toast = _toast(r)
        assert "/decide/undo-hide?at=" in toast["undoUrl"]
        opp = _rows(Opportunity, Opportunity.id == ids["second"])[0]
        assert opp.archived_at and opp.archive_reason == "not_interested"
        assert any(
            "Not for me: Pay or location" in (v.reason or "")
            for v in _rows(VerdictOverride, VerdictOverride.opportunity_id == ids["second"])
        )
        c.post(toast["undoUrl"])
        assert _rows(Opportunity, Opportunity.id == ids["second"])[0].archived_at is None
        assert not _rows(VerdictOverride, VerdictOverride.opportunity_id == ids["second"])


def test_not_for_me_is_refused_for_a_shortlisted_role(ids):
    with _client() as c:
        c.post(f"/opportunities/{ids['second']}/role/shortlist")
        r = c.post(f"/opportunities/{ids['second']}/role/not-for-me?reason=kind")
        assert "Take it off your shortlist first" in _toast(r)["message"]
    assert _rows(Opportunity, Opportunity.id == ids["second"])[0].archived_at is None


def test_archive_and_put_back(ids):
    with _client() as c:
        r = c.post(f"/opportunities/{ids['maybe']}/role/archive")
        undo = _toast(r)["undoUrl"]
        assert _rows(Opportunity, Opportunity.id == ids["maybe"])[0].archived_at
        assert 'data-testid="hidden-note"' in r.text and "Find it under Hidden" in r.text
        c.post(undo)
        assert _rows(Opportunity, Opportunity.id == ids["maybe"])[0].archived_at is None
        # An Undo from an older archive never undoes a newer one.
        c.post(f"/opportunities/{ids['maybe']}/role/archive")
        c.post(f"/opportunities/{ids['maybe']}/role/unarchive?at=2020-01-01")
        assert _rows(Opportunity, Opportunity.id == ids["maybe"])[0].archived_at


# ------------------------------------------------------------------------------- Your view
def test_your_view_yes_and_adjust_save_one_verdict_override_and_undo_deletes_only_it(ids):
    with _client() as c:
        r = c.post(f"/opportunities/{ids['strong']}/role/view", data={"mode": "agree"})
        toast = _toast(r)
        assert "Saved as a training example" in toast["message"]
        assert 'id="rl-view"' in r.text and "You agreed" in r.text
        ov = _rows(VerdictOverride)[0]
        assert ov.operator_score == 4.6 and ov.operator_verdict == ov.system_verdict == "apply"
        assert not _rows(Label)  # one record, the one Pipeline and Calibrate read
        r = c.post(
            f"/opportunities/{ids['strong']}/role/view",
            data={
                "mode": "adjust",
                "score": "3",
                "note": "More junior than it reads",
                "dim_role_fit": "4.0",
                "dim_trajectory": "2.5",
                "dim_domain_match": "4.3",
            },
        )
        assert "Your rating is saved" in _toast(r)["message"]
        rows = _rows(VerdictOverride)
        new = rows[-1]
        assert new.operator_score == 3.0 and new.operator_verdict and new.system_score == 4.6
        assert new.reason == "More junior than it reads"
        assert new.dimension_corrections == {
            "scores": {"role_fit": 4.0, "trajectory": 2.5}  # domain_match is unchanged once rounded
        }
        c.post(_toast(r)["undoUrl"])
        assert [x.id for x in _rows(VerdictOverride)] == [ov.id]
        # an Undo token for another role's row deletes nothing
        other = c.post(f"/opportunities/{ids['second']}/role/view/undo/{ov.id}")
        assert other.status_code == 404 and len(_rows(VerdictOverride)) == 1
        bad = c.post(
            f"/opportunities/{ids['strong']}/role/view", data={"mode": "adjust", "score": "9"}
        )
        assert "Pick a number from 1 to 5" in _toast(bad)["message"]
        assert len(_rows(VerdictOverride)) == 1
        none = c.post(f"/opportunities/{ids['unscored']}/role/view", data={"mode": "agree"})
        assert "has not scored this role" in _toast(none)["message"]


def test_a_rating_without_a_note_still_records_why(ids):
    with _client() as c:
        c.post(f"/opportunities/{ids['strong']}/role/view", data={"mode": "adjust", "score": "2"})
    assert "Rated 2 of 5" in _rows(VerdictOverride)[0].reason


def test_a_role_page_view_shows_as_you_said_in_pipeline(ids):
    with _client() as c:
        def pipeline() -> str:
            html = c.get("/opportunities").text
            return html + c.get("/opportunities/not-fit").text

        assert "you said" not in pipeline()
        c.post(f"/opportunities/{ids['strong']}/role/view", data={"mode": "adjust", "score": "3"})
        after = pipeline()
        assert "you said" in after and 'data-mine="' in after
        # and the Role page reads the same record back
        assert "You said" in c.get(f"/opportunities/{ids['strong']}").text


# ---------------------------------------------------------------------------------- the story
def test_a_story_is_saved_as_written_and_can_queue_one_rescore(ids):
    with _client() as c:
        empty = c.post(f"/opportunities/{ids['strong']}/role/story", data={})
        assert "Write at least one" in _toast(empty)["message"]
        r = c.post(
            f"/opportunities/{ids['strong']}/role/story",
            data={
                "situation": "Three sites reported differently.",
                "action": "",
                "result": "Late shipments fell 15%.",
                "rescore": "0",
            },
        )
        assert "Kestrel does not rewrite it" in _toast(r)["message"]
        story = [s for s in _rows(Story) if s.title.startswith("Three sites")][0]
        assert story.operator_edited and story.situation == "Three sites reported differently."
        assert story.result == "Late shipments fell 15%." and story.action is None
        assert not _rows(Task, Task.type == "t2")
        r = c.post(
            f"/opportunities/{ids['strong']}/role/story",
            data={"situation": "S", "action": "A", "result": "R", "rescore": "1"},
        )
        assert "will score this role again" in _toast(r)["message"]
        assert "Stories are not part of scoring yet" in _toast(r)["message"]
        tasks = _rows(Task, Task.type == "t2")
        assert len(tasks) == 1 and tasks[0].payload["opportunity_id"] == ids["strong"]
        again = c.post(
            f"/opportunities/{ids['strong']}/role/story", data={"situation": "S2", "rescore": "1"}
        )
        assert "already waiting" in _toast(again)["message"]
        assert len(_rows(Task, Task.type == "t2")) == 1


# ------------------------------------------------------------------------ online consent
DEST = "backend-x|https://models.example.test/v1|vendor/model-x"


def _fake_online(monkeypatch, employer="hidden"):
    from kestrel.web.pages import role

    async def route(s, settings, tier):
        return {
            "tier": tier,
            "hosted_active": True,
            "model": "vendor/model-x",
            "dest": DEST,
            "provider": "OpenRouter",
            "sends": "the posting and your profile",
            "employer": employer,
        }

    monkeypatch.setattr(role, "online_route", route)


def test_an_online_story_rescore_asks_first_and_is_checked_on_the_server(ids, monkeypatch):
    _fake_online(monkeypatch)
    data = {"situation": "S", "action": "A", "result": "R", "rescore": "1"}
    with _client() as c:
        ask = c.post(f"/opportunities/{ids['strong']}/role/story", data=data)
        assert 'id="rl-dialog"' in ask.text and "OpenRouter" in ask.text
        assert "The posting and your profile" in ask.text and "vendor/model-x" in ask.text
        assert "your current employer is hidden" in ask.text and not _rows(Task, Task.type == "t2")
        assert len(_rows(Story)) == 1  # the seeded one: nothing saved before the confirm
        wrong = c.post(
            f"/opportunities/{ids['strong']}/role/story",
            data={**data, "confirmed": "1", "model": "other/model"},
        )
        assert "changed since the dialog opened" in _toast(wrong)["message"]
        ok = c.post(
            f"/opportunities/{ids['strong']}/role/story",
            data={**data, "confirmed": "1", "model": "vendor/model-x", "dest": DEST},
        )
        assert "will score this role again" in _toast(ok)["message"]
    assert len(_rows(Task, Task.type == "t2")) == 1


def test_the_training_line_is_only_said_of_a_free_model(ids, monkeypatch):
    from kestrel.web.pages import role

    async def route(free):
        async def _r(s, settings, tier):
            return {
                "tier": tier, "model": "vendor/model-x", "dest": DEST, "provider": "OpenRouter",
                "sends": "the posting and your profile", "employer": "hidden", "free": free,
            }
        return _r

    data = {"situation": "S", "rescore": "1"}
    with _client() as c:
        monkeypatch.setattr(role, "online_route", asyncio.run(route(False)))
        paid = c.post(f"/opportunities/{ids['strong']}/role/story", data=data).text
        monkeypatch.setattr(role, "online_route", asyncio.run(route(True)))
        free = c.post(f"/opportunities/{ids['strong']}/role/story", data=data).text
    assert "under its own terms" in paid and "training" not in paid
    assert "free models may use what they receive for training" in free


def test_a_held_send_stays_held_even_with_the_dialog_confirmed(ids, monkeypatch):
    _fake_online(monkeypatch, employer="held")
    with _client() as c:
        ask = c.post(f"/opportunities/{ids['strong']}/role/research", data={"kind": "company"})
        assert "This send will be held" in ask.text
        r = c.post(
            f"/opportunities/{ids['strong']}/role/research",
            data={"kind": "company", "confirmed": "1", "model": "vendor/model-x", "dest": DEST},
        )
        assert "Kestrel will not send this" in _toast(r)["message"]


def test_research_and_contacts_queue_without_running_on_the_request(ids, monkeypatch):
    from kestrel.research import jobs

    calls = []

    async def fake(opp_id):
        calls.append(opp_id)
        return "Research complete for Larkspur Logistics: 2 signal(s) captured.", None

    async def boom(opp_id):
        return None, "Contact discovery failed: nope"

    monkeypatch.setattr(jobs, "research_org", fake)
    monkeypatch.setattr(jobs, "discover_contacts", boom)
    with _client() as c:
        r = c.post(f"/opportunities/{ids['strong']}/role/research", data={"kind": "company"})
        assert "Queued" in _toast(r)["message"] and calls == []
        assert 'id="rl-company"' in r.text and 'id="rl-orgtab"' in r.text
        r = c.post(f"/opportunities/{ids['strong']}/role/research", data={"kind": "contacts"})
        assert "Queued" in _toast(r)["message"]
        assert {t.type for t in _rows(Task)} >= {"org_research", "org_contacts"}


def test_the_old_research_buttons_refuse_an_online_model_without_the_dialog(ids, monkeypatch):
    _fake_online(monkeypatch)
    with _client() as c:
        r = c.post(f"/opportunities/{ids['strong']}/research")
        assert "Nothing was sent" in r.text and "asks first" in r.text
        r = c.post(f"/opportunities/{ids['strong']}/contacts")
        assert "Nothing was sent" in r.text


def test_research_online_approval_is_checked_before_a_task_exists(ids, monkeypatch):
    _fake_online(monkeypatch)
    url = f"/opportunities/{ids['strong']}/role/research"
    with _client() as client:
        ask = client.post(url, data={"kind": "contacts"})
        assert 'data-testid="rl-sends"' in ask.text and "vendor/model-x" in ask.text
        assert not _rows(Task, Task.type.in_(("org_research", "org_contacts")))
        wrong = client.post(url, data={"kind": "contacts", "confirmed": "1", "model": "other"})
        assert "changed since the dialog opened" in _toast(wrong)["message"]
        assert not _rows(Task, Task.type.in_(("org_research", "org_contacts")))
        # The same model name on a different service is not the destination the dialog showed.
        other = client.post(url, data={"kind": "contacts", "confirmed": "1",
                                       "model": "vendor/model-x",
                                       "dest": "other|https://elsewhere.example/v1|vendor/model-x"})
        assert "online service changed" in _toast(other)["message"]
        assert not _rows(Task, Task.type.in_(("org_research", "org_contacts")))
        approved = client.post(url, data={"kind": "contacts", "confirmed": "1",
                                          "model": "vendor/model-x", "dest": DEST})
        assert "Queued" in _toast(approved)["message"]
    tasks = _rows(Task, Task.type == "org_contacts")
    assert len(tasks) == 1 and tasks[0].payload["confirmed_online"] is True


def test_research_poll_and_cancel_are_scoped_to_the_role(ids):
    with _client() as client:
        url = f"/opportunities/{ids['strong']}/role/research"
        client.post(url)
        client.post(url)
        task = _rows(Task, Task.type == "org_research")[0]
        assert len(_rows(Task, Task.type == "org_research")) == 1
        state = client.get(url).text
        assert 'data-state="pending"' in state and "every 2s" in state
        assert "Pat Lee" in state
        client.post(f"/opportunities/{ids['second']}/role/research/{task.id}/cancel")
        assert _rows(Task, Task.id == task.id)[0].status == "pending"
        client.post(f"{url}/{task.id}/cancel")
        state = client.get(url).text
        assert 'data-state="cancelled"' in state and "Cancelled." in state
        assert "every 2s" not in state and "Pat Lee" in state
        assert client.get("/opportunities/999999/role/research").status_code == 404


# ------------------------------------------------------------------------------- documents
def test_documents_open_as_a_scaled_true_page_with_edit_and_download(ids):
    with _client() as c:
        tab = c.get(f"/opportunities/{ids['strong']}/documents").text
        assert "Tailored resume" in tab and "Download PDF" in tab and "No draft yet" in tab
        assert "Kestrel does not draft this yet" in tab
        pv = c.get(f"/opportunities/{ids['strong']}/role/preview/{ids['render']}")
        assert (
            pv.status_code == 200
            and "srcdoc=" in pv.text
            and 'sandbox="allow-same-origin"' in pv.text
        )
        assert (
            f"/renders/{ids['render']}/download" in pv.text
            and f"/renders/{ids['render']}?from=" in pv.text
        )
        assert (
            c.get(f"/opportunities/{ids['second']}/role/preview/{ids['render']}").status_code == 404
        )
        side = c.get(f"/opportunities/{ids['strong']}/role/side").text
        assert "Version 1" in side
        empty = c.get(f"/opportunities/{ids['second']}/documents").text
        assert (
            'data-testid="docs-empty"' in empty and "switch on once you shortlist the role" in empty
        )


def test_organization_tab_is_facts_with_blanks_and_the_company_page_link(ids):
    with _client() as c:
        org = c.get(f"/opportunities/{ids['strong']}/organization").text
        bare = c.get(f"/opportunities/{ids['second']}/organization").text
    assert (
        f"/orgs/{ids['org']}" in org and "Pat Lee" in org and "Opened a third distribution" in org
    )
    assert "Regional carrier with three centers" not in org  # the Company page owns the summary
    assert "Transportation and logistics" in org and "Austin, TX" in org
    assert "Not known" in bare and "Not researched yet" in bare


def test_history_lists_found_screen_scored_shortlisted_and_rating(ids):
    with _client() as c:
        c.post(f"/opportunities/{ids['strong']}/role/shortlist")
        c.post(f"/opportunities/{ids['strong']}/role/view", data={"mode": "agree"})
        h = c.get(f"/opportunities/{ids['strong']}/history").text
    assert "Scored 4.6 by Full review" in h and "qwen3:14b · prompt version evaluate/v7" in h
    assert "Found on Indeed" in h and "Added to my shortlist" in h or "shortlist" in h.lower()
    assert "Label (manual)" in h or "rated" in h.lower()


def test_no_em_dash_and_no_external_send_in_the_page(ids):
    with _client() as c:
        for path in ("", "/posting", "/organization", "/documents", "/history", "/role/side"):
            html = c.get(f"/opportunities/{ids['strong']}{path}").text
            assert "—" not in html.split("<script")[0]
    # The page offers drafts and hand-over links only: nothing named apply, submit or send.
    assert "Submit" not in html and "Apply now" not in html


def test_remove_from_more_needs_the_server_side_confirm(ids):
    with _client() as c:
        c.post(f"/opportunities/{ids['second']}/role/shortlist")
        refused = c.post(f"/opportunities/{ids['second']}/role/remove")
        assert "Nothing was removed" in _toast(refused)["message"]
        assert _toast(refused)["tone"] == "warn"
        assert len(_rows(Pursuit, Pursuit.opportunity_id == ids["second"])) == 1
        page = c.get(f"/opportunities/{ids['second']}/role/side").text
        ok = c.post(f"/opportunities/{ids['second']}/role/remove", data={"confirmed": "1"})
        assert "Nothing was removed" not in _toast(ok)["message"]
        assert not _rows(Pursuit, Pursuit.opportunity_id == ids["second"])
    assert page is not None


def test_no_legacy_panel_still_posts_research_to_the_old_routes(ids):
    for tpl in ("opp_decision.html", "opp_actions.html"):
        src = open(
            f"kestrel/web/templates/partials/{tpl}", encoding="utf-8"
        ).read()
        assert "/research" not in src and "/contacts" not in src, tpl
        assert "/organization" in src, tpl
