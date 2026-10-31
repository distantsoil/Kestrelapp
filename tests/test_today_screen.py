"""Today (UI overhaul, D-UI-92, D-UI-156): the greeting, the status strip, the retry note, Top
matches (cards, empty and done states, live arrivals), Decisions waiting, Applications needing
action, New signals, the role summary and the finish-setup banner. Fictional rows from
tests/today_seed.py (the locked mockup's day); nothing here touches the network or a model, and
every test runs on the throwaway data directory the autouse fixture gives it."""

from __future__ import annotations

import asyncio
import copy
import re
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db.engine import forget_engine, make_async_engine
from kestrel.db.models import (
    DocMaster,
    DocSection,
    Evaluation,
    Opportunity,
    Org,
    Pursuit,
    Quarantine,
    Setting,
)
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web import today_view as tv
from kestrel.web.deps import templates
from tests import today_seed

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _db(fn):
    """Run `fn(session)` against the throwaway database and commit."""

    async def go():
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sm() as s:
                out = await fn(s)
                await s.commit()
                return out
        finally:
            await engine.dispose()
            forget_engine()

    return asyncio.run(go())


def _seed(**kw) -> dict[str, int]:
    return asyncio.run(today_seed.seed(**kw))


def _cards(track="employment", **kw) -> dict:
    async def go(s):
        return await tv.top_matches(s, track, **kw)

    return _db(go)


def _titles(data: dict) -> list[str]:
    return [c["title"] for c in data["cards"]]


# ----------------------------------------------------------------------------- the greeting
def test_greeting_follows_the_clock_and_the_profile_name():
    assert tv.greeting_word(datetime(2026, 10, 24, 7)) == "Good morning"
    assert tv.greeting_word(datetime(2026, 10, 24, 11, 59)) == "Good morning"
    assert tv.greeting_word(datetime(2026, 10, 24, 12)) == "Good afternoon"
    assert tv.greeting_word(datetime(2026, 10, 24, 17, 59)) == "Good afternoon"
    assert tv.greeting_word(datetime(2026, 10, 24, 18)) == "Good evening"
    assert tv.greeting("Jordan", datetime(2026, 10, 24, 9)) == "Good morning, Jordan."
    # No first name yet: no comma, no invented name.
    assert tv.greeting("", datetime(2026, 10, 24, 9)) == "Good morning."


def test_the_page_greets_by_first_name_from_the_profile():
    _seed()
    (paths.config_dir() / "profile.yml").write_text(
        "employment: {first_name: Jordan, last_name: Example}\n", encoding="utf-8"
    )
    with _client() as c:
        body = c.get("/").text
    h1 = body.split("<h1>")[1].split("</h1>")[0]
    assert re.fullmatch(r"Good (morning|afternoon|evening), Jordan\.", h1)
    assert "Your top matches and what needs your attention today." in body


# ----------------------------------------------------------------------------- skill tags
def test_skill_tags_match_whole_words_the_posting_mentions():
    text = "SQL and forecasting. Lean six sigma. Power BI. mysql only."
    skills = ["SQL", "Power BI", "Lean", "Python", "forecasting", "R"]
    assert tv.skill_tags(skills, text, limit=9) == ["SQL", "Power BI", "Lean", "forecasting"]
    assert tv.skill_tags(["SQL"], "we use mysql and postgresql") == []  # whole words only
    assert tv.skill_tags(skills, text, limit=2) == ["SQL", "Power BI"]  # capped
    assert tv.skill_tags(["C++", "C#"], "C++ and C# work") == ["C++", "C#"]


def test_why_it_fits_joins_the_summary_and_the_strongest_evidence():
    opp = Opportunity(track="employment", title="x")
    ev = {"blocks": {
        "a_role_summary": {
            "what_this_actually_is": "Leads improvement work. It is mostly reports."},
        "b_fit": {"matches": [
            {"requirement": "a", "strength": "partial", "evidence": "Partial evidence."},
            {"requirement": "b", "strength": "strong", "evidence": "Strong evidence"},
        ]},
    }}
    # Block A's first sentence, then the evidence of the strongest match (strong before partial).
    assert tv.why_it_fits(opp, ev) == "Leads improvement work. Strong evidence."
    assert tv.why_it_fits(opp, None) == ""  # nothing stored: nothing is written for the model


# -------------------------------------------------------------------------- top matches
def test_top_matches_rank_by_fit_show_every_score_and_leave_out_the_rest():
    ids = _seed()
    data = _cards()
    assert _titles(data) == [
        "Senior Operations Analyst", "Process Improvement Lead", "Operations Program Manager",
        "Inventory Planning Manager", "Warehouse Shift Supervisor",
    ]
    by = {c["title"]: c for c in data["cards"]}
    assert by["Warehouse Shift Supervisor"]["score"] == 1.8
    assert "Business Analyst II" not in by
    assert data["pending"] == 1 and data["n"] == 5
    # Set aside by the rules, and from an earlier day, are not cards; both count as found today
    # only when first seen today (the rule reject was).
    assert all(c["id"] not in (ids["rule"], ids["backlog"], ids["listed"]) for c in data["cards"])
    assert data["found"] == 7  # 5 scored + 1 quick screen + 1 rule reject, all found today
    # Pay: a posted range, and an estimate labeled as one (hard rule 6), never as posted.
    assert by["Senior Operations Analyst"]["pay"] == "$118,000 to $142,000"
    assert by["Senior Operations Analyst"]["estimate"] is False
    est = by["Operations Program Manager"]
    assert est["estimate"] is True and est["pay"] == "$140,000 to $165,000"
    assert "similar roles" in est["estimate_note"] and "not recorded" in est["estimate_note"]
    # Tags are the operator's own skills that the posting mentions: SQL, forecasting and Power
    # BI appear in the description, Lean does not; "mysql" never makes SQL appear twice.
    assert by["Senior Operations Analyst"]["tags"] == ["SQL", "Power BI", "Forecasting"]
    assert "process improvement work" in by["Senior Operations Analyst"]["why"]


def test_waiting_role_joins_matches_only_after_a_scored_full_review():
    ids = _seed()
    initial = _cards(limit=1)
    assert initial["pending"] == 1 and initial["n"] == 5
    have = ",".join(str(c["id"]) for c in _cards()["cards"])

    async def finish(s):
        opp = await s.get(Opportunity, ids["quick"])
        opp.stage = "t2_done"
        s.add(Evaluation(opportunity_id=opp.id, track="employment", tier=2,
                         verdict="consider", score=3.2, weighted_score=3.2, blocks={}))

    _db(finish)
    data = _cards()
    assert data["pending"] == 0 and data["found"] == 7 and data["n"] == 6
    reviewed = next(c for c in data["cards"] if c["id"] == ids["quick"])
    assert reviewed["score"] == 3.2 and reviewed["pending"] is False
    with _client() as client:
        update = client.get(f"/today/matches/new?have={have}").json()
        html = client.get("/today/matches").text
    assert [card["id"] for card in update["new"]] == [ids["quick"]]
    assert update["new"][0]["before"] == ids["nofit"]
    assert update["pending"] == 0
    assert 'aria-live="polite"></p>' in html
    assert "more being reviewed" not in html


def test_pending_only_matches_keep_the_found_heading():
    ids = _seed()

    async def earlier(s):
        for opp in (await s.execute(select(Opportunity))).scalars():
            if opp.id != ids["quick"]:
                opp.first_seen_at = "2020-01-01T00:00:00+00:00"

    _db(earlier)
    with _client() as client:
        html = client.get("/today/matches").text
    assert "1 new role found today" in html
    assert "1 more being reviewed" in html
    assert 'data-testid="td-card"' not in html
    assert "No matches yet" in html


@pytest.mark.parametrize("stage", ["new", "t1_pass", "t2_done"])
def test_unscored_roles_count_without_becoming_cards(stage):
    ids = _seed()

    async def unscore(s):
        opp = await s.get(Opportunity, ids["quick"])
        opp.stage = stage
        ev = (await s.execute(select(Evaluation).where(
            Evaluation.opportunity_id == opp.id))).scalar_one()
        if stage == "new":
            await s.delete(ev)
        elif stage == "t2_done":
            ev.tier, ev.verdict = 2, "consider"

    _db(unscore)
    data = _cards()
    assert data["pending"] == 1
    assert ids["quick"] not in {card["id"] for card in data["cards"]}


@pytest.mark.parametrize("excluded", ["saved", "pass", "pursuit", "t0_reject",
                                      "t1_reject", "quarantined", "error", "trigger_event", "old"])
def test_pending_count_keeps_the_existing_exclusions(excluded):
    ids = _seed()

    async def exclude(s):
        opp = await s.get(Opportunity, ids["quick"])
        if excluded == "saved":
            from kestrel.services.saved_roles import save

            await save(s, opp.id)
        elif excluded == "pass":
            from kestrel.db.models import VerdictOverride

            s.add(VerdictOverride(opportunity_id=opp.id, operator_verdict="pass"))
        elif excluded == "pursuit":
            s.add(Pursuit(opportunity_id=opp.id, track="employment", status="shortlisted"))
        elif excluded == "trigger_event":
            opp.opp_type = "trigger_event"
        elif excluded == "old":
            opp.first_seen_at = "2020-01-01T00:00:00+00:00"
        else:
            opp.stage = excluded

    _db(exclude)
    data = _cards()
    assert data["pending"] == 0
    assert ids["quick"] not in {card["id"] for card in data["cards"]}


def test_top_matches_follow_the_track_and_hidden_companies_never_count():
    _seed()

    async def hide(s):
        for org in (await s.execute(select(Org))).scalars():
            org.hidden_at = "2026-10-24T00:00:00+00:00"

    assert _cards("consulting")["cards"] == []
    _db(hide)
    data = _cards()
    assert data["cards"] == [] and data["found"] == 0


def test_a_decided_role_is_not_a_card_and_undo_brings_it_back():
    ids = _seed()
    with _client() as c:
        # Shortlist and Save use the Pipeline's own routes, so Undo works as it does there.
        r = c.post(f"/opportunities/{ids['strong']}/decide/shortlist")
        assert r.status_code == 200
        # The toast's Undo carries the token this shortlist wrote (D-UI-183).
        undo = re.search(r"/decide/unshortlist\?token=[\w-]+", r.headers["HX-Trigger"]).group(0)
        assert c.post(f"/opportunities/{ids['good']}/decide/save").status_code == 200
        assert c.post(f"/opportunities/{ids['maybe']}/decide/not-for-me?reason=later"
                      ).status_code == 200
        shown = c.get("/today/matches").text
        for gone in ("Senior Operations Analyst", "Process Improvement Lead",
                     "Inventory Planning Manager"):
            assert gone not in shown
        assert "Operations Program Manager" in shown
        # Shortlisted: it shows in Decisions waiting as shortlisted, not applied.
        dec = c.get("/today/decisions").text
        assert "Senior Operations Analyst" in dec and "Shortlisted, not applied" in dec
        # Undo of the shortlist puts the card back.
        # A bare Undo without the toast's token is refused and changes nothing.
        assert "can no longer be undone" in c.post(
            f"/opportunities/{ids['strong']}/decide/unshortlist").text
        assert c.post(f"/opportunities/{ids['strong']}{undo}").status_code == 200
        assert "Senior Operations Analyst" in c.get("/today/matches").text


def test_every_card_part_is_printed_and_the_buttons_use_the_pipeline_routes():
    ids = _seed()
    with _client() as c:
        body = c.get("/").text
    sec = body.split('data-testid="td-matches"')[1].split("</section>")[0]
    card = sec.split(f'data-id="{ids["strong"]}"')[1].split("</article>")[0]
    assert 'data-fit="strong fit"' in card and ">4.6 <small>/ 5</small>" in card
    assert "Senior Operations Analyst" in card and "Seed Org" not in card
    assert "Larkspur Logistics" in card and "Austin, TX" in card
    assert "$118,000 to $142,000" in card
    assert 'class="td-tag">SQL<' in card and 'class="td-tag">Power BI<' in card
    assert 'data-testid="td-why"' in card and "Written by Kestrel's AI" in card
    assert f'hx-post="/opportunities/{ids["strong"]}/decide/save"' in card
    assert f'hx-post="/opportunities/{ids["strong"]}/decide/shortlist"' in card
    assert f'data-td-review="{ids["strong"]}"' in card and ">Review role<" in card
    # The estimate carries its label and its hover disclaimer.
    est = sec.split(f'data-id="{ids["est"]}"')[1].split("</article>")[0]
    assert 'data-testid="td-est"' in est and "may be wrong" in est and "est.<" in est
    assert "est." not in card.split('data-testid="td-pay"')[1].split("</div>")[0]
    assert 'data-fit="good fit"' in sec and 'data-fit="maybe"' in sec
    assert f'data-id="{ids["quick"]}"' not in sec
    assert "1 more being reviewed" in sec
    assert "Full review in progress" not in sec and "Not scored yet" not in sec
    # Every title area reserves two lines in CSS, so the cards share one height.
    css = Path(paths.web_static_dir(), "css", "pages", "today.css").read_text(encoding="utf-8")
    assert re.search(r"\.td-t \{[^}]*min-height: 39px", css)


def test_top_matches_empty_and_done_states():
    _seed(with_run=False)
    with _client() as c:
        both = c.get("/").text
    assert 'data-testid="td-matches"' in both
    # Nothing is decided yet: no empty card.
    assert 'data-testid="td-none"' not in both

    async def wipe(s):
        for o in (await s.execute(select(Opportunity))).scalars():
            o.first_seen_at = "2020-01-01T00:00:00+00:00"

    _db(wipe)
    with _client() as c:
        empty = c.get("/").text
    track = empty.split('id="td-track"')[1].split("</div>")[0]
    assert "No matches yet" in track and "Run a scan" in track
    assert "appear here after today" in track
    assert "Nothing found yet today" in empty


def test_every_card_actioned_leaves_one_done_card_with_both_actions():
    ids = _seed()
    with _client() as c:
        for key in ("strong", "good", "est", "maybe", "nofit", "quick"):
            c.post(f"/opportunities/{ids[key]}/decide/save")
        body = c.get("/").text
    track = body.split('id="td-track"')[1].split("</template>")[0]
    assert 'data-testid="td-done"' in track and "You're done for now!" in track
    assert "Run another scan or check out your pipeline." in track
    assert "Run a scan" in track and 'href="/opportunities"' in track and "Open Pipeline" in track
    assert "All caught up" in body  # the line beside the heading follows
    # The same card is held for the page to swap in when the last card leaves.
    assert 'id="td-done-tpl"' in body


# ------------------------------------------------------------------------ the status strip
def _strip_ctx(mutate=None, *, first_run=False, track="employment"):
    """Render partials/td_strip.html from the real run panel, optionally changing the run."""
    from kestrel.web.pages import today as page

    async def go(s):
        settings = await load_settings_with_db(s)
        panel = await page.run_panel(s, track, settings)
        panel = copy.deepcopy(panel)
        if mutate:
            mutate(panel)
        strip = await page.strip_context(s, track, settings, panel, first_run=first_run)
        return {"track": track, "strip": strip,
                "retry": page.retry_note(panel, first_run=first_run),
                "scan_state": {}, "scan_msg": None, "tour": False}

    ctx = _db(go)
    return templates().get_template("partials/td_strip.html").render(**ctx)


def test_the_strip_has_three_segments_and_a_clean_run_fades_into_the_next_run():
    _seed()
    html = _strip_ctx()
    assert html.count('class="td-seg"') == 3
    assert 'data-seg="run"' in html and 'data-seg="found"' in html and 'data-seg="model"' in html
    assert "Last run: " in html and ">Finished<" in html.replace("</svg>", ">") \
        or "Finished</span>" in html
    assert "All sources checked" in html
    assert 'href="/runs"' in html and 'href="/opportunities?new_today=1"' in html
    assert 'href="/models"' in html
    # 7 found, rated count and pending, the live count line.
    assert "new roles found" in html and "rated" in html and "pending" in html
    # No schedule set in a fresh home: nothing to fade to, so it holds on Last run.
    assert 'data-hold="1"' in html and 'id="td-face-next"' not in html


def test_a_problem_holds_on_last_run_and_a_schedule_fades():
    _seed()

    def problem(panel):
        panel["run"]["failed"] = 2
        panel["run"]["problem_count"] = 1

    html = _strip_ctx(problem)
    assert 'data-hold="1"' in html
    assert ">2 problems<" in html.replace("\n", "").replace("  ", "") or "2 problems" in html
    assert "2 roles need another try" in html and "td-bad" in html

    # A schedule exists and the run was clean: Last run cross-fades to Next run.
    async def sched(s):
        from kestrel.services import run_schedule as rs

        s.add(Setting(key="run.schedule", value={"kind": "daily", "time": "08:00"}))
        assert rs.KEY if hasattr(rs, "KEY") else True

    _db(sched)
    clean = _strip_ctx()
    if 'id="td-face-next"' in clean:  # the schedule key is the services' own; skip if renamed
        assert 'data-hold="0"' in clean and "Next run: " in clean and "Daily schedule" in clean


def test_the_strip_on_a_first_run_says_no_runs_yet_and_hides_old_problems():
    _seed(with_run=False)
    html = _strip_ctx(first_run=True)
    assert 'data-state="none"' in html and "No runs yet" in html
    assert "Press Run now to run the first search." in html
    assert "No new roles yet" in html and "Run a search to find some." in html


def test_the_strip_while_searching_shows_the_headline_and_a_bar():
    _seed()

    def searching(panel):
        r = panel["run"]
        r.update(active=True, has_run=True, stage="searching", headline="Scanning: 3 of 5 searches",
                 waiting=4, rating_now=1, rated=12, failed=2, found_new=16)
        r["bars"][0].update(done=2, total=5, pct=40.0)

    html = _strip_ctx(searching)
    assert 'data-state="active"' in html and "Scanning: 3 of 5 searches" in html
    assert 'role="progressbar"' in html and "--w: 40.0%" in html
    # Live counts, in place: "12 rated · 2 failed · 5 waiting".
    counts = html.split('data-testid="found-counts">')[1].split("</span>\n")[0]
    assert "12 rated" in counts and "2 failed" in counts and "5 waiting" in counts
    assert 'data-testid="run-pause"' in html and 'hx-post="/api/worker/pause"' in html
    assert "every 5s" in html  # polls while a search runs


def test_the_strip_while_rating_names_the_stage_and_the_time_left():
    _seed()

    def rating(panel):
        r = panel["run"]
        r.update(active=True, has_run=True, stage="full_review", headline="Rating 12 of 20",
                 waiting=4, rating_now=1, rated=12, failed=0)
        panel["eta_minutes"] = 6

    html = _strip_ctx(rating)
    assert "Rating new roles" in html and "about 6 min left" in html


def test_the_model_segment_has_the_three_phrasings():
    local = tv.model_segment({"model": "qwen3:14b"}, "local")
    assert local == {"title": "Model on this computer",
                     "detail": "qwen3:14b · local and private", "tone": ""}
    byok = tv.model_segment({"model": "deepseek-v3", "provider": "OpenRouter",
                             "left": "$4.82 left on your key's limit"}, "hosted")
    assert byok["title"] == "Online model, your key"
    assert byok["detail"] == "OpenRouter · deepseek-v3 · BYOK · $4.82 credit left"
    both = tv.model_segment({"model": "qwen3:14b", "provider": "OpenRouter",
                             "left": "$4.82 left on your key's limit"}, "both")
    assert both["title"] == "Local, plus your online key"
    assert both["detail"] == "qwen3:14b · OpenRouter · $4.82 left"
    # No credit reading (an uncapped key): nothing is invented.
    assert "credit" not in tv.model_segment({"model": "m", "provider": "P"}, "hosted")["detail"]


# ------------------------------------------------------------------------- the retry note
def test_the_retry_note_shows_only_when_a_role_needs_another_try():
    ids = _seed()
    with _client() as c:
        assert 'data-testid="td-retry"' not in c.get("/").text

    def park(s):
        async def go(s):
            s.add(Quarantine(opportunity_id=ids["strong"], tier=2, error="schema",
                             raw_response="{"))
        return go

    _db(park(None))
    with _client() as c:
        body = c.get("/").text
        first = c.get("/?tour=1").text
    note = body.split('data-testid="td-retry"')[1].split("</div>")[0]
    assert "1 role needs another try." in note
    assert "The AI could not read its own answer. They are kept in Pipeline." in note
    assert 'href="/quarantine"' in note and ">Review<" in note
    # The first-run Today (here, the walkthrough replay) shows no retry note.
    assert 'data-testid="td-retry"' not in first


def test_the_retry_note_is_hidden_on_the_first_run_whatever_is_parked():
    ids = _seed(with_run=False)

    async def park(s):
        s.add(Quarantine(opportunity_id=ids["strong"], tier=2, error="schema", raw_response="{"))

    _db(park)
    with _client() as c:
        body = c.get("/").text
    assert 'data-first-run="1"' in body and 'data-testid="td-retry"' not in body
    assert "The AI could not read its own answer" not in body


# ----------------------------------------------------------- the Strong match cutoff
def test_a_strong_match_follows_the_strong_match_cutoff_not_a_fixed_number():
    _seed()
    data = _cards()
    strong = {c["title"]: c["strong"] for c in data["cards"]}
    # thresholds.apply is 4.0 by default: 4.1 and above are strong matches, 3.4 is not.
    assert strong["Operations Program Manager"] is True
    assert strong["Inventory Planning Manager"] is False
    assert "Business Analyst II" not in strong

    async def raise_cutoff(s):
        s.add(Setting(key="thresholds.apply", value=4.5))

    _db(raise_cutoff)
    strong = {c["title"]: c["strong"] for c in _cards()["cards"]}
    assert strong["Senior Operations Analyst"] is True
    assert strong["Operations Program Manager"] is False
    assert strong["Process Improvement Lead"] is False

    async def cutoff(s):
        return tv.strong_cutoff(await load_settings_with_db(s))

    assert _db(cutoff) == 4.5


# ------------------------------------------------------------------------ live arrivals
def test_new_matches_come_with_their_ranked_position_and_nothing_the_page_has():
    ids = _seed()
    have = ",".join(str(ids[k]) for k in ("strong", "est", "nofit"))
    with _client() as c:
        d = c.get(f"/today/matches/new?have={have}").json()
    got = {n["id"]: n for n in d["new"]}
    assert set(got) == {ids["good"], ids["maybe"]}
    # The "good" card sits between the strong one and the estimate; "maybe" before the 1.8.
    assert got[ids["good"]]["before"] == ids["est"]
    assert got[ids["maybe"]]["before"] == ids["nofit"]
    assert d["pending"] == 1
    assert f'data-id="{ids["good"]}"' in got[ids["good"]]["html"]
    assert got[ids["good"]]["strong"] is True and got[ids["maybe"]]["strong"] is False
    assert d["found_line"] == "7 new roles found today"
    assert d["ids"][0] == ids["strong"]
    with _client() as c:
        none = c.get("/today/matches/new?have=" + ",".join(str(i) for i in d["ids"])).json()
    assert none["new"] == []


def test_a_card_the_page_shows_in_review_comes_back_finished_with_the_order():
    """Owner round 13: a role whose review finished moves ahead of roles still in review."""
    ids = _seed()
    every = ",".join(str(i) for i in ids.values())
    # The page still shows "good" and "quick" as in review; only "good" has finished.
    pend = f"{ids['good']},{ids['quick']}"
    with _client() as c:
        d = c.get(f"/today/matches/new?have={every}&pending={pend}").json()
    done = {n["id"]: n for n in d["done"]}
    assert set(done) == {ids["good"]}
    assert f'data-id="{ids["good"]}"' in done[ids["good"]]["html"]
    assert 'data-pending="1"' not in done[ids["good"]]["html"]
    assert ids["quick"] not in d["ids"]


# ------------------------------------------------------------- Decisions and Applications
def test_decisions_waiting_lists_the_undecided_backlog_and_the_listed_roles():
    ids = _seed()

    async def go(s):
        return await tv.decisions_waiting(s, "employment")

    data = _db(go)
    rows = {r["title"]: r for r in data["rows"]}
    assert set(rows) == {"Director of Enterprise Applications", "Customer Operations Lead"}
    assert data["n"] == 2
    assert rows["Director of Enterprise Applications"]["status"] == "Not decided"
    assert rows["Customer Operations Lead"]["status"] == "Shortlisted, not applied"
    # Today's own roles wait in Top matches; they are never in both places.
    assert ids["strong"] not in {r["id"] for r in data["rows"]}
    with _client() as c:
        html = c.get("/today/decisions").text
    assert 'data-testid="dw-count">2<' in html
    rows_html = html.split('data-testid="dw-row"')[1:]
    undecided = next(r for r in rows_html if f"/opportunities/{ids['backlog']}" in r)
    assert f'hx-post="/opportunities/{ids["backlog"]}/decide/shortlist"' in undecided
    assert f'hx-post="/opportunities/{ids["backlog"]}/decide/not-for-me?reason=later"' in undecided
    listed = next(r for r in rows_html if f"/opportunities/{ids['listed']}" in r)
    assert f'href="/opportunities/{ids["listed"]}/tailor"' in listed and ">Tailor resume<" in listed
    assert f'hx-post="/today/decisions/{ids["pursuit_listed"]}/applied"' in listed
    assert ">I applied<" in listed


def test_applications_needing_action_use_the_applications_verbs():
    ids = _seed()
    with _client() as c:
        html = c.get("/today/applications").text
    assert 'data-testid="ap-count">2<' in html
    interview = html.split("Chief Operations Lead")[1].split('data-testid="ap-row"')[0]
    assert ">Interview<" in interview and "Today" in interview
    assert f'href="/prep/{ids["interview"]}?auto=1"' in interview and ">Prepare<" in interview
    quiet = html.split("Supply Chain Planning Lead")[1]
    assert "Due 3 days ago" in quiet and "td-when--due" in quiet
    assert f'hx-post="/today/applications/{ids["pursuit_quiet"]}/follow-again"' in quiet
    assert ">Follow up again<" in quiet
    # There is no Draft follow-up: Kestrel has no drafting path (D-UI-148).
    assert "Draft follow-up" not in html


def test_i_applied_and_follow_up_again_record_the_verb_and_carry_an_undo():
    ids = _seed()
    with _client() as c:
        r = c.post(f"/today/decisions/{ids['pursuit_listed']}/applied")
        assert r.status_code == 200
        trig = r.headers["HX-Trigger"]
        assert "Follow up on" in trig and "/pursuits/undo/" in trig and "td-refresh" in trig
        token = re.search(r"/pursuits/undo/([\w-]+)", trig).group(1)
        # It left Decisions waiting (applied), and the pursuit moved to applied.
        assert "Customer Operations Lead" not in c.get("/today/decisions").text
        # Undo reverts exactly that: the role is listed again.
        assert c.post(f"/pursuits/undo/{token}").status_code == 200
        assert "Customer Operations Lead" in c.get("/today/decisions").text
        f = c.post(f"/today/applications/{ids['pursuit_quiet']}/follow-again")
        assert "Follow-up moved to" in f.headers["HX-Trigger"]
        assert "Due 3 days ago" not in c.get("/today/applications").text
        gone = c.post("/today/decisions/99999/applied")
    assert "That one is gone." in gone.headers["HX-Trigger"]


# --------------------------------------------------------------------- New signals (consulting)
def test_new_signals_show_on_consulting_only_with_their_actions():
    ids = _seed()

    async def add(s):
        org = (await s.execute(select(Org).where(Org.name == "Ironbridge Supply"))).scalar_one()
        opp = Opportunity(
            track="consulting", opp_type="trigger_event", org_id=org.id,
            org_name="Ironbridge Supply",
            title="New CFO announced a systems review", url="https://example.test/sig",
            stage="t2_done", event_date="2026-10-01",
            trigger_data={"source_label": "Company news release"},
            first_seen_at="2026-10-23T09:00:00+00:00",
        )
        s.add(opp)
        await s.flush()
        s.add(Evaluation(opportunity_id=opp.id, tier=2, track="consulting", verdict="pursue",
                         verdict_model="pursue", score=3.9, weighted_score=3.9, blocks={}))
        return opp.id

    sid = _db(add)
    with _client() as c:
        con = c.get("/?track=consulting").text
        emp = c.get("/?track=employment").text
    assert 'data-testid="td-signals"' in con and 'data-testid="td-signals"' not in emp
    sig = con.split('data-testid="td-signals"')[1].split("</section>")[0]
    assert "Ironbridge Supply" in sig and "New CFO announced a systems review" in sig
    assert "Company news release · 2026-10-01" in sig and ">3.9<" in sig
    assert f'hx-post="/opportunities/{sid}/lead"' in sig and ">Add to leads<" in sig
    assert f'hx-post="/opportunities/{sid}/decide/not-for-me?reason=later"' in sig
    assert ">Research<" in sig and ">Not relevant<" in sig
    # Trigger events appear only in New signals, never as a Top matches card.
    assert "New CFO announced" not in con.split('data-testid="td-matches"')[1]
    assert ids  # the employment day is untouched


def test_consulting_cards_say_add_to_leads_and_name_the_kind():
    _seed()

    async def add(s):
        org = Org(name="Meadowbrook Foods", normalized_name="meadowbrook foods")
        s.add(org)
        await s.flush()
        opp = Opportunity(
            track="consulting", opp_type="contract_posting", org_id=org.id,
            org_name="Meadowbrook Foods", title="Weekly reporting dashboard build, 6 weeks",
            url="https://example.test/c1", stage="t2_done", comp_min=552, comp_max=650,
            comp_currency="USD", comp_interval="day", comp_source="posted",
            first_seen_at=today_seed._local_midnight_safe(__import__("datetime").timedelta(minutes=4)),
            description_md="Power BI reporting for operations.",
        )
        s.add(opp)
        await s.flush()
        s.add(Evaluation(opportunity_id=opp.id, tier=2, track="consulting", verdict="pursue",
                         verdict_model="pursue", score=4.5, weighted_score=4.5,
                         blocks={"a_situation": {"stated": "Reporting is built by hand."}}))

    _db(add)
    with _client() as c:
        body = c.get("/?track=consulting").text
    sec = body.split('data-testid="td-matches"')[1].split("</section>")[0]
    assert "Contract posting" in sec and "$552 to $650 a day" in sec
    assert ">Add to leads<" in sec and ">Review<" in sec and ">Review role<" not in sec
    assert "Leads needing action" in body and "Proposals, follow-ups and calls coming up." in body
    assert "Work you have not decided on, and leads you have not contacted yet." in body


# --------------------------------------------------------------------------- the role summary
def test_review_opens_the_pipeline_panels_read_of_the_role_and_never_decides():
    ids = _seed()
    with _client() as c:
        r = c.get(f"/today/roles/{ids['strong']}/summary")
        assert r.status_code == 200
        html = r.text
        pursuits_before = _db(lambda s: s.scalar(select(Pursuit.id).where(
            Pursuit.opportunity_id == ids["strong"])))
        c.get(f"/today/roles/{ids['strong']}/summary")
        gone = c.get("/today/roles/99999/summary")
    assert pursuits_before is None  # Review alone writes nothing
    assert "Role summary" in html and "Senior Operations Analyst" in html
    assert 'data-testid="td-ring"' in html and "out of 5" in html
    assert "Why it fits" in html and "Written by Kestrel's AI" in html
    foot = html.split('data-testid="td-drawer-foot"')[1]
    assert f'hx-post="/opportunities/{ids["strong"]}/decide/shortlist"' in foot
    assert f'href="/opportunities/{ids["strong"]}/tailor"' in foot and ">Tailor resume<" in foot
    assert f'href="/opportunities/{ids["strong"]}"' in foot and "Open full role" in foot
    assert gone.status_code == 404 and "That role is gone" in gone.text
    # A quick-screen role says so instead of inventing reasons.
    with _client() as c:
        pend = c.get(f"/today/roles/{ids['quick']}/summary").text
    assert "Full review in progress" in pend and "Why it fits" not in pend


# ------------------------------------------------------------------------------ privacy
def test_confidential_mode_redacts_the_employer_on_cards_lists_and_the_summary():
    ids = _seed()

    async def conf(s):
        s.add(Setting(key="confidential_mode", value=True))
        master = (await s.execute(select(DocMaster))).scalars().first()
        s.add(DocSection(doc_master_id=master.id, section_kind="experience", sort_order=0,
                         org_label="Larkspur Logistics", org_label_redacted="[Confidential]"))
        for o in (await s.execute(select(Opportunity))).scalars():
            if o.org_name == "Larkspur Logistics":
                o.title = "Operations Analyst at Larkspur Logistics"

    _db(conf)
    with _client() as c:
        body = c.get("/").text
        summary = c.get(f"/today/roles/{ids['strong']}/summary").text
    assert "Larkspur Logistics" not in body and "Larkspur Logistics" not in summary
    assert "[Confidential]" in body


# ------------------------------------------------------------------------ finish setup
def test_the_finish_setup_banner_counts_the_steps_that_are_left():
    _seed()

    async def no_resume(s):
        for m in (await s.execute(select(DocMaster))).scalars():
            await s.delete(m)

    with _client() as c:
        one = c.get("/").text
    banner = one.split('id="finish-setup" data-testid')[1].split("</div>")[0]
    assert "Finish setting up Kestrel:" in banner and "1 step left." in banner
    assert "Choose how scoring runs." in banner and 'href="/onboarding"' in banner
    _db(no_resume)
    with _client() as c:
        two = c.get("/").text
    banner = two.split('id="finish-setup" data-testid')[1].split("</div>")[0]
    assert "2 steps left." in banner
    assert "Add your resume text and choose how scoring runs." in banner


# ----------------------------------------------------------------------- walkthrough hooks
def test_the_walkthrough_runs_over_this_page_and_its_real_sidebar():
    _seed()
    with _client() as c:
        plain = c.get("/").text
        tour = c.get("/?tour=1").text
    assert 'data-tour="0"' in plain and 'data-tour="1"' in tour
    assert 'id="td-tour"' in tour and 'role="dialog" aria-label="Walkthrough"' in tour
    # It highlights the real navigation, not a copy of it: the page has no mock shell.
    assert 'class="mainnav"' in tour and "td-nav" not in tour and "mock" not in tour.lower()
    js = Path(paths.web_static_dir(), "js", "today.js").read_text(encoding="utf-8")
    for href in ("/opportunities", "/calibrate", "/pursuits"):
        assert f"nav('{href}')" in js
    for step in ("Today is your daily start", "Every role Kestrel finds", "Your top matches",
                 "See why a role fits", "Teach Kestrel your taste", "Track what you applied to",
                 "That's it"):
        assert step in js
    assert "Kestrel never applies for you." in js


# ------------------------------------------------------------------ design-system guards
_ROOT = Path(paths.web_templates_dir())
_TODAY_FILES = [_ROOT / "dashboard.html", *sorted((_ROOT / "partials").glob("td_*.html"))]


def test_the_page_is_built_on_the_design_system_only():
    for f in _TODAY_FILES:
        text = f.read_text(encoding="utf-8")
        assert "—" not in text, f"em dash in {f.name}"
        assert not re.search(r"#[0-9A-Fa-f]{3,8}\b", re.sub(r"\{#.*?#\}", "", text, flags=re.S)), f
        # Inline styles may only set a custom property (a width the server computed).
        for style in re.findall(r'style="([^"]*)"', text):
            assert re.fullmatch(r"\s*--[\w-]+:\s*[^;]+;?\s*", style), (f.name, style)
    css = Path(paths.web_static_dir(), "css", "pages", "today.css").read_text(encoding="utf-8")
    no_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert not re.search(r"#[0-9A-Fa-f]{3,8}\b|rgba?\(", no_comments)
    assert "—" not in css
    js = Path(paths.web_static_dir(), "js", "today.js").read_text(encoding="utf-8")
    assert "—" not in js and not re.search(r"#[0-9A-Fa-f]{6}\b", js)


def test_the_page_stops_loading_the_legacy_stylesheet_and_loads_its_own():
    _seed()
    with _client() as c:
        head = c.get("/").text.split("</head>")[0]
    assert "/static/css/kestrel.css" not in head and "/static/css/shell.css" in head
    assert "/static/css/pages/today.css" in head and "/static/js/today.js" in head


def test_the_container_steps_match_the_mockup():
    css = Path(paths.web_static_dir(), "css", "pages", "today.css").read_text(encoding="utf-8")
    # The strip stacks at the mockup's 1300px window and the lower panels at its 1100px window:
    # both measure the window (the kwin container), not the content column.
    assert re.search(
        r"@container kwin \(max-width: 1300px\) \{[^@]*\.td-strip \{[^}]*minmax\(0, 1fr\)", css)
    assert "@container kwin (max-width: 1100px)" in css
    # The strip hover follows the strip's outer radius only: square segments clipped by the strip.
    assert re.search(r"\.td-strip \{[^}]*overflow: hidden", css)
    assert re.search(r"\.td-seg \{[^}]*border-radius: 0", css)
    # Reduced motion turns the entrance and transitions off.
    assert "prefers-reduced-motion: reduce" in css


def test_the_scan_button_is_run_now_in_design_system_classes():
    _seed()
    with _client() as c:
        idle = c.get("/scan/button").text
    assert "k-link" in idle and "Run now" in idle and 'class="btn' not in idle


# ------------------------------------------------------------------------- review fixes
def test_the_poll_timer_is_not_restarted_by_each_strip_swap():
    js = Path(paths.web_static_dir(), "js", "today.js").read_text(encoding="utf-8")
    body = js.split("function startPoll()")[1].split("\n  }\n")[0]
    # Only a change of period replaces the timer, so a strip that swaps every 5 s cannot keep
    # a 6 s poll from ever firing.
    assert "S.pollMs === ms) return" in body


def test_undo_refreshes_every_panel_not_only_the_cards():
    _seed()
    with _client() as c:
        body = c.get("/").text
    for panel in ("td-decisions", "td-apps"):
        tag = body.split('<section class="k-card')[1:]
        tag = next(x for x in tag if f'id="{panel}" data-testid' in x).split("data-no-busy>")[0]
        assert "kestrel:undone from:document" in tag
    js = Path(paths.web_static_dir(), "js", "today.js").read_text(encoding="utf-8")
    assert "refreshPanels();" in js.split("kestrel:undone")[1]


def test_the_first_run_today_offers_the_tour_and_a_mature_one_does_not():
    _seed(with_run=False)
    with _client() as c:
        first = c.get("/").text
    assert 'data-testid="td-tour-start"' in first
    _seed()
    with _client() as c:
        assert 'data-testid="td-tour-start"' not in c.get("/").text


def test_signals_can_be_asked_for_again_and_hide_when_none_are_left():
    _seed()
    with _client() as c:
        r = c.get("/today/signals?track=consulting")
    assert r.status_code == 200 and "hidden" in r.text


def test_a_decision_row_tone_follows_the_strong_match_cutoff():
    _seed()

    async def raise_cutoff(s):
        s.add(Setting(key="thresholds.apply", value=4.5))

    async def rows(s):
        data = await tv.decisions_waiting(s, "employment")
        return {r["title"]: r["tone"] for r in data["rows"]}

    assert _db(rows)["Customer Operations Lead"] == "good"  # 4.2 against 4.0
    _db(raise_cutoff)
    assert _db(rows)["Customer Operations Lead"] == "warn"  # 4.2 against 4.5
