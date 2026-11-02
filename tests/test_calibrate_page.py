"""Calibrate (D-UI-99): the page, the answer-first reveal, rate, change, skip, undo, the tune-up
and the history, in every state of the mockup. Cloud-safe: TestClient on the real app, a throwaway
data directory, fictional roles, no model and no network. The live-browser parity check is
tests/test_calibrate_browser.py."""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import Calibration, Evaluation, Label, Opportunity, Org, Setting, Source
from kestrel.web import calibrate_view as cv

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

DIMS = ["role_fit", "domain_match", "company_context", "comp_logistics", "trajectory"]


def _dims(seed: int) -> dict:
    return {k: {"score": 1 + (seed + i) % 5, "why": f"reason {k}"} for i, k in enumerate(DIMS)}


def _run(fn):
    async def go():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                return await fn(s)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _seed(n_roles: int = 12, n_labeled: int = 0, track: str = "employment") -> list[int]:
    """n_roles evaluated roles; the first n_labeled already carry the operator's label."""

    async def prepare():
        await reset_engine()
        for suffix in ("", "-wal", "-shm"):
            paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
        migrate.run_migrations()

    asyncio.run(prepare())
    forget_engine()

    async def fill(s):
        org = Org(name="Acme Freight", normalized_name="acme freight")
        src = Source(track=track, kind="greenhouse", name="b", enabled=True)
        s.add_all([org, src])
        await s.flush()
        ids = []
        for i in range(n_roles):
            opp = Opportunity(
                track=track,
                opp_type="job_posting",
                org_id=org.id,
                source_id=src.id,
                title=f"Operations Analyst {i}",
                description_md=f"Own weekly reporting {i}.",
                stage="t2_done",
                remote_type="hybrid",
                comp_min=90000,
                comp_max=110000,
                comp_currency="USD",
                comp_interval="yearly",
                url=f"https://example.test/{i}",
                first_seen_at="2026-10-20T00:00:00.000000+00:00",
            )
            s.add(opp)
            await s.flush()
            ids.append(opp.id)
            s.add(
                Evaluation(
                    opportunity_id=opp.id,
                    tier=2,
                    track=track,
                    verdict="consider",
                    score=3.4 + (i % 4) * 0.2,
                    weighted_score=3.4,
                    dimensions=_dims(i),
                    blocks={},
                )
            )
            if i < n_labeled:
                s.add(
                    Label(
                        opportunity_id=opp.id,
                        track=track,
                        score=float(1 + i % 5),
                        verdict="consider",
                        source="active_learning",
                        created_at=f"2026-10-0{1 + i % 9}T00:00:00.000000+00:00",
                    )
                )
        await s.commit()
        return ids

    ids = _run(fill)
    forget_engine()
    return ids


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _labels() -> list[Label]:
    async def go(s):
        return list((await s.execute(select(Label).order_by(Label.id))).scalars())

    return _run(go)


def _events(r) -> dict:
    return json.loads(r.headers["hx-trigger"])


def test_sorting_model_can_be_trained_from_calibrate_without_a_command():
    _seed()
    with _client() as c:
        page = c.get("/calibrate").text
        assert 'hx-post="/calibrate/train"' in page
        r = c.post("/calibrate/train")
        assert r.status_code == 200
        assert "ratings" in r.text and "Nothing queued" in r.text


HX = {"HX-Request": "true"}


# ------------------------------------------------------------------------------- the page
def test_page_states_the_session_and_hides_kestrels_score():
    _seed()
    with _client() as c:
        body = c.get("/calibrate").text
    assert 'data-testid="cal-card"' in body and "Good match?" in body
    assert "Your call first" in body and 'data-testid="cal-veil"' in body
    for word in ("Poor", "Weak", "Maybe", "Good", "Great"):
        assert f">{word}</button>" in body
    # Kestrel's reading is not in the markup until the operator answers.
    assert 'data-testid="cal-reveal"' not in body and "Kestrel said" not in body
    assert "reason role_fit" not in body
    assert "0 of 10" in body and "10 to rate" in body
    assert "Learns your taste" in body and "0</b> of 200" in body
    assert "30 more ratings" in body
    assert "—" not in body  # no em dash in the UI copy


def test_page_keeps_the_promotion_check_behind_show_details():
    _seed()
    with _client() as c:
        body = c.get("/calibrate").text
    assert "Show details" in body
    assert 'data-testid="promotion-panel"' in body and "0 labeled" in body
    assert "The rule-based quick screen is in charge" in body


def test_reveal_shows_the_score_only_after_an_answer_and_writes_nothing():
    ids = _seed()
    with _client() as c:
        r = c.get(f"/calibrate/reveal?opportunity_id={ids[0]}&score=4")
        bad = c.get(f"/calibrate/reveal?opportunity_id={ids[0]}&score=9")
        gone = c.get("/calibrate/reveal?opportunity_id=99999&score=3")
    assert r.status_code == 200
    assert "You said <b>Good</b> (4)" in r.text and "Kestrel said <b>" in r.text
    assert 'data-testid="cal-result"' in r.text
    assert (
        "Role fit" in r.text
        and 'id="cal-why-body"' in r.text
        and 'hx-swap-oob="innerHTML"' in r.text
    )
    assert 'name="reason"' in r.text and "nudge_role_fit" in r.text
    assert bad.status_code == 400 and gone.status_code == 404
    assert _labels() == []


def test_consulting_track_uses_the_consulting_words():
    ids = _seed(track="consulting")
    with _client() as c:
        r = c.get(f"/calibrate/reveal?opportunity_id={ids[0]}&score=3&track=consulting")
    assert "Engagement fit" not in r.text  # consulting dims were not seeded under those keys
    assert cv.REASONS["consulting"][0] == "Rate" and "Rate" in r.text
    assert "Pay" not in re.findall(r'name="reason" value="([^"]+)"', r.text)


# ------------------------------------------------------------------------------- rating
def test_rate_stores_a_label_with_a_derived_verdict_and_advances_in_place():
    ids = _seed()
    with _client() as c:
        page = c.get("/calibrate").text
        since = re.search(r'data-since="([^"]+)"', page).group(1)
        r = c.post(
            "/calibrate/rate",
            headers=HX,
            data={"opportunity_id": ids[0], "score": "5", "since": since, "skip": ""},
        )
    assert r.status_code == 200
    (lab,) = _labels()
    assert (lab.opportunity_id, lab.score, lab.source) == (ids[0], 5.0, "active_learning")
    assert lab.verdict == "apply"  # 5.0 is at or above the operator's Strong match cutoff
    assert lab.rationale is None and lab.dimensions is None
    assert 'id="cal-card"' in r.text and "1 of 10" in r.text  # strips refreshed out of band
    assert 'hx-swap-oob="true"' in r.text and 'id="cal-count"' in r.text
    assert 'id="nav-count-calibrate"' in r.text
    ev = _events(r)["k-toast"]
    assert ev["message"].startswith("Saved") and ev["undoUrl"] == f"/calibrate/undo/{lab.id}"
    assert ev["refresh"].startswith("/calibrate/card?since=")


@pytest.mark.parametrize(
    ("score", "verdict"), [(1, "pass"), (2, "pass"), (3, "consider"), (4, "apply"), (5, "apply")]
)
def test_the_verdict_follows_the_operators_cutoffs(score, verdict):
    ids = _seed()
    with _client() as c:
        c.post(
            "/calibrate/rate",
            data={"opportunity_id": ids[0], "score": str(score), "since": "2026-01-01"},
        )
    assert _labels()[0].verdict == verdict


def test_reasons_and_nudges_are_saved_and_unknown_reasons_dropped():
    ids = _seed()
    with _client() as c:
        c.post(
            "/calibrate/rate",
            data={
                "opportunity_id": ids[0],
                "score": "2",
                "since": "2026-01-01",
                "reason": ["Pay", "Hacked"],
                "nudge_comp_logistics": "-1",
                "nudge_role_fit": "1",
            },
        )
    (lab,) = _labels()
    assert lab.rationale == "Reasons: Pay."
    base = _dims(0)
    assert lab.dimensions["comp_logistics"]["score"] == max(1, base["comp_logistics"]["score"] - 1)
    assert lab.dimensions["role_fit"]["score"] == min(5, base["role_fit"]["score"] + 1)
    assert lab.dimensions["domain_match"] == base["domain_match"]


def test_rate_validates_its_input():
    ids = _seed()
    with _client() as c:
        assert (
            c.post("/calibrate/rate", data={"opportunity_id": ids[0], "score": "6"}).status_code
            == 400
        )
        assert c.post("/calibrate/rate", data={"opportunity_id": ids[0]}).status_code == 400
        assert (
            c.post("/calibrate/rate", data={"opportunity_id": 99999, "score": "3"}).status_code
            == 404
        )
    assert _labels() == []


def test_a_saved_rating_can_be_undone_and_the_role_returns_to_the_queue():
    ids = _seed()
    with _client() as c:
        r = c.post(
            "/calibrate/rate", data={"opportunity_id": ids[0], "score": "3", "since": "2026-01-01"}
        )
        lid = _labels()[0].id
        assert c.post(f"/calibrate/undo/{lid}").status_code == 200
        assert c.post(f"/calibrate/undo/{lid}").status_code == 404
        q = c.get("/api/learning/queue?track=employment").json()
    assert _labels() == []
    assert ids[0] in {i["opportunity_id"] for i in q["items"]}
    assert r.status_code == 200


def test_skip_moves_the_role_to_the_end_and_stores_nothing():
    ids = _seed(n_roles=3)
    with _client() as c:
        first = c.get("/calibrate/card?since=2026-01-01").text
        shown = re.search(r'data-testid="calitem-(\d+)"', first).group(1)
        r = c.post(
            "/calibrate/skip",
            headers=HX,
            data={"opportunity_id": shown, "since": "2026-01-01", "skip": ""},
        )
        nxt = re.search(r'data-testid="calitem-(\d+)"', r.text).group(1)
        back = c.post("/calibrate/skip/undo")
    assert nxt != shown and int(shown) in ids
    assert f'data-skip="{shown}"' in r.text
    assert _events(r)["k-toast"]["message"].startswith("Skipped")
    assert _events(r)["k-toast"]["refresh"].endswith("&skip=&all=1")
    assert back.status_code == 200 and _labels() == []


# ------------------------------------------------------------------------------- change
def test_change_loads_the_role_into_the_card_and_saves_as_manual():
    ids = _seed(n_roles=3, n_labeled=1)  # the first role is rated (score 1, Poor)
    with _client() as c:
        r = c.get(f"/calibrate/card?since=2026-01-01&change={ids[0]}")
        assert 'data-change="1"' in r.text and "Changing an earlier rating" in r.text
        assert "You said <b>Poor</b>" in r.text and 'data-testid="cal-reveal"' in r.text
        assert 'data-testid="cal-skip"' not in r.text  # no Skip on a change
        assert 'id="cal-top"' not in r.text and 'id="cal-history"' not in r.text  # card alone
        r2 = c.post(
            "/calibrate/rate",
            data={"opportunity_id": ids[0], "score": "4", "since": "2026-01-01", "change": "1"},
        )
        hist = c.get("/calibrate/history").text
    assert [lb.source for lb in _labels()] == ["active_learning", "manual"]
    assert _events(r2)["k-toast"]["message"].startswith("Rating changed")
    assert "Changed" in hist and "Good" in hist


def test_change_of_an_unrated_role_falls_back_to_the_queue():
    ids = _seed(n_roles=2)
    with _client() as c:
        r = c.get(f"/calibrate/card?since=2026-01-01&change={ids[0]}")
    assert r.status_code == 200 and "Changing an earlier rating" not in r.text


# ------------------------------------------------------------------------------- strips
def test_few_labels_say_not_enough_yet_in_the_history_header():
    _seed(n_roles=6, n_labeled=3)
    with _client() as c:
        body = c.get("/calibrate").text
    assert "Not enough data yet." in body
    assert "(n=" not in body
    assert "You and Kestrel were close on" not in body
    assert "27 more ratings" in body and "3/30" in body


def test_history_header_states_the_count_from_five_ratings():
    _seed(n_roles=8, n_labeled=6)
    with _client() as c:
        body = c.get("/calibrate").text
    assert re.search(r"You and Kestrel were close on \d+ of 6", body)
    assert "Not enough data yet" not in body


def test_history_pages_by_eight_and_pages_keep_their_state():
    _seed(n_roles=20, n_labeled=20)
    with _client() as c:
        p0 = c.get("/calibrate/history").text
        p2 = c.get("/calibrate/history?page=2").text
        p9 = c.get("/calibrate/history?page=9").text
    assert p0.count('data-testid="hist-') == 8 and "1 to 8 of 20" in p0
    assert p2.count('data-testid="hist-') == 4 and "17 to 20 of 20" in p2
    assert "17 to 20 of 20" in p9  # a page past the end clamps


def test_everything_is_done_summary_with_and_without_more_roles():
    _seed(n_roles=2)
    with _client() as c:
        since = "2026-01-01T00:00:00.000000+00:00"
        first = re.search(r"calitem-(\d+)", c.get(f"/calibrate/card?since={since}").text).group(1)
        c.post("/calibrate/rate", data={"opportunity_id": first, "score": "3", "since": since})
        second = re.search(r"calitem-(\d+)", c.get(f"/calibrate/card?since={since}").text).group(1)
        r = c.post("/calibrate/rate", data={"opportunity_id": second, "score": "3", "since": since})
    assert 'data-testid="cal-done"' in r.text and "All 2 rated." in r.text
    assert "Not enough ratings in this session to compare yet. 2 so far." in r.text
    assert (
        "Kestrel is not unsure about anything right now. It finds more after the next run."
        in r.text
    )
    assert 'data-testid="cal-keep-going"' not in r.text
    assert "Change an earlier rating" in r.text and "Back to Pipeline" in r.text


def test_keep_going_shows_while_unrated_roles_remain():
    _seed(n_roles=13)
    with _client() as c:
        since = "2026-01-01T00:00:00.000000+00:00"
        for _ in range(10):
            cur = re.search(r"calitem-(\d+)", c.get(f"/calibrate/card?since={since}").text).group(1)
            r = c.post(
                "/calibrate/rate", data={"opportunity_id": cur, "score": "3", "since": since}
            )
    assert "All 10 rated." in r.text
    assert "Kestrel has more roles it is unsure about." in r.text
    assert 'data-testid="cal-keep-going"' in r.text


def test_empty_queue_has_its_own_words():
    _seed(n_roles=0)
    with _client() as c:
        body = c.get("/calibrate").text
    assert "Nothing to rate right now." in body and "All clear" in body
    assert "It finds more after the next run." in body


# ------------------------------------------------------------------------------- tune-up
def test_tune_up_appears_at_the_threshold_and_is_never_applied_silently():
    _seed(n_roles=34, n_labeled=30)
    with _client() as c:
        body = c.get("/calibrate").text
        assert 'data-testid="cal-tune"' in body and "Weight tune-up ready" in body
        assert "Use the new weights" in body and "Keep current" in body
        assert "Ready to review" in body
        again = c.get("/calibrate").text  # a second view must not pile up proposals
    assert "Weight tune-up ready" in again

    async def rows(s):
        cals = list((await s.execute(select(Calibration))).scalars())
        sets = list(
            (
                await s.execute(select(Setting).where(Setting.key.like("scoring.weights.%")))
            ).scalars()
        )
        return cals, sets

    cals, sets = _run(rows)
    assert len(cals) == 1 and cals[0].status == "proposed" and cals[0].n_samples == 30
    assert sets == []  # nothing applied


def test_use_the_new_weights_applies_them_and_keep_current_does_not():
    _seed(n_roles=34, n_labeled=30)
    with _client() as c:
        c.get("/calibrate")

        async def cal_id(s):
            return (await s.execute(select(Calibration.id))).scalar_one()

        cid = _run(cal_id)
        r = c.post(f"/calibrate/tune/{cid}/accept", data={"since": "2026-01-01"})
        gone = c.post(f"/calibrate/tune/{cid}/accept", data={"since": "2026-01-01"})
        bad = c.post(f"/calibrate/tune/{cid}/maybe")
    assert r.status_code == 200 and "Weight tune-up ready" not in r.text
    assert "Done. Checks again every 30" in r.text
    assert _events(r)["k-toast"]["message"].startswith("New weights in use")
    assert gone.status_code == 404 and bad.status_code == 400

    async def applied(s):
        return list(
            (
                await s.execute(select(Setting).where(Setting.key.like("scoring.weights.%")))
            ).scalars()
        )

    assert len(_run(applied)) == 5


def test_keep_current_rejects_the_proposal_and_changes_no_weight():
    _seed(n_roles=34, n_labeled=30)
    with _client() as c:
        c.get("/calibrate")

        async def cal_id(s):
            return (await s.execute(select(Calibration.id))).scalar_one()

        r = c.post(f"/calibrate/tune/{_run(cal_id)}/reject", data={"since": "2026-01-01"})

    async def state(s):
        cal = (await s.execute(select(Calibration))).scalar_one()
        sets = list(
            (
                await s.execute(select(Setting).where(Setting.key.like("scoring.weights.%")))
            ).scalars()
        )
        return cal.status, sets

    assert _run(state) == ("rejected", [])
    assert _events(r)["k-toast"]["message"].startswith("Kept your current weights")


def test_no_tune_up_below_the_threshold():
    _seed(n_roles=34, n_labeled=29)
    with _client() as c:
        body = c.get("/calibrate").text
    assert "Weight tune-up ready" not in body and "1 more rating" in body


# ------------------------------------------------------------------------------- the words
def test_why_asked_names_the_largest_reason_without_numbers():
    from kestrel.learning.active import UncertainItem

    def item(**c):
        base = {"proximity": 0.0, "divergence": 0.0, "novelty": 0.5, "recency": 0.0}
        return UncertainItem(None, None, 0.0, {**base, **c})  # type: ignore[arg-type]

    assert "line between keep and skip" in cv.why_asked(item(proximity=0.9))
    assert "disagree" in cv.why_asked(item(divergence=1.0))
    assert "unlike anything" in cv.why_asked(item(novelty=0.95))
    assert "posted recently" in cv.why_asked(item(recency=1.0))
    # neutral novelty (nothing rated yet) is never reported as "unlike anything you rated"
    assert "unlike anything" not in cv.why_asked(item(novelty=0.5, recency=0.2))
    assert not re.search(r"\d", cv.why_asked(item(proximity=0.9)))


def test_word_and_closeness_rules():
    assert cv.word_for(1) == "Poor" and cv.word_for(4.1) == "Good" and cv.word_for(5) == "Great"
    assert cv.word_for(None) == "Not rated"
    assert cv.is_close(3.9, 4) and not cv.is_close(3.4, 4) and not cv.is_close(None, 4)
    assert cv.FLOOR == 5


def test_few_shot_threshold_is_the_configured_one():
    from kestrel.config import get_settings

    assert get_settings().learning.min_labels_for_few_shot == 15
    _seed()
    with _client() as c:
        body = c.get("/calibrate").text
    # The side card is drawn with bold lead-ins (calibrate-mockup-locked sideCard): Right away,
    # At 30, At 200; the configured example threshold rides in the first line.
    assert "<b>Right away</b>" in body and "From 15, Kestrel shows the AI a few of them" in body
    assert "<b>At 30</b>" in body and "<b>At 200</b>" in body


def test_change_of_a_role_without_an_evaluation_keeps_the_queue():
    ids = _seed(n_roles=3, n_labeled=1)

    async def drop(s):
        for ev in (
            await s.execute(select(Evaluation).where(Evaluation.opportunity_id == ids[0]))
        ).scalars():
            await s.delete(ev)
        await s.commit()

    _run(drop)
    with _client() as c:
        r = c.get(f"/calibrate/card?since=2026-01-01&change={ids[0]}")
    assert 'data-testid="calitem-' in r.text and "Changing an earlier rating" not in r.text


def test_unsafe_posting_links_and_tampered_ids_are_handled():
    ids = _seed(n_roles=2)

    async def poison(s):
        (await s.get(Opportunity, ids[0])).url = "javascript:alert(1)"
        (await s.get(Opportunity, ids[1])).url = "javascript:alert(1)"
        await s.commit()

    _run(poison)
    with _client() as c:
        body = c.get("/calibrate/card?since=2026-01-01&skip=%C2%B2,x").text
        blank = c.get("/calibrate/card?since=")
    assert "javascript:" not in body and 'data-testid="cal-original"' not in body
    assert blank.status_code == 200


# ------------------------------------------------------------------------ review fixes
def test_reveal_refuses_a_role_from_the_other_track():
    ids = _seed(track="consulting")
    with _client() as c:
        wrong = c.get(f"/calibrate/reveal?opportunity_id={ids[0]}&score=4")
        right = c.get(f"/calibrate/reveal?opportunity_id={ids[0]}&score=4&track=consulting")
    assert wrong.status_code == 404 and "Kestrel said" not in wrong.text
    assert right.status_code == 200 and "Kestrel said" in right.text


def test_rate_refuses_a_role_from_the_other_track():
    ids = _seed(track="consulting")
    with _client() as c:
        r = c.post(
            "/calibrate/rate",
            headers=HX,
            data={"opportunity_id": ids[0], "score": "4", "since": "2026-01-01", "skip": ""},
        )
    assert r.status_code == 404
    assert _labels() == []


def _tune_ready():
    _seed(n_roles=34, n_labeled=30)

    async def cal_id(s):
        return (await s.execute(select(Calibration.id))).scalar_one()

    return cal_id


def test_tune_up_toasts_offer_undo_for_use_and_keep():
    cal_id = _tune_ready()
    with _client() as c:
        c.get("/calibrate")
        cid = _run(cal_id)
        acc = c.post(f"/calibrate/tune/{cid}/accept", data={"since": "2026-01-01"})
    toast = _events(acc)["k-toast"]
    assert toast["undoUrl"] == f"/calibrate/tune-undo/{cid}" and "refresh" in toast


def test_undo_of_use_new_weights_restores_the_proposal_and_the_old_weights():
    cal_id = _tune_ready()
    with _client() as c:
        c.get("/calibrate")
        cid = _run(cal_id)
        c.post(f"/calibrate/tune/{cid}/accept", data={"since": "2026-01-01"})
        undone = c.post(f"/calibrate/tune-undo/{cid}")
        again = c.post(f"/calibrate/tune-undo/{cid}")

    async def state(s):
        cal = await s.get(Calibration, cid)
        sets = {
            r.key: r.value
            for r in (
                await s.execute(select(Setting).where(Setting.key.like("scoring.weights.%")))
            ).scalars()
        }
        return cal.status, cal.previous_weights, sets

    status, prev, sets = _run(state)
    assert undone.status_code == 200 and again.status_code == 404
    assert status == "proposed"
    assert sets == {}  # no overrides existed when the proposal was accepted
    from kestrel.config import EmploymentWeights
    assert EmploymentWeights().model_dump() == prev


def test_undo_of_keep_current_makes_the_proposal_wait_again():
    cal_id = _tune_ready()
    with _client() as c:
        c.get("/calibrate")
        cid = _run(cal_id)
        rej = c.post(f"/calibrate/tune/{cid}/reject", data={"since": "2026-01-01"})
        undone = c.post(f"/calibrate/tune-undo/{cid}")
    assert _events(rej)["k-toast"]["undoUrl"] == f"/calibrate/tune-undo/{cid}"
    assert undone.status_code == 200

    async def status(s):
        return (await s.get(Calibration, cid)).status

    assert _run(status) == "proposed"


def test_tune_undo_is_refused_across_tracks():
    cal_id = _tune_ready()
    with _client() as c:
        c.get("/calibrate")
        cid = _run(cal_id)
        c.post(f"/calibrate/tune/{cid}/reject", data={"since": "2026-01-01"})
        r = c.post(f"/calibrate/tune-undo/{cid}?track=consulting")
    assert r.status_code == 404


def test_no_shipped_copy_states_a_count_as_n_equals():
    """#96: the count is said in words ("2 of 2", "2 so far"), never as statistics shorthand."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "kestrel" / "web"
    hits = [
        str(p.relative_to(root))
        for p in [*root.glob("templates/**/*.html"), root / "calibrate_view.py",
                  *(f for f in (root / "static" / "js").glob("*.js") if ".min." not in f.name)]
        if re.search(r"\(n=|insufficient \(n", p.read_text(encoding="utf-8"))
    ]
    assert hits == []


def test_a_rating_that_hits_a_lock_is_retried_and_saved(monkeypatch):
    """D-DB-1 / #89: the first attempt finds the file locked; the save still lands, once."""
    from sqlalchemy.exc import OperationalError

    from kestrel.web import db_retry
    from kestrel.web import routes_learning as rl

    async def no_wait(_s: float) -> None:
        return None

    monkeypatch.setattr(db_retry, "_sleep", no_wait)
    real, calls = rl.record_label, []

    async def flaky(s, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("INSERT", {}, Exception("database is locked"))
        return await real(s, **kw)

    monkeypatch.setattr(rl, "record_label", flaky)
    ids = _seed()
    with _client() as c:
        r = c.post(
            "/calibrate/rate",
            headers=HX,
            data={"opportunity_id": ids[0], "score": "4", "since": "2026-01-01", "skip": ""},
        )
    assert r.status_code == 200 and len(calls) == 2
    assert len(_labels()) == 1
