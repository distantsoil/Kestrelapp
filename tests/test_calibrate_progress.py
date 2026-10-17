"""The /calibrate promotion-progress panel (reranker.promotion_status + routes_learning.py). Covers
the common fresh-install case (no labels, no training run yet), the small-sample case (a prior
training run measured beats_llm/recall_ok on too few held-out items to mean anything, hard rule 6),
the actually-promoted case, and the page's defensive wrapping so a failure in the status computation
renders inline instead of a 500. No live model or external endpoint is ever called: promotion_status
itself never trains a model, and the one test that does train (`test_promotion_status_reflects_an_
actual_promotion`) fits a local HistGradientBoostingRegressor on synthetic data, same as
tests/test_learning.py already does."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from fastapi.testclient import TestClient

from kestrel import paths
from kestrel.config import get_settings
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import Label, MlModel, Opportunity, OpportunityEmbedding, Org, Source
from kestrel.learning import reranker as rr

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


async def _active_count(sm) -> int:
    from sqlalchemy import func, select

    async with sm() as s:
        return (
            await s.execute(
                select(func.count()).select_from(MlModel).where(MlModel.is_active.is_(True))
            )
        ).scalar_one()


# --------------------------------------------------------------------- promotion_status() itself.


async def test_promotion_status_no_labels(sm):
    """Fresh install: nothing labeled, nothing trained. Every held-out criterion is 'not yet
    evaluated' (None), never a guessed pass/fail, and the copy says the heuristic is still in
    charge."""
    settings = get_settings()
    async with sm() as s:
        status = await rr.promotion_status(s, "employment", settings)

    assert status.n_labeled == 0
    assert status.min_labels_for_reranker == settings.learning.min_labels_for_reranker
    by_key = {c.key: c for c in status.criteria}
    assert by_key["enough_labels"].met is False
    assert by_key["enough_labels"].current == "0 labeled"
    assert by_key["mae_ok"].met is None
    assert by_key["beats_llm"].met is None
    assert by_key["recall_ok"].met is None
    assert status.ready is False
    assert status.reranker_active is False
    assert status.last_trained_at is None
    # Asserts the PROPERTY, not the exact copy: when no reranker is active the panel must say the
    # heuristic is deciding, and must never claim the learned reranker is.
    assert "The rule-based quick screen is in charge" in status.next_step
    assert "learned reranker is currently active" not in status.next_step
    assert str(settings.learning.min_labels_for_reranker) in status.next_step


async def test_promotion_status_labels_small_samples_as_insufficient(sm):
    """A handful of usable labels (nowhere near the training floor) plus a STALE training run whose
    held-out apply/LLM counts were below their own floors. The panel must say 'insufficient (n=...,
    need >= ...)' for those, never a bare percentage (hard rule 6: a number from fewer than five
    observations is never rendered as if it were solid)."""
    settings = get_settings()
    async with sm() as s:
        org = Org(name="A", normalized_name="a")
        src = Source(track="employment", kind="greenhouse", name="s", enabled=True)
        s.add_all([org, src])
        await s.flush()
        for _ in range(4):  # usable (labeled + embedded), but far below the training floor
            opp = Opportunity(
                track="employment", opp_type="job_posting", org_id=org.id, source_id=src.id,
                title="VP of IT", stage="t2_done",
            )
            s.add(opp)
            await s.flush()
            vec = np.zeros(8, dtype=np.float32)
            s.add(
                OpportunityEmbedding(
                    opportunity_id=opp.id, model="nomic-embed-text", vector=vec.tobytes(), dims=8
                )
            )
            s.add(
                Label(
                    opportunity_id=opp.id, track="employment", score=3.0, verdict="consider",
                    source="active_learning",
                )
            )
        s.add(
            MlModel(
                kind="reranker", track="employment", version="v1", n_samples=150,
                metrics={
                    "mae": 0.4, "n_held": 40, "n_apply": 2, "n_llm": 3,
                    "recall_apply": None, "reranker_agreement_on_llm_set": None,
                    "llm_agreement": None,
                    "criteria": {
                        "enough_labels": False, "mae_ok": True, "beats_llm": False,
                        "recall_ok": False,
                    },
                },
                is_active=False,
            )
        )
        await s.commit()

    async with sm() as s:
        status = await rr.promotion_status(s, "employment", settings)

    assert status.n_labeled == 4  # the live recount, independent of the stale run's n_samples=150
    by_key = {c.key: c for c in status.criteria}
    assert by_key["recall_ok"].met is False
    assert "too few to judge (2 of 5 needed)" in by_key["recall_ok"].current
    assert by_key["beats_llm"].met is False
    assert "too few to judge (3 of 5 needed)" in by_key["beats_llm"].current
    assert by_key["mae_ok"].met is True
    assert "MAE 0.4" in by_key["mae_ok"].current and "40 held out" in by_key["mae_ok"].current
    assert status.ready is False
    assert status.reranker_active is False
    assert status.last_trained_n_labeled == 150
    # enough_labels is live and currently the blocking criterion (4 << 200), so labeling guidance
    # takes priority over "not yet promoted" detail from the stale run.
    assert "Label 196 more employment items" in status.next_step
    # Asserts the PROPERTY, not the exact copy: when no reranker is active the panel must say the
    # heuristic is deciding, and must never claim the learned reranker is.
    assert "The rule-based quick screen is in charge" in status.next_step
    assert "learned reranker is currently active" not in status.next_step


async def test_promotion_status_not_ready_lists_failing_criteria_once_labels_are_enough(sm):
    """Once the live label count clears the (lowered) floor, a REAL training run on four labels
    hands back a single held-out row. The gate must refuse that outright, stating the n, and the
    panel then surfaces the run plainly as 'not yet promoted', naming the failing criteria, rather
    than silently implying the model is closer than it is."""
    settings = get_settings()
    settings.learning.min_labels_for_reranker = 3  # lower the floor so 4 labels clears it
    async with sm() as s:
        org = Org(name="A", normalized_name="a")
        src = Source(track="employment", kind="greenhouse", name="s", enabled=True)
        s.add_all([org, src])
        await s.flush()
        for i in range(4):
            opp = Opportunity(
                track="employment", opp_type="job_posting", org_id=org.id, source_id=src.id,
                title="VP of IT", stage="t2_done",
            )
            s.add(opp)
            await s.flush()
            vec = np.zeros(8, dtype=np.float32)
            vec[i % 8] = 1.0
            s.add(
                OpportunityEmbedding(
                    opportunity_id=opp.id, model="nomic-embed-text", vector=vec.tobytes(), dims=8
                )
            )
            s.add(
                Label(
                    opportunity_id=opp.id, track="employment", score=3.0 + i * 0.3,
                    verdict="apply", source="active_learning",
                )
            )
        await s.commit()

    async with sm() as s:
        result = await rr.train_reranker(s, "employment", settings)
    # The one-row path: enough labels by the lowered setting, but the held-out set is one row.
    assert result.criteria["enough_labels"] is True
    assert result.metrics["n_held"] == 1
    assert result.criteria["mae_ok"] is False  # refused on n, not on the MAE value
    assert result.promoted is False
    assert "n_held=1" in result.reason and f"< {rr._MIN_HELD_FOR_MAE}" in result.reason
    assert "n_apply=" in result.reason  # recall is refused on its own floor too, n stated
    assert (await _active_count(sm)) == 0

    async with sm() as s:
        status = await rr.promotion_status(s, "employment", settings)

    assert status.ready is False
    assert status.reranker_active is False
    assert "was not promoted" in status.next_step
    assert "Held-out prediction error" in status.next_step  # names the failing criteria
    by_key = {c.key: c for c in status.criteria}
    mae = by_key["mae_ok"].current
    assert "too few to judge" in mae and "1 held out" in mae
    # Asserts the PROPERTY, not the exact copy: when no reranker is active the panel must say the
    # heuristic is deciding, and must never claim the learned reranker is.
    assert "The rule-based quick screen is in charge" in status.next_step
    assert "learned reranker is currently active" not in status.next_step


async def test_promotion_status_reflects_an_actual_promotion(sm):
    """Once a training run actually meets every criterion, the panel says so plainly, with every
    rate still shown beside its n."""
    from tests.learning_fakes import seed_dataset

    async with sm() as s:
        await seed_dataset(s, n=250, signal=True)
    settings = get_settings()
    async with sm() as s:
        result = await rr.train_reranker(s, "employment", settings)
    assert result.promoted is True

    async with sm() as s:
        status = await rr.promotion_status(s, "employment", settings)

    assert status.reranker_active is True
    assert status.ready is True
    by_key = {c.key: c for c in status.criteria}
    for key in ("enough_labels", "mae_ok", "beats_llm", "recall_ok"):
        assert by_key[key].met is True
    assert "%" in by_key["recall_ok"].current and "from " in by_key["recall_ok"].current
    assert "active" in status.next_step.lower()


# --------------------------------------------------------------------------- The /calibrate route.




def _wipe_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        p = paths.db_path().with_name(paths.db_path().name + suffix)
        p.unlink(missing_ok=True)


async def _seed_empty() -> None:
    """A migrated, otherwise-empty database, matching tests/test_web_api.py's pattern: a fresh
    per-test file, engine forgotten afterward so the TestClient app builds its own on its own
    loop."""
    await reset_engine()
    _wipe_db()
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    await engine.dispose()
    forget_engine()


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def test_calibrate_page_renders_insufficient_state_without_500ing():
    asyncio.run(_seed_empty())
    with _client() as c:
        r = c.get("/calibrate")
    assert r.status_code == 200
    body = r.text
    assert 'data-testid="promotion-panel"' in body
    assert "0 labeled" in body  # the sample size is stated, never a bare metric
    assert "The rule-based quick screen is in charge" in body
    assert "learned ranking is currently active" not in body


def test_calibrate_page_never_500s_when_status_computation_fails(monkeypatch):
    """The panel's own computation is defended separately from promotion_status's ordinary 'not
    ready' data path: force an unexpected exception and confirm the PAGE still renders 200 with an
    inline message, per the hard constraint that this page must never 500."""
    asyncio.run(_seed_empty())

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(rr, "promotion_status", _boom)
    with _client() as c:
        r = c.get("/calibrate")
    assert r.status_code == 200
    assert "Promotion status unavailable" in r.text


def test_mae_row_is_labeled_insufficient_below_the_display_floor():
    """The MAE row was built inline and bypassed the hard-rule-6 floor entirely, rendering
    "MAE 0.41 (n=1 held-out)" bare with a green tick, in the panel added to satisfy that rule.
    n_held is 1 whenever min_labels_for_reranker is small, and that setting is operator-settable."""
    from kestrel.learning.reranker import _HONEST_SAMPLE_FLOOR, _mae_text

    assert "too few to judge" in _mae_text(0.41, 1)
    assert "too few to judge" in _mae_text(0.41, _HONEST_SAMPLE_FLOOR - 1)
    assert "too few to judge" not in _mae_text(0.41, _HONEST_SAMPLE_FLOOR)
    assert "0.41" in _mae_text(0.41, 1)  # the number is still shown, just not as established


def test_the_panel_never_claims_the_heuristic_is_in_charge_while_a_reranker_is_active():
    """A failed retrain leaves the previously promoted model ACTIVE while becoming `latest`, so the
    panel read the failed run's criteria and still appended "heuristic scoring is in charge". That
    is a false statement about what actually decided the operator's results."""
    import inspect

    from kestrel.learning import reranker

    src = inspect.getsource(reranker.promotion_status)
    # The sentence must be derived from active_exists, never hardcoded into a branch.
    assert "in_charge" in src
    assert src.count('"The rule-based quick screen is in charge."') == 1, (
        "the in-charge sentence is hardcoded in more than one place and can drift from reality"
    )


def test_usable_label_counting_matches_build_rows():
    """An embedding row with an empty vector keeps its key in the corpus map but maps to None.
    Counting keys made the panel say "enough labels, go train" while training refused with
    "insufficient usable labels"."""
    import inspect

    from kestrel.learning import reranker

    src = inspect.getsource(reranker.promotion_status)
    assert "emb.get(ex.opportunity_id) is not None" in src, (
        "panel counts embedding KEYS, which drifts from build_rows' usable-vector check"
    )
