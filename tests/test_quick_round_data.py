"""The quick round's data and offer logic (issue 171, D-UI-272, D-LEARN-2): what an answer
stores, what counts toward the thresholds, and when the Today strip may ask. Cloud-safe."""

from __future__ import annotations

import pytest

from kestrel.config import get_settings
from kestrel.learning import quick
from kestrel.learning.active import askable_group_count_stmt
from kestrel.learning.capture import labeled_examples
from kestrel.learning.ledger import delete_label
from tests import quick_fixtures as qf

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


@pytest.mark.parametrize(
    ("answer", "score", "verdict"),
    [("top", 5.0, "apply"), ("yes", 4.0, "apply"), ("no", 2.0, "pass")],
)
def test_an_answer_is_one_quick_label_with_the_mapped_score(answer, score, verdict):
    ids = qf.seed(6)

    async def go(s):
        return await quick.save_answer(
            s, opportunity_id=ids[0], track="employment", answer=answer, settings=get_settings())

    row = qf.run(go)
    (saved,) = qf.labels()
    assert saved.id == row.id
    assert (saved.source, saved.score) == ("quick", score)
    assert saved.opportunity_id == ids[0] and saved.track == "employment"
    assert saved.verdict in {verdict, "pursue"}  # the top verdict word is the track's own


def test_not_sure_writes_nothing():
    ids = qf.seed(6)

    async def go(s):
        return await quick.save_answer(
            s, opportunity_id=ids[0], track="employment", answer="unsure", settings=get_settings())

    assert qf.run(go) is None
    assert qf.labels() == []


def test_undo_deletes_the_label_and_the_role_returns_to_the_queue():
    ids = qf.seed(6)

    async def save(s):
        return await quick.save_answer(
            s, opportunity_id=ids[0], track="employment", answer="yes", settings=get_settings())

    row = qf.run(save)

    async def queue(s):
        return [u.opportunity.id for u in await quick.round_items(
            s, "employment", get_settings(), seen=[])]

    assert ids[0] not in qf.run(queue)
    assert qf.run(lambda s: delete_label(s, row.id)) is True
    assert qf.labels() == []
    assert ids[0] in qf.run(queue)


def test_quick_labels_count_toward_the_thresholds_and_the_nav_count():
    ids = qf.seed(8, n_labeled=3)
    assert qf.run(lambda s: quick.answer_count(s, "employment")) == 3
    assert [e.operator_score for e in qf.run(lambda s: labeled_examples(s, "employment"))] == [
        4.0, 4.0, 4.0]
    left = qf.run(lambda s: s.scalar(askable_group_count_stmt("employment")))
    assert left == 8 - 3  # the nav badge no longer counts the answered roles
    assert ids  # fixtures return every role id


def test_the_round_never_offers_an_answered_or_already_shown_role():
    ids = qf.seed(12, n_labeled=2)

    async def queue(s, seen):
        return [u.opportunity.id for u in await quick.round_items(
            s, "employment", get_settings(), seen=seen)]

    first = qf.run(lambda s: queue(s, []))
    assert len(first) == quick.ROUND_SIZE and not set(first) & set(ids[:2])
    second = qf.run(lambda s: queue(s, first))
    assert not set(second) & (set(first) | set(ids[:2]))


def test_the_meter_names_the_next_unlock_with_real_counts():
    s = get_settings()
    assert quick.meter_for(6, s).text == (
        "6 of 15 answers. At 15, Kestrel ranks new roles using your examples.")
    assert quick.meter_for(15, s).text.startswith("15 of 30 answers. At 30, Kestrel suggests")
    assert quick.meter_for(31, s).text.startswith("31 of 200 answers. At 200, Kestrel learns")
    assert quick.meter_for(200, s).target is None


# ---------------------------------------------------------------------------------- the offer
def _due() -> bool:
    return qf.run(lambda s: quick.is_due(s, "employment"))


def test_no_offer_before_the_first_finished_run():
    qf.seed(8)
    assert not _due()
    qf.add_finished_runs(1)
    assert _due()


def test_every_third_finished_run_after_a_round_with_the_default_cadence():
    qf.seed(8, finished_runs=1)
    qf.run(lambda s: quick.mark_taken(s, "employment"))
    assert not _due()
    qf.add_finished_runs(2)
    assert not _due()  # runs 2 and 3
    qf.add_finished_runs(1)
    assert _due()  # run 4: the next offer


def test_after_every_run_and_once_a_week_cadences():
    qf.seed(8, finished_runs=1)
    assert qf.run(lambda s: quick.set_cadence(s, "every"))
    qf.run(lambda s: quick.mark_taken(s, "employment"))
    assert not _due()
    qf.add_finished_runs(1)
    assert _due()
    assert qf.run(lambda s: quick.set_cadence(s, "weekly"))
    state = qf.run(lambda s: quick.mark_taken(s, "employment"))
    assert state.next_at and not _due()  # a week has to pass, however many runs finish
    qf.add_finished_runs(3)
    assert not _due()
    assert not qf.run(lambda s: quick.set_cadence(s, "nope"))


def test_changing_how_often_re_anchors_the_pending_wait_to_the_last_round():
    """D-LEARN-10: a week's wait from a round taken under "Once a week" does not outlive a switch
    to "After every run", and a switch back applies the week again from that same round."""
    qf.seed(8, finished_runs=1)
    assert qf.run(lambda s: quick.set_cadence(s, "weekly"))
    qf.run(lambda s: quick.mark_taken(s, "employment"))
    qf.add_finished_runs(1)
    assert not _due()  # a week has not passed
    assert qf.run(lambda s: quick.set_cadence(s, "every"))
    assert _due()  # one run finished since the round: "After every run" offers it now
    assert qf.run(lambda s: quick.set_cadence(s, "weekly"))
    assert not _due()
    assert qf.run(lambda s: quick.set_cadence(s, "every3"))
    assert not _due()  # one of three runs
    qf.add_finished_runs(2)
    assert _due()


def test_snooze_waits_three_runs_and_undo_restores_the_strip_exactly():
    qf.seed(8, finished_runs=1)
    before = qf.run(lambda s: quick.read_state(s, "employment"))
    qf.run(lambda s: quick.snooze(s, "employment"))
    assert not _due()
    qf.add_finished_runs(2)
    assert not _due()
    qf.add_finished_runs(1)
    assert _due()
    qf.run(lambda s: quick.snooze(s, "employment"))
    assert not _due()
    qf.run(lambda s: quick.write_state(s, "employment", before))
    assert qf.run(lambda s: quick.read_state(s, "employment")) == before and _due()


def test_stop_asking_hides_it_and_the_toggle_brings_it_back_at_the_next_run():
    qf.seed(8, finished_runs=2)
    qf.run(lambda s: quick.stop_asking(s, "employment"))
    assert not _due()
    qf.add_finished_runs(5)
    assert not _due()
    qf.run(lambda s: quick.set_enabled(s, "employment", True))
    assert not _due()  # not retroactive
    qf.add_finished_runs(1)
    assert _due()


def test_the_offer_state_is_kept_per_track_in_settings_rows():
    qf.seed(8, finished_runs=1)
    qf.run(lambda s: quick.snooze(s, "employment"))
    assert qf.setting("ui.quick.next_runs.employment") == 4
    assert _due() is False
    assert qf.run(lambda s: quick.is_due(s, "consulting")) is True  # its own state


def test_turning_the_offer_back_on_waits_for_the_next_run_on_every_track():
    # The toggle is global (D-UI-278), so turning it back on from one track must not show the
    # other track's strip at once: both wait for the next finished run.
    qf.seed(8, finished_runs=2)
    qf.run(lambda s: quick.stop_asking(s, "employment"))
    qf.add_finished_runs(3)
    qf.run(lambda s: quick.set_enabled(s, "employment", True))
    assert not _due()
    assert qf.run(lambda s: quick.is_due(s, "consulting")) is False
    qf.add_finished_runs(1)
    assert _due() and qf.run(lambda s: quick.is_due(s, "consulting")) is True


def test_only_runs_that_finished_normally_count():
    from kestrel.db.models import Run

    qf.seed(8)

    async def add(s):
        for status in ("failed", "interrupted", "cancelled"):
            s.add(Run(name="desktop", status=status, mode="local", finished_at=qf.FINISHED))
        await s.commit()

    qf.run(add)
    assert qf.run(quick.finished_runs) == 0 and not _due()
    qf.add_finished_runs(1)
    assert qf.run(quick.finished_runs) == 1 and _due()
