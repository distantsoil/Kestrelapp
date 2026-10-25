"""The Applications / Leads board (D-UI-46): four columns per track, the stage select, the verb
buttons ("Applied?"), add-a-role by paste or URL, overdue ordering, nav label per track, and the
table view. Offline: the URL fetch is respx-mocked, never live."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import make_async_engine, reset_engine
from kestrel.db.models import (
    DocMaster,
    DocRender,
    Opportunity,
    Pursuit,
    StatusEvent,
    Task,
)
from kestrel.services import pursuit_board as pb
from kestrel.services.tracks import forget_cache
from kestrel.web import app as app_module
from kestrel.web.deps import NAV_GROUPS, nav_title

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


def _run(fn):
    """Run `fn(session)` on a throwaway engine (never the app's cached one: cross-loop trap)."""

    async def _go():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                return await fn(s)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


async def _both_tracks(s):
    from kestrel.services.tracks import BOTH, save_enabled

    await save_enabled(s, BOTH)
    await s.commit()


def _fresh(consulting: bool = False) -> TestClient:
    """A clean database. A new install is employment-only, so consulting is turned on only by
    the tests about the Leads board."""
    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_cache()
    if consulting:
        _run(_both_tracks)
    return TestClient(app_module.build_app(worker=False))


async def _seed_one(s, *, track="employment", status="shortlisted", title="Head of IT", **kw):
    opp = Opportunity(
        track=track, opp_type="job_posting" if track == "employment" else "engagement",
        title=title, org_name="Acme", url=f"https://x.example/{title}", stage="t2_done",
    )
    s.add(opp)
    await s.flush()
    p = Pursuit(opportunity_id=opp.id, track=track, status=status, **kw)
    s.add(p)
    await s.commit()
    return opp.id, p.id


def _seed(**kw) -> tuple[int, int]:
    return _run(lambda s: _seed_one(s, **kw))


def _pursuit(pid: int):
    async def _go(s):
        p = await s.get(Pursuit, pid)
        ev = (
            await s.execute(select(StatusEvent).where(StatusEvent.pursuit_id == pid).order_by(
                StatusEvent.id))
        ).scalars().all()
        return p, ev

    return _run(_go)


def _column(html: str, label: str) -> str:
    """The HTML of one board column, by heading."""
    parts = html.split('data-testid="apps-col"')
    return next(p for p in parts[1:] if f'data-col-label="{label}"' in p.split(">", 1)[0])


# -------------------------------------------------------------------------- columns per track
def test_status_columns_cover_every_status_exactly_once():
    from kestrel.db.enums import PURSUIT_STATUS

    for track, cols in pb.COLUMNS.items():
        flat = [st for c in cols for st in c.statuses]
        assert sorted(flat) == sorted(PURSUIT_STATUS[track])
        assert len(cols) == 4


def test_board_has_four_employment_columns_and_places_cards():
    c = _fresh()
    _seed(status="preparing", title="Prep role")
    _seed(status="screening", title="Screen role")
    _seed(status="final", title="Final role")
    _seed(status="ghosted", title="Ghost role")
    with c:
        html = c.get("/pursuits").text
    for label in ("To apply", "Applied", "Interviewing", "Offer / Closed"):
        assert f'data-col-label="{label}"' in html
    assert "Prep role" in _column(html, "To apply")
    assert "Screen role" in _column(html, "Applied")
    assert "Final role" in _column(html, "Interviewing")
    assert "Ghost role" in _column(html, "Offer / Closed")
    # D-UI-100: the page has no lede line; the promise lives in Help and About.
    assert "Kestrel never applies for you" not in html and 'data-testid="never-applies"' not in html
    assert "<h1" in html and ">Applications</h1>" in html


def test_board_has_four_consulting_columns():
    c = _fresh(consulting=True)
    _seed(track="consulting", status="researching", title="Lead A")
    _seed(track="consulting", status="proposal_sent", title="Lead B")
    with c:
        html = c.get("/pursuits?track=consulting").text
    for label in ("To contact", "In conversation", "Proposal", "Won / Closed"):
        assert f'data-col-label="{label}"' in html
    assert "Lead A" in _column(html, "To contact")
    assert "Lead B" in _column(html, "Proposal")
    assert ">Leads</h1>" in html and "Reached out" not in _column(html, "Proposal")


def test_leads_board_uses_consulting_words_and_employment_is_unchanged():
    """D-UI-54: the first column is "To contact", the button "Reached out" (green "Reached out"
    once pressed); employment keeps "To apply" and "I applied"."""
    c = _fresh(consulting=True)
    _, lead = _seed(track="consulting", status="shortlisted", title="Lead")
    _seed(status="shortlisted", title="Role")
    with c:
        leads = c.get("/pursuits?track=consulting").text
        jobs = c.get("/pursuits").text
        assert 'data-col-label="To contact"' in leads and 'data-col-label="To apply"' not in leads
        assert 'data-step-key="applied"' in _column(leads, "To contact")
        assert ">Reached out</button>" in _column(leads, "To contact")
        assert "I applied" not in leads and "Add a lead I found elsewhere" in leads
        assert "Import past leads" in leads
        assert 'data-col-label="To apply"' in jobs
        assert ">I applied</button>" in _column(jobs, "To apply")
        assert "Reached out</button>" not in jobs and "Add a role I found elsewhere" in jobs
        done = c.post(f"/pursuits/{lead}/act/applied/quick", data={}).text
        assert "✓ Reached out" in done and "Applied" not in done
    assert pb.done_label(pb.VERB_LABELS["consulting"]["applied"]) == "✓ Reached out"
    assert pb.done_label(pb.VERB_LABELS["employment"]["applied"]) == "✓ Applied"
    assert pb.stage_name("shortlisted") == "My shortlist"
    assert pb.stage_name("shortlisted", "consulting") == "To contact"
    assert pb.stage_label("shortlisted") == "Shortlisted"


def test_table_view_still_renders():
    c = _fresh()
    _seed(status="applied", title="Table role")
    with c:
        html = c.get("/pursuits?view=table").text
    assert 'data-testid="apps-table"' in html and "Table role" in html
    assert 'data-testid="apps-board-grid"' not in html


# --------------------------------------------------------------------------------- nav label
def test_nav_label_follows_the_active_track():
    c = _fresh(consulting=True)
    with c:
        both = c.get("/pursuits").text
        cons = c.get("/pursuits?track=consulting").text
    side = lambda h: h.split('<aside class="side"')[1].split("</aside>")[0]  # noqa: E731
    assert "Applications</a>" in side(both) and "Leads</a>" not in side(both)
    assert "Leads</a>" in side(cons) and "Applications</a>" not in side(cons)
    assert nav_title("/pursuits") == "Applications"
    assert nav_title("/pursuits", "consulting") == "Leads"
    assert any(i.href == "/pursuits" for _g, items in NAV_GROUPS for i in items)


# ------------------------------------------------------------------------------ stage select
def test_stage_select_writes_a_status_event_and_moves_the_card():
    c = _fresh()
    _, pid = _seed(status="shortlisted")
    with c:
        r = c.post(f"/pursuits/{pid}/status", data={"to_status": "screening"},
                   headers={"HX-Request": "true"})
        assert r.status_code == 200 and 'data-testid="board"' in r.text
        assert "Head of IT" in _column(r.text, "Applied")
        assert c.post(f"/pursuits/{pid}/status", data={"to_status": "won"}).status_code == 400
        assert c.post(f"/pursuits/{pid}/status", data={"to_status": "preparing"}).status_code == 204
    p, events = _pursuit(pid)
    assert p.status == "preparing"
    assert [(e.from_status, e.to_status) for e in events] == [
        ("shortlisted", "screening"), ("screening", "preparing"),
    ]


def test_drag_to_applied_stamps_the_date():
    c = _fresh()
    _, pid = _seed(status="shortlisted")
    with c:
        assert c.post(f"/pursuits/{pid}/status", data={"to_status": "applied"}).status_code == 204
    p, events = _pursuit(pid)
    assert p.applied_at and len(events) == 1


# ------------------------------------------------------------------------------- Applied?
def _add_render(opp_id: int) -> int:
    async def _go(s):
        m = DocMaster(track="employment", name="Exec resume", doc_kind="resume")
        s.add(m)
        await s.flush()
        r = DocRender(opportunity_id=opp_id, doc_master_id=m.id, filename="Acme-secret.pdf")
        s.add(r)
        await s.commit()
        return r.id

    return _run(_go)


def test_i_applied_form_offers_this_roles_renders_without_file_names():
    c = _fresh()
    opp_id, pid = _seed()
    rid = _add_render(opp_id)
    with c:
        board = c.get("/pursuits").text
        html = c.get(f"/pursuits/{pid}/panel?form=applied").text
    assert f'<option value="{rid}" selected>Exec resume' in html
    assert "None / not tailored" in html
    assert "Acme-secret" not in html and "Acme-secret" not in board
    # Tailor opens the drawer's own entry (a render of the track's resume), with the way back
    # here; /opportunities/{id}/tailor is a bare partial and never a link target.
    assert f"/renders/new?master_id=1&amp;opportunity_id={opp_id}&amp;from=/pursuits" in board
    assert f"/renders/new?master_id=1&amp;opportunity_id={opp_id}&amp;from=/pursuits" in html
    bare = f"/opportunities/{opp_id}/tailor"
    assert bare not in html and bare not in board
    assert (date.today() + timedelta(days=7)).isoformat() in html  # follow-up default


def test_i_applied_sets_status_dates_document_next_action_and_event():
    c = _fresh()
    opp_id, pid = _seed()
    rid = _add_render(opp_id)
    today = date.today()
    with c:
        r = c.post(f"/pursuits/{pid}/act/applied", data={
            "document_id": str(rid), "applied_on": today.isoformat(),
            "follow_up": (today + timedelta(days=7)).isoformat(),
        })
        assert r.status_code == 200
        assert "Head of IT" in _column(r.text, "Applied")
    p, events = _pursuit(pid)
    assert p.status == "applied" and p.document_id == rid
    assert p.applied_at == f"{today.isoformat()}T00:00:00+00:00"
    assert p.next_action == "Follow up"
    assert p.next_action_at == (today + timedelta(days=7)).isoformat()
    assert [(e.from_status, e.to_status) for e in events] == [("shortlisted", "applied")]


def test_i_applied_with_a_custom_date_and_no_document():
    c = _fresh()
    _, pid = _seed()
    with c:
        html = c.get(f"/pursuits/{pid}/panel?form=applied").text
        assert '<details class="ap-disc">' in html and 'name="applied_on"' in html
        c.post(f"/pursuits/{pid}/act/applied", data={
            "document_id": "", "applied_on": "2026-10-01", "follow_up": ""})
    p, _ = _pursuit(pid)
    assert p.applied_at == "2026-10-01T00:00:00+00:00" and p.document_id is None
    assert p.next_action_at == "2026-10-08"  # blank follow-up defaults to 7 days after


def test_i_applied_rejects_another_roles_document_and_bad_dates():
    c = _fresh()
    other_opp, _ = _seed(title="Other")
    foreign = _add_render(other_opp)
    _, pid = _seed(title="Mine")
    with c:
        r = c.post(f"/pursuits/{pid}/act/applied", data={"document_id": str(foreign)})
        assert "does not belong" in r.text
        r = c.post(f"/pursuits/{pid}/act/applied", data={"applied_on": "someday"})
        assert "not a valid date" in r.text
        assert c.post("/pursuits/9999/act/applied", data={}).status_code == 404
        assert "not available" in c.post(f"/pursuits/{pid}/act/won", data={}).text
    p, events = _pursuit(pid)
    assert p.status == "shortlisted" and events == []


# ---------------------------------------------------------------------- the other verbs
def test_applied_column_verbs():
    c = _fresh()
    today = date.today()
    _, pid = _seed(status="applied", next_action="Follow up",
                   next_action_at=(today - timedelta(days=2)).isoformat())
    with c:
        c.post(f"/pursuits/{pid}/act/follow_again", data={})
        p, events = _pursuit(pid)
        assert p.status == "applied" and events == []
        assert p.next_action_at == (today + timedelta(days=7)).isoformat()
        c.post(f"/pursuits/{pid}/act/heard_back", data={"note": "recruiter call"})
        p, events = _pursuit(pid)
        assert p.status == "screening" and events[-1].note == "recruiter call"
        assert p.next_action is None
        # A date relative to today, never a fixed one: once a fixed date passes, the card rightly
        # reads "interview was <date>; record the outcome" and this assertion fails on the clock.
        when = today + timedelta(days=3)
        c.post(f"/pursuits/{pid}/act/interview",
               data={"when": when.isoformat(), "at_time": "10:00"})
        p, events = _pursuit(pid)
        assert p.status == "interviewing" and p.next_action == "Interview"
        assert p.next_action_at == f"{when.isoformat()}T10:00"
        assert f"{when.strftime('%b')} {when.day}, 10:00 AM" in c.get("/pursuits").text
        assert "Choose the interview date" in c.post(
            f"/pursuits/{pid}/act/interview", data={}).text
        c.post(f"/pursuits/{pid}/act/offer", data={})
    p, events = _pursuit(pid)
    assert p.status == "offer" and [e.to_status for e in events] == [
        "screening", "interviewing", "offer"]


def test_closing_verbs_and_outcome_capture():
    c = _fresh()
    _, pid = _seed(status="interviewing")
    with c:
        c.post(f"/pursuits/{pid}/act/rejected", data={})
        p, _ = _pursuit(pid)
        assert p.status == "rejected" and p.closed_at
        html = c.get("/pursuits").text
        assert "Closed (1)" in _column(html, "Offer / Closed")
        form = c.get(f"/pursuits/{pid}/panel?form=outcome").text
        assert 'name="final_status"' in form and 'name="furthest_stage"' in form
        r = c.post(f"/pursuits/{pid}/outcome", data={
            "final_status": "withdrawn", "furthest_stage": "interviewing",
            "rejection_reason": "comp too low", "days_to_first_response": "4"},
            headers={"HX-Request": "true"})
        assert "Outcome saved" in r.headers["HX-Trigger"] and 'data-testid="board"' in r.text
    p, events = _pursuit(pid)
    assert (p.status, p.outcome, p.outcome_reason, p.outcome_stage) == (
        "withdrawn", "withdrawn", "comp too low", "interviewing")
    assert events[-1].to_status == "withdrawn"


def test_consulting_verbs():
    c = _fresh(consulting=True)
    _, pid = _seed(track="consulting", status="researching", title="Lead")
    with c:
        html = c.get("/pursuits?track=consulting").text
        assert 'data-step-key="applied"' in html and "Capability statement" in html
        c.post(f"/pursuits/{pid}/act/applied", data={"applied_on": "2026-10-10"})
        p, _ = _pursuit(pid)
        assert p.status == "outreach_sent" and p.next_action == "Follow up"
        c.post(f"/pursuits/{pid}/act/heard_back", data={})
        c.post(f"/pursuits/{pid}/act/proposal", data={})
        p, _ = _pursuit(pid)
        assert p.status == "proposal_sent" and p.next_action == "Follow up on proposal"
        c.post(f"/pursuits/{pid}/act/offer", data={})
        assert _pursuit(pid)[0].status == "won"
        assert "not available" in c.post(f"/pursuits/{pid}/act/interview", data={}).text


# ------------------------------------------------------------------------------- follow-ups
def test_overdue_cards_show_days_quiet_and_sort_first():
    c = _fresh()
    today = date.today()
    old = (today - timedelta(days=12)).isoformat()
    recent = today.isoformat() + "T08:00:00+00:00"
    _seed(status="applied", title="Calm role", last_activity_at=recent,
          next_action_at=(today + timedelta(days=3)).isoformat(), next_action="Follow up")
    _seed(status="applied", title="Newer overdue", last_activity_at=recent,
          applied_at=f"{(today - timedelta(days=5)).isoformat()}T00:00:00+00:00",
          next_action_at=(today - timedelta(days=1)).isoformat(), next_action="Follow up")
    _seed(status="applied", title="Quiet role", last_activity_at="2026-01-01T00:00:00+00:00",
          applied_at=f"{old}T00:00:00+00:00",
          next_action_at=(today - timedelta(days=4)).isoformat(), next_action="Follow up")
    with c:
        col = _column(c.get("/pursuits").text, "Applied")
    assert "12 days quiet" in col and "5 days quiet" in col
    assert col.index("Quiet role") < col.index("Newer overdue") < col.index("Calm role")


def test_quiet_days_helper():
    today = date(2026, 10, 15)
    p = Pursuit(opportunity_id=1, track="employment", status="applied", id=1,
                applied_at="2026-10-03T00:00:00+00:00", next_action_at="2026-10-10")
    assert pb.quiet_days(p, today) == 12
    p.next_action_at = "2026-10-15"
    assert pb.quiet_days(p, today) is None


# ------------------------------------------------------------------- the ghosted hint (#30)
def _quiet_card(days: int, **kw) -> int:
    today = date.today()
    applied = f"{(today - timedelta(days=days)).isoformat()}T00:00:00+00:00"
    _opp, pid = _seed(
        status=kw.pop("status", "applied"), applied_at=applied,
        last_activity_at=applied, next_action_at=(today - timedelta(days=1)).isoformat(),
        next_action="Follow up", **kw,
    )
    return pid


def test_ghost_hint_threshold_is_past_the_configured_days():
    today = date(2026, 10, 15)
    p = Pursuit(opportunity_id=1, track="employment", status="applied", id=1,
                applied_at="2026-09-24T00:00:00+00:00", next_action_at="2026-10-10")
    assert pb.quiet_days(p, today) == 21
    assert pb.ghost_hint(p, today, days=21) is None  # exactly the threshold is not past it
    assert pb.ghost_hint(p, today, days=20) == 21
    p.status = "interviewing"
    assert pb.ghost_hint(p, today, days=1) is None  # only a card waiting on a reply
    assert pb.GHOST_STATUS == {"employment": "ghosted", "consulting": "no_response"}


def test_a_long_quiet_card_offers_to_mark_ghosted_and_a_recent_one_does_not():
    c = _fresh()
    _quiet_card(30)
    _quiet_card(10, title="Recent quiet")
    with c:
        col = _column(c.get("/pursuits").text, "Applied")
    assert col.count('data-testid="ghost-hint"') == 1
    assert "No reply in 30 days." in col
    assert 'data-testid="ghost-hint-act"' in col
    assert "&#34;to_status&#34;: &#34;ghosted&#34;" in col or '"to_status": "ghosted"' in col


def test_the_hint_acts_through_move_to_status_and_adds_no_status():
    c = _fresh()
    pid = _quiet_card(40)
    with c:
        r = c.post(f"/pursuits/{pid}/status", data={"to_status": "ghosted"},
                   headers={"HX-Request": "true"})
        assert r.status_code == 200
        board = c.get("/pursuits").text
    p, events = _pursuit(pid)
    assert p.status == "ghosted" and p.closed_at  # the closed_at stamp is move_to_status's
    assert events[-1].to_status == "ghosted"
    assert 'data-testid="ghost-hint"' not in board  # a closed card has nothing to count


def test_a_consulting_lead_offers_no_response():
    c = _fresh(consulting=True)
    pid = _quiet_card(30, track="consulting", status="outreach_sent")
    with c:
        html = c.get("/pursuits?track=consulting").text
    assert "No reply in 30 days." in html
    assert "no_response" in html
    assert pid


# ------------------------------------------------------------------------------- add a role
def test_add_by_paste_creates_opportunity_pursuit_event_and_queues_scoring():
    c = _fresh()
    text = "VP of Infrastructure\n\nLead the platform team for a regional bank. " * 5
    with c:
        r = c.post("/pursuits/add", data={"text": text, "org_name": "Tidal", "track": "employment"},
                   headers={"HX-Request": "true"})
        assert r.status_code == 200 and "Added to your list" in r.text
        assert "VP of Infrastructure" in _column(r.text, "To apply")

    async def _go(s):
        opps = (await s.execute(select(Opportunity))).scalars().all()
        ps = (await s.execute(select(Pursuit))).scalars().all()
        evs = (await s.execute(select(StatusEvent))).scalars().all()
        tasks = (await s.execute(select(Task).where(Task.type == "embed"))).scalars().all()
        return opps, ps, evs, tasks

    opps, ps, evs, tasks = _run(_go)
    assert len(opps) == len(ps) == len(evs) == 1 and len(tasks) == 1
    assert opps[0].stage == "new" and ps[0].status == "shortlisted"
    assert (evs[0].from_status, evs[0].to_status) == (None, "shortlisted")
    with c:
        again = c.post("/pursuits/add", data={"text": text, "track": "employment"})
    assert "Already on your list" in again.text
    assert len(_run(_go)[1]) == 1


def test_add_requires_something_and_a_web_link():
    c = _fresh()
    with c:
        assert "Paste the posting text or a link" in c.post("/pursuits/add", data={}).text
        assert "must start with http" in c.post(
            "/pursuits/add", data={"url": "file:///etc/passwd"}).text
    assert _run(lambda s: s.scalar(select(Opportunity.id))) is None


_PAGE = (
    "<html><head><title>Director of Technology - Vestis</title><style>x{}</style></head><body>"
    "<h1>Director of Technology</h1><p>" + "Own enterprise technology strategy. " * 20 + "</p>"
    "</body></html>"
)


@respx.mock
def test_add_by_url_fetches_once_and_reads_the_page():
    robots = respx.get("https://jobs.example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n"))
    page = respx.get("https://jobs.example.com/roles/42").mock(
        return_value=httpx.Response(200, text=_PAGE))
    c = _fresh()
    with c:
        r = c.post("/pursuits/add", data={"url": "https://jobs.example.com/roles/42"})
    assert "Added to your list" in r.text
    assert "Director of Technology" in _column(r.text, "To apply")
    assert page.call_count == 1 and robots.called
    opp = _run(lambda s: s.scalar(select(Opportunity)))
    assert opp.title == "Director of Technology - Vestis" and opp.url.endswith("/roles/42")
    assert "enterprise technology strategy" in (opp.description_md or "")
    assert "<p>" not in (opp.description_md or "") and "x{}" not in (opp.description_md or "")


@respx.mock
def test_add_by_url_failure_is_reported_and_pasted_text_is_kept():
    respx.get("https://jobs.example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://jobs.example.com/roles/9").mock(return_value=httpx.Response(404))
    c = _fresh()
    with c:
        none = c.post("/pursuits/add", data={"url": "https://jobs.example.com/roles/9"})
        assert "Could not read that page" in none.text and "Paste the posting text" in none.text
        assert _run(lambda s: s.scalar(select(Opportunity.id))) is None
        kept = c.post("/pursuits/add", data={
            "url": "https://jobs.example.com/roles/9", "text": "Head of Platform\nBuild things."})
    assert "Could not read that page" in kept.text and "Using the text you pasted" in kept.text
    assert "Head of Platform" in _column(kept.text, "To apply")


@respx.mock
def test_add_by_url_respects_robots_txt():
    respx.get("https://blocked.example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n"))
    page = respx.get("https://blocked.example.com/job").mock(
        return_value=httpx.Response(200, text=_PAGE))
    c = _fresh()
    with c:
        r = c.post("/pursuits/add", data={"url": "https://blocked.example.com/job"})
    assert "robots.txt disallows" in r.text and not page.called


def test_add_in_consulting_mode_lands_in_to_contact():
    c = _fresh()
    with c:
        r = c.post("/pursuits/add", data={
            "text": "Fractional CIO for a 200 person firm\nNeed part time leadership.",
            "track": "consulting"})
    assert "Fractional CIO" in _column(r.text, "To contact")


# ---------------------------------------------------------------- review round: one move path
def test_dragging_to_applied_gets_the_i_applied_follow_up():
    c = _fresh()
    _, pid = _seed()
    with c:
        r = c.post(f"/pursuits/{pid}/status", data={"to_status": "applied"},
                   headers={"HX-Request": "true"})
    assert r.status_code == 200
    p, events = _pursuit(pid)
    assert p.applied_at and p.next_action == "Follow up"
    assert p.next_action_at == (date.today() + timedelta(days=pb.FOLLOW_UP_DAYS)).isoformat()
    assert [(e.from_status, e.to_status) for e in events] == [("shortlisted", "applied")]


def test_moving_out_of_a_closed_stage_reopens_and_into_one_closes():
    c = _fresh()
    _, pid = _seed(status="applied")
    with c:
        c.post(f"/pursuits/{pid}/status", data={"to_status": "rejected"})
        assert _pursuit(pid)[0].closed_at
        c.post(f"/pursuits/{pid}/status", data={"to_status": "interviewing"})
    assert _pursuit(pid)[0].closed_at is None


def test_an_invalid_stage_is_a_notice_htmx_can_show():
    c = _fresh()
    _, pid = _seed()
    with c:
        r = c.post(f"/pursuits/{pid}/status", data={"to_status": "won"},
                   headers={"HX-Request": "true"})
    assert r.status_code == 200 and 'data-testid="board-notice"' in r.text
    assert "is not a stage for employment cards; nothing was changed." in r.text
    assert _pursuit(pid)[0].status == "shortlisted"


def test_a_past_interview_asks_for_the_outcome_instead_of_counting_quiet_days():
    today = date(2026, 10, 15)
    p = Pursuit(opportunity_id=1, track="employment", status="interviewing", id=1,
                applied_at="2026-09-20T00:00:00+00:00", next_action="Interview",
                next_action_at="2026-10-09T14:00")
    assert pb.quiet_days(p, today) is None
    assert pb.interview_passed(p, today) == date(2026, 10, 9)
    c = _fresh()
    _seed(status="interviewing", title="Panel role", applied_at="2026-09-20T00:00:00+00:00",
          next_action="Interview", next_action_at=(date.today() - timedelta(days=6)).isoformat())
    with c:
        col = _column(c.get("/pursuits").text, "Interviewing")
    assert "days quiet" not in col and "interview was" in col and "Record the outcome" in col


def test_a_missing_org_is_never_a_dash():
    c = _fresh()

    async def _go(s):
        opp = Opportunity(track="employment", opp_type="job_posting", title="No org role",
                          url="https://x.example/none", stage="t2_done")
        s.add(opp)
        await s.flush()
        s.add(Pursuit(opportunity_id=opp.id, track="employment", status="shortlisted"))
        await s.commit()

    _run(_go)
    with c:
        board = c.get("/pursuits").text
        table = c.get("/pursuits?view=table").text
    assert '<div class="ap-o">Unknown organization</div>' in board
    assert "—" not in _column(board, "To apply") and "Unknown organization" in table


@respx.mock
def test_add_by_url_never_saves_a_sign_in_page_as_the_posting():
    """A redirect to a login wall (another host or a /login path) or a page titled Sign in is
    refused with a paste-the-text message; nothing is created."""
    respx.get("https://jobs.example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n"))
    respx.get("https://www.linkedin.example/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n"))
    respx.get("https://jobs.example.com/a").mock(return_value=httpx.Response(
        302, headers={"location": "https://sso.example.net/start"}))
    offsite = respx.get("https://sso.example.net/start").mock(
        return_value=httpx.Response(200, text=_PAGE))
    respx.get("https://jobs.example.com/b").mock(return_value=httpx.Response(
        302, headers={"location": "/login?next=/b"}))
    wall = (
        "<html><head><title>Sign in | LinkedIn</title></head><body><h1>Sign in</h1>"
        "<p>" + "Stay updated on your professional world. " * 10 + "</p></body></html>"
    )
    respx.get("https://www.linkedin.example/jobs/view/1").mock(
        return_value=httpx.Response(200, text=wall))
    c = _fresh()
    with c:
        a = c.post("/pursuits/add", data={"url": "https://jobs.example.com/a"}).text
        b = c.post("/pursuits/add", data={"url": "https://jobs.example.com/b"}).text
        w = c.post("/pursuits/add", data={"url": "https://www.linkedin.example/jobs/view/1"}).text
    assert "redirects to another site (sso.example.net)" in a and not offsite.called
    assert "sign-in page" in b and "sign-in page" in w
    for text in (a, b, w):
        assert "paste it instead" in text
    assert _run(lambda s: s.scalar(select(Opportunity.id))) is None


@respx.mock
def test_add_by_url_follows_a_same_site_redirect_and_checks_its_robots():
    robots = respx.get("https://example.org/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n"))
    respx.get("https://example.org/r/1").mock(return_value=httpx.Response(
        301, headers={"location": "https://www.example.org/jobs/1"}))
    respx.get("https://www.example.org/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n"))
    final = respx.get("https://www.example.org/jobs/1").mock(
        return_value=httpx.Response(200, text=_PAGE))
    c = _fresh()
    with c:
        r = c.post("/pursuits/add", data={"url": "https://example.org/r/1"})
    # The final host's robots.txt forbids it: refused, though the first host allowed the link.
    assert robots.called and not final.called and "robots.txt disallows" in r.text


def test_add_by_a_malformed_url_is_a_notice_not_a_500():
    c = _fresh()
    with c:
        r = c.post("/pursuits/add", data={"url": "https://exa mple.com:99999/x"},
                   headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Could not read that page" in r.text


# ------------------------------------------------------------ Remove on the card (#23, D-UI-75)
def test_card_remove_deletes_the_pursuit_like_the_panel_and_returns_the_board():
    from kestrel.db.models import Notification, Offer, Outcome, VerdictOverride
    from kestrel.web.pages.opportunity_actions import ADD_REASONS

    c = _fresh()
    opp_id, pid = _seed(status="applied", title="Wrong role")
    _other_opp, other_pid = _seed(status="shortlisted", title="Keeper")

    async def extras(s):
        s.add_all([
            StatusEvent(pursuit_id=pid, from_status="shortlisted", to_status="applied"),
            Outcome(pursuit_id=pid, opportunity_id=opp_id, final_status="rejected"),
            Offer(pursuit_id=pid, base=150000),
            Notification(kind="liveness", title="Closed", opportunity_id=opp_id, pursuit_id=pid),
            VerdictOverride(opportunity_id=opp_id, operator_verdict="agree",
                            reason=sorted(ADD_REASONS)[0]),
            VerdictOverride(opportunity_id=opp_id, operator_verdict="agree", reason="my own"),
        ])
        await s.commit()

    _run(extras)
    with c:
        html = c.get("/pursuits").text
        assert 'data-testid="board-remove"' in html and "Remove from Applications" in html
        assert 'data-id="' + str(pid) + '"' in html and 'data-id="' + str(other_pid) + '"' in html
        r = c.post(f"/pursuits/{pid}/remove", headers={"HX-Request": "true"})
        assert r.status_code == 200
        assert 'data-testid="board"' in r.text and "Wrong role" not in r.text
        assert "Keeper" in r.text and "Removed from your shortlist." in r.text
        assert 'hx-swap-oob="true"' in r.text  # the sidebar badges move with the board
        missing = c.post("/pursuits/999999/remove", headers={"HX-Request": "true"})
        assert missing.status_code == 200 and 'data-testid="board-notice"' in missing.text

    async def left(s):
        n_p = len((await s.execute(select(Pursuit).where(Pursuit.id == pid))).all())
        ev = len((await s.execute(select(StatusEvent).where(StatusEvent.pursuit_id == pid))).all())
        out = len((await s.execute(select(Outcome).where(Outcome.pursuit_id == pid))).all())
        off = len((await s.execute(select(Offer).where(Offer.pursuit_id == pid))).all())
        note = (await s.execute(select(Notification))).scalar_one()
        reasons = [
            v.reason for v in (await s.execute(select(VerdictOverride))).scalars().all()
        ]
        return n_p, ev, out, off, note.pursuit_id, reasons

    assert _run(left) == (0, 0, 0, 0, None, ["my own"])
    assert _pursuit(other_pid)[0] is not None


def test_card_remove_asks_only_when_an_offer_would_be_lost_and_speaks_each_track():
    from kestrel.db.models import Offer

    c = _fresh(consulting=True)
    _, fresh = _seed(status="shortlisted", title="Fresh role")
    _, with_offer = _seed(status="offer", title="Offer role")
    _seed(track="consulting", status="shortlisted", title="Fresh lead")

    async def offer(s):
        s.add(Offer(pursuit_id=with_offer, base=150000))
        await s.commit()

    _run(offer)
    with c:
        emp = c.get("/pursuits").text
        con = c.get("/pursuits?track=consulting").text

    def confirm(html: str, pid: int) -> str:
        tag = html.split(f'data-id="{pid}"', 1)[1].split(">", 1)[0]
        return tag.split('data-confirm="', 1)[1].split('"', 1)[0]

    assert confirm(emp, fresh) == ""  # Undo covers it
    assert confirm(emp, with_offer) == "offer"  # an entered offer cannot be put back
    assert "Remove from Applications" in emp and "Remove from Leads" in con


def test_removing_a_card_can_be_undone_with_its_history_and_outcome():
    import json

    from kestrel.db.models import Outcome

    c = _fresh()
    opp_id, pid = _seed(status="applied", title="Put me back", notes_md="my notes", priority=1)

    async def extras(s):
        s.add_all([
            StatusEvent(pursuit_id=pid, from_status=None, to_status="shortlisted"),
            StatusEvent(pursuit_id=pid, from_status="shortlisted", to_status="applied"),
            Outcome(pursuit_id=pid, opportunity_id=opp_id, final_status="rejected"),
        ])
        await s.commit()

    _run(extras)
    with c:
        r = c.post(f"/pursuits/{pid}/remove", headers={"HX-Request": "true"})
        trig = json.loads(r.headers["HX-Trigger"])
        assert "Removed" in trig["k-toast"]["message"] and "apps-close-panel" in trig
        undo_url = trig["k-toast"]["undoUrl"]
        assert _pursuit(pid)[0] is None
        assert c.post(undo_url).status_code == 200
        again = c.post(undo_url)  # used up: nothing happens twice
        assert "no longer be undone" in again.text
    p, events = _pursuit_by_opp(opp_id)
    assert p.status == "applied" and p.notes_md == "my notes" and p.priority == 1
    assert [e.to_status for e in events] == ["shortlisted", "applied"]

    async def outcomes(s):
        return len((await s.execute(select(Outcome))).all())

    assert _run(outcomes) == 1


def _pursuit_by_opp(opp_id: int):
    async def _go(s):
        p = (await s.execute(select(Pursuit).where(Pursuit.opportunity_id == opp_id))).scalar_one()
        ev = (
            await s.execute(select(StatusEvent).where(StatusEvent.pursuit_id == p.id).order_by(
                StatusEvent.id))
        ).scalars().all()
        return p, ev

    return _run(_go)


def test_a_board_pref_save_racing_another_never_loses_the_choice(monkeypatch):
    # #14 Windows CI: two /pursuits/prefs saves close together both read "no row" and the second
    # insert failed on the key, losing the choice. Stage that race deterministically: the row
    # already exists (the other save won) but this save's read comes back empty.
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession

    from kestrel.db.engine import get_sessionmaker
    from kestrel.db.models import Setting
    from kestrel.web.pages.pursuits import RANGE_KEY

    client = _fresh()

    async def seed():
        async with get_sessionmaker()() as s:
            s.add(Setting(key=RANGE_KEY, value="30"))
            await s.commit()

    asyncio.run(seed())
    real_get = AsyncSession.get

    async def stale_get(self, model, ident, *a, **k):
        if model is Setting and ident == RANGE_KEY:
            return None  # the other save's row is not visible to this read
        return await real_get(self, model, ident, *a, **k)

    monkeypatch.setattr(AsyncSession, "get", stale_get)
    assert client.post("/pursuits/prefs", data={"range": "7"}).status_code == 200
    monkeypatch.setattr(AsyncSession, "get", real_get)

    async def read():
        async with get_sessionmaker()() as s:
            return (await s.get(Setting, RANGE_KEY)).value

    assert asyncio.run(read()) == "7"
