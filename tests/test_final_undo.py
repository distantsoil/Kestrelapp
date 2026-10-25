"""Final lane U regressions, using isolated data and real action boundaries."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from kestrel.db.models import (
    Notification,
    Opportunity,
    Pursuit,
    Setting,
    StatusEvent,
    VerdictOverride,
)
from tests.test_settings_routes import _all, _client, _db, _get, _toast

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

IMPORT = "Company,Role,Date,Status,Notes\nAcme,Analyst,2026-10-01,Applied,original\n"


def add(client, text=IMPORT):
    return client.post(
        "/settings/data/applied/add",
        data={"text": text, "include_1": "on", "include_2": "on", "file_name": "apps.csv"},
    )


@pytest.mark.parametrize("created", [False, True])
def test_import_undo_refuses_later_edits_atomically(created):
    with _client() as c:
        first = add(c)
        pid = _all(select(Pursuit.id))[0][0]
        changed = (
            first if created else add(c, IMPORT.replace("Applied,original", "Interview,import"))
        )

        async def edit(s):
            p = await s.get(Pursuit, pid)
            p.notes_md = "later note"
            if not created:
                p.status = "rejected"
                s.add(StatusEvent(pursuit_id=pid, from_status="interviewing", to_status="rejected"))

        _db(edit, commit=True)
        before = _get(Pursuit, pid)
        events = _all(select(StatusEvent.id, StatusEvent.to_status))
        out = c.post(_toast(changed)["undoUrl"]).json()
    assert not out["ok"] and out["message"] == "This can no longer be undone."
    after = _get(Pursuit, pid)
    assert after.notes_md == before.notes_md and after.status == before.status
    assert _all(select(StatusEvent.id, StatusEvent.to_status)) == events


def test_remove_undo_refuses_later_archive_without_restoring_card():
    with _client() as c:
        add(c)
        pid = _all(select(Pursuit.id))[0][0]
        oid = _get(Pursuit, pid).opportunity_id
        removed = c.post(f"/pursuits/{pid}/remove", headers={"HX-Request": "true"})

        async def hide(s):
            opp = await s.get(Opportunity, oid)
            opp.archived_at = "2026-10-25T23:00:00+00:00"
            opp.archive_reason = "not_interested"

        _db(hide, commit=True)
        result = c.post(json.loads(removed.headers["HX-Trigger"])["k-toast"]["undoUrl"])
    assert "This can no longer be undone." in result.text
    assert _get(Pursuit, pid) is None
    assert _get(Opportunity, oid).archive_reason == "not_interested"


def test_remove_undo_restores_notification_link_and_keeps_read_state():
    with _client() as c:
        add(c)
        pid = _all(select(Pursuit.id))[0][0]

        async def notify(s):
            n = Notification(pursuit_id=pid, kind="follow_up", title="Follow up")
            s.add(n)
            await s.flush()
            return n.id

        nid = _db(notify, commit=True)
        removed = c.post(f"/pursuits/{pid}/remove", headers={"HX-Request": "true"})
        assert _get(Notification, nid).pursuit_id is None

        async def read(s):
            n = await s.get(Notification, nid)
            n.read_at = "2026-10-25T12:00:00+00:00"

        _db(read, commit=True)
        c.post(_toast(removed)["undoUrl"])
    p = _all(select(Pursuit.id))[0][0]
    assert _get(Notification, nid).pursuit_id == p
    assert _get(Notification, nid).read_at == "2026-10-25T12:00:00+00:00"


def test_remove_keeps_a_label_committed_after_its_snapshot(monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel import paths
    from kestrel.db.engine import make_async_engine
    from kestrel.services import pursuit_undo
    from kestrel.web.pages.opportunity_actions import ADD_REASONS

    original = pursuit_undo.remove_snapshot
    later = []

    async def interleave(s, p, opp):
        snap = await original(s, p, opp)
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as other:
                label = VerdictOverride(
                    opportunity_id=opp.id, reason=ADD_REASONS[0], operator_verdict="pass"
                )
                other.add(label)
                await other.commit()
                later.append(label.id)
        finally:
            await engine.dispose()
        return snap

    monkeypatch.setattr(pursuit_undo, "remove_snapshot", interleave)
    with _client() as c:
        add(c)
        pid = _all(select(Pursuit.id))[0][0]

        async def prior(s):
            p = await s.get(Pursuit, pid)
            s.add(
                VerdictOverride(
                    opportunity_id=p.opportunity_id, reason=ADD_REASONS[0], operator_verdict="apply"
                )
            )

        _db(prior, commit=True)
        removed = c.post(f"/pursuits/{pid}/remove", headers={"HX-Request": "true"})
        assert _get(VerdictOverride, later[0]) is not None
        c.post(_toast(removed)["undoUrl"])
    assert _get(VerdictOverride, later[0]).operator_verdict == "pass"
    assert (
        _all(select(VerdictOverride.operator_verdict).order_by(VerdictOverride.id.desc()))[0][0]
        == "pass"
    )


def test_idle_scheduler_cleans_expired_undo_without_a_new_save(monkeypatch):
    import asyncio

    from kestrel.web import app

    with _client():

        async def seed(s):
            s.add_all(
                [
                    Setting(key="undo.pursuit.old", value={}, updated_at="2000-01-01"),
                    Setting(key="undo.applied.old", value={}, updated_at="2000-01-01"),
                    Setting(key="undo.pursuit.fresh", value={}),
                    Setting(key="ui.scheme", value="indigo", updated_at="2000-01-01"),
                ]
            )

        _db(seed, commit=True)

        async def run():
            stop = asyncio.Event()

            async def fire(*args, **kwargs):
                stop.set()

            monkeypatch.setattr("kestrel.services.run_schedule.fire_if_due", fire)
            await app._scheduler_loop(stop)

        asyncio.run(run())
    assert _get(Setting, "undo.pursuit.old") is None
    assert _get(Setting, "undo.applied.old") is None
    assert _get(Setting, "undo.pursuit.fresh") is not None
    assert _get(Setting, "ui.scheme").value == "indigo"


@pytest.mark.parametrize("surface", ["decide", "role"])
def test_shortlist_undo_preserves_later_notes(surface):
    with _client() as c:

        async def seed(s):
            o = Opportunity(track="employment", opp_type="job_posting", title="Analyst")
            s.add(o)
            await s.flush()
            return o.id

        oid = _db(seed, commit=True)
        saved = c.post(f"/opportunities/{oid}/{surface}/shortlist")
        pid = _all(select(Pursuit.id))[0][0]

        async def edit(s):
            (await s.get(Pursuit, pid)).notes_md = "Keep this later work"

        _db(edit, commit=True)
        result = c.post(_toast(saved)["undoUrl"])
    assert "This can no longer be undone." in result.text
    assert _get(Pursuit, pid).notes_md == "Keep this later work"


def test_shortlist_undo_preserves_independent_archive_fields():
    with _client() as c:

        async def seed(s):
            o = Opportunity(track="employment", opp_type="job_posting", title="Analyst")
            s.add(o)
            await s.flush()
            return o.id

        oid = _db(seed, commit=True)
        saved = c.post(f"/opportunities/{oid}/role/shortlist")

        async def archive(s):
            o = await s.get(Opportunity, oid)
            o.archived_at = "2026-10-25T12:00:00+00:00"
            o.archive_reason = "applied"
            o.stage_reason = "Imported applied history"

        _db(archive, commit=True)
        c.post(_toast(saved)["undoUrl"])
    assert not _all(select(Pursuit.id))
    o = _get(Opportunity, oid)
    assert o.archived_at == "2026-10-25T12:00:00+00:00"
    assert o.archive_reason == "applied"
    assert o.stage_reason == "Imported applied history"


def test_old_saved_role_undo_preserves_a_later_save():
    with _client() as c:
        add(c)
        oid = _all(select(Opportunity.id))[0][0]
        url = f"/opportunities/{oid}/decide/save"
        first = _toast(c.post(url))["undoUrl"]
        c.post(first)
        second = _toast(c.post(url))["undoUrl"]
        assert second
        result = c.post(first)
    assert "This can no longer be undone." in result.text
    assert _get(Setting, f"saved_role.{oid}") is not None


async def test_calibration_undo_preserves_later_weights(sm):
    from kestrel.db.models import Calibration
    from kestrel.learning import calibration

    async with sm() as s:
        rows = [
            Calibration(
                track="employment",
                n_samples=20,
                previous_weights={"fit": before},
                proposed_weights={"fit": after},
                status="proposed",
            )
            for before, after in [(0.1, 0.2), (0.2, 0.3)]
        ]
        s.add_all(rows)
        await s.commit()
        for row in rows:
            await calibration.accept_calibration(s, row.id)
        assert not await calibration.undo_calibration(s, rows[0].id)
        assert (await s.get(Setting, "scoring.weights.employment.fit")).value == 0.3


@pytest.mark.parametrize("later", ["opportunity", "offer"])
def test_import_undo_keeps_later_related_work(later):
    from kestrel.db.models import Offer

    with _client() as c:
        first = add(c)
        pid = _all(select(Pursuit.id))[0][0]
        oid = _get(Pursuit, pid).opportunity_id

        async def edit(s):
            if later == "opportunity":
                (await s.get(Opportunity, oid)).title = "Later title"
            else:
                s.add(Offer(pursuit_id=pid, base=123000))

        _db(edit, commit=True)
        result = c.post(_toast(first)["undoUrl"])
    assert "This can no longer be undone." in result.text
    assert _get(Pursuit, pid) is not None
    assert _get(Opportunity, oid) is not None


def test_role_unshortlist_undo_restores_notes_and_priority():
    with _client() as c:

        async def seed(s):
            o = Opportunity(track="employment", opp_type="job_posting", title="Analyst")
            s.add(o)
            await s.flush()
            return o.id

        oid = _db(seed, commit=True)
        c.post(f"/opportunities/{oid}/role/shortlist")
        pid = _all(select(Pursuit.id))[0][0]

        async def edit(s):
            p = await s.get(Pursuit, pid)
            p.notes_md, p.priority = "Keep my notes", 2

        _db(edit, commit=True)
        removed = c.post(f"/opportunities/{oid}/role/unshortlist")
        c.post(_toast(removed)["undoUrl"])
    restored = _all(select(Pursuit.notes_md, Pursuit.priority))
    assert restored == [("Keep my notes", 2)]


async def test_calibration_undo_restores_acceptance_time_state(sm):
    from kestrel.db.models import Calibration
    from kestrel.learning import calibration
    async with sm() as s:
        cal = Calibration(track="employment", n_samples=20, previous_weights={"role_fit": 0.1},
                          proposed_weights={"role_fit": 0.2}, status="proposed")
        s.add_all([cal, Setting(key="scoring.weights.employment.role_fit", value=0.4)])
        await s.commit()
        await calibration.accept_calibration(s, cal.id)
        assert await calibration.undo_calibration(s, cal.id)
        assert (await s.get(Setting, "scoring.weights.employment.role_fit")).value == 0.4


def test_mode_undo_refuses_later_model_mode_change():
    with _client() as c:
        async def seed(s):
            s.add_all([Setting(key="setup.run_choice", value="both"),
                       Setting(key="llm.mode", value="hosted")])
        _db(seed, commit=True)
        saved = c.post("/settings/computer/mode", data={"mode": "local"})
        async def edit(s):
            (await s.get(Setting, "llm.mode")).value = "hosted"
        _db(edit, commit=True)
        assert not c.post(_toast(saved)["undoUrl"]).json()["ok"]
    assert _get(Setting, "llm.mode").value == "hosted"


def test_visible_company_unhide_has_no_undo():
    from kestrel.db.models import Org
    with _client() as c:
        async def seed(s):
            org = Org(name="Acme", normalized_name="acme")
            s.add(org)
            await s.flush()
            return org.id
        oid = _db(seed, commit=True)
        response = c.post(f"/settings/roles/unhide/{oid}")
        assert "undoUrl" not in response.headers.get("HX-Trigger", "")


def test_duplicate_suggestion_dismiss_has_no_undo():
    with _client() as c:
        c.post("/sources/suggestions/dismiss", data={"name": "Acme"})
        second = c.post("/sources/suggestions/dismiss", data={"name": "Acme"})
        assert "undoUrl" not in second.headers.get("HX-Trigger", "")


async def test_field_undo_preserves_an_older_outcome_when_action_appended_one(sm):
    from kestrel.db.models import Outcome
    from kestrel.services import pursuit_undo
    async with sm() as s:
        opp = Opportunity(track="employment", opp_type="job_posting", title="Analyst")
        s.add(opp)
        await s.flush()
        p = Pursuit(opportunity_id=opp.id, track="employment", status="offer")
        s.add(p)
        await s.flush()
        old = Outcome(pursuit_id=p.id, opportunity_id=opp.id, final_status="withdrawn")
        s.add(old)
        await s.flush()
        snap = await pursuit_undo.snapshot(s, p)
        s.add(Outcome(pursuit_id=p.id, opportunity_id=opp.id, final_status="accepted"))
        await s.flush()
        token = await pursuit_undo.save(s, snap)
        await s.commit()
        assert (await pursuit_undo.restore(s, token))[0] == "employment"
        await s.commit()
        outcomes = (await s.execute(select(Outcome.id, Outcome.final_status))).all()
        assert outcomes == [(old.id, "withdrawn")]


@pytest.mark.parametrize("stale", [False, True])
def test_grouped_model_undo_is_atomic(stale):
    from kestrel.web.pages import models_ui
    with _client() as c:
        async def changes(s):
            tokens = {}
            for tier in ("tier2", "tier3"):
                snap = await models_ui.snapshot(s, tier, None)
                s.add(Setting(key=f"llm.tiers.{tier}.model", value="action-model"))
                tokens[tier] = await models_ui.store_snapshot(s, tier, snap)
            return tokens
        tokens = _db(changes, commit=True)
        if stale:
            async def edit(s):
                (await s.get(Setting, "llm.tiers.tier3.model")).value = "later-model"
            _db(edit, commit=True)
        result = c.post("/bench/undo", data={"step": ["rating", "deeper_read"],
                         "token_rating": tokens["tier2"], "token_deeper_read": tokens["tier3"]})
    if stale:
        assert "This can no longer be undone." in result.text
        assert _get(Setting, "llm.tiers.tier2.model").value == "action-model"
        assert _get(Setting, "llm.tiers.tier3.model").value == "later-model"
    else:
        assert _get(Setting, "llm.tiers.tier2.model") is None
        assert _get(Setting, "llm.tiers.tier3.model") is None


async def test_expired_and_wrong_endpoint_tokens_cannot_change_a_card(sm):
    from kestrel.services import pursuit_undo
    async with sm() as s:
        o = Opportunity(track="employment", opp_type="job_posting", title="Analyst")
        s.add(o)
        await s.flush()
        p = Pursuit(opportunity_id=o.id, track="employment", status="shortlisted")
        s.add(p)
        await s.flush()
        snap = await pursuit_undo.snapshot(s, p)
        p.priority = 2
        token = await pursuit_undo.save(s, snap)
        row = await s.get(Setting, pursuit_undo.PREFIX + token)
        row.updated_at = "2000-01-01"
        await s.commit()
        assert (await pursuit_undo.restore(s, token))[0] is None
        assert p.priority == 2
        token = await pursuit_undo.save(s, {"kind": "attach", "pursuit_id": p.id, "after": 3})
        assert (await pursuit_undo.restore(s, token))[0] is None
        assert await s.get(Setting, pursuit_undo.PREFIX + token) is not None


def test_role_unshortlist_refuses_a_card_with_an_offer():
    from kestrel.db.models import Offer
    with _client() as c:
        async def seed(s):
            o = Opportunity(track="employment", opp_type="job_posting", title="Analyst")
            s.add(o)
            await s.flush()
            p = Pursuit(opportunity_id=o.id, track="employment", status="shortlisted")
            s.add(p)
            await s.flush()
            offer = Offer(pursuit_id=p.id, base=100000)
            s.add(offer)
            await s.flush()
            return o.id, p.id, offer.id
        oid, pid, offer_id = _db(seed, commit=True)
        result = c.post(f"/opportunities/{oid}/role/unshortlist")
        assert "undoUrl" not in result.headers.get("HX-Trigger", "")
    assert _get(Pursuit, pid) is not None
    assert _get(Offer, offer_id).base == 100000
