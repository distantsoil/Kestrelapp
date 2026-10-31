"""Regression tests for the back-end review fixes (D-UI-160)."""

from __future__ import annotations

import html
import re

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from kestrel.config import load_settings
from kestrel.db.models import ConnectionPosition, Contact, Org
from kestrel.research import connections as cx
from tests.test_settings_routes import _all, _client, _csv, _db

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

CEDAR = ("First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
         "Imogen,Hale,u,,Cedar Ridge,Ops,12 Aug 2024\n"
         "Dmitri,Okon,u,,Harbor,Planner,03 Mar 2023\n")


def _payload(review: str) -> str:
    return html.unescape(re.search(r'name="payload" value="([^"]*)"', review).group(1))


def _seed_orgs(*names):
    async def f(s):
        for n in names:
            s.add(Org(name=n, normalized_name=n.lower()))
        await s.flush()
        return {o.name: o.id for o in (await s.execute(select(Org))).scalars()}

    return _db(f, commit=True)


# ------------------------------------------------------------------------------------- R1
def test_r1_failed_import_logs_no_bound_parameters(monkeypatch):
    async def boom(*_a, **_k):
        raise SQLAlchemyError("INSERT INTO contact (name) VALUES (?) [parameters: ('Sam Ortiz',)]")

    with _client() as c:
        review = c.post("/settings/data/contacts/read", files=_csv(CEDAR, "c.csv")).text
        monkeypatch.setattr(cx, "apply_import", boom)
        with structlog.testing.capture_logs() as logs:
            r = c.post("/settings/data/contacts/apply", data={"payload": _payload(review)})
    assert "Nothing was imported" in r.text
    assert logs and "Sam Ortiz" not in repr(logs) and "parameters" not in repr(logs)


# ------------------------------------------------------------------------------------- R8
def test_r8_keeping_the_proposed_match_confirms_it():
    with _client() as c:
        ids = _seed_orgs("Cedar Ridge Foods")
        review = c.post("/settings/data/contacts/read", files=_csv(CEDAR, "c.csv")).text
        c.post("/settings/data/contacts/apply", data={
            "payload": _payload(review), "match_company_0": "Cedar Ridge",
            "match_0": str(ids["Cedar Ridge Foods"])})
    pos = _all(select(ConnectionPosition.match, ConnectionPosition.operator_decided,
                      ConnectionPosition.company_name))
    cedar = [p for p in pos if p[2] == "Cedar Ridge"][0]
    assert cedar[0] == cx.MATCH_MANUAL and cedar[1] is True
    linked = _all(select(Contact.org_id).where(Contact.name.like("Imogen%")))
    assert linked[0][0] == ids["Cedar Ridge Foods"]


# ------------------------------------------------------------------------------------- R20
def test_r20_rulings_follow_the_company_name_not_the_list_position():
    with _client() as c:
        _seed_orgs("Cedar Ridge Foods", "Harbor Freight")
        review = c.post("/settings/data/contacts/read", files=_csv(CEDAR, "c.csv")).text
        # The review listed Harbor as the first row; the posted ruling names it explicitly.
        c.post("/settings/data/contacts/apply", data={
            "payload": _payload(review), "match_company_7": "Harbor", "match_7": "none"})
    pos = {p[0]: p[1] for p in _all(select(ConnectionPosition.company_name,
                                           ConnectionPosition.match))}
    assert pos["Harbor"] == cx.MATCH_NONE
    assert pos["Cedar Ridge"] != cx.MATCH_NONE


# ------------------------------------------------------------------------------------- R2
async def test_r2_an_online_current_model_is_not_checked_without_the_tick(sm):
    from kestrel.bench import check

    settings = load_settings()
    from kestrel.db.models import Label, Opportunity

    async with sm() as s:
        for i in range(6):
            o = Opportunity(track="employment", opp_type="posting", title=f"Role {i}")
            s.add(o)
            await s.flush()
            s.add(Label(opportunity_id=o.id, track="employment", score=4.0, verdict="apply"))
        await s.commit()
    hosted = settings.model_copy(deep=True)
    hosted.llm.backends["hosted"] = hosted.llm.backends["local"].model_copy(
        update={"external": True})
    hosted.llm.tiers["tier2"].backend = "hosted"
    async with sm() as s:
        res = await check.queue_check(s, settings=hosted)
    assert not res.queued and res.reason == "online_refused" and "Rating" in res.message
    async with sm() as s:  # a local setup is unaffected
        assert (await check.queue_check(s, settings=settings)).queued


# ------------------------------------------------------------------------------------- R3
@pytest.fixture
def _evals(tmp_path, monkeypatch):
    """Dev builds resolve the evals folder beside the repo; keep these tests out of it."""
    from kestrel import paths

    d = tmp_path / "evals"
    d.mkdir()
    monkeypatch.setattr(paths, "evals_dir", lambda: d)
    return d


def test_r3_the_check_names_its_answer_key_and_builds_it_from_your_ratings_in_app(_evals):
    from kestrel.bench import check
    from tests.model_check_seed import seed_labels
    from tests.test_hosted_mode import _fresh_app

    client = _fresh_app()
    seed_labels(6)
    page = client.get("/bench").text
    assert "shipped example roles, not your ratings" in page
    assert "against the roles you rated" not in page
    assert 'data-testid="ck-key-build"' in page
    r = client.post("/bench/answer-key")
    assert "are now the answer key" in r.text
    info = check.answer_key_info()
    assert info["uses_mine"] and info["mine"] == 6
    assert "6 of your rated roles plus" in client.get("/bench").text


def test_r3_too_few_ratings_keep_the_shipped_key_and_say_so(_evals):
    from tests.model_check_seed import seed_labels
    from tests.test_hosted_mode import _fresh_app

    client = _fresh_app()
    seed_labels(3)
    r = client.post("/bench/answer-key")
    assert "Rate at least 5 roles first" in r.text


# ------------------------------------------------------------------------------------- R4
async def test_r4_an_older_undo_does_not_roll_back_a_later_move(sm):
    from kestrel.db.models import Opportunity, Pursuit, StatusEvent
    from kestrel.services import pursuit_undo as pu

    async with sm() as s:
        o = Opportunity(track="employment", opp_type="posting", title="Planner")
        s.add(o)
        await s.flush()
        p = Pursuit(opportunity_id=o.id, track="employment", status="screening")
        s.add(p)
        await s.commit()
        snap = await pu.snapshot(s, p)
        p.status = "applied"
        token_a = await pu.save(s, snap)
        await s.commit()
        p.status = "interviewing"  # a later move of the same card
        await s.commit()
        track, msg = await pu.restore(s, token_a)
        await s.commit()
        assert track is None and msg == "This can no longer be undone."
        assert (await s.get(Pursuit, p.id)).status == "interviewing"
        events = (await s.execute(select(StatusEvent))).scalars().all()
        assert not [e for e in events if e.note == "Undid the last change"]


async def test_r4_an_undo_with_nothing_after_it_still_reverts(sm):
    from kestrel.db.models import Opportunity, Pursuit
    from kestrel.services import pursuit_undo as pu

    async with sm() as s:
        o = Opportunity(track="employment", opp_type="posting", title="Planner")
        s.add(o)
        await s.flush()
        p = Pursuit(opportunity_id=o.id, track="employment", status="screening")
        s.add(p)
        await s.commit()
        snap = await pu.snapshot(s, p)
        p.status = "applied"
        token = await pu.save(s, snap)
        await s.commit()
        await pu.restore(s, token)
        await s.commit()
        assert (await s.get(Pursuit, p.id)).status == "screening"


# ------------------------------------------------------------------------------------- R5
def test_r5_refused_undos_carry_the_marker_the_toast_reads():
    from kestrel.db.models import Opportunity

    async def seed(s):
        o = Opportunity(track="employment", opp_type="posting", title="Planner")
        s.add(o)
        await s.flush()
        return o.id

    with _client() as c:
        oid = _db(seed, commit=True)
        marker = 'data-undo-msg data-ok="0"'
        urls = [
            f"/opportunities/{oid}/role/shortlist/undo?pursuit=999",
            f"/opportunities/{oid}/decide/unshortlist",
            f"/opportunities/{oid}/decide/undo-hide?at=2020-01-01",
            f"/pipeline/bulk-undo/shortlist?ids={oid}",
            f"/pipeline/bulk-undo/not-for-me?ids={oid}&at=2020-01-01",
            "/offers/employment/999/reopen",
        ]
        for url in urls:
            r = c.post(url)
            assert r.status_code == 200 and marker in r.text, url
            assert "This can no longer be undone." in r.text or "offer" in r.text.lower(), url


# ------------------------------------------------------------------------------------- R7
def test_r7_a_click_that_hid_nothing_offers_no_undo_and_a_stale_undo_is_refused():
    import json
    from urllib.parse import unquote

    from kestrel.db.models import Opportunity

    async def seed(s):
        org = Org(name="Larkspur Logistics", normalized_name="larkspur logistics",
                  hidden_at="2026-01-01T00:00:00+00:00")
        s.add(org)
        await s.flush()
        o = Opportunity(track="employment", opp_type="posting", title="Planner", org_id=org.id)
        s.add(o)
        await s.flush()
        return org.id, o.id

    with _client() as c:
        org_id, oid = _db(seed, commit=True)
        # Already hidden deliberately: this click changes nothing and offers no Undo.
        r = c.post(f"/opportunities/{oid}/decide/hide-company")
        toast = json.loads(r.headers["HX-Trigger"])["k-toast"]
        assert "undoUrl" not in toast and "already hidden" in toast["message"]
        assert _all(select(Org.hidden_at))[0][0] == "2026-01-01T00:00:00+00:00"
        # An Undo carrying an older stamp is refused and leaves the hide in place.
        stale = c.post(f"/opportunities/{oid}/decide/unhide-company?org={org_id}&at=1999")
        assert 'data-undo-msg data-ok="0"' in stale.text
        assert _all(select(Org.hidden_at))[0][0] is not None
        # A fresh hide offers an Undo that works once.
        c.post(f"/orgs/{org_id}/unhide")
        r = c.post(f"/opportunities/{oid}/decide/hide-company")
        undo = json.loads(r.headers["HX-Trigger"])["k-toast"]["undoUrl"]
        assert c.post(unquote(undo) if "%" not in undo else undo).status_code == 200
        assert _all(select(Org.hidden_at))[0][0] is None


# --------------------------------------------------------------------------------- R18, R19
def test_r18_undo_of_remove_puts_the_interview_questions_back():
    import json

    from kestrel.db.models import InterviewQuestionSet
    from tests.test_pursuit_board import _fresh, _pursuit, _run, _seed

    c = _fresh()
    opp_id, pid = _seed(status="applied", title="Keep my questions")

    async def add(s):
        s.add(InterviewQuestionSet(opportunity_id=opp_id, pursuit_id=pid, prompt_version="v1",
                                   questions=[{"q": "Why us?"}]))
        await s.commit()

    _run(add)
    with c:
        r = c.post(f"/pursuits/{pid}/remove", headers={"HX-Request": "true"})
        undo_url = json.loads(r.headers["HX-Trigger"])["k-toast"]["undoUrl"]
        assert _pursuit(pid)[0] is None
        assert c.post(undo_url).status_code == 200

    async def sets(s):
        return (await s.execute(select(InterviewQuestionSet.questions))).scalars().all()

    assert _run(sets) == [[{"q": "Why us?"}]]


def test_r19_a_failed_snapshot_write_leaves_the_card_on_the_board(monkeypatch):
    from kestrel.services import pursuit_undo
    from tests.test_pursuit_board import _fresh, _pursuit, _seed

    c = _fresh()
    _opp, pid = _seed(status="applied", title="Stay put")

    async def boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(pursuit_undo, "save", boom)
    with c:
        r = c.post(f"/pursuits/{pid}/remove", headers={"HX-Request": "true"})
    assert "Could not remove" in r.text
    assert _pursuit(pid)[0] is not None


# ------------------------------------------------------------------------------------- R9
def test_r9_a_summary_written_by_a_hosted_model_is_not_called_local_after_a_switch_back():
    from kestrel.web import company_view as cv

    record = {"status": "ok", "model": "vendor/big-model", "sentences": [], "tags": [],
              "sources": [], "generated_at": "2026-10-01T00:00:00+00:00"}
    view = cv.summary_view(record, live=False, online_model=None)  # Tier 2 is local again
    assert view["label"] == "Written by vendor/big-model"
    assert "this computer" not in view["label"]
    # Nothing stored yet and nothing generating: no claim about who wrote it.
    assert cv.summary_view(None, live=False, online_model=None)["state"] == "none"


# --------------------------------------------------------------------------- R10, R26
def _hosted_app_with_source():
    import asyncio

    import respx

    from kestrel.db.engine import get_sessionmaker
    from kestrel.db.models import Source
    from tests.test_hosted_mode import _fresh_app
    from tests.test_hosted_overrides import _connect

    client = _fresh_app()
    with respx.mock as router:
        _connect(client, router)

    async def seed() -> int:
        async with get_sessionmaker()() as s:
            src = Source(track="employment", kind="greenhouse", name="Board", enabled=True,
                         config={"token": "x"})
            s.add(src)
            await s.commit()
            return src.id

    return client, asyncio.run(seed())


def test_r10_run_now_says_so_when_the_usual_models_are_online(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    client, sid = _hosted_app_with_source()
    body = " ".join(client.get(f"/sources/{sid}/run-now/choose").text.split())
    assert "Your usual models are online" in body
    assert "If you score with an online model, Kestrel asks before anything is sent" not in body


def test_r26_the_training_warning_names_only_free_models():
    from kestrel.llm.providers import is_free_variant
    from kestrel.web.deps import templates

    env = templates().env
    for model, expect in (("vendor/model:free", True), ("vendor/model", False)):
        route = {"online": True, "native": True, "model": model, "provider": "OpenRouter",
                 "dest": "backend:or", "sends": "the practice answer",
                 "practice_sends": "your answer",
                 "employer": "none", "any_free": is_free_variant(model)}
        text = env.get_template("partials/pp_online.html").render(
            route=route, opp_id=1, credit=None, role="a role")
        assert ("a free model may use it for training" in text) is expect
    text = env.get_template("partials/analytics_online.html").render(
        provider="OpenRouter", model="vendor/model", sends="Sent", employer="none",
        track="employment", range="90d", hosted=True)
    assert "may use it for training" not in text
    text = env.get_template("partials/analytics_online.html").render(
        provider="OpenRouter", model="vendor/model:free", sends="Sent", employer="none",
        track="employment", range="90d", hosted=True)
    assert "a free model may use it for training" in text


# ------------------------------------------------------------------------------------- R17
def test_r17_hand_built_undo_links_are_refused_not_written_or_500():
    from kestrel.db.models import Opportunity, Setting
    from kestrel.web.pages.settings_roles import SHOWN_REASON

    async def seed(s):
        o = Opportunity(track="employment", opp_type="posting", title="Planner", stage="new",
                        stage_reason=SHOWN_REASON)
        s.add(o)
        await s.flush()
        return o.id

    with _client() as c:
        oid = _db(seed, commit=True)
        refused = [
            f"/settings/roles/show/{oid}/undo?stage=zzz",
            "/prep/undo/choice?opp=x&q=a&was=0&new=1",
            "/stories/undo?id=abc",
            "/settings/prefs/notify/undo?bogus=1&now=on",
            "/settings/undo?key=scoring.weights.employment.role_fit&prev=%22abc%22&now=absent",
            "/settings/undo?key=scoring.weights.employment.role_fit&prev=7&now=absent",
        ]
        for url in refused:
            r = c.post(url)
            assert r.status_code < 500, url
            assert "no longer be undone" in r.text or "no longer" in r.text.lower(), url
        row = _all(select(Opportunity.stage))[0][0]
        assert row == "new"  # the bad stage was never written
        assert not _all(select(Setting.key).where(Setting.key.like("scoring.weights.%")))
        bad_weights = c.post("/settings/scoring/weights/restore/undo?track=employment&rows="
                             + '{"role_fit":"abc"}')
        assert "no longer be undone" in bad_weights.text
        assert not _all(select(Setting.key).where(Setting.key.like("scoring.weights.%")))
        bad_notify = c.post("/settings/prefs/notify/undo?new_match=%7Bbad&now=on")
        assert bad_notify.status_code < 500


# ------------------------------------------------------------------------------------- R22
async def test_r22_roles_with_an_active_pursuit_are_not_counted_as_kept_out(sm):
    from kestrel.db.models import Opportunity, Pursuit
    from kestrel.services import hidden_orgs

    async with sm() as s:
        org = Org(name="Larkspur Logistics", normalized_name="larkspur logistics",
                  hidden_at="2026-01-01T00:00:00+00:00")
        s.add(org)
        await s.flush()
        ids = []
        for i in range(5):
            o = Opportunity(track="employment", opp_type="posting", title=f"Role {i}",
                            org_id=org.id)
            s.add(o)
            await s.flush()
            ids.append(o.id)
        s.add(Pursuit(opportunity_id=ids[0], track="employment", status="applied"))
        await s.commit()
        [row] = await hidden_orgs.list_hidden(s)
    assert row.role_count == 4


# ------------------------------------------------------------------------------------- R24
def test_r24_model_check_seeds_refuse_a_data_home_that_is_not_a_marked_throwaway(monkeypatch):
    from tests.model_check_seed import seed_check, seed_labels

    monkeypatch.delenv("KESTREL_DATA_HOME")
    with pytest.raises(RuntimeError, match="refuses to run"):
        seed_labels(3)
    with pytest.raises(RuntimeError, match="refuses to run"):
        seed_check()
