"""The Applications screen (D-UI-100): what each mockup state renders, and every write behind it
(panel, priority, notes, reminders, follow-ups, outcome capture, Undo). Offline: the database is
the test's own, seeded by tests/apps_seed.py, and nothing here reaches a network or a model."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db.engine import make_async_engine
from kestrel.db.models import Outcome, Pursuit, Setting, StatusEvent
from kestrel.services import pursuit_board as pb
from kestrel.web import app as app_module
from tests import apps_seed

pytestmark = pytest.mark.usefixtures("_isolate_data_home")
HX = {"HX-Request": "true"}


def _client(track: str = "employment", both: bool = False):
    ids = apps_seed.seed(track, both)
    return TestClient(app_module.build_app(worker=False)), ids


def _run(fn):
    async def _go():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                return await fn(s)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _pursuit(pid: int):
    async def _go(s):
        p = await s.get(Pursuit, pid)
        ev = (
            await s.execute(
                select(StatusEvent).where(StatusEvent.pursuit_id == pid).order_by(StatusEvent.id)
            )
        ).scalars().all()
        return p, ev

    return _run(_go)


def _toast(resp) -> dict:
    return json.loads(resp.headers["HX-Trigger"])["k-toast"]


def _card(html: str, pid: int) -> str:
    """The HTML of one board card."""
    return html.split(f'data-testid="pcard" data-id="{pid}"', 1)[1].split("</article>", 1)[0]


# -------------------------------------------------------------------------- the card (mockup)
def test_a_card_shows_monogram_fit_priority_quiet_next_step_stage_and_one_button():
    c, ids = _client()
    with c:
        html = c.get("/pursuits").text
    card = _card(html, ids["Senior Operations Analyst"])
    assert ">LL<" in card and "Larkspur Logistics" in card
    assert "Fit 4.6" in card and "High priority" in card
    assert "12 days quiet" in card  # applied 12 days ago, follow-up 5 days late
    assert "Follow up" in card and "was due" in card
    assert 'data-stage-menu' in card and ">Heard back?</button>" in card
    # No location or pay line on the card (they are in the panel).
    assert "Austin" not in card and "$118k" not in card
    # An unscored role says so, never a dash.
    assert card.count("data-step-kind") == 1


def test_a_role_with_no_evaluation_reads_being_scored():
    c, ids = _client()

    async def drop(s):
        from kestrel.db.models import Evaluation

        for e in (await s.execute(select(Evaluation))).scalars():
            await s.delete(e)
        await s.commit()

    _run(drop)
    with c:
        html = c.get("/pursuits").text
    assert "Being scored" in _card(html, ids["Senior Data Analyst"])
    assert "Fit " not in _card(html, ids["Senior Data Analyst"]).replace("Fit score", "")


def test_the_next_step_button_per_stage_for_both_tracks():
    emp = {s: pb.next_step("employment", s) for s in PURSUIT_STATUS_EMP}
    assert emp["shortlisted"].label == "I applied" and emp["shortlisted"].kind == "form"
    assert emp["applied"].label == "Heard back?" and emp["applied"].kind == "menu"
    assert emp["interviewing"].label == "What happened?" and emp["offer"].label == "Record outcome"
    assert emp["rejected"] is None and emp["ghosted"] is None and emp["accepted"] is None
    stages = ("shortlisted", "outreach_sent", "conversation", "proposal_sent", "won", "lost")
    con = {s: pb.next_step("consulting", s) for s in stages}
    assert con["shortlisted"].label == "Reached out" and con["shortlisted"].kind == "act"
    assert con["outreach_sent"].label == "They replied?"
    assert con["conversation"].label == "Proposal sent" and con["proposal_sent"].label == (
        "What happened?")
    assert con["won"].label == "Record outcome" and con["lost"] is None


PURSUIT_STATUS_EMP = ("shortlisted", "preparing", "applied", "screening", "interviewing", "final",
                      "offer", "accepted", "rejected", "withdrawn", "ghosted")


def test_closed_cards_fold_into_the_group_and_offers_stay_in_the_column():
    c, ids = _client()
    with c:
        html = c.get("/pursuits").text
    last = html.split('data-col-label="Offer / Closed"', 1)[1]
    open_part, folded = last.split('class="ap-closedg"', 1)
    assert "Business Operations Manager" in open_part  # an offer is still being acted on
    for title in ("IT Systems Manager", "Customer Operations Lead", "Inventory Planning Manager"):
        assert title in folded and title not in open_part
    assert "Closed · 3" in html
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    assert "8 open · 3 closed · " in text and "2 need a follow-up" in text


def test_the_page_has_no_lede_and_the_filters_the_spec_names():
    c, _ = _client()
    with c:
        html = c.get("/pursuits").text
    assert "Kestrel never applies for you" not in html
    for label in ("Fit score", "Priority", "Work type", "Company", "Added", "Follow-ups",
                  "Updated", "Per column"):
        assert f">{label}<" in html or f"{label}<select" in html or f"{label}<input" in html, label
    assert "Import past applications" in html and "/settings#applied" in html
    assert "Add a role I found elsewhere" in html and 'id="ap-add"' in html


def test_the_page_is_converted_to_the_shared_design_system():
    c, _ = _client()
    with c:
        html = c.get("/pursuits").text
    assert "/static/css/pages/applications.css" in html and "/static/js/applications.js" in html
    assert "/static/css/kestrel.css" not in html  # the legacy sheet is gone
    assert 'class="k-scene"' in html or "k-scene" in html


# ------------------------------------------------------------------------------- the right rail
def test_the_rail_lists_upcoming_interviews_and_follow_ups_due_now_and_later():
    c, ids = _client()
    with c:
        html = c.get("/pursuits").text
    rail = html.split('id="apps-rail"', 1)[1]
    assert "Upcoming interviews" in rail and "Director of Enterprise Applications" in rail
    assert "2:00 PM · Video call" in rail and "2:00 PM · On site" in rail
    assert "/prep/" in rail and ">Prepare<" in rail
    due_now = rail.split('data-testid="rail-followups"', 1)[1]
    assert "Finish the cover letter" in due_now and "12 days quiet" in due_now
    assert "Process Improvement Lead" in due_now  # later this week, folded away
    assert 'data-fu="later" hidden' in due_now
    assert "3 more later this week" in due_now or "1 more later this week" in due_now


def test_the_rail_says_so_when_nothing_is_scheduled_or_due():
    c, _ = _client()
    with c:
        # The first run seeds nothing for consulting: a switched-off track still renders.
        html = c.get("/pursuits?track=consulting").text
    assert "Nothing scheduled" in html and "Nothing due now" in html


def test_a_consulting_board_says_calls_and_leads():
    c, ids = _client("consulting")
    with c:
        html = c.get("/pursuits?track=consulting").text
    assert "Upcoming calls" in html and ">Leads</h1>" in html
    assert "KPI framework refresh" in html.split('id="apps-rail"', 1)[1]
    assert "Add a lead I found elsewhere" in html


# ---------------------------------------------------------------------------------- the panel
def test_the_panel_holds_stage_next_step_record_buttons_documents_notes_history_and_remove():
    c, ids = _client()
    pid = ids["Senior Operations Analyst"]
    with c:
        html = c.get(f"/pursuits/{pid}/panel").text
        assert c.get("/pursuits/999999/panel").status_code == 404
    assert "Senior Operations Analyst" in html and "Larkspur Logistics" in html
    assert "Austin, TX" in html and "$118k to $142k" in html  # location and pay live here
    assert "Next: Follow up" in html and "12 days with no update" in html
    for label in ("Heard back", "No reply yet", "Interview scheduled"):
        assert f">{label}</button>" in html
    for part in ("Documents", "Notes", "History", "Remove", "Open full role", "Show new listings"):
        assert part in html
    assert 'data-testid="panel-history"' in html and "You applied" in html


def test_the_panels_inline_forms_each_open_from_a_record_button():
    c, ids = _client()
    shortlisted = ids["Product Operations Manager"]
    applied = ids["Senior Operations Analyst"]
    offer = ids["Business Operations Manager"]
    with c:
        form = c.get(f"/pursuits/{shortlisted}/panel?form=applied").text
        assert 'data-testid="form-applied"' in form and 'name="follow_up"' in form
        assert (date.today() + timedelta(days=7)).isoformat() in form
        assert "It never contacts anyone for you" in form
        iv = c.get(f"/pursuits/{applied}/panel?form=interview").text
        assert 'name="when"' in iv and 'name="at_time"' in iv and 'name="kind"' in iv
        out = c.get(f"/pursuits/{offer}/panel?form=outcome").text
    assert 'name="final_status"' in out and "Accepted" in out and "Declined" in out
    assert "Not selected" in out and "No reply" in out and "Days to first reply" in out


def test_a_closed_card_panel_shows_how_it_ended_and_offers_edit_outcome():
    c, ids = _client()
    pid = ids["IT Systems Manager"]
    with c:
        html = c.get(f"/pursuits/{pid}/panel").text
    assert "Not selected" in html and "Furthest stage: Screening" in html
    assert "first reply after 9 days" in html and ">Edit outcome</button>" in html


def test_notes_save_and_come_back():
    c, ids = _client()
    pid = ids["Senior Data Analyst"]
    with c:
        assert c.post(f"/pursuits/{pid}/notes", data={"notes": "Ask about the team"}
                      ).status_code == 204
        assert "Ask about the team" in c.get(f"/pursuits/{pid}/panel").text
        assert c.post("/pursuits/999999/notes", data={"notes": "x"}).status_code == 404
    assert _pursuit(pid)[0].notes_md == "Ask about the team"


# ----------------------------------------------------------------------------- Undo for changes
def test_a_stage_change_toasts_with_undo_and_undo_keeps_the_history():
    c, ids = _client()
    pid = ids["Senior Data Analyst"]  # preparing
    with c:
        r = c.post(f"/pursuits/{pid}/status", data={"to_status": "screening"}, headers=HX)
        toast = _toast(r)
        assert toast["message"] == "Moved to Screening." and "/pursuits/undo/" in toast["undoUrl"]
        before_events = len(_pursuit(pid)[1])
        assert c.post(toast["undoUrl"]).status_code == 200
        p, events = _pursuit(pid)
        assert p.status == "preparing" and p.closed_at is None
        # Nothing is deleted: the undo is one more event.
        assert len(events) == before_events + 1
        assert events[-1].to_status == "preparing" and events[-1].note == "Undid the last change"
        assert "no longer be undone" in c.post(toast["undoUrl"]).text  # used up


def test_a_failed_move_is_a_notice_with_no_toast_and_nothing_to_undo():
    c, ids = _client()
    pid = ids["Senior Data Analyst"]
    with c:
        r = c.post(f"/pursuits/{pid}/status", data={"to_status": "won"}, headers=HX)
    assert "HX-Trigger" not in r.headers and 'data-testid="board-notice"' in r.text
    assert "ap-notice" in r.text  # a failure is visible, not read out only


def test_every_verb_toasts_what_moved_and_undo_restores_the_card():
    c, ids = _client()
    pid = ids["Product Operations Manager"]  # shortlisted, next action Apply
    with c:
        before = _pursuit(pid)[0]
        snap = (before.status, before.next_action, before.next_action_at, before.applied_at)
        r = c.post(f"/pursuits/{pid}/act/applied", data={}, headers=HX)
        toast = _toast(r)
        due = (date.today() + timedelta(days=7)).strftime("%b ") + str((date.today() + timedelta(
            days=7)).day)
        assert toast["message"] == f"Moved to Applied. Follow up on {due}."
        assert _pursuit(pid)[0].status == "applied"
        c.post(toast["undoUrl"])
    after = _pursuit(pid)[0]
    assert (after.status, after.next_action, after.next_action_at, after.applied_at) == snap


def test_interview_scheduled_stores_the_kind_and_lands_in_upcoming_interviews():
    c, ids = _client()
    pid = ids["Senior Operations Analyst"]
    when = date.today() + timedelta(days=5)
    with c:
        r = c.post(f"/pursuits/{pid}/act/interview", headers=HX, data={
            "when": when.isoformat(), "at_time": "10:00", "kind": "Phone screen"})
        assert "10:00 AM" in _toast(r)["message"] and "Upcoming interviews" in _toast(r)["message"]
        html = c.get("/pursuits").text
    p, _ = _pursuit(pid)
    assert p.status == "interviewing" and p.next_action == "Interview (Phone screen)"
    assert p.next_action_at == f"{when.isoformat()}T10:00"
    assert "10:00 AM · Phone screen" in html.split('id="apps-rail"', 1)[1]


def test_a_consulting_call_is_recorded_like_an_interview():
    c, ids = _client("consulting")
    pid = ids["Order-to-cash process documentation"]  # outreach_sent
    when = date.today() + timedelta(days=2)
    with c:
        r = c.post(f"/pursuits/{pid}/act/call", headers=HX, data={
            "when": when.isoformat(), "kind": "Discovery call"})
        assert "Upcoming calls" in _toast(r)["message"]
    p, _ = _pursuit(pid)
    assert p.status == "conversation" and p.next_action == "Call (Discovery call)"
    assert pb.is_scheduled(p)


def test_a_status_event_is_written_for_the_card_menu_a_button_and_a_drag():
    c, ids = _client()
    a, b, d = (ids["Senior Data Analyst"], ids["Product Operations Manager"],
               ids["Process Improvement Lead"])
    with c:
        c.post(f"/pursuits/{a}/status", data={"to_status": "applied"}, headers=HX)  # menu
        c.post(f"/pursuits/{b}/act/applied", data={}, headers=HX)  # button
        c.post(f"/pursuits/{d}/status", data={"to_status": "interviewing"})  # drop
    for pid, status in ((a, "applied"), (b, "applied"), (d, "interviewing")):
        p, events = _pursuit(pid)
        assert p.status == status and events[-1].to_status == status


# ----------------------------------------------------------------------------------- priority
def test_high_priority_toggles_from_the_menu_with_undo():
    c, ids = _client()
    pid = ids["Senior Data Analyst"]
    with c:
        r = c.post(f"/pursuits/{pid}/priority", headers=HX)
        assert _toast(r)["message"] == "Marked high priority."
        assert _pursuit(pid)[0].priority == 1
        assert "High priority" in _card(r.text, pid)
        c.post(_toast(r)["undoUrl"])
        assert _pursuit(pid)[0].priority == 0
        r2 = c.post(f"/pursuits/{ids['Product Operations Manager']}/priority", headers=HX)
        assert _toast(r2)["message"] == "High priority removed."
        assert c.post("/pursuits/999999/priority").status_code == 404


# ---------------------------------------------------------------------------------- follow-ups
def test_ticking_a_follow_up_clears_the_reminder_and_undo_brings_it_back():
    c, ids = _client()
    pid = ids["Senior Data Analyst"]
    with c:
        before = _pursuit(pid)[0]
        keep = (before.next_action, before.next_action_at)
        r = c.post(f"/pursuits/{pid}/done", headers=HX)
        assert _toast(r)["message"] == "Marked done."
        p, _ = _pursuit(pid)
        assert p.next_action is None and p.next_action_at is None
        assert "Finish the cover letter" not in r.text.split('data-testid="rail-followups"')[1]
        c.post(_toast(r)["undoUrl"])
    p, _ = _pursuit(pid)
    assert (p.next_action, p.next_action_at) == keep


def test_add_a_reminder_sets_the_next_action_and_its_date_only():
    c, ids = _client()
    pid = ids["Process Improvement Lead"]
    with c:
        r = c.post("/pursuits/reminder", headers=HX, data={
            "pursuit_id": str(pid), "what": "Email the recruiter", "days": "3"})
        assert _toast(r)["message"] == "Reminder set for this week."
        p, events = _pursuit(pid)
        assert p.next_action == "Email the recruiter"
        assert p.next_action_at == (date.today() + timedelta(days=3)).isoformat()
        assert p.status == "applied"  # a reminder never moves the card
        blank = c.post("/pursuits/reminder", headers=HX, data={"pursuit_id": str(pid), "days": "0"})
        assert _pursuit(pid)[0].next_action == "Follow up"  # a blank action says Follow up
        bad = c.post("/pursuits/reminder", headers=HX, data={"pursuit_id": "999999"})
        assert "Choose which application" in bad.text and "HX-Trigger" not in bad.headers
        assert blank.status_code == 200
    assert len(events) == len(_pursuit(pid)[1])


# ---------------------------------------------------------------------------- outcome capture
def test_outcome_capture_writes_and_updates_one_row_and_never_sends_anything():
    c, ids = _client()
    pid = ids["Business Operations Manager"]  # offer
    with c:
        r = c.post(f"/pursuits/{pid}/outcome", headers=HX, data={
            "final_status": "accepted", "furthest_stage": "offer",
            "days_to_first_response": "6", "rejection_reason": "Good fit"})
        assert _toast(r)["message"] == "Outcome saved. Kestrel learns from it."
        again = c.post(f"/pursuits/{pid}/outcome", headers=HX, data={
            "final_status": "withdrawn", "furthest_stage": "offer", "rejection_reason": "Comp"})
        assert again.status_code == 200

    async def rows(s):
        return (await s.execute(select(Outcome).where(Outcome.pursuit_id == pid))).scalars().all()

    out = _run(rows)
    assert len(out) == 1  # edited, not duplicated
    assert (out[0].final_status, out[0].rejection_reason) == ("withdrawn", "Comp")
    assert out[0].days_to_first_response is None
    p, events = _pursuit(pid)
    assert p.status == "withdrawn" and p.outcome == "withdrawn" and p.closed_at
    assert [e.to_status for e in events][-2:] == ["accepted", "withdrawn"]  # full history kept


def test_undoing_an_outcome_removes_it_and_restores_the_stage():
    c, ids = _client()
    pid = ids["Business Operations Manager"]
    with c:
        r = c.post(f"/pursuits/{pid}/outcome", headers=HX, data={
            "final_status": "rejected", "furthest_stage": "offer"})
        c.post(_toast(r)["undoUrl"])

    async def rows(s):
        return len((await s.execute(select(Outcome).where(Outcome.pursuit_id == pid))).all())

    p, _ = _pursuit(pid)
    assert p.status == "offer" and p.outcome is None and p.closed_at is None
    assert _run(rows) == 0


def test_an_outcome_status_the_track_does_not_have_is_refused():
    c, ids = _client()
    with c:
        r = c.post(f"/pursuits/{ids['Business Operations Manager']}/outcome",
                   data={"final_status": "won"})
    assert r.status_code == 400


# ------------------------------------------------------------------------- add a role (toast)
def test_adding_a_role_toasts_and_marks_the_new_card():
    c, _ = _client()
    text = "Operations Analyst, Fulfillment\n\nOwn the weekly reporting for the warehouse. " * 6
    with c:
        r = c.post("/pursuits/add", data={"text": text, "org_name": "Quarry Lane",
                                          "track": "employment"}, headers=HX)
    assert _toast(r)["message"] == "Added to To apply. Kestrel is scoring it now."
    assert 'data-new="1"' in r.text and ">New<" in r.text and "undoUrl" not in json.dumps(
        _toast(r))


def test_a_failed_add_is_a_visible_notice_for_the_sheet_to_show():
    c, _ = _client()
    with c:
        r = c.post("/pursuits/add", data={"track": "employment"}, headers=HX)
    assert "HX-Trigger" not in r.headers
    assert 'class="ap-notice"' in r.text and "Paste the posting text or a link" in r.text


# ---------------------------------------------------------------------------- limits (#30)
def test_range_and_cap_are_saved_and_the_closed_state_is_url_only():
    c, _ = _client()
    with c:
        assert c.post("/pursuits/prefs", data={"range": "30", "cap": "10"}).status_code == 200
        html = c.get("/pursuits").text
        assert 'data-range="30"' in html and 'data-cap="10"' in html
        assert 'data-open-closed="1"' in c.get("/pursuits?open=closed").text

    async def keys(s):
        return {r.key for r in (await s.execute(select(Setting))).scalars()}

    assert {"ui.board_range", "ui.board_cap"} <= _run(keys)


# ----------------------------------------------------------------- the table view (every pursuit)
def test_the_table_lists_every_pursuit_with_stage_next_step_due_and_fit():
    c, ids = _client()
    with c:
        html = c.get("/pursuits?view=table").text
    assert html.count('data-testid="prow"') == len(ids)
    row = html.split(f'data-testid="prow" data-id="{ids["Senior Operations Analyst"]}"', 1)[1]
    row = row.split("</tr>", 1)[0]
    assert "Applied" in row and "Follow up" in row and "12 days quiet" in row and "4.6" in row


# ------------------------------------------------------------------------------- static guards
_STATIC = Path(paths.web_static_dir())
_TEMPLATES = Path(paths.web_templates_dir())


def test_the_screens_own_sheet_uses_tokens_only_and_no_em_dash():
    css = (_STATIC / "css" / "pages" / "applications.css").read_text(encoding="utf-8")
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert not re.search(r"#[0-9A-Fa-f]{3,8}\b", bare), "hex color in applications.css"
    assert not re.search(r"\b(?:rgb|rgba|hsl|hsla)\(", bare)
    assert "—" not in css


def test_the_screens_files_never_send_and_never_say_send():
    files = [
        _STATIC / "js" / "applications.js",
        *(_TEMPLATES / "partials").glob("pursuit_*.html"),
        _TEMPLATES / "pursuits.html",
    ]
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert "—" not in text, f
        assert not re.search(r"\bSend\b", text), f"the word Send in {f.name}"
    js = (_STATIC / "js" / "applications.js").read_text(encoding="utf-8")
    assert "mailto:" not in js and "XMLHttpRequest" not in js
    # Every request the script makes goes to this app.
    assert "http://" not in js and "https://" not in js
    assert re.findall(r"fetch\(\s*['\"]([^'\"]+)", js) == ["/pursuits/prefs"]


def test_the_card_never_carries_the_employer_in_confidential_mode():
    from tests.test_pursuit_filters import _EMPLOYER
    from tests.test_pursuit_filters import _client as filters_client

    with filters_client(confidential=True) as c:
        board = c.get("/pursuits").text
        panel_ids = re.findall(r'data-testid="pcard" data-id="(\d+)"', board)
        panels = " ".join(c.get(f"/pursuits/{i}/panel").text for i in panel_ids)
        table = c.get("/pursuits?view=table").text
    for blob in (board, panels, table):
        assert _EMPLOYER not in blob


def test_an_action_with_the_panel_open_answers_with_the_panel_and_the_rail_out_of_band():
    c, ids = _client()
    pid = ids["Senior Data Analyst"]
    with c:
        r = c.post(f"/pursuits/{pid}/status", headers=HX,
                   data={"to_status": "applied", "panel": str(pid)})
        closed = c.post(f"/pursuits/{pid}/status", headers=HX, data={"to_status": "screening"})
    assert 'id="ap-panel-in"' in r.text and f'data-panel-id="{pid}"' in r.text
    assert 'id="apps-rail"' in r.text and 'id="apps-counts"' in r.text
    # The board is the swap target itself, never marked out of band.
    assert 'id="apps-board" class="ap-boardwrap"' in r.text
    assert 'hx-swap-oob' not in r.text.split('id="apps-board"', 1)[1].split(">", 1)[0]
    assert 'id="ap-panel-in"' not in closed.text  # no panel asked for, none sent


def test_a_typed_reminder_that_starts_with_call_is_a_follow_up_not_an_interview():
    c, ids = _client()
    pid = ids["Process Improvement Lead"]  # applied
    with c:
        c.post("/pursuits/reminder", headers=HX, data={
            "pursuit_id": str(pid), "what": "Call recruiter", "days": "3"})
        html = c.get("/pursuits").text
    rail = html.split('id="apps-rail"', 1)[1]
    interviews, followups = rail.split('data-testid="rail-followups"', 1)
    assert "Call recruiter" not in interviews and "Call recruiter" in followups
    p, _ = _pursuit(pid)
    assert not pb.is_scheduled(p)


def test_only_a_result_status_can_close_a_card_and_a_gone_card_is_a_notice():
    c, ids = _client()
    pid = ids["Senior Operations Analyst"]  # applied
    with c:
        r = c.post(f"/pursuits/{pid}/outcome", headers=HX, data={"final_status": "screening"})
        assert "Choose how it ended" in r.text and "HX-Trigger" not in r.headers
        assert c.post(f"/pursuits/{pid}/outcome", data={"final_status": "applied"}
                      ).status_code == 400
        gone = c.post("/pursuits/999999/status", headers=HX, data={"to_status": "applied"})
        assert gone.status_code == 200 and "no longer on the board" in gone.text
        assert c.post("/pursuits/999999/status", data={"to_status": "applied"}).status_code == 404
    p, events = _pursuit(pid)
    assert p.status == "applied" and p.closed_at is None


def test_editing_an_outcome_then_undoing_restores_the_earlier_one():
    c, ids = _client()
    pid = ids["IT Systems Manager"]  # rejected, with a seeded outcome (screening, 9 days)
    with c:
        r = c.post(f"/pursuits/{pid}/outcome", headers=HX, data={
            "final_status": "rejected", "furthest_stage": "final",
            "days_to_first_response": "2", "rejection_reason": "Changed my mind"})
        c.post(_toast(r)["undoUrl"])

    async def rows(s):
        return (await s.execute(select(Outcome).where(Outcome.pursuit_id == pid))).scalars().all()

    out = _run(rows)
    assert len(out) == 1
    assert (out[0].furthest_stage, out[0].days_to_first_response) == ("screening", 9)


def test_confidential_mode_hides_the_employer_in_typed_text_everywhere():
    from tests.test_pursuit_filters import _EMPLOYER
    from tests.test_pursuit_filters import _client as filters_client

    with filters_client(confidential=True) as c:
        board = c.get("/pursuits").text
        ids = re.findall(r'data-testid="pcard" data-id="(\d+)"', board)
        c.post("/pursuits/reminder", headers=HX, data={
            "pursuit_id": ids[0], "what": f"Ask {_EMPLOYER} about the team", "days": "0"})
        c.post(f"/pursuits/{ids[0]}/outcome", headers=HX, data={
            "final_status": "rejected", "rejection_reason": f"{_EMPLOYER} hired inside"})
        blobs = [c.get("/pursuits").text, c.get("/pursuits?view=table").text]
        blobs += [c.get(f"/pursuits/{i}/panel").text for i in ids]
        blobs.append(c.get(f"/pursuits/{ids[0]}/panel?form=outcome").text)
    for blob in blobs:
        assert _EMPLOYER not in blob


def test_the_folded_groups_are_out_of_the_tab_order_while_closed():
    css = (_STATIC / "css" / "pages" / "applications.css").read_text(encoding="utf-8")
    assert ".ap-closedg:not(.is-open) .ap-cgi { visibility: hidden" in css
    assert ".ap-mf:not(.is-open) > .ap-mfi { visibility: hidden" in css


# ------------------------------------------------------------------------------ review fixes
def test_the_search_box_placeholder_is_the_mockup_text_and_slash_still_focuses_it():
    c, _ids = _client()
    with c:
        html = c.get("/pursuits").text
    assert 'placeholder="Search titles and companies"' in html
    assert "( / )" not in html
    assert "e.key === '/'" in Path("kestrel/web/static/js/applications.js").read_text()


def test_undoing_an_older_toast_does_not_revert_later_changes():
    c, ids = _client()
    pid = ids["Senior Data Analyst"]
    with c:
        first = _toast(c.post(f"/pursuits/{pid}/priority", headers=HX))
        c.post(f"/pursuits/{pid}/status", data={"to_status": "screening"}, headers=HX)
        c.post(f"/pursuits/{pid}/notes", data={"notes": "kept note"})
        c.post(first["undoUrl"])
    p, _ev = _pursuit(pid)
    assert p.priority == 0  # the priority change was undone
    assert p.status == "screening" and p.notes_md == "kept note"  # later changes stay


def test_an_expired_undo_says_so_in_plain_words():
    c, _ids = _client()
    with c:
        r = c.post("/pursuits/undo/not-a-real-token")
    assert r.status_code == 200
    assert "This can no longer be undone." in r.text and 'data-ok="0"' in r.text


def test_a_failed_form_submit_leaves_the_open_panel_alone_so_typing_is_kept():
    c, ids = _client()
    pid = ids["Senior Data Analyst"]
    with c:
        r = c.post(
            f"/pursuits/{pid}/act/applied",
            data={"panel": str(pid), "applied_on": "someday"}, headers=HX,
        )
        assert "not a valid date" in r.text
        assert 'id="ap-panel-in"' not in r.text  # no panel swap, so the form is not reset
        ok = c.post(
            f"/pursuits/{pid}/act/applied", data={"panel": str(pid)}, headers=HX
        )
        assert 'id="ap-panel-in"' in ok.text


def test_undoing_a_remove_refreshes_the_sidebar_badges():
    c, ids = _client()
    pid = ids["Senior Data Analyst"]
    with c:
        r = c.post(f"/pursuits/{pid}/remove", headers=HX)
        undo = json.loads(r.headers["HX-Trigger"])["k-toast"]["undoUrl"]
        out = c.post(undo)
    assert 'hx-swap-oob="true"' in out.text and "nav-count-" in out.text


def test_the_rail_lists_interviews_earliest_date_first():
    c, _ids = _client()
    with c:
        rail = c.get("/pursuits").text.split('data-testid="rail-interviews"', 1)[1]
        rail = rail.split('data-testid="rail-followups"', 1)[0]
    days = re.findall(r"[A-Z]{3}\s*</?[^>]*>\s*(\d{1,2})", rail)
    assert days == sorted(days, key=int) or len(days) < 2
