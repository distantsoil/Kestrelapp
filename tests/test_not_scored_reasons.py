"""Pipeline says why a role has no score (issue #90, D-UI-236). One chip per reason, two groups:
Being scored (Waiting, Paused, Scoring) and Won't be scored unless you act (No description,
Hidden: applied, Taken off retry list). The SQL grouping (queries._group_expr) and the Python
word (queries.row_fit) must agree for every reason, no chip says rejected or failed, and a
pasted description goes back through Tier 0 on the normal pipeline. Cloud-safe: a migrated SQLite
file and the test client; no model, no browser, no network."""

from __future__ import annotations

import asyncio
import html as htmllib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from kestrel.db.models import Opportunity, Org, Source, Task
from kestrel.web import fit
from kestrel.web.fit import (
    EMPTY_POSTING_STAGE_REASON,
    GROUP_LABELS,
    SCORING_CHIPS,
    UNSCORED_LABEL,
    scoring_chip,
)
from kestrel.web.queries import OppFilters, list_opportunities
from tests import test_pipeline_view as T

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

_LONG = " ".join(["Owns the function and its vendors, security and budget."] * 8)


def _db(fn):
    return asyncio.run(T._with_session(fn))


# ------------------------------------------------------------------------------ the chip table
def test_the_group_is_renamed_and_the_old_label_is_gone():
    assert UNSCORED_LABEL == "Won't be scored unless you act"
    assert GROUP_LABELS[fit.GROUP_UNSCORED] == UNSCORED_LABEL
    assert "Not scored" not in GROUP_LABELS.values()


@pytest.mark.parametrize(
    ("state", "label", "kind"),
    [
        ("not_queued", "Waiting", "wait"),
        ("queued", "Waiting", "wait"),
        ("held", "Paused", "wait"),
        ("background", "Paused", "wait"),
        ("running", "Scoring", "live"),
        ("empty", "No description", "final"),
        ("hidden", "Hidden: applied", "final"),
        ("removed", "Taken off retry list", "final"),
    ],
)
def test_each_reason_maps_to_one_chip(state, label, kind):
    chip = scoring_chip(state)
    assert (chip["label"], chip["kind"]) == (label, kind)
    assert chip["tip"] and "—" not in chip["tip"] and "–" not in chip["tip"]


def test_a_waiting_role_adds_how_many_are_ahead_only_when_known():
    assert scoring_chip("queued", 3)["label"] == "Waiting, 3 ahead"
    assert scoring_chip("queued", 0)["label"] == "Waiting"
    assert scoring_chip("queued", None)["label"] == "Waiting"
    assert scoring_chip("not_queued", 4)["label"] == "Waiting"  # no place in line yet


def test_no_chip_says_rejected_or_failed_for_a_role_that_was_never_judged():
    """Hard rule 4: only the Scoring failed group's own chip may say failed."""
    for state, (label, tip, _kind) in SCORING_CHIPS.items():
        if state == "failed":
            continue
        text = f"{label} {tip}".lower()
        assert "reject" not in text.replace("was not rejected", "")
        assert "fail" not in text


def test_stopped_has_the_in_progress_style_and_failed_reads_scoring_failed():
    """Stopped sits in Being scored, so it is muted and dashed like Waiting, not the quiet final
    style; the failed chip says what the mockup draws."""
    assert scoring_chip("stuck")["label"] == "Stopped" and scoring_chip("stuck")["kind"] == "wait"
    assert scoring_chip("failed")["label"] == "Scoring failed"


def test_the_waiting_tip_says_it_scores_on_its_own_and_empty_says_why():
    assert "scores on its own" in SCORING_CHIPS["not_queued"][1]
    assert "nothing to score" in SCORING_CHIPS["empty"][1]
    assert "skips it" in SCORING_CHIPS["hidden"][1]
    assert "not rejected" in SCORING_CHIPS["removed"][1]


# ------------------------------------------------------------------- fit_for, one per reason
def test_fit_for_puts_each_final_reason_in_the_unscored_group_and_a_live_task_back():
    empty = EMPTY_POSTING_STAGE_REASON
    assert fit.fit_for(None, "t1_pass", stage_reason=empty).group == fit.GROUP_UNSCORED
    assert fit.fit_for(None, "t1_pass", stage_reason=empty, live=True).group == fit.GROUP_NEW
    assert fit.fit_for(None, "new", hidden_applied=True).group == fit.GROUP_UNSCORED
    assert fit.fit_for(None, "new", hidden_applied=True, live=True).group == fit.GROUP_NEW
    assert fit.fit_for(None, "quarantined", removed=True).group == fit.GROUP_UNSCORED
    assert fit.fit_for(None, "quarantined", removed=True, live=True).group == fit.GROUP_NEW
    assert fit.fit_for(None, "quarantined").group == fit.GROUP_FAILED  # not removed: still failed
    # Waiting and the rest keep the old group.
    assert fit.fit_for(None, "t0_pass").group == fit.GROUP_NEW
    assert fit.fit_for(None, "new").group == fit.GROUP_NEW


# ---------------------------------------------------- SQL and Python agree, reason by reason
async def _reasons(s) -> dict[str, int]:
    """One role for every reason a role has no score. Returns reason -> opportunity id."""
    from kestrel.db.models import Quarantine
    from kestrel.pipeline import lane
    from kestrel.services import retry_removal

    org = (await s.execute(select(Org))).scalars().first()
    src = (await s.execute(select(Source))).scalars().first()
    made: dict[str, Opportunity] = {
        "waiting": T._opp(org, src, "w1", stage="t0_pass"),
        "queued": T._opp(org, src, "w2", stage="t0_pass"),
        "background": T._opp(org, src, "w3", stage="t0_pass"),
        "scoring": T._opp(org, src, "w4", stage="t0_pass"),
        "empty": T._opp(org, src, "n1", stage="t1_pass", stage_reason=EMPTY_POSTING_STAGE_REASON,
                        description_md=""),
        "removed": T._opp(org, src, "n3", stage="quarantined"),
    }
    s.add_all(made.values())
    await s.flush()
    s.add(Task(type="t1", payload={"opportunity_id": made["queued"].id}, status="pending"))
    s.add(Task(type="t1", priority=lane.LANE_PRIORITY,
               payload={"opportunity_id": made["background"].id, **lane.lane_extra(0.0)}))
    s.add(Task(type="t2", payload={"opportunity_id": made["scoring"].id}, status="leased"))
    s.add(Quarantine(opportunity_id=made["removed"].id, tier=2, error="invalid JSON"))
    await s.flush()
    await retry_removal.mark(s, made["removed"].id)
    await s.commit()
    return {k: o.id for k, o in made.items()}


_EXPECT = {
    "waiting": (fit.GROUP_NEW, "not_queued", "Waiting"),
    "queued": (fit.GROUP_NEW, "queued", "Waiting"),
    "background": (fit.GROUP_NEW, "background", "Paused"),
    "scoring": (fit.GROUP_NEW, "running", "Scoring"),
    "empty": (fit.GROUP_UNSCORED, "empty", "No description"),
    "removed": (fit.GROUP_UNSCORED, "removed", "Taken off retry list"),
}


async def _both_views(s):
    """Every row the grouped list gives, active and hidden together, with the group the SQL CASE
    (queries._group_expr) puts each in, read per row from its own statement."""
    from kestrel.web.queries import GROUP_ORDER, _base_stmt, _group_expr, _with_operator

    rows = []
    for kw in ({}, {"hidden_only": True}):
        pg = await list_opportunities(s, OppFilters(grouped=True, page_size=200, **kw))
        rows += pg.rows
    sql = {}
    stmt = _with_operator(_base_stmt(OppFilters(include_archived=True), Opportunity.id,
                                     _group_expr()))
    for oid, g in (await s.execute(stmt)).all():
        sql[int(oid)] = GROUP_ORDER[int(g)]
    return {r.opp.id: r for r in rows}, sql


def test_sql_group_and_row_fit_agree_for_every_reason():
    asyncio.run(T._seed())
    ids = _db(_reasons)
    by_id, sql = _db(_both_views)
    for reason, (group, state, label) in _EXPECT.items():
        row = by_id[ids[reason]]
        assert row.fit.group == group == sql[ids[reason]], reason
        assert row.scoring == state, reason
        assert scoring_chip(state)["label"] == label, reason


async def _edge_cases(s) -> dict[str, int]:
    """The cases where a task, a switch or the hidden flag decides the group."""
    from kestrel.db.models import Quarantine, Setting
    from kestrel.pipeline.orphans import ORPHAN_KEY, record_key

    org = (await s.execute(select(Org))).scalars().first()
    src = (await s.execute(select(Source))).scalars().first()
    hid = {"archived_at": T.iso(T._now()), "archive_reason": "applied"}
    made = {
        "held": T._opp(org, src, "e1", stage="t0_pass"),
        "stuck": T._opp(org, src, "e2", stage="t0_pass"),
        "hidden_live": T._opp(org, src, "e3", stage="new", **hid),
        "hidden_held": T._opp(org, src, "e4", stage="new", **hid),
        "archived_empty": T._opp(org, src, "e5", stage="t1_pass", description_md="",
                                 stage_reason=EMPTY_POSTING_STAGE_REASON,
                                 archived_at=T.iso(T._now()), archive_reason="not_for_me"),
        "failed": T._opp(org, src, "e6", stage="quarantined"),
        "removed_live": T._opp(org, src, "e7", stage="quarantined"),
    }
    s.add_all(made.values())
    await s.flush()
    from kestrel.services import retry_removal

    s.add(Task(type="t1", payload={"opportunity_id": made["held"].id}, status="pending"))
    s.add(Task(type="t2", payload={"opportunity_id": made["hidden_live"].id}, status="leased"))
    s.add(Task(type="t2", payload={"opportunity_id": made["hidden_held"].id}, status="pending"))
    s.add(Task(type="t2", payload={"opportunity_id": made["removed_live"].id},
               status="pending"))
    s.add(Quarantine(opportunity_id=made["failed"].id, tier=2, error="invalid JSON"))
    s.add(Quarantine(opportunity_id=made["removed_live"].id, tier=2, error="invalid JSON"))
    s.add(Setting(key=ORPHAN_KEY, value={record_key(made["stuck"].id, "t0_pass"):
                                         "2026-10-16T00:00:00+00:00"}))
    await s.flush()
    await retry_removal.mark(s, made["removed_live"].id)
    await s.commit()
    return {k: o.id for k, o in made.items()}


def test_the_sql_group_matches_the_row_word_per_row_for_the_edge_cases():
    """Held, stuck, a hidden role with a live (and a held) task, an archived empty posting, a
    failed role and a removed role with a task: the group the SQL gives each row is the group its
    word names, row by row, never only in the counts."""
    from kestrel.services import tracks

    asyncio.run(T._seed())
    ids = _db(_edge_cases)
    try:
        orig = tracks.switched_off_tracks
        tracks.switched_off_tracks = lambda: ("employment",)  # type: ignore[assignment]
        try:
            by_id, sql = _db(_both_views)
        finally:
            tracks.switched_off_tracks = orig  # type: ignore[assignment]
    finally:
        tracks.forget_cache()
    want = {
        # Everything a task can still score stays in Being scored, held ones reading Paused.
        "held": (fit.GROUP_NEW, "held"),
        "stuck": (fit.GROUP_NEW, "stuck"),
        "hidden_live": (fit.GROUP_NEW, "running"),
        "hidden_held": (fit.GROUP_NEW, "held"),
        "failed": (fit.GROUP_FAILED, "failed"),
        "removed_live": (fit.GROUP_NEW, "held"),
    }
    for key, (group, state) in want.items():
        row = by_id[ids[key]]
        assert row.fit.group == group == sql[ids[key]], key
        assert row.scoring == state, key
    # An archived empty posting with nothing queued is unscored in both.
    assert sql[ids["archived_empty"]] == fit.GROUP_UNSCORED
    assert by_id[ids["archived_empty"]].fit.group == fit.GROUP_UNSCORED
    assert scoring_chip("held")["label"] == "Paused"


def test_a_score_anyway_task_held_by_a_switched_off_track_shows_paused():
    """One "a task is queued" definition (queries.scoring_task_exists): a hidden-applied role whose
    Score anyway task waits on a switched-off track is Being scored, reading Paused, in the list,
    the panel and the role page alike."""
    from kestrel.services import tracks

    asyncio.run(T._seed())
    ids = _db(_edge_cases)
    oid = ids["hidden_held"]
    try:
        orig = tracks.switched_off_tracks
        tracks.switched_off_tracks = lambda: ("employment",)  # type: ignore[assignment]
        try:
            with T._client() as c:
                row = T._row(c.get("/opportunities?hidden=1").text, oid)
                panel = c.get(f"/opportunities/{oid}/panel").text
                page = htmllib.unescape(c.get(f"/opportunities/{oid}").text)
        finally:
            tracks.switched_off_tracks = orig  # type: ignore[assignment]
    finally:
        tracks.forget_cache()
    assert 'data-group="new"' in row and 'data-scoring="held">Paused</span>' in row
    assert 'data-scoring="held"' in panel and UNSCORED_LABEL not in htmllib.unescape(panel)
    assert f"<h3>{UNSCORED_LABEL}</h3>" not in page


def test_hidden_applied_rows_are_unscored_and_only_in_the_hidden_view():
    ids = asyncio.run(T._seed())

    async def hide(s):
        org = (await s.execute(select(Org))).scalars().first()
        src = (await s.execute(select(Source))).scalars().first()
        o = T._opp(org, src, "ha", stage="new", archive_reason="applied",
                   archived_at=T.iso(T._now()), stage_reason="Imported applied history")
        s.add(o)
        await s.commit()
        return o.id

    oid = _db(hide)

    async def both(s):
        default = await list_opportunities(s, OppFilters(grouped=True, page_size=200))
        hidden = await list_opportunities(s, OppFilters(grouped=True, hidden_only=True,
                                                        page_size=200))
        return default, hidden

    default, hidden = _db(both)
    assert oid not in {r.opp.id for r in default.rows}
    assert default.group_counts[fit.GROUP_UNSCORED] == 0
    row = {r.opp.id: r for r in hidden.rows}[oid]
    assert row.fit.group == fit.GROUP_UNSCORED and row.scoring == "hidden"
    assert scoring_chip(row.scoring)["label"] == "Hidden: applied"
    assert ids["h"] in {r.opp.id for r in hidden.rows}  # the seeded hidden role is there too
    with T._client() as c:
        assert f'id="row-{oid}"' not in c.get("/opportunities").text
        page = htmllib.unescape(c.get("/opportunities?hidden=1").text)
    assert f"{UNSCORED_LABEL} · " in page and ">Hidden: applied<" in page


def test_a_removed_role_with_a_live_task_is_back_in_being_scored():
    asyncio.run(T._seed())
    ids = _db(_reasons)

    async def queue(s):
        s.add(Task(type="t2", payload={"opportunity_id": ids["removed"]}, status="pending"))
        await s.commit()

    _db(queue)

    async def listed(s):
        return await list_opportunities(s, OppFilters(grouped=True, page_size=200))

    row = {r.opp.id: r for r in _db(listed).rows}[ids["removed"]]
    assert row.fit.group == fit.GROUP_NEW and row.scoring in ("queued", "running")


# ----------------------------------------------------------------------------- the rendered list
def test_the_list_shows_two_groups_with_reason_chips_and_the_new_label():
    asyncio.run(T._seed())
    ids = _db(_reasons)
    with T._client() as c:
        html = T._get(c, "/opportunities")
    text = htmllib.unescape(html)
    assert f"{UNSCORED_LABEL} · 2" in text and "Not scored" not in text
    assert text.index("Being scored · ") < text.index(UNSCORED_LABEL)
    for reason, (_group, state, label) in _EXPECT.items():
        row = T._row(html, ids[reason])
        assert f'data-scoring="{state}"' in row and f">{label}" in row, reason
        assert "pl-chip--" in row
    # The age line keeps its own wording; there is no Found or Checking CHIP.
    assert ">Found<" not in html and ">Checking<" not in html


def test_the_empty_panel_offers_paste_open_posting_and_score_anyway():
    asyncio.run(T._seed())
    ids = _db(_reasons)
    with T._client() as c:
        panel = c.get(f"/opportunities/{ids['empty']}/panel").text
    assert 'data-testid="panel-paste-open"' in panel and "Paste the description" in panel
    assert f'hx-post="/opportunities/{ids["empty"]}/description"' in panel
    assert 'data-testid="panel-open-posting"' in panel
    assert 'data-testid="panel-score-anyway"' in panel
    assert 'data-testid="panel-chip"' in panel and "No description" in panel


def test_the_paused_panel_offers_to_turn_the_track_on():
    from kestrel.services import tracks

    asyncio.run(T._seed())
    ids = _db(_reasons)
    try:
        with T._client() as c:
            orig = tracks.switched_off_tracks
            tracks.switched_off_tracks = lambda: ("employment",)  # type: ignore[assignment]
            try:
                panel = c.get(f"/opportunities/{ids['queued']}/panel").text
            finally:
                tracks.switched_off_tracks = orig  # type: ignore[assignment]
    finally:
        tracks.forget_cache()
    assert 'data-scoring="held"' in panel and "Paused" in panel
    assert 'data-testid="panel-turn-on"' in panel


# ------------------------------------------------------------------------- Paste the description
def _post(c, oid, text):
    return c.post(f"/opportunities/{oid}/description", data={"text": text},
                  headers={"HX-Request": "true"})


def test_pasted_text_is_stored_and_sends_the_role_back_through_tier_zero():
    asyncio.run(T._seed())
    ids = _db(_reasons)
    oid = ids["empty"]
    with T._client() as c:
        r = _post(c, oid, _LONG)
    assert r.status_code == 200
    toast = json.loads(r.headers["HX-Trigger"])["k-toast"]["message"]
    assert "Description saved" in toast

    async def read(s):
        o = await s.get(Opportunity, oid)
        embeds = (await s.execute(select(Task).where(Task.type == "embed"))).scalars().all()
        return o, [t.payload for t in embeds]

    opp, embeds = _db(read)
    assert opp.description_md == _LONG.strip()
    assert opp.stage == "new" and opp.stage_reason is None  # Tier 0 sweeps `new` again
    assert opp.version == 2 and opp.description_tokens > 0 and opp.content_hash
    assert {"opportunity_id": oid} in embeds


def test_pasted_text_reaches_tier_zero_and_the_pipeline_not_a_new_path():
    """The stage the endpoint leaves is the one run_tier0_batch sweeps, and the role is no
    longer a stub, so Tier 2 will not park it as empty again."""
    from kestrel.pipeline.tier2 import is_stub_posting

    asyncio.run(T._seed())
    ids = _db(_reasons)
    oid = ids["empty"]
    with T._client() as c:
        _post(c, oid, _LONG)

    async def check(s):
        o = await s.get(Opportunity, oid)
        src = await s.get(Source, o.source_id)
        return is_stub_posting(o, src), o.stage

    stub, stage = _db(check)
    assert stub is False and stage == "new"


def test_text_too_short_to_score_is_refused_and_changes_nothing():
    asyncio.run(T._seed())
    ids = _db(_reasons)
    oid = ids["empty"]
    with T._client() as c:
        r = _post(c, oid, "VP of Planning, remote.")
    ev = json.loads(r.headers["HX-Trigger"])["k-toast"]
    assert "too short" in ev["message"] and ev["tone"] == "warn"

    async def read(s):
        return await s.get(Opportunity, oid)

    opp = _db(read)
    assert opp.stage == "t1_pass" and opp.stage_reason == EMPTY_POSTING_STAGE_REASON
    assert opp.version == 1


def test_only_a_role_with_no_description_takes_pasted_text():
    ids = asyncio.run(T._seed())
    with T._client() as c:
        r = _post(c, ids["a"], _LONG)
    assert "already has its description" in json.loads(r.headers["HX-Trigger"])["k-toast"][
        "message"]

    async def read(s):
        return (await s.get(Opportunity, ids["a"])).description_md

    assert _db(read) != _LONG.strip()


def test_pasting_for_a_missing_role_says_so():
    asyncio.run(T._seed())
    with T._client() as c:
        r = _post(c, 99999, _LONG)
    assert "gone" in json.loads(r.headers["HX-Trigger"])["k-toast"]["message"]


def test_the_endpoint_makes_no_outbound_http_call(monkeypatch):
    """Hard rule 1: pasting stores text and nothing else. The HTTP client the app fetches pages
    with is patched to fail the test if it is ever opened or used."""
    from kestrel.ingest import http as ingest_http

    calls: list[str] = []

    def boom(*a, **k):
        calls.append("called")
        raise AssertionError("pasting must not open an HTTP client")

    monkeypatch.setattr(ingest_http.HttpClient, "__init__", boom)
    monkeypatch.setattr(ingest_http.HttpClient, "get", boom, raising=False)
    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "send", boom)
    monkeypatch.setattr(httpx.Client, "send", boom)
    asyncio.run(T._seed())
    ids = _db(_reasons)
    with T._client() as c:
        r = _post(c, ids["empty"], _LONG)
    assert r.status_code == 200 and calls == []


def test_a_hidden_role_refuses_a_paste_and_changes_nothing():
    asyncio.run(T._seed())
    ids = _db(_edge_cases)
    oid = ids["archived_empty"]
    with T._client() as c:
        r = _post(c, oid, _LONG)
    ev = json.loads(r.headers["HX-Trigger"])["k-toast"]
    assert ev["message"] == "Unhide it first, then paste the description." and ev["tone"] == "warn"

    async def read(s):
        return await s.get(Opportunity, oid)

    opp = _db(read)
    assert opp.description_md == "" and opp.stage == "t1_pass" and opp.version == 1
    assert opp.stage_reason == EMPTY_POSTING_STAGE_REASON


def test_the_panel_of_a_hidden_empty_role_says_to_unhide_first_instead_of_the_form():
    asyncio.run(T._seed())
    ids = _db(_edge_cases)
    with T._client() as c:
        panel = c.get(f"/opportunities/{ids['archived_empty']}/panel").text
    assert 'data-testid="paste-hidden"' in panel and "Unhide it first" in panel
    assert 'data-testid="panel-paste-open"' not in panel


def test_text_over_fifty_thousand_characters_is_refused_with_a_plain_message():
    asyncio.run(T._seed())
    ids = _db(_reasons)
    oid = ids["empty"]
    with T._client() as c:
        r = _post(c, oid, "word " * 10_001)  # 50,005 characters
        ok = _post(c, oid, ("word " * 9_999).strip())  # under the cap
    ev = json.loads(r.headers["HX-Trigger"])["k-toast"]
    assert ev["message"] == "That is too long to paste. Keep it under 50,000 characters."
    assert "Description saved" in json.loads(ok.headers["HX-Trigger"])["k-toast"]["message"]


def test_the_form_caps_the_textarea_at_the_same_limit():
    asyncio.run(T._seed())
    ids = _db(_reasons)
    with T._client() as c:
        panel = c.get(f"/opportunities/{ids['empty']}/panel").text
    assert 'maxlength="50000"' in panel


def test_the_role_page_offers_paste_and_says_it_waits_for_one():
    asyncio.run(T._seed())
    ids = _db(_reasons)
    oid = ids["empty"]
    with T._client() as c:
        page = c.get(f"/opportunities/{oid}").text
        assert 'data-testid="panel-paste-open"' in page and 'name="back" value="role"' in page
        assert f'action="/opportunities/{oid}/description"' in page
        text = htmllib.unescape(page)
        assert "It waits for a description you paste, or for a rescrape that brings one." in text
        assert "when a later scrape brings the full text" not in text
        # The plain form comes back to the role page with a notice, not the panel.
        r2 = c.post(f"/opportunities/{oid}/description", data={"text": "no", "back": "role"},
                    follow_redirects=False)
        assert "msg=description_short" in r2.headers["location"]
        r = c.post(f"/opportunities/{oid}/description", data={"text": _LONG, "back": "role"},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith(f"/opportunities/{oid}")
        assert "msg=description_saved" in r.headers["location"]


def test_attach_description_records_a_comp_observation_and_recomputes_the_ghost_score():
    """The twin of _handle_reseen: parsed pay is recorded for the comp corpus, and the ghost score
    follows the new text."""
    from kestrel.db.models import CompObservation

    asyncio.run(T._seed())
    ids = _db(_reasons)
    oid = ids["empty"]
    text = ("Own operations strategy and a team of 8 direct reports. Base pay $150,000 to "
            "$180,000 a year. " + _LONG)

    async def prep(s):
        o = await s.get(Opportunity, oid)
        o.comp_min = o.comp_max = None
        o.ghost_score = 0.99  # a stale value the recompute must replace
        await s.commit()

    _db(prep)
    with T._client() as c:
        _post(c, oid, text)

    async def read(s):
        o = await s.get(Opportunity, oid)
        obs = (await s.execute(select(CompObservation).where(
            CompObservation.opportunity_id == oid))).scalars().all()
        return o, obs

    opp, obs = _db(read)
    assert (opp.comp_min, opp.comp_max) == (150000, 180000) and opp.comp_source == "parsed"
    assert len(obs) == 1 and (obs[0].min, obs[0].max) == (150000, 180000)
    assert obs[0].source == "parsed"
    assert opp.ghost_score != 0.99


def test_a_stand_in_is_a_stub_only_below_the_bar():
    from kestrel.pipeline.tier2 import is_stub_posting

    short = SimpleNamespace(trigger_data=None, event_date=None, opp_type="job_posting",
                            description_md="Too short.")
    long = SimpleNamespace(trigger_data=None, event_date=None, opp_type="job_posting",
                           description_md=_LONG)
    assert is_stub_posting(short) and not is_stub_posting(long)


def test_pasted_text_cannot_make_the_dedup_or_team_patterns_slow():
    """A pasted description reaches normalize_desc and the team check; neither pattern may go
    polynomial on hostile text (CodeQL py/polynomial-redos on #90's paste route)."""
    import time

    from kestrel.ingest.dedup import normalize_desc
    from kestrel.ingest.normalize import mentions_team

    t0 = time.monotonic()
    normalize_desc("&utm_" * 20_000)
    normalize_desc("?utm_" * 20_000)
    normalize_desc("?utm_a" * 20_000)
    mentions_team("0" * 50_000)
    assert time.monotonic() - t0 < 1.0
    kept = normalize_desc("Go https://x.test/j?utm_source=a&utm_medium=b now ?ref=c ok")
    assert kept == "go now ok"
    assert mentions_team("a 12-person team") and mentions_team("a 12345 person team")
