"""Pipeline, the overhauled screen (D-UI-96, the locked design mockup (kept privately)):
the role panel's four kinds, Not for me because..., Shortlist and Undo, Keep it anyway, Hide
company, the bulk bar, saved views, More filters, the near-floor tag, a role removed from Needs
another try, and Consulting's Signals tab. Cloud-safe: a migrated SQLite file and the test client;
no model, no browser, no network. The list's older behavior stays in test_pipeline_view.py."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import timedelta

import pytest
from sqlalchemy import select

from kestrel.db.models import (
    Opportunity,
    Org,
    Pursuit,
    SavedView,
    Setting,
    Source,
    Task,
    VerdictOverride,
)
from tests import test_pipeline_view as T

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

_HX = {"HX-Request": "true"}


def _events(response) -> dict:
    return json.loads(response.headers.get("HX-Trigger", "{}"))


def _db(fn):
    return asyncio.run(T._with_session(fn))


async def _labels(s):
    return list((await s.execute(select(VerdictOverride).order_by(VerdictOverride.id))).scalars())


def _toast_undo(response) -> str | None:
    return (_events(response).get("k-toast") or {}).get("undoUrl")


# ============================================================================== the panel kinds
def test_a_set_aside_role_has_its_rule_the_posting_and_keep_it_anyway():
    ids = asyncio.run(T._seed())

    async def reject(s):
        k = await s.get(Opportunity, ids["k"])
        k.stage, k.stage_reason = "t0_reject", "blocker:anti_title: help desk"
        k.description_md = "Runs the help desk. " * 60
        await s.commit()

    _db(reject)
    with T._client() as c:
        html = c.get(f"/opportunities/{ids['k']}/panel").text
    assert 'data-kind="nf"' in html and "Not a fit" in html
    assert 'data-testid="rule-why"' in html
    assert "Your rules set this aside before any AI ran" in html
    assert "From the posting" in html and "Shown as posted. Kestrel has not summarized it." in html
    assert "Runs the help desk." in html and "…" in html  # the posting, cut at a sensible length
    assert f'hx-post="/opportunities/{ids["k"]}/decide/keep-anyway"' in html
    assert 'data-testid="panel-review-rules"' in html and 'data-testid="panel-open"' in html
    # The model never scored it, so the panel shows no ring, no bars and no summary.
    assert "pl-ring" not in html and "Written by Kestrel" not in html


def test_a_failed_role_says_why_and_a_removed_one_says_it_was_removed():
    from kestrel.db.models import Quarantine
    from kestrel.services import retry_removal

    ids = asyncio.run(T._seed())

    async def fail(s):
        s.add(Quarantine(opportunity_id=ids["e"], tier=2, error="invalid JSON: Expecting value"))
        await s.commit()

    _db(fail)
    with T._client() as c:
        failed = c.get(f"/opportunities/{ids['e']}/panel").text
        removed_row = T._row(T._get(c, "/opportunities"), ids["d"])
    assert 'data-kind="pending"' in failed and "Scoring failed" in failed
    assert "Kestrel kept the raw answer for review." in failed
    assert 'data-testid="panel-needs-try"' in failed
    assert f'href="/rescore/choose?opp_ids={ids["e"]}&page=1&next=/opportunities"' in failed
    assert "removed from Needs another try" not in removed_row

    async def remove(s):
        await retry_removal.mark(s, ids["e"])
        await retry_removal.mark(s, ids["d"])
        await s.commit()

    _db(remove)
    with T._client() as c:
        html = T._get(c, "/opportunities")
        panel = c.get(f"/opportunities/{ids['e']}/panel").text
        quiet = c.get(f"/opportunities/{ids['e']}/panel").status_code
    # Kept out of Scoring failed, labeled honestly: not scored, with the way back to undo.
    e = T._row(html, ids["e"])
    assert 'data-group="new"' in e and 'data-scoring="removed">not scored</span>' in e
    assert "removed from Needs another try" in e
    assert 'href="/quarantine" data-testid="removed-undo">Undo in Needs another try</a>' in e
    assert "Scoring failed ·" not in html and "Being scored · 2" in html
    assert "Needs another try" in html and 'data-testid="failed-open"' not in html
    assert quiet == 200 and "You removed this role from Needs another try" in panel
    assert 'href="/quarantine"' in panel and "Scoring failed" not in panel


def test_a_removed_role_comes_back_to_scoring_failed_when_it_fails_again():
    from kestrel.db.models import Quarantine
    from kestrel.services import retry_removal

    ids = asyncio.run(T._seed())

    async def go(s):
        s.add(Quarantine(opportunity_id=ids["e"], tier=2, error="invalid JSON",
                         created_at="2026-01-01T00:00:00Z"))
        await retry_removal.mark(s, ids["e"])
        s.add(Quarantine(opportunity_id=ids["e"], tier=2, error="invalid JSON again"))
        await s.commit()

    _db(go)
    with T._client() as c:
        html = T._get(c, "/opportunities")
    assert 'data-scoring="failed"' in T._row(html, ids["e"]) and "Scoring failed · 1" in html


# ============================================================================ Not for me because
@pytest.mark.parametrize("reason,text,dimension", [
    ("kind", "Not my kind of role", "role_fit"),
    ("pay_place", "Pay or location", "comp_logistics"),
    ("level", "Wrong level", "trajectory"),
])
def test_a_fit_reason_hides_the_role_and_records_a_pass_with_its_dimension(
    reason, text, dimension
):
    ids = asyncio.run(T._seed())
    with T._client() as c:
        r = c.post(f"/opportunities/{ids['b']}/decide/not-for-me?reason={reason}", headers=_HX)
        assert r.status_code == 200 and r.headers["HX-Reswap"] == "none"
        ev = _events(r)
        assert ev["pl-gone"] == {"ids": [ids["b"]]} and ev["pl-close"] == ids["b"]
        assert "Thanks, that helps Kestrel learn" in ev["k-toast"]["message"]
        assert ev["k-toast"]["undoUrl"].startswith(f"/opportunities/{ids['b']}/decide/undo-hide")
        assert ids["b"] not in T._row_ids(T._get(c, "/opportunities"))
        hidden = T._get(c, "/opportunities?hidden=1")
        assert "hidden: not interested" in T._row(hidden, ids["b"])
    (label,) = _db(_labels)
    # A fit reason is a pass the ranker learns from, with what it taught (never in
    # label.dimensions, which holds the scores calibration reads).
    assert (label.operator_verdict, label.operator_score) == ("pass", 1.5)
    assert label.reason == f"Not for me: {text}."
    assert label.dimension_corrections == {"reason": reason, "dimension": dimension}

    async def state(s):
        return (await s.get(Opportunity, ids["b"])).archive_reason

    assert _db(state) == "not_interested"


def test_just_not_now_hides_the_role_and_writes_no_label_so_no_earlier_word_is_hidden():
    ids = asyncio.run(T._seed())

    async def earlier(s):
        s.add(VerdictOverride(opportunity_id=ids["b"], operator_verdict="apply",
                              operator_score=4.0, reason="before"))
        await s.commit()

    _db(earlier)
    with T._client() as c:
        c.post(f"/opportunities/{ids['b']}/decide/not-for-me?reason=later")
        c.post(f"/opportunities/{ids['b']}/decide/put-back")
    assert [lb.reason for lb in _db(_labels)] == ["before"]


def test_a_consulting_lead_has_its_own_four_reasons():
    ids = asyncio.run(T._seed())
    with T._client() as c:
        panel = c.get(f"/opportunities/{ids['c']}/panel").text
        c.post(f"/opportunities/{ids['c']}/decide/not-for-me?reason=rate")
    for text in ("Not my kind of work", "Rate too low", "Can&#39;t deliver it", "Just not now"):
        assert f">{text}</button>" in panel, text
    assert "Add to leads" in panel and "Wrong level" not in panel
    (label,) = _db(_labels)
    assert label.dimension_corrections == {"reason": "rate", "dimension": "economics"}


def test_an_unknown_reason_is_just_not_now_and_a_hidden_role_is_not_hidden_twice():
    ids = asyncio.run(T._seed())
    with T._client() as c:
        c.post(f"/opportunities/{ids['b']}/decide/not-for-me?reason=bored")
        again = c.post(f"/opportunities/{ids['b']}/decide/not-for-me?reason=kind")
        missing = c.post("/opportunities/999999/decide/not-for-me?reason=kind")
    assert _db(_labels) == []  # an unknown reason is read as Just not now: no label
    assert "pl-gone" in _events(again)  # nothing more written
    assert "That role is gone." in _events(missing)["k-toast"]["message"]


def test_undo_puts_the_role_back_and_removes_only_the_label_that_click_wrote():
    ids = asyncio.run(T._seed())

    async def earlier(s):
        s.add(VerdictOverride(opportunity_id=ids["b"], operator_verdict="apply", operator_score=4.0,
                              reason="before"))
        await s.commit()

    _db(earlier)
    with T._client() as c:
        r = c.post(f"/opportunities/{ids['b']}/decide/not-for-me?reason=kind")
        undo = _toast_undo(r)
        assert "label=" in undo
        assert c.post(undo, headers=_HX).status_code == 200
        assert ids["b"] in T._row_ids(T._get(c, "/opportunities"))
    labels = _db(_labels)
    assert [lb.reason for lb in labels] == ["before"]  # the click taught nothing after Undo

    async def state(s):
        b = await s.get(Opportunity, ids["b"])
        return b.archived_at, b.archive_reason

    assert _db(state) == (None, None)


def test_put_it_back_unhides_from_the_hidden_chip_and_keeps_the_label():
    ids = asyncio.run(T._seed())
    with T._client() as c:
        c.post(f"/opportunities/{ids['b']}/decide/not-for-me?reason=kind")
        panel = c.get(f"/opportunities/{ids['b']}/panel").text
        r = c.post(f"/opportunities/{ids['b']}/decide/put-back")
        assert ids["b"] in T._row_ids(T._get(c, "/opportunities"))
    assert 'data-testid="panel-put-back"' in panel and "hidden</span>" in panel
    assert _events(r)["k-toast"]["message"] == "Back in Pipeline."
    assert len(_db(_labels)) == 1  # the operator's word at the time stays


# ================================================================================= Shortlist
def test_shortlist_adds_the_pursuit_and_undo_removes_it_only_when_this_click_made_it():
    ids = asyncio.run(T._seed())
    with T._client() as c:
        r = c.post(f"/opportunities/{ids['b']}/decide/shortlist", headers=_HX)
        ev = _events(r)
        assert ev["pipelineRefresh"] is True
        assert ev["k-toast"]["message"] == "Added to your shortlist."
        undo = ev["k-toast"]["undoUrl"]
        assert 'data-testid="panel-on-list"' in r.text and 'id="nav-count-shortlist"' in r.text
        again = c.post(f"/opportunities/{ids['b']}/decide/shortlist", headers=_HX)
        assert "undoUrl" not in _events(again)["k-toast"]  # it was already there: nothing to undo
        assert "Already" in _events(again)["k-toast"]["message"]
        assert c.post(undo, headers=_HX).status_code == 200
    assert _db(lambda s: T._pursuit_rows(s, ids["b"])) == []
    assert _db(_labels) == []  # the Add's label goes with it


def test_shortlist_on_a_lead_speaks_consulting():
    ids = asyncio.run(T._seed())
    with T._client() as c:
        r = c.post(f"/opportunities/{ids['c']}/decide/shortlist", headers=_HX)
    assert _events(r)["k-toast"]["message"] == "Added to your leads."
    assert "In my leads" in r.text


# ============================================================================== Keep it anyway
def test_keep_it_anyway_queues_a_score_with_the_rules_check_skipped():
    ids = asyncio.run(T._seed())

    async def reject(s):
        k = await s.get(Opportunity, ids["k"])
        k.stage, k.stage_reason = "t0_reject", "blocker:anti_title: help desk"
        await s.commit()

    _db(reject)
    with T._client() as c:
        before = T._row(T._get(c, "/opportunities"), ids["k"])
        r = c.post(f"/opportunities/{ids['k']}/decide/keep-anyway", headers=_HX)
    assert 'data-testid="keep-anyway"' in before
    ev = _events(r)
    assert ev["pl-gone"] == {"ids": [ids["k"]]}
    assert ev["k-toast"]["message"] == "Kept. Kestrel will score it with the AI."

    async def tasks(s):
        return [(t.type, t.payload) for t in (await s.execute(select(Task))).scalars()]

    ((typ, payload),) = _db(tasks)
    assert typ == "t1" and payload["opportunity_id"] == ids["k"]
    assert payload["tier0_override"] is True and payload["requested"] is True


def test_keep_it_anyway_names_a_refusal_instead_of_hiding_it(monkeypatch):
    ids = asyncio.run(T._seed())
    with T._client() as c:
        r = c.post("/opportunities/999999/decide/keep-anyway", headers=_HX)
    ev = _events(r)
    assert ev["k-toast"]["tone"] == "warn" and "pl-gone" not in ev
    assert ids["a"]


# ================================================================================== Hide company
def test_hide_company_removes_its_roles_with_undo_and_keeps_an_active_pursuit():
    ids = asyncio.run(T._seed())
    with T._client() as c:
        before = T._row_ids(T._get(c, "/opportunities"))
        r = c.post(f"/opportunities/{ids['d']}/decide/hide-company", headers=_HX)
        ev = _events(r)
        assert "hidden. Its roles leave Pipeline." in ev["k-toast"]["message"]
        undo = ev["k-toast"]["undoUrl"]
        assert "/decide/unhide-company?org=" in undo
        after = T._row_ids(T._get(c, "/opportunities"))
        # Northwind's roles are gone, but a (shortlisted) role with an active pursuit stays.
        assert after == [ids["a"]]
        assert set(before) - set(after) == {ids["b"], ids["d"], ids["e"], ids["k"]}
        assert c.post(undo, headers=_HX).status_code == 200
        assert T._row_ids(T._get(c, "/opportunities")) == before
        assert "no company" in _events(
            c.post("/opportunities/999999/decide/hide-company"))["k-toast"]["message"]


def test_the_row_menu_offers_save_for_later_and_hide_company():
    ids = asyncio.run(T._seed())
    with T._client() as c:
        html = T._get(c, "/opportunities")
    row = T._row(html, ids["a"])
    assert f'hx-post="/opportunities/{ids["a"]}/decide/save"' in row and "Save for later" in row
    assert f'hx-post="/opportunities/{ids["a"]}/decide/hide-company"' in row


def test_save_for_later_is_a_bookmark_under_the_saved_chip_and_nothing_else():
    ids = asyncio.run(T._seed())
    with T._client() as c:
        r = c.post(f"/opportunities/{ids['b']}/decide/save", headers=_HX)
        ev = _events(r)
        assert ev["k-toast"]["message"] == "Saved for later. Find it under Saved."
        again = c.post(f"/opportunities/{ids['b']}/decide/save", headers=_HX)
        assert _events(again)["k-toast"]["message"] == "Already saved."
        html = T._get(c, "/opportunities")
        saved = T._get(c, "/opportunities?saved=1")
        assert re.search(r'data-chip="saved">Saved<span class="k-chip__n">1</span>', html)
        assert T._row_ids(saved) == [ids["b"]] and 'data-testid="saved-tag"' in saved
        assert "Remove from Saved" in saved
        assert c.post(ev["k-toast"]["undoUrl"], headers=_HX).status_code == 200
        assert T._row_ids(T._get(c, "/opportunities?saved=1")) == []
        assert c.post("/opportunities/999999/decide/save").status_code == 200
    # No pursuit, no label, no task: a bookmark is not a decision.
    assert _db(lambda s: T._pursuit_rows(s, ids["b"])) == []
    assert _db(_labels) == []


# ===================================================================================== the bar
def test_bulk_shortlist_adds_the_new_ones_and_undo_removes_only_those():
    ids = asyncio.run(T._seed())
    with T._client() as c:
        r = c.post("/pipeline/bulk/shortlist", data={"ids": f"{ids['a']},{ids['b']},{ids['d']}"},
                   headers=_HX)
        ev = _events(r)
        assert ev["k-toast"]["message"] == "2 roles added to your shortlist."
        assert ev["pl-clear"] is True and ev["pipelineRefresh"] is True
        undo = ev["k-toast"]["undoUrl"]
        assert f"{ids['a']}" not in undo.split("ids=")[1].split(",")  # a was already on it
        assert 'id="nav-count-shortlist"' in r.text
        assert c.post(undo, headers=_HX).status_code == 200
    assert _db(lambda s: T._pursuit_rows(s, ids["b"])) == []
    assert len(_db(lambda s: T._pursuit_rows(s, ids["a"]))) == 1  # the earlier one stays


def test_bulk_not_for_me_hides_with_no_label_and_undo_brings_them_back():
    ids = asyncio.run(T._seed())
    with T._client() as c:
        r = c.post("/pipeline/bulk/not-for-me", data={"ids": f"{ids['b']},{ids['d']}"}, headers=_HX)
        ev = _events(r)
        assert ev["pl-gone"] == {"ids": [ids["b"], ids["d"]]}
        assert ev["k-toast"]["message"] == "2 roles moved to Hidden."
        hidden = T._get(c, "/opportunities?hidden=1")
        assert {ids["b"], ids["d"]} <= set(T._row_ids(hidden))
        assert c.post(ev["k-toast"]["undoUrl"], headers=_HX).status_code == 200
        assert ids["b"] in T._row_ids(T._get(c, "/opportunities"))
    assert _db(_labels) == []  # a bulk hide gives no reason, so it teaches nothing


def test_bulk_mark_reviewed_records_an_agree_label_and_skips_unscored_roles():
    ids = asyncio.run(T._seed())
    with T._client() as c:
        r = c.post("/pipeline/bulk/mark-reviewed", data={"ids": f"{ids['b']},{ids['d']}"},
                   headers=_HX)
        ev = _events(r)
        assert ev["k-toast"]["message"] == "1 role marked reviewed. 1 role not scored yet."
        # Your call is a role the operator has not decided: reviewing it takes it off the chip.
        assert T._row_ids(T._get(c, "/opportunities?needs_review=1")) == []
        assert c.post(ev["k-toast"]["undoUrl"], headers=_HX).status_code == 200
        assert T._row_ids(T._get(c, "/opportunities?needs_review=1")) == [ids["b"]]
    assert _db(_labels) == []


def test_bulk_deeper_read_only_says_it_needs_a_per_role_ok_and_bad_calls_are_refused():
    ids = asyncio.run(T._seed())
    with T._client() as c:
        deeper = c.post("/pipeline/bulk/deeper-read", data={"ids": str(ids["a"])}, headers=_HX)
        nothing = c.post("/pipeline/bulk/shortlist", data={"ids": ""}, headers=_HX)
        unknown = c.post("/pipeline/bulk/delete-everything", data={"ids": str(ids["a"])})
        junk = c.post("/pipeline/bulk/shortlist", data={"ids": "x,-3,0"})
    assert "needs your OK for each role" in _events(deeper)["k-toast"]["message"]
    # The wording names the control the Role page actually offers.
    assert "Deeper read with a stronger model" in _events(deeper)["k-toast"]["message"]
    assert "Pick one or more roles first." in _events(nothing)["k-toast"]["message"]
    assert "not available" in _events(unknown)["k-toast"]["message"]
    assert "Pick one or more roles first." in _events(junk)["k-toast"]["message"]
    assert _db(lambda s: T._pursuit_rows(s, ids["d"])) == [] and _db(_labels) == []


def test_the_bar_offers_the_five_actions_and_score_again_goes_to_the_model_choice():
    asyncio.run(T._seed())
    with T._client() as c:
        html = c.get("/opportunities").text
    bar = html.split('id="pl-bulk" role=')[1].split('id="pl-bclear"')[0]
    for action in ("shortlist", "not-for-me", "score-again", "deeper-read", "mark-reviewed"):
        assert f'data-bulk="{action}"' in bar, action
    js = open("kestrel/web/static/js/pipeline.js", encoding="utf-8").read()
    assert "/rescore/choose?opp_ids=" in js and "page=1&next=" in js


# ============================================================================== saved views
def test_save_a_view_apply_it_make_it_default_and_remove_it():
    ids = asyncio.run(T._seed())
    with T._client() as c:
        form = {"view_name": "Mine", "shortlisted": "1", "sort": "first_seen", "q": "",
                "page": "3", "open": "9"}
        r = c.post("/pipeline/views", data=form, headers=_HX)
        assert _events(r)["k-toast"]["message"] == "Saved the view Mine."
        empty = c.post("/pipeline/views", data={"view_name": "  "}, headers=_HX)
        assert "Give the view a name first." in _events(empty)["k-toast"]["message"]

        async def views(s):
            return list((await s.execute(select(SavedView))).scalars())

        (v,) = _db(views)
        # Only filters and the sort are stored, never paging or the open panel.
        assert v.track == "employment" and v.filters == {"shortlisted": "1"}
        assert v.sort == {"sort": "first_seen"} and not v.is_default
        menu = c.get("/pipeline/views").text
        assert "My default" in menu and ">Mine" in menu and f"view={v.id}" in menu
        applied = c.get(f"/opportunities?view={v.id}")
        assert T._row_ids(applied.text) == [ids["a"]] and "View: Mine" in applied.text
        # With no view chosen the list is everything; a default opens first, until `view=none`.
        assert len(T._row_ids(c.get("/opportunities").text)) > 1
        assert c.post(f"/pipeline/views/{v.id}/default", headers=_HX).status_code == 200
        bare = c.get("/opportunities").text
        assert T._row_ids(bare) == [ids["a"]] and "View: Mine" in bare
        assert len(T._row_ids(c.get("/opportunities?view=none").text)) > 1
        # A change on top of a view wins over it.
        both = c.get(f"/opportunities?view={v.id}&shortlisted=")
        assert len(T._row_ids(both.text)) > 1
        assert "default" in c.get("/pipeline/views").text
        assert c.post(f"/pipeline/views/{v.id}/remove", headers=_HX).status_code == 200
        assert _db(views) == [] and len(T._row_ids(c.get("/opportunities").text)) > 1
    assert ids["a"]


def test_views_are_per_track_and_saving_the_same_name_replaces_its_filters():
    asyncio.run(T._seed())
    with T._client() as c:
        c.post("/pipeline/views", data={"view_name": "X", "hidden": "1"}, headers=_HX)
        c.post("/pipeline/views", data={"view_name": "X", "saved": "1"}, headers=_HX)
        c.post("/pipeline/views?track=consulting", data={"view_name": "Lead", "needs_review": "1"},
               headers=_HX)

        async def views(s):
            return [(v.track, v.name, v.filters) for v in
                    (await s.execute(select(SavedView).order_by(SavedView.id))).scalars()]

        rows = _db(views)
        employment = c.get("/pipeline/views").text
        consulting = c.get("/pipeline/views?track=consulting").text
    assert rows == [("employment", "X", {"saved": "1"}),
                    ("consulting", "Lead", {"needs_review": "1"})]
    assert ">X" in employment and ">Lead" not in employment
    assert ">Lead" in consulting and ">X" not in consulting


# ============================================================================= More filters
def test_more_filters_have_their_inputs_and_each_one_narrows_the_list(monkeypatch):
    ids = asyncio.run(T._seed())

    async def vary(s):
        a = await s.get(Opportunity, ids["a"])
        b = await s.get(Opportunity, ids["b"])
        d = await s.get(Opportunity, ids["d"])
        k = await s.get(Opportunity, ids["k"])
        a.remote_type, a.comp_max = "remote", 300000
        b.remote_type, b.comp_max, b.ghost_score = "onsite", 90000, 0.9
        d.comp_min = d.comp_max = None  # no posted pay
        k.comp_interval = "hour"  # an hourly rate is not under an annual floor
        k.comp_min = k.comp_max = 50
        a.liveness, b.liveness = "live", "closed"
        a.date_posted = T.iso(T._now() - timedelta(days=1))
        b.date_posted = T.iso(T._now() - timedelta(days=20))
        await s.commit()

    _db(vary)

    async def floor(track, which):
        return {"floor": 200000.0, "target": 280000.0}.get(which)

    monkeypatch.setattr("kestrel.web.routes_pages._pay_value", floor)
    with T._client() as c:
        page = c.get("/opportunities?more=1").text
        ids_of = lambda q: set(T._row_ids(T._get(c, f"/opportunities?{q}")))  # noqa: E731
        # Pay against the floor keeps roles with no posted pay and roles quoted in another unit.
        assert ids_of("pay=floor") == {ids["a"], ids["d"], ids["e"], ids["k"]}
        assert ids_of("pay=target") == {ids["a"], ids["d"], ids["k"]}  # e tops out at 260k
        assert ids_of("work=remote") == {ids["a"]} and ids_of("work=onsite") == {ids["b"]}
        assert ids_of("posted=3") >= {ids["a"]} and ids["b"] not in ids_of("posted=3")
        assert ids["b"] in ids_of("posted=30")
        assert ids["b"] not in ids_of("warn=no_ghost") and ids["a"] in ids_of("warn=no_ghost")
        assert ids_of("warn=review") == {ids["b"]}
        assert ids_of("live=live") == {ids["a"]} and ids_of("live=closed") == {ids["b"]}
        assert ids["a"] in ids_of("score_min=4") and ids["b"] not in ids_of("score_min=4")
        assert ids_of("org=north") >= {ids["a"], ids["b"]}
        assert ids_of("source=greenhouse") >= {ids["a"]} and ids_of("source=lever") == set()
        legit = ids_of("legit=clean")
        assert ids["a"] in legit and ids_of("legit=reject") == set()
        # Bad values are ignored, never a 500.
        junk = "/opportunities?pay=x&work=y&posted=z&warn=q&live=w&legit=v"
        assert c.get(junk).status_code == 200
    for name in ("score_min", "pay", "work", "posted", "org", "source", "warn", "live", "legit"):
        assert f'name="{name}"' in page, name
    assert 'id="pl-mf"' in page and 'data-testid="save-view"' in page
    assert "At or above my minimum" in page and "Hide possible ghost postings" in page


def test_the_pay_filter_reads_the_operators_floor_and_target():
    from kestrel.web import routes_pages as rp

    assert asyncio.run(rp._pay_value("employment", "bogus")) is None
    got = asyncio.run(rp._pay_value("employment", "floor"))
    assert got is None or isinstance(got, float)


# ======================================================================== near-floor pay tag
def test_a_role_within_the_margin_shows_below_your_minimum_and_no_pay_never_does():
    ids = asyncio.run(T._seed())

    async def near(s):
        a = await s.get(Opportunity, ids["a"])
        a.stage_reason = "soft:near_floor; comp_max 112000 within 5% of the floor"
        d = await s.get(Opportunity, ids["d"])
        d.comp_min = d.comp_max = None
        d.stage_reason = "soft:near_floor"  # stale text, but there is no posted pay to be near
        await s.commit()

    _db(near)
    with T._client() as c:
        html = T._get(c, "/opportunities")
        panel = c.get(f"/opportunities/{ids['a']}/panel").text
        none = c.get(f"/opportunities/{ids['d']}/panel").text
    row = T._row(html, ids["a"])
    assert re.search(r'data-testid="near-floor"[^>]*>Below your minimum</span>', row)
    assert "near-floor" not in T._row(html, ids["d"])
    assert 'data-testid="pay-note">Below your minimum</div>' in panel
    assert "Below your minimum" not in none


def test_a_role_with_no_posted_pay_reads_pay_not_listed():
    ids = asyncio.run(T._seed())

    async def none(s):
        b = await s.get(Opportunity, ids["b"])
        b.comp_min = b.comp_max = None
        await s.commit()

    _db(none)
    with T._client() as c:
        row = T._row(T._get(c, "/opportunities"), ids["b"])
        panel = c.get(f"/opportunities/{ids['b']}/panel").text
    assert "Pay not listed" in row and "Pay not listed" in panel
    assert "No pay posted. Kestrel kept it." in panel


def test_an_estimated_pay_is_marked_est_and_never_shown_as_posted():
    ids = asyncio.run(T._seed())

    async def est(s):
        a = await s.get(Opportunity, ids["a"])
        a.comp_source = "inferred"
        await s.commit()

    _db(est)
    with T._client() as c:
        row = T._row(T._get(c, "/opportunities"), ids["a"])
        panel = c.get(f"/opportunities/{ids['a']}/panel").text
    assert "$230,000 to $260,000 est." in row
    assert 'class="pl-est"' in panel and "does not list pay" in panel


# ============================================================================ the row's parts
def test_a_row_says_how_long_ago_it_was_found_and_the_hover_says_both_ages():
    ids = asyncio.run(T._seed())

    async def ages(s):
        a = await s.get(Opportunity, ids["a"])
        a.first_seen_at = T.iso(T._now() - timedelta(hours=3))
        a.date_posted = T.iso(T._now() - timedelta(days=2))
        d = await s.get(Opportunity, ids["d"])
        d.first_seen_at = T.iso(T._now() - timedelta(days=2))
        await s.commit()

    _db(ages)
    with T._client() as c:
        html = T._get(c, "/opportunities")
    a, d = T._row(html, ids["a"]), T._row(html, ids["d"])
    assert re.search(
        r'class="pl-age is-fresh" title="Found by Kestrel 3h ago; posted 2 days ago\.">'
        r"Found 3h ago<", a)
    assert re.search(r'class="pl-age" title="[^"]*">Found 2d ago<', d)  # not brand-colored


# ======================================================================== Consulting signals
async def _signals(s, org_name: str = "Ridgeline Utilities"):
    org = (await s.execute(select(Org).where(Org.name == org_name))).scalar_one()
    src = (await s.execute(select(Source))).scalars().first()
    out = {}
    for key, title, kind, hours in (
        ("sig1", "New CFO announced a systems review", "cio_departure", 20),
        ("sig2", "Closed an acquisition of a regional insurer", "acquisition", 60),
        ("sig3", "Raised a Series B round", "funding_round", 300),
    ):
        o = T._opp(
            org, src, key, track="consulting", opp_type="trigger_event", title=title, url=None,
            stage="t2_done", comp_min=None, comp_max=None, location_raw=None,
            first_seen_at=T.iso(T._now() - timedelta(hours=hours)),
            event_date=T.iso(T._now() - timedelta(hours=hours))[:10],
            trigger_data={"event_type": kind, "evidence_quote": "The board named a review.",
                          "implied_need": "New finance leaders often bring in outside help.",
                          "source_url": "https://example.com/news"},
            description_md="x",
        )
        s.add(o)
        await s.flush()
        s.add(T._ev(o, track="consulting", verdict="pursue", verdict_model="pursue",
                    score=3.9 if hours < 100 else 2.6, blocks={
                        "e_approach": {"opening_move": "A short note on a 30-day review.",
                                       "to_whom": "The new CFO.", "why_now_line": "x"},
                    }))
        out[key] = o.id
    await s.commit()
    return out


def test_consulting_has_two_tabs_and_signals_group_this_week_and_earlier():
    ids = asyncio.run(T._seed())
    sig = _db(_signals)
    with T._client() as c:
        opp = c.get("/opportunities?track=consulting").text
        sigs = c.get("/opportunities?track=consulting&tab=signals").text
        emp = c.get("/opportunities?track=employment").text
        week = c.get("/opportunities?track=consulting&tab=signals&type=week").text
        acq = c.get("/opportunities?track=consulting&tab=signals&type=acquisition").text
    # Two tabs with counts; Employment has none. Opportunities leave trigger events out.
    assert 'data-testid="tab-opportunities"' in opp and 'data-testid="tab-signals"' in opp
    assert 'id="pl-tabcount-sig">3</b>' in opp and 'role="tablist"' in opp
    assert 'data-testid="tab-signals"' not in emp
    assert T._row_ids(opp) == [ids["c"]] and not set(T._row_ids(opp)) & set(sig.values())
    # Signals: This week then Earlier, type chips, the day-one copy.
    assert set(T._row_ids(sigs)) == set(sig.values())
    assert "This week · 2" in sigs and "Earlier · 1" in sigs
    assert sigs.index("This week · 2") < sigs.index("Earlier · 1")
    for label in ("Leadership change", "Acquisition", "Expansion", "Funding", "Not relevant"):
        assert label in sigs, label
    assert "Signals are public events" in sigs and 'data-testid="pl-signal"' in sigs
    assert set(T._row_ids(week)) == {sig["sig1"], sig["sig2"]}
    assert set(T._row_ids(acq)) == {sig["sig2"]}
    assert "2 signals · 2 this week" not in sigs and "3 signals · 2 this week" in sigs


def test_a_signal_panel_says_what_happened_why_who_and_the_opening_move():
    asyncio.run(T._seed())
    sig = _db(_signals)
    with T._client() as c:
        html = c.get(f"/opportunities/{sig['sig1']}/panel").text
    assert 'data-kind="signal"' in html and "Signal · Leadership change" in html
    assert "What happened" in html and "New CFO announced a systems review" in html
    assert 'href="https://example.com/news"' in html and "Read the source" in html
    assert "Why it may need outside help" in html and "New finance leaders often bring" in html
    assert "Who to approach" in html and "The new CFO." in html
    assert "Opening move" in html and "A short note on a 30-day review." in html
    assert "Kestrel never contacts anyone" in html
    for text in ("Not my kind of work", "Wrong size company", "No real need", "Just not now"):
        assert f">{text}</button>" in html, text
    assert ">Add to leads<" in html and "Research company" in html
    assert "Not relevant because…" in html


def test_add_to_leads_from_a_signal_uses_existing_pursuit_fields_only():
    asyncio.run(T._seed())
    sig = _db(_signals)
    with T._client() as c:
        form = {"step": "Call the new CFO", "due": "today"}
        r = c.post(f"/opportunities/{sig['sig1']}/lead", data=form, headers=_HX)
        ev = _events(r)
        assert ev["k-toast"]["message"] == "Added to Leads. Next step: Call the new CFO, today."
        assert "In my leads" in r.text
        undo = ev["k-toast"]["undoUrl"]
        blank = {"step": "", "due": "next"}
        empty = c.post(f"/opportunities/{sig['sig2']}/lead", data=blank, headers=_HX)
        listed = c.get("/opportunities?track=consulting&tab=signals").text
        assert c.post(undo, headers=_HX).status_code == 200

    async def pursuits(s):
        return {p.opportunity_id: p for p in (await s.execute(select(Pursuit))).scalars()}

    got = _db(pursuits)
    # A pursuit from status, next_action and next_action_at only: no contact, stage or amount.
    p = got[sig["sig2"]]
    assert p.track == "consulting" and p.next_action == "Research the company"
    from datetime import date

    assert (date.fromisoformat(p.next_action_at) - date.today()).days in (6, 7, 8)
    assert sig["sig1"] not in got  # undone
    assert "Next step: Research the company, next week." in _events(empty)["k-toast"]["message"]
    assert 'data-testid="in-leads"' in listed


def test_due_dates_are_today_three_days_and_a_week_on():
    from datetime import UTC, date, datetime

    from kestrel.web.pages.pipeline import due_date

    now = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)
    local = now.astimezone().date()
    assert due_date("today", now) == local.isoformat()
    assert due_date("week", now) == (local + timedelta(days=3)).isoformat()
    assert due_date("next", now) == (local + timedelta(days=7)).isoformat()
    assert due_date("nonsense", now) == due_date("week", now)
    assert isinstance(local, date)


def test_not_relevant_hides_a_signal_and_its_reasons_are_its_own():
    asyncio.run(T._seed())
    sig = _db(_signals)
    with T._client() as c:
        r = c.post(f"/opportunities/{sig['sig1']}/decide/not-for-me?reason=need", headers=_HX)
        ev = _events(r)
        assert ev["k-toast"]["message"].startswith("Marked not relevant.")
        listed = c.get("/opportunities?track=consulting&tab=signals").text
        hidden = c.get("/opportunities?track=consulting&tab=signals&hidden=1").text
    assert sig["sig1"] not in T._row_ids(listed) and T._row_ids(hidden) == [sig["sig1"]]
    (label,) = _db(_labels)
    assert label.reason == "Not relevant: No real need."
    assert label.dimension_corrections == {"reason": "need", "dimension": "strategic_value"}


def test_signals_are_redacted_in_confidential_mode():
    asyncio.run(T._seed(confidential=True))
    sig = _db(lambda s: _signals(s, T._EMPLOYER))
    with T._client() as c:
        page = c.get("/opportunities?track=consulting&tab=signals").text
        panel = c.get(f"/opportunities/{sig['sig1']}/panel").text
    assert T._EMPLOYER not in page and T._EMPLOYER not in panel
    assert "https://example.com/news" not in panel  # a source link can name the employer


# ================================================================================ view helpers
def test_view_helpers_are_plain_and_stable():
    from kestrel.web import pipeline_view as pv

    assert pv.monogram("Larkspur Logistics")["letters"] == "LL"
    assert pv.monogram("northwind")["letters"] == "N"
    assert pv.monogram("Larkspur Logistics") == pv.monogram("Larkspur Logistics")
    assert 0 <= pv.monogram("x")["tone"] < pv.MONO_TONES and pv.monogram(None)["letters"] == "?"
    assert pv.ago_words(0.2) == "just now" and pv.ago_words(5.4) == "5h ago"
    assert pv.ago_words(49) == "2d ago"
    assert [r[0] for r in pv.REASONS_EMPLOYMENT] == ["kind", "pay_place", "level", "later"]
    assert [r[2] for r in pv.REASONS_EMPLOYMENT] == [
        "role_fit", "comp_logistics", "trajectory", None]
    assert pv.reason_by_key("employment", "rate") is None
    assert pv.reason_by_key("consulting", "rate")[1] == "Rate too low"


def test_a_healthy_row_has_no_warning_and_the_warning_names_what_is_wrong():
    from types import SimpleNamespace as NS

    from kestrel.web import pipeline_view as pv

    opp = NS(ghost_score=0.0, seen_count=1, liveness="live", track="employment")
    ev = NS(legitimacy={"status": "clean"}, needs_review=False, tier=2, prompt_version=None)
    assert pv.warning_text(opp, ev) is None
    opp.ghost_score, opp.seen_count = 0.8, 4
    assert "Possible ghost posting: Kestrel has seen it 4 times." in pv.warning_text(opp, ev)
    opp.liveness = "closed"
    assert "looks closed" in pv.warning_text(opp, ev)
    ev.legitimacy = {"status": "reject"}
    assert pv.warning_text(opp, ev).startswith("This may not be a real opening.")
    stale = NS(legitimacy=None, needs_review=False, tier=2, prompt_version="evaluate/v0")
    assert "older scoring instructions" in pv.warning_text(
        NS(ghost_score=0.0, seen_count=1, liveness="live", track="employment"), stale)


def test_the_pipeline_sheet_and_templates_obey_the_rules():
    from pathlib import Path

    root = Path("kestrel/web")
    css = (root / "static/css/pages/pipeline.css").read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(", css)
    assert "@media (prefers-reduced-motion" in css and "@container" in css
    page = (root / "templates/opportunities.html").read_text(encoding="utf-8")
    assert "static_url('css/pages/pipeline.css')" in page and "static_url('js/pipeline.js')" in page
    assert "css/shell.css" in page and "kestrel.css" not in page  # no legacy sheet
    for name in ("opportunities.html", "partials/opp_rows.html", "partials/pl_row.html",
                 "partials/pl_panel.html", "partials/pl_views.html"):
        text = (root / "templates" / name).read_text(encoding="utf-8")
        assert "—" not in text, name
    js = (root / "static/js/pipeline.js").read_text(encoding="utf-8")
    assert "—" not in js


def test_the_old_panel_markup_is_gone_from_the_page_and_the_new_one_is_wired():
    asyncio.run(T._seed())
    with T._client() as c:
        html = c.get("/opportunities").text
    assert 'id="drawer"' not in html and 'x-data="pipeline()"' not in html
    assert 'id="pl-panel"' in html and 'src="/static/js/pipeline.js?v=' in html
    assert 'href="/static/css/pages/pipeline.css?v=' in html
    assert '/static/css/kestrel.css' not in html
    assert c.get("/static/css/pages/pipeline.css").status_code == 200


def test_a_bookmark_is_state_not_configuration():
    from kestrel.services.setting_overrides import NON_CONFIG_PREFIXES

    assert "saved_role." in NON_CONFIG_PREFIXES
    ids = asyncio.run(T._seed())
    with T._client() as c:
        c.post(f"/opportunities/{ids['b']}/decide/save")

    async def rows(s):
        return [r.key for r in (await s.execute(select(Setting))).scalars()]

    assert f"saved_role.{ids['b']}" in _db(rows)


def test_a_lead_row_shows_its_engagement_value_and_a_posting_count():
    ids = asyncio.run(T._seed())

    async def value(s):
        ev = (await s.execute(select(T.Evaluation).where(
            T.Evaluation.opportunity_id == ids["c"]))).scalar_one()
        ev.blocks = {**ev.blocks,
                     "d_economics": {"engagement_value": 18000, "vs_floor": "above_floor"}}
        c = await s.get(Opportunity, ids["c"])
        c.seen_count = 2
        await s.commit()

    _db(value)
    with T._client() as c:
        html = T._get(c, "/opportunities?track=consulting")
        panel = c.get(f"/opportunities/{ids['c']}/panel").text
    row = T._row(html, ids["c"])
    assert "Ridgeline Utilities · Remote · $2,400 a day · about $18,000 total · Seen twice" in row
    assert 'class="pl-kind">RFP' in row.replace("\n", "").replace("  ", "") or ">RFP</div>" in row
    assert "Above your day-rate floor" in panel and "Rate" in panel


def test_the_page_script_announces_arrivals_and_never_hardcodes_a_color():
    js = open("kestrel/web/static/js/pipeline.js", encoding="utf-8").read()
    assert "is-fresh-in" in js and "announceArrivals" in js
    assert "rgb(" not in js and "rgba(" not in js


# ============================================================== review fixes (undo safety, privacy)
def test_add_to_leads_on_an_existing_lead_offers_no_undo_and_undo_spares_worked_pursuits():
    asyncio.run(T._seed())
    sig = _db(_signals)
    with T._client() as c:
        first = c.post(f"/opportunities/{sig['sig1']}/lead", data={"due": "week"}, headers=_HX)
        assert "undoUrl" in _events(first)["k-toast"]

        async def work(s):
            p = (await s.execute(select(Pursuit).where(
                Pursuit.opportunity_id == sig["sig1"]))).scalar_one()
            p.status = "proposal_sent"
            await s.commit()

        _db(work)
        again = c.post(f"/opportunities/{sig['sig1']}/lead", data={"due": "week"}, headers=_HX)
        assert "undoUrl" not in _events(again)["k-toast"]
        # A stale Undo from the first click must not delete the pursuit the operator worked.
        c.post(f"/opportunities/{sig['sig1']}/decide/unshortlist", headers=_HX)
        c.post(f"/pipeline/bulk-undo/shortlist?ids={sig['sig1']}", headers=_HX)
    assert len(_db(lambda s: T._pursuit_rows(s, sig["sig1"]))) == 1


def test_undo_urls_only_touch_what_the_action_touched():
    ids = asyncio.run(T._seed())

    async def labels(s):
        s.add(VerdictOverride(opportunity_id=ids["a"], operator_verdict="apply", operator_score=4.5,
                              reason="my real word"))
        await s.commit()

    _db(labels)
    real = _db(_labels)[0].id
    with T._client() as c:
        c.post(f"/pipeline/bulk-undo/mark-reviewed?labels={real}", headers=_HX)
        c.post(f"/opportunities/{ids['a']}/decide/undo-hide?label={real}", headers=_HX)
        # A role hidden some other way (applied elsewhere) is not unhidden by an Undo URL.
        c.post(f"/opportunities/{ids['h']}/decide/undo-hide", headers=_HX)
        c.post(f"/pipeline/bulk-undo/not-for-me?ids={ids['h']}", headers=_HX)
    assert [lb.reason for lb in _db(_labels)] == ["my real word"]

    async def hidden(s):
        return (await s.get(Opportunity, ids["h"])).archived_at

    assert _db(hidden) is not None


def test_mark_reviewed_writes_one_label_per_read_not_a_new_one_each_click():
    ids = asyncio.run(T._seed())
    with T._client() as c:
        c.post("/pipeline/bulk/mark-reviewed", data={"ids": str(ids["b"])}, headers=_HX)
        again = c.post("/pipeline/bulk/mark-reviewed", data={"ids": str(ids["b"])}, headers=_HX)
    assert len(_db(_labels)) == 1
    assert "0 roles marked reviewed" in _events(again)["k-toast"]["message"]


def test_hide_company_toast_does_not_name_the_employer_in_confidential_mode():
    ids = asyncio.run(T._seed(confidential=True))
    with T._client() as c:
        r = c.post(f"/opportunities/{ids['d']}/decide/hide-company", headers=_HX)
    toast = _events(r)["k-toast"]["message"]
    assert T._EMPLOYER not in toast and T._PLACEHOLDER in toast


def test_the_pay_filter_never_drops_an_estimate_or_another_currency(monkeypatch):
    ids = asyncio.run(T._seed())

    async def vary(s):
        a = await s.get(Opportunity, ids["a"])
        a.comp_max, a.comp_source = 90000, "inferred"  # an estimate under the floor
        b = await s.get(Opportunity, ids["b"])
        b.comp_max, b.comp_currency = 90000, "GBP"  # pounds, not compared with a dollar floor
        k = await s.get(Opportunity, ids["k"])
        k.comp_max = 90000  # posted dollars under the floor: this one goes
        await s.commit()

    _db(vary)

    async def floor(track, which):
        return {"floor": 200000.0}.get(which)

    monkeypatch.setattr("kestrel.web.routes_pages._pay_value", floor)
    with T._client() as c:
        got = set(T._row_ids(T._get(c, "/opportunities?pay=floor")))
    assert ids["a"] in got and ids["b"] in got and ids["k"] not in got


def test_the_review_warning_filter_means_the_same_as_your_call():
    ids = asyncio.run(T._seed())
    with T._client() as c:
        c.post("/pipeline/bulk/mark-reviewed", data={"ids": str(ids["b"])}, headers=_HX)
        got = T._row_ids(T._get(c, "/opportunities?warn=review"))
        chip = T._row_ids(T._get(c, "/opportunities?needs_review=1"))
    assert got == chip == []


# ================================================================== review fixes round 2
def _rehide_separately(oid: int):
    async def go(s):
        o = await s.get(Opportunity, oid)
        o.archived_at, o.archive_reason = "2099-01-01T00:00:00.000000+00:00", "not_interested"
        await s.commit()

    _db(go)


def _archived_at(oid: int):
    async def go(s):
        return (await s.get(Opportunity, oid)).archived_at

    return _db(go)


def test_a_stale_undo_hide_does_not_restore_a_role_hidden_separately_afterward():
    ids = asyncio.run(T._seed())
    with T._client() as c:
        r = c.post(f"/opportunities/{ids['b']}/decide/not-for-me?reason=kind", headers=_HX)
        undo = _toast_undo(r)
        assert "at=" in undo
        _rehide_separately(ids["b"])
        c.post(undo, headers=_HX)
        assert _archived_at(ids["b"]) == "2099-01-01T00:00:00.000000+00:00"
        assert [lb.reason for lb in _db(_labels)] == ["Not for me: Not my kind of role."]
        # A fresh hide's own Undo still works.
        _db(lambda s: _unarchive(s, ids["b"]))
        r2 = c.post(f"/opportunities/{ids['b']}/decide/not-for-me?reason=level", headers=_HX)
        c.post(_toast_undo(r2), headers=_HX)
    assert _archived_at(ids["b"]) is None


async def _unarchive(s, oid):
    o = await s.get(Opportunity, oid)
    o.archived_at = o.archive_reason = None
    await s.commit()


def test_a_stale_bulk_undo_refuses_the_whole_action():
    ids = asyncio.run(T._seed())
    with T._client() as c:
        r = c.post("/pipeline/bulk/not-for-me", data={"ids": f"{ids['a']},{ids['b']}"}, headers=_HX)
        undo = _toast_undo(r)
        assert "at=" in undo
        # The operator put one back and hid it again on its own before pressing the old Undo.
        _rehide_separately(ids["b"])
        c.post(undo, headers=_HX)
    assert _archived_at(ids["a"]) is not None
    assert _archived_at(ids["b"]) == "2099-01-01T00:00:00.000000+00:00"


def test_the_on_my_shortlist_button_keeps_full_strength_in_the_page_css():
    import importlib.resources as ir

    css = (ir.files("kestrel.web") / "static/css/pages/pipeline.css").read_text("utf-8")
    assert ".pl-foot .k-btn--done[disabled]" in css and "opacity: 1" in css
