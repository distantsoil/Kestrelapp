"""The quick round's routes and pages (issue 171, D-UI-272, D-UI-273, D-LEARN-2): the answers, the
done screen, Today's strip with its Not now choices and Undo, and the Calibrate tabs. Cloud-safe:
TestClient on the real app, a throwaway data directory, fictional roles, no model and no network.
The live-browser checks are in tests/test_quick_round_browser.py."""

from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest
from sqlalchemy import func, select

from kestrel.db.models import Evaluation, Label, Task
from kestrel.llm.client import LLMClient
from tests import quick_fixtures as qf
from tests import today_seed

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


@pytest.fixture
def closed_world(monkeypatch):
    """Every model call and every network request raises and is recorded."""
    seen: list[str] = []

    async def complete(self, *a, **kw):
        seen.append("complete")
        raise RuntimeError("test: a model call was made")

    async def embed(self, *a, **kw):
        seen.append("embed")
        raise RuntimeError("test: a model call was made")

    async def ahandle(self, request):
        seen.append(f"{request.method} {request.url}")
        raise httpx.ConnectError("test: the network is closed")

    monkeypatch.setattr(LLMClient, "complete", complete)
    monkeypatch.setattr(LLMClient, "embed", embed)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", ahandle)
    return seen


def _toast(r) -> dict:
    return json.loads(r.headers["hx-trigger"])["k-toast"]


def _counts() -> tuple[int, int]:
    async def go(s):
        return (
            (await s.execute(select(func.count()).select_from(Evaluation))).scalar_one(),
            (await s.execute(select(func.count()).select_from(Task))).scalar_one(),
        )

    return qf.run(go)


# ------------------------------------------------------------------------------- the round
def test_the_round_shows_ten_cards_with_only_what_the_posting_says():
    qf.seed(12)
    with qf.client() as c:
        r = c.get("/calibrate/quick")
    assert r.status_code == 200
    cards = re.findall(r'<article class="qr-card".*?</article>', r.text, re.S)
    assert len(cards) == 10
    first = cards[0]
    for needle in ("Operations Analyst", "Acme Freight", "Columbus, OH", "Hybrid",
                   "$90,000 to $110,000", "Open posting", "https://example.test/"):
        assert needle in first
    assert 'target="_blank"' in first and 'rel="noopener noreferrer"' in first
    # Kestrel's score is not in a card (the fixtures' scores are 3.4 to 4.0).
    assert not re.search(r"\d\.\d", re.sub(r"<[^>]+>", " ", first))
    assert "Would you want this one in your pipeline?" in r.text
    assert "Answers save as you go" in r.text


def test_an_unsafe_posting_link_is_left_out_of_the_card():
    ids = qf.seed(6)

    async def bad(s):
        from kestrel.db.models import Opportunity

        for oid in ids:
            (await s.get(Opportunity, oid)).url = "javascript:alert(1)"
        await s.commit()

    qf.run(bad)
    with qf.client() as c:
        body = c.get("/calibrate/quick").text
    assert "javascript:" not in body and 'data-testid="qr-open"' not in body


def test_an_empty_queue_has_its_own_words():
    qf.seed(0)
    with qf.client() as c:
        body = c.get("/calibrate/quick").text
    assert 'data-testid="qr-empty"' in body and "Nothing to sort right now." in body


def test_keep_going_never_repeats_a_role_already_shown():
    ids = qf.seed(14)
    with qf.client() as c:
        first = re.findall(r'data-opp="(\d+)"', c.get("/calibrate/quick").text)
        more = re.findall(r'data-opp="(\d+)"', c.get(
            "/calibrate/quick/cards", params={"seen": ",".join(first)}).text)
    assert len(first) == 10 and len(more) == 4
    assert not set(first) & set(more) and set(first) | set(more) == {str(i) for i in ids}


@pytest.mark.parametrize(("word", "score"), [("top", 5.0), ("yes", 4.0), ("no", 2.0)])
def test_an_answer_saves_one_quick_label_at_once(word, score):
    ids = qf.seed(6)
    with qf.client() as c:
        r = c.post("/calibrate/quick/answer", data={"opportunity_id": ids[0], "answer": word})
    assert r.status_code == 200
    body = r.json()
    (row,) = qf.labels()
    assert body == {"saved": True, "label_id": row.id, "answers": 1,
                    "offer": {"enabled": "1", "runs": "1", "at": ""}}
    assert (row.source, row.score, row.opportunity_id) == ("quick", score, ids[0])


def test_not_sure_saves_nothing_and_does_not_count_as_taking_the_offer():
    ids = qf.seed(6, finished_runs=1)
    with qf.client() as c:
        r = c.post("/calibrate/quick/answer", data={"opportunity_id": ids[0], "answer": "unsure"})
    assert r.json() == {"saved": False, "label_id": None, "answers": 0, "offer": None}
    assert qf.labels() == []
    assert qf.setting("ui.quick.next_runs.employment") is None


def test_an_answer_is_validated_and_stays_on_its_track():
    ids = qf.seed(6)
    with qf.client() as c:
        assert c.post("/calibrate/quick/answer", data={"answer": "yes"}).status_code == 400
        assert c.post("/calibrate/quick/answer",
                      data={"opportunity_id": ids[0], "answer": "maybe"}).status_code == 400
        assert c.post("/calibrate/quick/answer",
                      data={"opportunity_id": 99999, "answer": "yes"}).status_code == 404
        # The role is an employment role; the consulting tab may not rate it.
        assert c.post("/calibrate/quick/answer?track=consulting",
                      data={"opportunity_id": ids[0], "answer": "yes"}).status_code == 404
    assert qf.labels() == []


def test_a_round_never_calls_a_model_or_the_network_or_scores_anything(closed_world):
    ids = qf.seed(12, finished_runs=1)
    before = _counts()
    with qf.client() as c:
        assert c.get("/calibrate/quick").status_code == 200
        for i, word in enumerate(("yes", "no", "top", "unsure")):
            assert c.post("/calibrate/quick/answer",
                          data={"opportunity_id": ids[i], "answer": word}).status_code == 200
        assert c.get("/calibrate/quick/cards", params={"seen": str(ids[0])}).status_code == 200
        done = c.get("/calibrate/quick/done", params={"yes": 1, "no": 1, "top": 1})
        assert done.status_code == 200
        assert c.get("/calibrate").status_code == 200
    assert closed_world == []
    assert _counts() == before  # no evaluation written, no task queued


def test_undo_removes_only_a_quick_label_of_this_track():
    ids = qf.seed(6)
    with qf.client() as c:
        saved = c.post("/calibrate/quick/answer",
                       data={"opportunity_id": ids[0], "answer": "yes"}).json()["label_id"]
        manual = c.post("/api/learning/label", json={
            "opportunity_id": ids[1], "score": 4, "track": "employment", "source": "manual"
        }).json()["label_id"]
        assert c.post(f"/calibrate/quick/undo/{manual}").status_code == 404
        assert c.post("/calibrate/quick/undo/99999").status_code == 404
        r = c.post(f"/calibrate/quick/undo/{saved}")
    assert r.status_code == 200 and r.json() == {"undone": True, "answers": 1}
    assert [row.id for row in qf.labels()] == [manual]


def test_a_quick_answer_takes_the_offer_and_the_strip_waits_for_the_next_one():
    ids = qf.seed(8, finished_runs=1)
    with qf.client() as c:
        assert 'data-testid="td-qr"' in c.get("/today/quick").text
        c.post("/calibrate/quick/answer", data={"opportunity_id": ids[0], "answer": "yes"})
        assert 'data-testid="td-qr"' not in c.get("/today/quick").text
    assert qf.setting("ui.quick.next_runs.employment") == 4


def _locked_offer(monkeypatch):
    """mark_taken always meets a locked database; the retry budget runs out at once."""
    from sqlalchemy.exc import OperationalError

    from kestrel.learning import quick
    from kestrel.web import db_retry

    async def no_wait(_s: float) -> None:
        return None

    clock = iter(range(0, 10_000, 5))
    monkeypatch.setattr(db_retry, "_sleep", no_wait)
    monkeypatch.setattr(db_retry, "_monotonic", lambda: float(next(clock)))
    real = quick.mark_taken

    async def locked(*a, **kw):
        raise OperationalError("UPDATE setting", {}, Exception("database is locked"))

    monkeypatch.setattr(quick, "mark_taken", locked)
    return real


def test_an_answer_whose_offer_bookkeeping_fails_is_not_saved_so_the_client_is_right(
        monkeypatch):
    # INVARIANT: the client's view of saved or not always matches the database. A failed
    # mark_taken must not leave a committed label behind a "not saved" answer, and the retry
    # the person then makes must write exactly one label.
    from kestrel.learning import quick

    ids = qf.seed(6, finished_runs=1)
    real = _locked_offer(monkeypatch)
    with qf.client() as c:
        r = c.post("/calibrate/quick/answer", data={"opportunity_id": ids[0], "answer": "yes"})
        assert r.status_code == 503
        assert qf.labels() == []
        monkeypatch.setattr(quick, "mark_taken", real)
        again = c.post("/calibrate/quick/answer",
                       data={"opportunity_id": ids[0], "answer": "yes"})
    assert again.status_code == 200
    (row,) = qf.labels()
    assert again.json()["saved"] is True and again.json()["label_id"] == row.id
    assert qf.setting("ui.quick.next_runs.employment") == 4


def test_undoing_the_only_answer_of_a_round_puts_the_offer_back_as_it_was():
    # An Undo of the round's last remaining answer sends the offer as it was before the round
    # (the first answer's `offer`), so the strip is not left snoozed by an answer that is gone.
    ids = qf.seed(8, finished_runs=1)
    with qf.client() as c:
        saved = c.post("/calibrate/quick/answer",
                       data={"opportunity_id": ids[0], "answer": "yes"}).json()
        assert 'data-testid="td-qr"' not in c.get("/today/quick").text
        r = c.post(f"/calibrate/quick/undo/{saved['label_id']}", data=saved["offer"])
        assert r.status_code == 200 and r.json()["undone"] is True
        assert 'data-testid="td-qr"' in c.get("/today/quick").text
    assert qf.labels() == []
    assert qf.setting("ui.quick.next_runs.employment") == 1


def test_undo_with_a_bad_offer_restores_nothing_and_keeps_the_label():
    ids = qf.seed(8, finished_runs=1)
    with qf.client() as c:
        saved = c.post("/calibrate/quick/answer",
                       data={"opportunity_id": ids[0], "answer": "yes"}).json()
        r = c.post(f"/calibrate/quick/undo/{saved['label_id']}",
                   data={"enabled": "1", "runs": "x", "at": ""})
        assert r.status_code == 400
    assert len(qf.labels()) == 1
    assert qf.setting("ui.quick.next_runs.employment") == 4


# ---------------------------------------------------------------------------- the done screen
def test_the_done_screen_below_fifteen_answers_does_not_promise_re_ranking():
    qf.seed(8, n_labeled=6, embed_labeled=True)
    with qf.client() as c:
        body = c.get("/calibrate/quick/done", params={
            "yes": 2, "top": 1, "no": 5, "unsure": 1, "seen": ""}).text
    assert "Done. Kestrel now has 6 of your answers." in body
    assert ("Your answers are saved. At 15, Kestrel starts ranking new roles with them. "
            "Change any answer later in Calibrate, under Detailed.") in body
    assert "re-ranks your pipeline" not in body
    assert "<b>3</b>&nbsp;yes, <b>1</b>&nbsp;of them a top pick" in body
    assert "<b>5</b>&nbsp;not for me" in body and "<b>1</b>&nbsp;not sure" in body
    assert "9 to go" in body and "24 to go" in body and "194 to go" in body
    assert 'data-state="next"' in body and 'data-state="hit"' not in body


def test_the_done_screen_from_fifteen_answers_says_it_re_ranks_on_the_next_run():
    qf.seed(18, n_labeled=15, embed_labeled=True)
    with qf.client() as c:
        body = c.get("/calibrate/quick/done", params={"yes": 1}).text
    assert "Done. Kestrel now has 15 of your answers." in body
    assert ("Kestrel re-ranks your pipeline with these on the next run. Change any answer later "
            "in Calibrate, under Detailed.") in body
    assert 'data-state="hit"' in body and "15 answers" in body and "15 to go" in body
    assert "You approve any change before it applies." in body


def test_the_done_screen_offers_more_only_while_roles_remain():
    qf.seed(14)
    with qf.client() as c:
        seen = ",".join(re.findall(r'data-opp="(\d+)"', c.get("/calibrate/quick").text))
        some = c.get("/calibrate/quick/done", params={"seen": ""}).text
        none = c.get("/calibrate/quick/done", params={"seen": seen + ",1,2,3,4,5,6,7,8,9,10,"
                                                      "11,12,13,14,15,16"}).text
    assert "Keep going: 10 more" in some and "Back to Today" in some and "Rate in detail" in some
    assert "Keep going" not in none and "Back to Today" in none
    assert "not unsure about any other role" in none


# ---------------------------------------------------------------------------- Today's strip
def test_the_strip_words_and_the_meter_on_today():
    asyncio.run(today_seed.seed())
    with qf.client() as c:
        body = c.get("/").text
    assert 'data-testid="td-qr"' in body
    assert "7 roles to sort, yes or no. About a minute." in body
    assert ("Kestrel picked the ones from this run it was least sure about. Each answer helps it "
            "rank your pipeline.") in body
    assert "<b>0 of 15</b> answers. At 15, Kestrel ranks new roles using your examples." in body
    assert body.index('data-testid="td-strip"') < body.index('data-testid="td-qr"') < body.index(
        'data-testid="td-matches"')
    assert "Ask after the next 3 runs" in body and "Stop asking" in body
    assert "The roles wait in Calibrate." in body and "Turn it back on in Calibrate." in body


def test_the_strip_uses_the_real_number_below_ten_and_hides_below_five():
    qf.seed(7, finished_runs=1)
    with qf.client() as c:
        assert "7 roles to sort, yes or no." in c.get("/today/quick").text
    qf.seed(4, finished_runs=1)
    with qf.client() as c:
        assert 'data-testid="td-qr"' not in c.get("/today/quick").text


def test_the_strip_does_not_show_before_a_run_finished_or_while_one_is_active():
    qf.seed(8)
    with qf.client() as c:
        assert 'data-testid="td-qr"' not in c.get("/today/quick").text
    qf.add_finished_runs(1)
    with qf.client() as c:
        assert 'data-testid="td-qr"' in c.get("/today/quick").text

    async def active(s):
        from kestrel.analytics.today import latest_run  # noqa: F401  (the run view reads Run rows)
        from kestrel.db.models import Run

        s.add(Run(name="desktop", status="running", mode="local"))
        await s.commit()

    qf.run(active)
    with qf.client() as c:
        assert 'data-testid="td-qr"' not in c.get("/today/quick").text


def test_the_strip_meter_names_the_next_unlock():
    qf.seed(20, finished_runs=1, n_labeled=15, embed_labeled=True)
    with qf.client() as c:
        assert "<b>15 of 30</b> answers. At 30, Kestrel suggests a scoring tune-up." in c.get(
            "/today/quick").text


def test_not_now_snooze_hides_the_strip_with_an_undo_that_restores_it_exactly():
    qf.seed(8, finished_runs=2)
    with qf.client() as c:
        assert 'data-testid="td-qr"' in c.get("/today/quick").text
        r = c.post("/today/quick/snooze")
        assert r.status_code == 204
        toast = _toast(r)
        assert toast["message"].startswith("Okay. Kestrel will ask again after 3 more runs.")
        assert 'data-testid="td-qr"' not in c.get("/today/quick").text
        assert qf.setting("ui.quick.next_runs.employment") == 5
        assert c.post(toast["undoUrl"]).json() == {"undone": True}
        assert 'data-testid="td-qr"' in c.get("/today/quick").text
    assert qf.setting("ui.quick.next_runs.employment") == 1


def test_not_now_stop_asking_switches_the_offer_off_and_undo_switches_it_on():
    qf.seed(8, finished_runs=2)
    with qf.client() as c:
        r = c.post("/today/quick/stop")
        toast = _toast(r)
        assert toast["message"] == "Okay. Kestrel will stop asking. Turn it back on in Calibrate."
        assert qf.setting("ui.quick.enabled") is False
        qf.add_finished_runs(6)
        assert 'data-testid="td-qr"' not in c.get("/today/quick").text
        assert c.post(toast["undoUrl"]).json() == {"undone": True}
        assert qf.setting("ui.quick.enabled") is True
        assert 'data-testid="td-qr"' in c.get("/today/quick").text


def test_the_restore_route_refuses_bad_values():
    qf.seed(6, finished_runs=1)
    with qf.client() as c:
        assert c.post("/today/quick/restore", params={"enabled": "2"}).status_code == 400
        assert c.post("/today/quick/restore", params={"runs": "x"}).status_code == 400
        assert c.post("/today/quick/restore", params={"at": "not a time"}).status_code == 400


def test_the_snooze_is_kept_per_track():
    qf.seed(8, finished_runs=1)
    with qf.client() as c:
        c.post("/today/quick/snooze?track=employment")
    assert qf.setting("ui.quick.next_runs.employment") == 4
    assert qf.setting("ui.quick.next_runs.consulting") is None


# ------------------------------------------------------------------------ the Calibrate tabs
def test_calibrate_opens_on_the_quick_tab_with_its_words():
    qf.seed(10, n_labeled=6, embed_labeled=True)
    with qf.client() as c:
        body = c.get("/calibrate").text
    assert re.search(r'id="cal-tab-quick"[^>]*aria-selected="true"', body)
    assert re.search(r'id="cal-tab-detailed"[^>]*aria-selected="false"', body)
    assert 'id="cal-panel-detailed" role="tabpanel" aria-labelledby="cal-tab-detailed"' in body
    assert re.search(r'id="cal-panel-detailed"[^>]*hidden', body)
    assert "Sort 10 roles, yes or no" in body
    assert ("Kestrel shows the roles it is least sure about. You answer from the title, company, "
            "place and pay. Your answers shape what rises in your pipeline. Kestrel never changes "
            "how it scores without asking you first.") in body
    assert "<b>6 of 15</b> answers. At 15, Kestrel ranks new roles using your examples." in body
    assert "For more control, use Detailed" in body
    assert "Offer a quick round after runs" in body and "How often" in body
    for text in ("After the first run, then every 3 runs", "After every run", "Once a week"):
        assert text in body
    assert "nothing changes in scoring until you approve" not in body


def test_the_detailed_tab_is_the_existing_page_and_can_open_first():
    qf.seed(10)
    with qf.client() as c:
        body = c.get("/calibrate?tab=detailed").text
    assert re.search(r'id="cal-tab-detailed"[^>]*aria-selected="true"', body)
    assert not re.search(r'id="cal-panel-detailed"[^>]*hidden', body)
    assert re.search(r'id="cal-panel-quick"[^>]*hidden', body)
    for hook in ("cal-card", "cal-strips", "cal-history"):
        assert f'data-testid="{hook}"' in body


@pytest.mark.parametrize(
    "kind", ["calibration_ready", "reranker_ready", "reranker_refused", "reranker_drift"])
def test_old_calibrate_links_open_the_detailed_tab(kind):
    # These pointed at the rating page before the tabs existed; Detailed is that page unchanged.
    from kestrel.web.notifications_view import context_action

    assert context_action(kind, None)["href"] == "/calibrate?tab=detailed"


def test_the_learned_card_opens_the_rating_history_on_the_detailed_tab():
    qf.seed(6)
    with qf.client() as c:
        r = c.get("/settings/scoring/learned?track=employment")
    assert r.status_code == 200
    assert ('<a class="k-link" href="/calibrate?track=employment&amp;tab=detailed">'
            "See your rating history</a>") in r.text


def test_the_offer_settings_save_as_they_change():
    qf.seed(8, finished_runs=1)
    with qf.client() as c:
        r = c.post("/calibrate/quick/settings", data={"cadence": "weekly"})
        assert r.status_code == 204 and "Saved" in _toast(r)["message"]
        assert qf.setting("ui.quick.cadence") == "weekly"
        assert qf.setting("ui.quick.enabled") is False  # the switch was absent: off
        c.post("/calibrate/quick/settings", data={"cadence": "every", "enabled": "on"})
        assert qf.setting("ui.quick.cadence") == "every" and qf.setting("ui.quick.enabled") is True
        assert c.post("/calibrate/quick/settings", data={"cadence": "never"}).status_code == 400
        body = c.get("/calibrate").text
    assert re.search(r'<option value="every" selected>', body)
    assert re.search(r'role="switch"[^>]*checked', body)


def test_quick_answers_are_labeled_quick_in_the_detailed_history():
    ids = qf.seed(8)
    with qf.client() as c:
        c.post("/calibrate/quick/answer", data={"opportunity_id": ids[0], "answer": "yes"})
        body = c.get("/calibrate?tab=detailed").text
    assert f'data-testid="hist-quick-{ids[0]}"' in body
    n = len(qf.labels())
    assert n == 1


def test_quick_answers_count_in_the_calibrate_strips_and_the_nav():
    qf.seed(10, n_labeled=4)
    with qf.client() as c:
        body = c.get("/calibrate?tab=detailed").text
    assert "4/30" in body  # the tune-up ring counts the quick answers
    labels = qf.run(lambda s: s.scalar(select(func.count()).select_from(Label)))
    assert labels == 4


def test_fifteen_answers_the_few_shot_gate_cannot_use_promise_no_re_rank():
    """D-LEARN-10: the unlock follows the pool Tier 2 draws examples from (answered roles with a
    current embedding), not every answer: 15 answers with no embedding yet promise nothing."""
    qf.seed(18, n_labeled=15)
    with qf.client() as c:
        body = c.get("/calibrate/quick/done", params={"yes": 1}).text
    assert "Done. Kestrel now has 15 of your answers." in body
    assert "re-ranks your pipeline" not in body
    assert "15 to go" in body  # the few-shot unlock is still ahead


def test_with_examples_switched_off_nothing_promises_ranking_by_them():
    qf.seed(18, n_labeled=15, embed_labeled=True)

    async def off(s):
        from kestrel.db.models import Setting

        s.add(Setting(key="learning.use_examples", value=False))
        await s.commit()

    qf.run(off)
    with qf.client() as c:
        body = c.get("/calibrate/quick/done", params={"yes": 1}).text
    assert "re-ranks your pipeline" not in body
    assert "Ranks new roles using your examples" not in body


# ----------------------------------------------------------------- Confidential mode (fable C1)
def test_quick_cards_hide_the_current_employer_in_confidential_mode():
    """Invariant: with Confidential mode on, no quick round card names the current employer."""
    from kestrel.db.engine import forget_engine
    from kestrel.db.models import DocMaster, DocSection, Setting

    qf.seed(4)

    async def on(s):
        s.add(Setting(key="confidential_mode", value=True))
        m = DocMaster(track="employment", doc_kind="resume", name="Resume")
        s.add(m)
        await s.flush()
        s.add(DocSection(doc_master_id=m.id, section_kind="role", title="Experience",
                         org_label="Acme Freight", org_label_redacted="Current Employer",
                         end_date="present"))
        await s.commit()

    qf.run(on)
    forget_engine()
    with qf.client() as c:
        for path in ("/calibrate/quick", "/calibrate/quick/cards"):
            r = c.get(path)
            assert r.status_code == 200 and "qr-card" in r.text
            assert "Acme Freight" not in r.text
            assert "Current Employer" in r.text
