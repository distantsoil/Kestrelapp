"""The Offers screen (D-UI-122, D-UI-136): every route and action, one test per state of the
locked mockup, and the rules the screen must keep (the walk-away is the operator's, nothing is
sent, a sample under five shows no range, the current employer is redacted).

Cloud-safe: database and web layer only; the model is a stand-in and nothing touches the network.
The fictional offers are the mockup's (tests/offers_seed.py)."""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import make_async_engine
from kestrel.db.models import (
    EngagementOffer,
    Offer,
    Outcome,
    Pursuit,
    Setting,
    StatusEvent,
)
from kestrel.negotiation.scripts import ScriptResult
from kestrel.privacy import Employer, EmployerMap
from kestrel.web import offers_view as ov
from tests import offers_seed

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

HX = {"HX-Request": "true"}


@pytest.mark.parametrize("track", ["employment", "consulting"])
async def test_old_offer_undo_preserves_a_later_close(track):
    await offers_seed.seed_async()
    # Pick a real open offer on the requested track.
    model = Offer if track == "employment" else EngagementOffer
    async with _sm()() as s:
        offer = await s.execute(select(model).where(model.status.in_(("written", "verbal"))))
        oid = offer.scalars().first().id
    with _client() as c:
        old = _toast(c.post(f"/offers/{track}/{oid}/outcome", data={"outcome": "declined"}))
        c.post(f"/offers/{track}/{oid}/reopen", headers=HX)
        c.post(f"/offers/{track}/{oid}/outcome", data={"outcome": "accepted"})
        before = await _get(model, oid)
        result = c.post(old["undoUrl"])
    assert "This can no longer be undone." in result.text
    assert (await _get(model, oid)).status == "accepted"
    p = await _get(Pursuit, before.pursuit_id)
    assert p.status == ("accepted" if track == "employment" else "won")
    async with _sm()() as s:
        outcomes = (await s.execute(select(Outcome).where(Outcome.pursuit_id == p.id))).scalars()
        assert [o.final_status for o in outcomes] == [p.status]


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _sm() -> async_sessionmaker:
    return async_sessionmaker(make_async_engine(paths.db_path()), expire_on_commit=False)


async def _get(model, id_):
    async with _sm()() as s:
        return await s.get(model, id_)


def _toast(r) -> dict:
    return json.loads(r.headers["HX-Trigger"])["k-toast"]


# ------------------------------------------------------------------------------ Compare tab
async def test_the_header_the_tabs_and_one_card_per_offer():
    await offers_seed.seed_async()
    with _client() as c:
        r = c.get("/offers")
    t = r.text
    assert r.status_code == 200
    assert "<h1>Offers</h1>" in t
    assert 'data-testid="of-count"' in t and "2 open" in t
    assert "Record an offer" in t
    # One lede line, stated once: the trade-offs, never advice, never sent.
    assert t.count("never advises and never sends anything") == 1
    for tab in ("Compare", "Negotiate", "History"):
        assert f">{tab}</a>" in t
    assert t.count('data-testid="of-card"') == 2
    assert "Larkspur Logistics" in t and "Northwind Health" in t
    assert "first year, all in" in t


async def test_the_card_shows_money_the_stacked_bar_and_the_facts_grid():
    await offers_seed.seed_async()
    with _client() as c:
        t = c.get("/offers").text
    assert "$155k" in t  # 136,000 + 10% + 5,000
    assert "Base $136k" in t and "Bonus 10%" in t and "Signing $5k" in t
    assert "Equity $10k/yr" in t  # a fourth of the $40,000 estimate
    for fact in ("Level", "Reports to", "Where", "Time off", "Starts", "Equity"):
        assert f"<span>{fact}</span>" in t
    assert "Austin, TX · Hybrid, 3 days" in t
    assert "Equity is counted at your own estimate" in t  # behind Show details


async def test_the_deadline_pill_turns_amber_at_seven_days_or_fewer():
    await offers_seed.seed_async()
    with _client() as c:
        t = c.get("/offers").text
    pills = re.findall(r'<span class="(of-dl[^"]*)"[^>]*>.*?Decide by ([^<]*)', t)
    by_days = {text.split("·")[-1].strip(): cls for cls, text in pills}
    assert "is-soon" in by_days["5 days"]  # Northwind
    assert "is-soon" not in by_days["9 days"]  # Larkspur


def test_deadline_view_edges():
    on = date(2026, 10, 24)
    fmt = "MM/DD/YYYY"
    assert ov.deadline_view("2026-10-31", today=on, fmt=fmt)["soon"] is True  # seven days
    assert ov.deadline_view("2026-11-01", today=on, fmt=fmt)["soon"] is False
    assert "today" in ov.deadline_view("2026-10-24", today=on, fmt=fmt)["text"]
    late = ov.deadline_view("2026-10-22", today=on, fmt=fmt)
    assert late["soon"] is True and "passed 2 days ago" in late["text"]
    assert ov.deadline_view(None, today=on, fmt=fmt) is None
    assert ov.deadline_view("not a date", today=on, fmt=fmt) is None


async def test_the_pay_band_and_a_similar_range_only_from_five_observations():
    await offers_seed.seed_async()
    with _client() as c:
        t = c.get("/offers").text
    # Larkspur: twelve similar roles, so the bracket and its count are drawn.
    assert "Similar roles · 12" in t and "Similar roles pay" in t and "(12 roles)" in t
    # Northwind: three observations, so no range and the count in words (hard rule 6).
    assert "Too few similar roles (3)" in t
    assert "Under 5, so Kestrel shows no range." in t
    assert t.count('class="of-band__sim"') == 1
    assert "Floor $120k" in t and "Target $150k" in t
    assert "between your floor and target" in t and "above your target" in t
    assert "Posted range $118,000 to $142,000" in " ".join(t.split())


def test_band_view_needs_five_to_draw_a_bracket():
    kw = dict(value=136000, floor=120000, target=150000, unit="Base", noun="roles")
    drawn = ov.band_view(similar=(121000, 136000), similar_n=12, **kw)
    assert drawn["similar"]["label"] == "Similar roles · 12"
    hidden = ov.band_view(similar=None, similar_n=3, **kw)
    assert hidden["similar"] is None and len(hidden["ticks"]) == 2
    assert ov.band_view(value=100, floor=None, target=None, similar=None, similar_n=0,
                        unit="Base", noun="roles") is None


async def test_a_pay_floor_that_is_not_set_points_to_profile_not_a_made_up_one(monkeypatch):
    await offers_seed.seed_async(profile=False)
    with _client() as c:
        t = c.get("/offers").text
        assert "Floor $" not in t  # the shipped example has a target and no floor
        monkeypatch.setattr("kestrel.web.pages.offers._floor_target", lambda track: (None, None))
        none = c.get("/offers").text
    assert "Floor $" not in none and "Target $" not in none
    assert 'href="/profile">Profile</a> to see this beside your pay band' in none


async def test_the_comparison_has_money_bars_stars_weights_and_no_winner():
    await offers_seed.seed_async()
    with _client() as c:
        t = c.get("/offers").text
    assert 'data-testid="of-matrix"' in t and "Comparison matrix" in t
    for label in ("First-year total", "Four-year value", "Level and scope", "Reporting line",
                  "Location and travel", "Company risk", "Fit with your direction"):
        assert label in t
    assert t.count('class="of-w"') == 7  # a live weight on every row
    assert "Matters" in t  # unset weights read Matters
    assert "$603k" in t and "$740k" in t  # four-year value, no raises
    assert "Ahead on first-year total, four-year value" in t
    assert "This reflects your weights and ratings, not advice." in t
    assert "First-year total is base, target bonus, signing and one fourth" in t
    shown = t[t.index('id="of-matrix"'): t.index('id="of-dialog"')].lower()  # not the flyouts
    assert "winner" not in shown and "recommend" not in shown and "best offer" not in shown
    # Judgment rows show the source fact beside the operator's own stars.
    assert "Senior Analyst" in t and "VP of Operations" in t and "No research yet" in t
    assert 'hx-post="/offers/employment/' in t and "/rate" in t


async def test_one_open_offer_says_when_the_comparison_appears():
    ids = await offers_seed.seed_async()
    with _client() as c:
        c.post(f"/offers/employment/{ids['Northwind Health']['offer']}/outcome",
               data={"outcome": "declined"})
        t = c.get("/offers").text
    assert "The comparison appears when two or more offers are open." in t
    assert 'data-testid="of-matrix"' not in t


# ------------------------------------------------------------------------------ the stars
async def test_a_star_is_the_operators_rating_and_pressing_it_again_clears_it():
    ids = await offers_seed.seed_async()
    oid = ids["Larkspur Logistics"]["offer"]
    with _client() as c:
        r = c.post(f"/offers/employment/{oid}/rate", data={"axis": "level_scope", "value": "4"})
        assert r.status_code == 200 and 'id="of-matrix"' in r.text
        assert "<html" not in r.text  # the matrix alone swaps
        assert (await _get(Offer, oid)).qual_json == {"level_scope": 4.0}
        c.post(f"/offers/employment/{oid}/rate", data={"axis": "org_risk", "value": "2"})
        assert (await _get(Offer, oid)).qual_json == {"level_scope": 4.0, "org_risk": 2.0}
        c.post(f"/offers/employment/{oid}/rate", data={"axis": "level_scope", "value": "4"})
        assert (await _get(Offer, oid)).qual_json == {"org_risk": 2.0}  # same star: cleared


async def test_a_star_for_a_row_the_track_does_not_have_changes_nothing():
    ids = await offers_seed.seed_async()
    oid = ids["Larkspur Logistics"]["offer"]
    with _client() as c:
        for axis in ("payment_terms", "made_up", "trajectory_fit"):
            r = c.post(f"/offers/employment/{oid}/rate", data={"axis": axis, "value": "5"})
            assert r.status_code == 204
        assert c.post("/offers/nowhere/1/rate", data={"axis": "level_scope",
                                                      "value": "5"}).status_code == 204
    assert (await _get(Offer, oid)).qual_json is None


async def test_stars_are_clamped_and_land_on_the_right_table():
    ids = await offers_seed.seed_async()
    emp, con = ids["Larkspur Logistics"]["offer"], ids["Meadowbrook Foods"]["offer"]
    with _client() as c:
        c.post(f"/offers/consulting/{con}/rate", data={"axis": "payment_terms", "value": "99"})
    assert (await _get(EngagementOffer, con)).qual_json == {"payment_terms": 5.0}
    assert (await _get(Offer, emp)).qual_json is None  # same id elsewhere is untouched


# ---------------------------------------------------------------------------------- weights
async def test_a_weight_changes_who_is_ahead_and_swaps_only_the_summary():
    await offers_seed.seed_async()
    with _client() as c:
        r = c.post("/offers/weights", data={"total_first_year": "0", "four_year_value": "0"})
        assert r.status_code == 200
        assert 'id="of-summary"' in r.text and "<html" not in r.text
        assert "Not clearly ahead on anything you weight highly" in r.text
        assert "Ahead on" not in r.text.replace("Not clearly ahead on", "")
        again = c.post("/offers/weights", data={"total_first_year": "5", "four_year_value": "3"})
        assert "Ahead on first-year total, four-year value" in again.text
    async with _sm()() as s:
        assert (await s.get(Setting, "negotiation.matrix_weights")).value == {
            "total_first_year": 5.0, "four_year_value": 3.0}


async def test_weights_are_clamped_to_ignore_through_most():
    await offers_seed.seed_async()
    with _client() as c:
        c.post("/offers/weights", data={"total_first_year": "40", "four_year_value": "-2"})
        t = c.get("/offers").text
    assert "Most" in t and "Ignore" in t


def test_ahead_on_needs_a_single_best_on_a_row_weighted_matters_or_more():
    def card(i, fy):
        return {"id": i, "track": "employment", "org": f"Org {i}", "title": "T", "mono": "O",
                "mono_i": 0, "terms": {}, "offer": type("O", (), {"qual_json": None,
                                                                 "base": fy})(),
                "total": {"value": fy}, "fit": None}

    cards = [card(1, 100), card(2, 100)]  # a tie leads nowhere
    assert ov.build_matrix(cards, "employment", {}).summary == [
        {"name": "Org 1", "leads": []}, {"name": "Org 2", "leads": []}]
    cards = [card(1, 100), card(2, 200)]
    m = ov.build_matrix(cards, "employment",
                        {"total_first_year": 2, "four_year_value": 2})  # below Matters
    assert m.summary[1]["leads"] == []
    m = ov.build_matrix(cards, "employment", {"total_first_year": 3, "four_year_value": 0})
    assert m.summary[1]["leads"] == ["first-year total"]
    assert ov.ahead_text([]).startswith("Not clearly ahead")


# ---------------------------------------------------------------------------------- outcomes
async def test_i_accepted_closes_the_offer_and_the_pursuit_and_undo_reopens():
    ids = await offers_seed.seed_async()
    me = ids["Northwind Health"]
    with _client() as c:
        r = c.post(f"/offers/employment/{me['offer']}/outcome", data={"outcome": "accepted"},
                   headers=HX)
        assert r.status_code == 200
        toast = _toast(r)
        assert "Marked accepted" in toast["message"]
        assert toast["undoUrl"].startswith(f"/offers/employment/{me['offer']}/reopen")
        assert 'hx-swap-oob="outerHTML"' in r.text and "1 open" in r.text
        assert "is-closed" in r.text  # dimmed, still there, with Reopen
        assert ">Reopen<" in r.text
    assert (await _get(Offer, me["offer"])).status == "accepted"
    p = await _get(Pursuit, me["pursuit"])
    assert p.status == "accepted" and p.closed_at and p.outcome == "accepted"
    async with _sm()() as s:
        row = (await s.execute(select(Outcome).where(Outcome.pursuit_id == me["pursuit"]))
               ).scalar_one()
        assert row.final_status == "accepted"
        events = (await s.execute(select(StatusEvent).where(
            StatusEvent.pursuit_id == me["pursuit"]))).scalars().all()
        assert any(e.to_status == "accepted" for e in events)  # through move_to_status
    with _client() as c:  # the toast's Undo posts here, without HX-Request
        u = c.post(toast["undoUrl"])
        assert u.status_code == 204 and "of-reload" in u.headers["HX-Trigger"]
    assert (await _get(Offer, me["offer"])).status == "verbal"  # where it was
    p = await _get(Pursuit, me["pursuit"])
    assert p.status == "offer" and p.closed_at is None and p.outcome is None
    async with _sm()() as s:
        assert (await s.execute(select(Outcome).where(
            Outcome.pursuit_id == me["pursuit"]))).scalars().all() == []


@pytest.mark.parametrize("outcome,emp_status,con_status", [
    ("accepted", "accepted", "won"),
    ("declined", "withdrawn", "lost"),
    ("expired", "withdrawn", "no_response"),
])
async def test_each_outcome_closes_the_pursuit_the_way_its_track_does(
        outcome, emp_status, con_status):
    ids = await offers_seed.seed_async()
    emp, con = ids["Larkspur Logistics"], ids["Meadowbrook Foods"]
    with _client() as c:
        c.post(f"/offers/employment/{emp['offer']}/outcome", data={"outcome": outcome})
        c.post(f"/offers/consulting/{con['offer']}/outcome", data={"outcome": outcome})
    assert (await _get(Offer, emp["offer"])).status == outcome
    assert (await _get(EngagementOffer, con["offer"])).status == outcome
    assert (await _get(Pursuit, emp["pursuit"])).status == emp_status
    assert (await _get(Pursuit, con["pursuit"])).status == con_status


async def test_closing_a_closed_offer_or_a_made_up_outcome_is_refused_in_words():
    ids = await offers_seed.seed_async()
    oid = ids["Larkspur Logistics"]["offer"]
    with _client() as c:
        c.post(f"/offers/employment/{oid}/outcome", data={"outcome": "declined"})
        again = c.post(f"/offers/employment/{oid}/outcome", data={"outcome": "accepted"})
        assert "already closed" in _toast(again)["message"]
        odd = c.post(f"/offers/employment/{oid}/outcome", data={"outcome": "sent"})
        assert _toast(odd)["tone"] == "warn"
        gone = c.post("/offers/employment/99999/outcome", data={"outcome": "accepted"})
        assert "no longer exists" in _toast(gone)["message"]
    assert (await _get(Offer, oid)).status == "declined"


async def test_reopen_from_the_menu_returns_the_body_and_the_card_is_open_again():
    ids = await offers_seed.seed_async()
    oid = ids["Larkspur Logistics"]["offer"]
    with _client() as c:
        c.post(f"/offers/employment/{oid}/outcome", data={"outcome": "expired"})
        r = c.post(f"/offers/employment/{oid}/reopen", headers=HX)
        assert r.status_code == 200 and "2 open" in r.text and "is-closed" not in r.text
    assert (await _get(Offer, oid)).status == "verbal"


async def test_a_closed_card_stays_two_weeks_then_lives_in_history_only():
    ids = await offers_seed.seed_async()
    me = ids["Northwind Health"]
    with _client() as c:
        c.post(f"/offers/employment/{me['offer']}/outcome", data={"outcome": "declined"})
        assert c.get("/offers").text.count('data-testid="of-card"') == 2
        async with _sm()() as s:
            p = await s.get(Pursuit, me["pursuit"])
            p.closed_at = (date.today() - timedelta(days=ov.RECENT_CLOSED_DAYS + 1)).isoformat()
            await s.commit()
        t = c.get("/offers").text
        assert t.count('data-testid="of-card"') == 1
        hist = c.get("/offers?tab=history").text
    assert "Declined" in hist


# ----------------------------------------------------------------------------- negotiate tab
async def test_negotiate_locks_the_draft_until_a_walk_away_is_set():
    await offers_seed.seed_async()
    with _client() as c:
        t = c.get("/offers?tab=negotiate").text
    assert 'data-testid="of-negotiate"' in t
    assert 'data-locked="1"' in t and "Set your walk-away first" in t
    assert "You set this. Kestrel never picks it for you" in t
    assert t.count('data-testid="of-sit"') == 7
    assert "Counter below target" in t and "The first-number question" in t
    # Prefilled from a pick of another offer, the walk-away carries over and unlocks.
    with _client() as c:
        t2 = c.get("/offers?tab=negotiate&walk_away=140000&ask=150000").text
    assert 'data-locked="0"' in t2 and 'value="140000"' in t2 and 'value="150000"' in t2
    assert "Write the draft" in t2


async def test_a_competing_offer_is_only_offered_when_a_real_one_exists():
    ids = await offers_seed.seed_async()
    with _client() as c:
        both = c.get("/offers?tab=negotiate").text
        assert 'data-sit="competing_offer"' in both
        assert re.search(r'data-sit="competing_offer"[^>]*disabled', both) is None
        c.post(f"/offers/employment/{ids['Northwind Health']['offer']}/outcome",
               data={"outcome": "declined"})
        alone = c.get("/offers?tab=negotiate").text
    assert re.search(r'data-sit="competing_offer"[^>]*disabled', alone)
    assert "Needs a second real offer" in alone


async def test_negotiate_with_no_open_offer_says_so():
    ids = await offers_seed.seed_async()
    with _client() as c:
        for k in ("Larkspur Logistics", "Northwind Health"):
            c.post(f"/offers/employment/{ids[k]['offer']}/outcome", data={"outcome": "expired"})
        t = c.get("/offers?tab=negotiate").text
    assert "No offer is open to negotiate." in t


def _result(**over) -> ScriptResult:
    base = dict(
        situation_read="A fair offer.",
        walk_away="140000",
        scripts=[{
            "scenario": "equity_vs_cash",
            "script": "Thank you.\n\nI raised throughput 22% across two distribution centers.",
            "justification_basis": "A result.", "likely_pushback": "Firm band.",
            "response_to_pushback": "Ask about signing.", "supported": True,
        }],
        leverage_inventory=["A second offer"], risks=["Level"],
    )
    base.update(over)
    return ScriptResult(**base)


@pytest.fixture
def drafts(monkeypatch):
    """Stand in for the model and the profile; record what draft_scripts was asked."""
    calls: list[dict] = []

    async def fake(_s, _c, pid, *, walk_away, **kw):
        calls.append({"pid": pid, "walk_away": walk_away, **kw})
        return fake.result

    fake.result = _result()
    monkeypatch.setattr("kestrel.web.pages.offers.draft_scripts", fake)
    monkeypatch.setattr(
        "kestrel.web.pages.offers.load_profile",
        lambda track: type("P", (), {"proof_points": [{
            "claim": "raised throughput 22% across two distribution centers",
            "evidence": "Resume, Meadowbrook Foods, bullet 2"}]})(),
    )
    return calls


async def test_a_draft_needs_a_walk_away_and_never_computes_one(drafts):
    ids = await offers_seed.seed_async()
    pid = ids["Larkspur Logistics"]["pursuit"]
    with _client() as c:
        r = c.post(f"/offers/{pid}/scripts", data={"walk_away": "  ", "ask": "150000"})
    assert "A walk-away number is required before drafting scripts." in r.text
    assert drafts == []  # the model was never asked


async def test_an_ask_below_the_walk_away_is_refused_by_the_server(drafts):
    ids = await offers_seed.seed_async()
    pid = ids["Larkspur Logistics"]["pursuit"]
    with _client() as c:
        r = c.post(f"/offers/{pid}/scripts", data={"walk_away": "140,000", "ask": "$130000"})
        assert "Your ask is below your walk-away. Kestrel keeps drafts above it." in r.text
        ok = c.post(f"/offers/{pid}/scripts", data={"walk_away": "140000", "ask": "150000"})
        assert "Draft only · never sent" in ok.text
        # A figure with words around it is still read, so "140k base" cannot hide a low ask.
        words = c.post(f"/offers/{pid}/scripts",
                       data={"walk_away": "140k base", "ask": "100000"})
        assert "below your walk-away" in words.text
        # A walk-away in words is the operator's to word when no ask has to be checked.
        free = c.post(f"/offers/{pid}/scripts", data={"walk_away": "whatever feels fair"})
        assert "Draft only" in free.text
    assert len(drafts) == 2


async def test_the_draft_asks_for_the_chosen_situation_and_passes_the_ask_unchanged(drafts):
    ids = await offers_seed.seed_async()
    pid = ids["Larkspur Logistics"]["pursuit"]
    with _client() as c:
        r = c.post(f"/offers/{pid}/scripts", data={
            "walk_away": "140000", "ask": "152,500", "situation": "equity_vs_cash"})
    assert drafts == [{"pid": pid, "walk_away": "140000", "scenarios": ["equity_vs_cash"],
                       "ask": "152,500"}]
    t = r.text
    assert "Draft: equity or cash" in t
    assert "Copy draft" in t and "Draft again" in t and "Edit" in t
    assert "Your walk-away, unchanged:" in t and 'data-exact="140000"' in t
    assert "Kestrel never sends it" in t
    assert "<p>Thank you.</p>" in t  # paragraphs
    # The operator's own proof point is highlighted, with where it came from on hover.
    assert 'class="of-claim"' in t and "Resume, Meadowbrook Foods, bullet 2" in t
    assert "Show details" in t and "Situation read:" in t and "Likely pushback:" in t


async def test_an_unknown_situation_is_not_passed_to_the_model(drafts):
    ids = await offers_seed.seed_async()
    pid = ids["Larkspur Logistics"]["pursuit"]
    with _client() as c:
        c.post(f"/offers/{pid}/scripts", data={"walk_away": "140000", "situation": "bluff"})
    assert "scenarios" not in drafts[0]


async def test_a_draft_that_rests_on_nothing_supplied_is_flagged(drafts):
    ids = await offers_seed.seed_async()
    pid = ids["Larkspur Logistics"]["pursuit"]
    r0 = _result()
    r0.scripts[0]["supported"] = False
    # the stand-in's result is shared through the closure
    import kestrel.web.pages.offers as page

    async def fake(_s, _c, pid_, *, walk_away, **kw):
        return r0

    page.draft_scripts = fake
    try:
        with _client() as c:
            t = c.post(f"/offers/{pid}/scripts", data={"walk_away": "140000"}).text
    finally:
        pass
    assert "Rests on nothing you supplied. Check it before use." in t


async def test_no_real_competing_offer_means_no_draft_not_an_invented_one(drafts):
    ids = await offers_seed.seed_async()
    pid = ids["Larkspur Logistics"]["pursuit"]
    drafts_result = _result(scripts=[])
    import kestrel.web.pages.offers as page

    async def fake(_s, _c, pid_, *, walk_away, **kw):
        return drafts_result

    page.draft_scripts = fake
    with _client() as c:
        t = c.post(f"/offers/{pid}/scripts", data={
            "walk_away": "140000", "situation": "competing_offer"}).text
    assert "needs a real second offer on record" in t


async def test_a_model_that_is_not_running_is_a_sentence_not_a_500(monkeypatch):
    ids = await offers_seed.seed_async()
    pid = ids["Larkspur Logistics"]["pursuit"]

    async def boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("kestrel.web.pages.offers.draft_scripts", boom)
    with _client() as c:
        r = c.post(f"/offers/{pid}/scripts", data={"walk_away": "140000"})
    assert r.status_code == 200 and "Scripts need the local model running" in r.text


def test_highlight_finds_a_whole_claim_or_its_longest_run_and_leaves_the_rest():
    pts = [("raised throughput 22% across two distribution centers", "Resume bullet 2")]
    whole = ov.highlight("I raised throughput 22% across two distribution centers last year.", pts)
    assert [seg for seg in whole if seg[1]][0][0].startswith("raised throughput")
    assert "".join(t for t, _ in whole) == (
        "I raised throughput 22% across two distribution centers last year.")
    trimmed = ov.highlight("We raised throughput 22% across two sites.", pts)
    assert any(src for _, src in trimmed)  # four words in a row are enough
    assert not any(src for _, src in ov.highlight("Nothing relevant here at all.", pts))
    assert ov.paragraphs("a\n\nb\n\n\n") == ["a", "b"]


def test_ask_and_walk_away_compare_only_plain_amounts():
    assert ov.ask_below_walk_away("100,000", "$140000") is True
    assert ov.ask_below_walk_away("150000", "140000") is False
    assert ov.ask_below_walk_away("140000", "140000") is False
    assert ov.ask_below_walk_away("", "140000") is False
    assert ov.ask_below_walk_away("100k", "140000") is True  # a k suffix is still an amount
    assert ov.ask_below_walk_away("150k", "140,000") is False
    assert ov.ask_below_walk_away("about 100", "140000") is True


# -------------------------------------------------------------------------------- history
async def test_history_lists_past_offers_and_says_too_few_under_five():
    await offers_seed.seed_async()
    with _client() as c:
        t = c.get("/offers?tab=history", headers=HX).text
    assert "Past offers" in t and t.count('data-testid="of-hrow"') == 2
    assert "Operations Analyst · Meadowbrook Foods (past)" in t
    assert "Accepted" in t and "Declined" in t and "$104k" in t
    assert "2 past offers. Too few to show a pattern (2)" in t


async def test_history_with_nothing_yet():
    await offers_seed.seed_async(past=False)
    with _client() as c:
        t = c.get("/offers?tab=history").text
    assert "No past offers yet" in t and "Too few to show a pattern (0)" in t


# ---------------------------------------------------------------------------- empty state
async def test_before_an_offer_arrives_shows_pay_ranges_with_sample_sizes_in_words():
    await offers_seed.seed_async(consulting=False, past=False)
    async with _sm()() as s:
        for o in (await s.execute(select(Offer))).scalars().all():
            await s.delete(o)
        await s.commit()
    with _client() as c:
        r = c.get("/offers")
        t = r.text
        part = c.get("/offers", headers=HX).text
    assert 'data-testid="of-empty"' in t and "No offers yet" in t
    assert "What roles like yours pay" in t and "Before an offer arrives" in t
    assert "from 12 similar roles" in t and "$121k to $136k, middle $128k" in t
    assert "Too few to tell (3)" in t and "Under 5, so Kestrel shows no range for this title." in t
    assert "Your floor $120k" in t and "Your target $150k" in t
    for step in ("Know your walk-away number", "List what else matters",
                 "Read the pay range in the posting", "Kestrel drafts, you send"):
        assert step in t
    # The header's Record button hides (the empty state has its own); the count hides with it.
    assert re.search(r'id="of-rec"[^>]*\bhidden\b', t)
    assert re.search(r'id="of-count"[^>]*\bhidden\b', t)
    assert 'hx-swap-oob="outerHTML"' in part  # an action's answer carries them along


# ------------------------------------------------------------------------------- the sheet
async def test_the_record_sheet_groups_fields_and_says_zero_means_zero_once():
    ids = await offers_seed.seed_async()
    with _client() as c:
        t = c.get(f"/offers/new?pursuit={ids['Larkspur Logistics']['pursuit']}").text
    assert t.count("Leave unknown amounts blank. Zero means zero.") == 1
    for group in ("The offer", "Pay", "Dates and terms"):
        assert f"<h4>{group}</h4>" in t
    for label in ("Base salary", "Bonus target %", "Signing bonus", "Equity value",
                  "Time off (days)", "Decide by", "Level or title", "Reports to", "Where"):
        assert label in t
    assert "Your estimate over 4 years" in t
    assert f'value="{ids["Larkspur Logistics"]["pursuit"]}" selected' in t


async def test_the_consulting_sheet_has_the_engagement_money_and_terms():
    await offers_seed.seed_async()
    with _client() as c:
        t = c.get("/offers/new?track=consulting").text
    for group in ("The engagement", "Money", "Terms"):
        assert f"<h4>{group}</h4>" in t
    for label in ("Day rate", "Days committed", "Length in weeks", "Payment terms", "Expenses",
                  "Renewal", "Exclusivity", "Who owns the work", "Notice period", "Decide by"):
        assert label in t
    assert 'name="deadline"' in t


async def test_the_sheet_only_lists_applications_on_the_active_track():
    await offers_seed.seed_async()
    with _client() as c:
        emp = c.get("/offers/new").text
        con = c.get("/offers/new?track=consulting").text
    assert "Larkspur Logistics" in emp and "Meadowbrook Foods · Weekly" not in emp
    assert "Meadowbrook Foods · Weekly" in con and "Northwind Health" not in con


async def test_edit_prefills_and_saves_only_the_fields_the_sheet_carries():
    ids = await offers_seed.seed_async()
    oid = ids["Larkspur Logistics"]["offer"]
    async with _sm()() as s:
        o = await s.get(Offer, oid)
        o.notes_md, o.benefits_notes = "keep these", "and these"
        await s.commit()
    with _client() as c:
        sheet = c.get(f"/offers/employment/{oid}/edit").text
        assert "Edit offer" in sheet and 'value="136000.0"' in sheet
        assert "Larkspur Logistics · Senior Operations Analyst" in sheet  # read-only here
        assert 'name="pursuit_id"' not in sheet
        r = c.post(f"/offers/employment/{oid}/edit", headers=HX, data={
            "status": "written", "base": "$140,000", "bonus_target_pct": "", "signing": "0",
            "deadline": "2026-12-01"})
        assert r.status_code == 200 and "of-saved" in r.headers["HX-Trigger"]
        assert _toast(r)["message"] == "Offer updated."
    o = await _get(Offer, oid)
    assert o.base == 140000.0 and o.signing == 0.0  # a typed zero stays zero
    assert o.bonus_target_pct is None  # a blank clears
    assert o.deadline == "2026-12-01"
    assert o.notes_md == "keep these" and o.benefits_notes == "and these"  # not on the sheet
    assert o.level_title == "Senior Analyst"


async def test_edit_of_a_missing_offer_or_a_made_up_status_is_said_in_the_sheet():
    ids = await offers_seed.seed_async()
    oid = ids["Larkspur Logistics"]["offer"]
    with _client() as c:
        gone = c.post("/offers/employment/99999/edit", data={"base": "1"})
        assert "no longer exists" in gone.text
        assert gone.headers["HX-Retarget"] == "#of-form-error"
        odd = c.post(f"/offers/employment/{oid}/edit", data={"status": "sent"})
        assert "Unknown offer status" in odd.text
        assert c.get("/offers/nowhere/1/edit").status_code == 404
        assert c.get("/offers/employment/99999/edit").status_code == 404


async def test_a_form_problem_is_shown_inside_the_open_sheet():
    await offers_seed.seed_async()
    with _client() as c:
        r = c.post("/offers", data={"base": "1"})
    assert r.headers["HX-Retarget"] == "#of-form-error"
    assert "Choose which application this offer is for." in r.text


async def test_recording_an_offer_answers_with_the_body_a_toast_and_the_close_event():
    await offers_seed.seed_async()
    async with _sm()() as s:
        pid = (await s.execute(select(Pursuit.id).where(Pursuit.status == "negotiating"))
               ).scalars().first()
        assert pid is not None  # a consulting one: the track is the pursuit's, not the form's
    with _client() as c:
        from kestrel.db.models import Opportunity

        async with _sm()() as s:
            old = (await s.execute(select(Opportunity).where(Opportunity.track == "employment")
                                   )).scalars().first()
            opp = Opportunity(track="employment", opp_type="job_posting", source_id=old.source_id,
                              org_id=old.org_id, title="Program Manager", stage="t3_pass")
            s.add(opp)
            await s.flush()
            p = Pursuit(opportunity_id=opp.id, track="employment", status="applied")
            s.add(p)
            await s.commit()
            new_id = p.id
        r = c.post("/offers", headers=HX, data={"pursuit_id": new_id, "base": "150000"})
    assert r.status_code == 200
    trig = json.loads(r.headers["HX-Trigger"])
    assert trig["of-saved"] is True and "Offer saved" in trig["k-toast"]["message"]
    assert 'data-testid="of-card"' in r.text


# --------------------------------------------------------------------------- consulting
async def test_consulting_reads_as_engagement_offers_with_its_own_cards_and_rows():
    await offers_seed.seed_async()
    with _client() as c:
        t = c.get("/offers?track=consulting").text
    assert "<h1>Engagement offers</h1>" in t
    assert "$18,000" in t and "engagement value · $600 a day × 30 days" in t
    assert "$22,400" in t
    for fact in ("Length", "Payment", "Expenses", "Renewal", "Exclusivity", "Your work"):
        assert f"<span>{fact}</span>" in t
    assert "6 weeks" in t and "Net 30" in t and "Option: 3 months of support" in t
    for row in ("Engagement value", "Day rate", "Payment terms", "Renewal option",
                "Fit with your work"):
        assert row in t
    assert "Floor $450" in t and "Target $600" in t
    assert "Engagement value is the day rate times the days committed." in t
    # No day-rate corpus exists in this stack, and the card says so with the count.
    assert "Too few similar engagements (0)" in t
    nego = c.get("/offers?tab=negotiate&track=consulting").text
    for sit in ("Rate versus scope", "Payment terms", "Renewal option", "Exclusivity"):
        assert sit in nego
    assert "The lowest day rate you would accept" in nego
    assert "Counter below target" not in nego


# ------------------------------------------------------------------------------- privacy
async def test_confidential_mode_redacts_every_fact_the_cards_and_the_matrix_show(monkeypatch):
    ids = await offers_seed.seed_async()
    async with _sm()() as s:
        o = await s.get(Offer, ids["Larkspur Logistics"]["offer"])
        o.reports_to = "The Northwind Freight COO"
        o.location_policy = "Near the Northwind Freight yard"
        o.level_title = "Above my Northwind Freight grade"
        await s.commit()
    emap = EmployerMap(employers=[Employer(name="Northwind Freight", shown_as="a freight company")])

    async def fake(_session):
        return True, emap

    monkeypatch.setattr("kestrel.web.pages.offers.confidential_once", fake)
    with _client() as c:
        t = c.get("/offers").text
        sheet = c.get(f"/offers/employment/{ids['Larkspur Logistics']['offer']}/edit").text
    assert "Northwind Freight" not in t
    assert "The a freight company COO" in t and "Near the a freight company yard" in t
    # The sheet is where the operator edits their own typed text, so it shows it as typed:
    # saving a redaction over it would destroy it (D-UI-136). The application label is redacted.
    assert 'The Northwind Freight COO' in sheet


# ------------------------------------------------------------------------ rules and chrome
async def test_no_screen_in_offers_has_a_send_action():
    ids = await offers_seed.seed_async()
    drafted_pid = ids["Larkspur Logistics"]["pursuit"]
    with _client() as c:
        edit = f"/offers/employment/{ids['Larkspur Logistics']['offer']}/edit"
        pages = [c.get(u).text for u in (
            "/offers", "/offers?tab=negotiate", "/offers?tab=history", "/offers/new", edit,
            f"/offers?tab=negotiate&pursuit={drafted_pid}&walk_away=1")]
    for t in pages:
        assert "mailto:" not in t and "tel:" not in t
        assert not re.search(r">\s*(Send|Send email|Reply now|Submit)\b", t)


async def test_the_page_uses_the_design_system_and_drops_the_legacy_sheet():
    await offers_seed.seed_async()
    with _client() as c:
        t = c.get("/offers").text
    assert "/static/css/pages/offers.css" in t and "/static/js/offers.js" in t
    assert "/static/css/shell.css" in t and "/static/css/kestrel.css" not in t
    assert "k-layout" in t and "k-help-col" in t  # the shared stacking, not an override
    assert t.count("k-flyout") >= 4  # one flyout per help card
    # no em dashes in copy
    assert "—" not in re.sub(r"<script.*?</script[^>]*>", "", t, flags=re.S | re.I)


async def test_the_body_alone_comes_for_an_htmx_request_and_the_tab_is_remembered():
    await offers_seed.seed_async()
    with _client() as c:
        full = c.get("/offers?tab=history")
        part = c.get("/offers?tab=history", headers=HX)
    assert "<html" in full.text and "<html" not in part.text
    assert 'data-tab="history"' in part.text
    assert re.search(r'class="of-tab is-on"[^>]*data-testid="of-tab-history"', part.text)
    with _client() as c:
        assert 'data-tab="compare"' in c.get("/offers?tab=nonsense").text  # unknown tab: Compare


# ---------------------------------------------------------------------------- the migration
def test_migration_0024_adds_and_drops_the_engagement_deadline(tmp_path):
    import sqlite3

    from alembic import command

    db = tmp_path / "m.db"

    def go(direction: str, rev: str) -> None:
        with paths.resource_path(paths.packaged_migrations()) as mig_dir:
            cfg = migrate._config(str(mig_dir), db)
            (command.upgrade if direction == "up" else command.downgrade)(cfg, rev)

    def cols() -> set[str]:
        con = sqlite3.connect(db)
        try:
            return {r[1] for r in con.execute("PRAGMA table_info(engagement_offer)")}
        finally:
            con.close()

    go("up", "0023")
    assert "deadline" not in cols()
    go("up", "head")
    assert "deadline" in cols()
    go("down", "0023")
    assert "deadline" not in cols()


def test_the_page_style_and_markup_use_theme_tokens_only():
    """Every color comes from a theme token (CLAUDE.md, Output style): no hex or rgb color in the
    page's sheet or script, and no color set inline in its templates."""
    from pathlib import Path

    static = Path(paths.web_static_dir())
    for rel in ("css/pages/offers.css", "js/offers.js"):
        text = (static / rel).read_text(encoding="utf-8")
        assert not re.search(r"#[0-9A-Fa-f]{3,8}\b", text), rel
        assert not re.search(r"\b(?:rgb|rgba|hsl|hsla)\(", text), rel
    templates = Path(paths.web_templates_dir())
    for name in ("offers.html", *(p.name for p in (templates / "partials").glob("offer_*.html"))):
        text = (templates / ("partials" if name != "offers.html" else ".") / name).read_text(
            encoding="utf-8")
        for style in re.findall(r'style="([^"]*)"', text):
            assert "color" not in style and "background" not in style, (name, style)


async def test_undo_does_not_revive_an_application_that_was_already_closed():
    ids = await offers_seed.seed_async()
    me = ids["Northwind Health"]
    async with _sm()() as s:
        p = await s.get(Pursuit, me["pursuit"])
        p.status = "rejected"
        await s.commit()
    with _client() as c:
        r = c.post(f"/offers/employment/{me['offer']}/outcome", data={"outcome": "declined"})
    assert "already closed" in _toast(r)["message"]
    assert (await _get(Offer, me["offer"])).status == "verbal"
    assert (await _get(Pursuit, me["pursuit"])).status == "rejected"


async def test_reopen_leaves_an_open_offer_alone():
    ids = await offers_seed.seed_async()
    oid = ids["Larkspur Logistics"]["offer"]
    with _client() as c:
        r = c.post(f"/offers/employment/{oid}/reopen", headers=HX)
    assert "not closed" in _toast(r)["message"]
    assert (await _get(Offer, oid)).status == "written"  # not flattened to verbal


async def test_confidential_mode_redacts_the_pay_range_titles(monkeypatch):
    await offers_seed.seed_async(consulting=False, past=False)
    async with _sm()() as s:
        for o in (await s.execute(select(Offer))).scalars().all():
            await s.delete(o)
        await s.commit()
    emap = EmployerMap(employers=[Employer(name="Operations", shown_as="a field")])

    async def fake(_session):
        return True, emap

    monkeypatch.setattr("kestrel.web.pages.offers.confidential_once", fake)
    with _client() as c:
        t = c.get("/offers").text
    market = t[t.index('data-testid="of-market"'):]
    assert "Senior a field Analyst" in market and "Senior Operations Analyst" not in market


# ------------------------------------------------------------------ typed amounts (review fix)
@pytest.mark.parametrize("text,value,unit,cur", [
    ("$150k/yr", 150_000, "year", "USD"),
    ("150,000 USD", 150_000, None, "USD"),
    ("150k", 150_000, None, None),
    ("1.2m", 1_200_000, None, None),
    ("12k a month", 144_000, "year", None),
    ("\u20ac120k per year", 120_000, "year", "EUR"),
    ("550 a day", 550, "day", None),
    ("140k base", 140_000, None, None),
])
def test_parse_amount_reads_the_formats_people_type(text, value, unit, cur):
    a = ov.parse_amount(text)
    assert (a.value, a.unit, a.currency) == (value, unit, cur)


@pytest.mark.parametrize("text", ["", "negotiable", "1 and 2", "150k USD or 140k EUR"])
def test_parse_amount_refuses_what_it_cannot_read_as_one_amount(text):
    assert ov.parse_amount(text) is None


def test_an_ask_in_another_unit_or_currency_is_refused_and_never_compared():
    assert "different" not in (ov.ask_problem("150k", "140k") or "")
    assert "same unit" in ov.ask_problem("600 a day", "140k a year")
    assert "one currency" in ov.ask_problem("150k EUR", "140k USD")
    assert "below your walk-away" in ov.ask_problem("$10k/mo", "130k/yr")  # 120k a year
    assert ov.ask_problem("$11k/mo", "130k/yr") is None  # 132k a year
    assert "one amount" in ov.ask_problem("lots", "140k")
    assert "walk-away as one amount" in ov.ask_problem("150k", "enough")
    assert ov.ask_problem("", "enough") is None


async def test_a_mismatched_ask_never_reaches_the_model(drafts):
    ids = await offers_seed.seed_async()
    pid = ids["Larkspur Logistics"]["pursuit"]
    with _client() as c:
        for walk, ask, word in (("140k USD", "150k EUR", "one currency"),
                                ("140k a year", "600 a day", "same unit"),
                                ("$140k/yr", "$130k/yr", "below your walk-away")):
            r = c.post(f"/offers/{pid}/scripts", data={"walk_away": walk, "ask": ask})
            assert word in r.text
        ok = c.post(f"/offers/{pid}/scripts", data={"walk_away": "$140k/yr", "ask": "150,000 USD"})
        assert "Draft only" in ok.text
    assert len(drafts) == 1


# --------------------------------------- where the draft is written (review fix, privacy copy)
def _route(monkeypatch, *, online: bool, employer: str = "off"):
    """Settings as the Offers page reads them: the negotiate tier on this computer, or on an
    online backend with Hosted mode acknowledged."""
    from kestrel.web.pages import offers as page

    orig = page.load_settings_with_db

    async def patched(session):
        st = (await orig(session)).model_copy(deep=True)
        if online:
            hm = st.llm.hosted_mode
            hm.enabled = True
            hm.acknowledged_at = "2026-10-01T00:00:00Z"
            hm.acknowledged_terms = hm.disclosure_terms()
            ext = next(n for n, b in st.llm.backends.items() if b.external)
            st.llm.tiers["tier3"] = st.llm.tiers["tier3"].model_copy(
                update={"backend": ext, "model": "vendor/draft-model"})
        else:
            ext = next(n for n, b in st.llm.backends.items() if not b.external)
            st.llm.tiers["tier3"] = st.llm.tiers["tier3"].model_copy(update={"backend": ext})
        st.confidential_mode = employer != "off"
        return st

    async def state(_s, _settings):
        return employer

    monkeypatch.setattr(page, "load_settings_with_db", patched)
    monkeypatch.setattr("kestrel.web.pages.needs_try._employer_state", state)
    monkeypatch.setattr("kestrel.web.pages.needs_try._hosted_models",
                        lambda _s: _async(("OpenRouter", "", [], [], {}, None)))


async def _async(v):
    return v


def _dest(model="vendor/draft-model"):
    """The destination the online route names: the first external backend, as `_route` sets it."""
    from kestrel.config import get_settings

    name, backend = next((n, b) for n, b in get_settings().llm.backends.items() if b.external)
    return f"{name}|{backend.base_url}|{model}"


async def test_a_local_draft_says_it_stays_on_this_computer(drafts, monkeypatch):
    _route(monkeypatch, online=False)
    await offers_seed.seed_async()
    with _client() as c:
        page = c.get("/offers?tab=negotiate").text
    assert "Nothing leaves it" in page and "Drafts stay on this computer" in page
    assert 'data-testid="of-send"' not in page and "Drafts use an online model" not in page


async def test_an_online_draft_states_what_is_sent_and_never_claims_local(drafts, monkeypatch):
    _route(monkeypatch, online=True, employer="hidden")
    await offers_seed.seed_async()
    with _client() as c:
        page = c.get("/offers?tab=negotiate").text
    assert "Nothing leaves it" not in page and "on your computer" not in page
    assert "Drafts stay on this computer" not in page and "Drafts use an online model" in page
    assert 'data-testid="of-send"' in page and "OpenRouter" in page
    assert "your walk-away and ask" in page and "negotiation prep" in page  # tier 3 disclosure
    assert "your current employer is hidden" in page
    assert 'name="confirmed"' in page and "vendor/draft-model" in page


async def test_an_online_draft_is_refused_server_side_without_the_confirm(drafts, monkeypatch):
    _route(monkeypatch, online=True)
    ids = await offers_seed.seed_async()
    pid = ids["Larkspur Logistics"]["pursuit"]
    with _client() as c:
        none = c.post(f"/offers/{pid}/scripts", data={"walk_away": "140000"})
        other = c.post(f"/offers/{pid}/scripts", data={
            "walk_away": "140000", "confirmed": "1", "model": "some/other-model"})
        same_model_elsewhere = c.post(f"/offers/{pid}/scripts", data={
            "walk_away": "140000", "confirmed": "1", "model": "vendor/draft-model",
            "dest": "other|https://elsewhere.example/v1|vendor/draft-model"})
        ok = c.post(f"/offers/{pid}/scripts", data={
            "walk_away": "140000", "confirmed": "1", "model": "vendor/draft-model",
            "dest": _dest()})
    assert "Nothing was sent" in none.text and "Nothing was sent" in other.text
    assert "Nothing was sent" in same_model_elsewhere.text
    assert "Draft only" in ok.text
    assert len(drafts) == 1  # only the confirmed send reached the model


async def test_an_online_draft_is_held_when_confidential_mode_has_no_employer(drafts, monkeypatch):
    _route(monkeypatch, online=True, employer="held")
    ids = await offers_seed.seed_async()
    pid = ids["Larkspur Logistics"]["pursuit"]
    with _client() as c:
        page = c.get("/offers?tab=negotiate").text
        r = c.post(f"/offers/{pid}/scripts", data={
            "walk_away": "140000", "confirmed": "1", "model": "vendor/draft-model",
            "dest": _dest()})
    assert "This send will be held" in page
    assert "Nothing was sent" in r.text and "Settings" in r.text
    assert drafts == []


async def test_a_local_draft_needs_no_confirm(drafts, monkeypatch):
    _route(monkeypatch, online=False)
    ids = await offers_seed.seed_async()
    pid = ids["Larkspur Logistics"]["pursuit"]
    with _client() as c:
        ok = c.post(f"/offers/{pid}/scripts", data={"walk_away": "140000"})
    assert "Draft only" in ok.text and len(drafts) == 1


# --------------------------------------------------------------- nits and card parity
async def test_negotiate_inputs_show_separators_but_submit_plain_numbers():
    await offers_seed.seed_async()
    with _client() as c:
        t = c.get("/offers?tab=negotiate&walk_away=140000&ask=150000").text
    assert re.search(r'<input type="hidden" name="walk_away"[^>]*value="140000"', t)
    assert re.search(r'id="of-walk" data-plain="of-walk-val"[^>]*value="\$140,000"', t)
    assert 'name="walk_away"' not in re.search(r'<input class="k-input" id="of-walk"[^>]*>', t)[0]


async def test_the_card_reads_like_the_mockup():
    await offers_seed.seed_async()
    with _client() as c:
        t = c.get("/offers").text
    flat = " ".join(t.split())
    assert re.search(r"Decide by [A-Z][a-z]{2} \d{1,2}(, \d{4})? · \d+ days", flat)
    assert "inside the posted range" in flat and "Base is inside" not in flat
    assert "Posted range $118,000 to $142,000" in flat  # behind Show details
    # Show details sits right under the legend, before the deadline pill.
    card = t[t.index('data-testid="of-card"'):]
    assert card.index('class="of-leg"') < card.index('class="of-det"') < card.index("of-dl")


def test_a_chosen_region_format_beats_the_short_form():
    from datetime import date
    d = date(2026, 11, 3)
    assert ov.deadline_view("2026-11-03", today=date(2026, 10, 25), fmt="DD/MM/YYYY")["text"] \
        == "Decide by 03/11/2026 · 9 days"
    assert ov.deadline_view("2026-11-03", today=date(2026, 10, 25),
                            fmt=ov.ui_settings.DEFAULT_DATE_FORMAT)["text"] \
        == "Decide by Nov 3 · 9 days"
    assert ov.deadline_view("2027-01-02", today=date(2026, 10, 25),
                            fmt=ov.ui_settings.DEFAULT_DATE_FORMAT)["text"].startswith(
        "Decide by Jan 2, 2027")
    assert d


async def test_reopen_ignores_an_unknown_pursuit_stage_in_the_link():
    ids = await offers_seed.seed_async()
    oid = ids["Larkspur Logistics"]["offer"]
    pid = ids["Larkspur Logistics"]["pursuit"]
    with _client() as c:
        c.post(f"/offers/employment/{oid}/outcome", data={"outcome": "declined"}, headers=HX)
        r = c.post(f"/offers/employment/{oid}/reopen?offer_status=verbal&pursuit_status=hacked",
                   headers=HX)
        assert r.status_code == 200
    assert (await _get(Pursuit, pid)).status == "offer"


async def test_migration_0024_downgrades_with_offers_and_their_pursuits_on_file():
    import sqlite3

    from alembic import command

    assert migrate.head_revision() >= "0024"
    await offers_seed.seed_async()
    db = paths.db_path()
    with paths.resource_path(paths.packaged_migrations()) as mig_dir:
        command.downgrade(migrate._config(str(mig_dir), db), "0023")
    con = sqlite3.connect(db)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(engagement_offer)")}
        n_eng = con.execute("SELECT count(*) FROM engagement_offer").fetchone()[0]
        n_off = con.execute("SELECT count(*) FROM offer").fetchone()[0]
        n_p = con.execute("SELECT count(*) FROM pursuit").fetchone()[0]
    finally:
        con.close()
    assert "deadline" not in cols and n_eng >= 1 and n_off >= 2 and n_p >= 3
    with paths.resource_path(paths.packaged_migrations()) as mig_dir:
        command.upgrade(migrate._config(str(mig_dir), db), "head")


def test_parse_amount_is_linear_on_long_runs_of_digits():
    import time

    start = time.perf_counter()
    ov.parse_amount("0" * 50_000 + "x" + "0," * 25_000)
    assert time.perf_counter() - start < 1.0
    assert ov._NUM.findall("1,234,567 and 2.5m") == [("1,234,567", ""), ("2.5", "m")]
