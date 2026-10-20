"""GATE (Phase 7): a synthetic 250-item label set exercises the full learning loop — calibration
proposes sane weights; the reranker's four promotion criteria evaluate correctly in a PASSING and a
FAILING case; a saved model reloads through the persisted PCA and scores identically; a
version-mismatched artifact is refused; active learning ranks a known-uncertain item first; drift
detection demotes on injected degradation and writes a notification; and outcome analysis reports
non-discrimination when the score does not predict response."""

from __future__ import annotations

import numpy as np
from sqlalchemy import func, select

from kestrel.config import get_settings
from kestrel.db.models import (
    Evaluation,
    MlModel,
    Notification,
    Opportunity,
    OpportunityEmbedding,
    Org,
    Outcome,
    Pursuit,
    Source,
)
from kestrel.learning import calibration as calib
from kestrel.learning import outcomes as outcomes_mod
from kestrel.learning import reranker as rr
from kestrel.learning.active import rank_uncertain
from tests.learning_fakes import seed_dataset, seed_high_quality_labels_with_low_scores


async def test_calibration_proposes_sane_weights(sm):
    async with sm() as s:
        await seed_dataset(s, n=250, signal=True)
    settings = get_settings()
    async with sm() as s:
        cal = await calib.propose_calibration(s, "employment", settings)
    assert cal is not None
    w = cal.proposed_weights
    assert abs(sum(w.values()) - 1.0) < 1e-3  # sum-to-one
    assert all(v >= 0 for v in w.values())  # non-negativity
    assert w["role_fit"] == max(w.values())  # recovers the role_fit-heavy ground truth
    assert cal.cv_mae_proposed < cal.cv_mae_previous  # proposed beats the default weights
    assert cal.status == "proposed"  # never applied silently


async def test_reranker_promotes_when_criteria_met(sm):
    async with sm() as s:
        await seed_dataset(s, n=250, signal=True)
    settings = get_settings()
    async with sm() as s:
        result = await rr.train_reranker(s, "employment", settings)
    assert result.n_labeled == 250
    assert result.criteria["enough_labels"] and result.criteria["mae_ok"]
    assert result.criteria["beats_llm"] and result.criteria["recall_ok"]
    assert result.promoted is True
    assert result.metrics["mae"] < settings.learning.reranker_max_mae


async def test_reranker_does_not_promote_without_signal(sm):
    async with sm() as s:
        await seed_dataset(s, n=250, signal=False, random_target=True)
    settings = get_settings()
    async with sm() as s:
        result = await rr.train_reranker(s, "employment", settings)
    assert result.promoted is False
    assert result.criteria["mae_ok"] is False  # an unlearnable target fails the MAE bar


async def test_saved_model_reloads_through_pca_and_scores_identically(sm):
    async with sm() as s:
        await seed_dataset(s, n=250, signal=True)
    settings = get_settings()
    async with sm() as s:
        result = await rr.train_reranker(s, "employment", settings)
        assert result.promoted
        loaded_a = await rr.load_active_reranker(s, "employment")
        assert loaded_a is not None and loaded_a.fitted.pca is not None  # PCA was persisted
        opps = (await s.execute(select(Opportunity).limit(20))).scalars().all()
        emb = {
            e.opportunity_id: np.frombuffer(e.vector, dtype=np.float32)
            for e in (await s.execute(select(OpportunityEmbedding))).scalars().all()
        }
        orgs = {o.id: o for o in (await s.execute(select(Org))).scalars().all()}
        src = {x.id: x.kind for x in (await s.execute(select(Source))).scalars().all()}

        def preds(loaded):
            out = []
            for o in opps:
                o._source_kind = src.get(o.source_id)
                out.append(rr.predict_score(loaded, o, orgs.get(o.org_id), emb.get(o.id)))
            return out

        first = preds(loaded_a)
        loaded_b = await rr.load_active_reranker(s, "employment")  # reload from disk again
        second = preds(loaded_b)
    assert first == second  # deterministic through the persisted PCA + model
    assert len(set(first)) > 1  # non-trivial predictions, not all zeros


async def test_version_mismatched_artifact_is_refused(sm):
    async with sm() as s:
        await seed_dataset(s, n=250, signal=True)
    settings = get_settings()
    async with sm() as s:
        result = await rr.train_reranker(s, "employment", settings)
        assert result.promoted
        ml = await s.get(MlModel, result.ml_model_id)
        ml.sklearn_version = "0.0.0-not-installed"
        await s.commit()
        loaded = await rr.load_active_reranker(s, "employment")
        assert loaded is None  # refused, fall back to LLM Tier 1
        n_notes = (
            await s.execute(
                select(func.count()).select_from(Notification).where(
                    Notification.kind == "reranker_refused"
                )
            )
        ).scalar_one()
    assert n_notes == 1


async def test_active_learning_ranks_the_uncertain_item_first(sm):
    settings = get_settings()
    async with sm() as s:
        org = Org(name="A", normalized_name="a")
        src = Source(track="employment", kind="greenhouse", name="s", enabled=True)
        s.add_all([org, src])
        await s.flush()
        uncertain_id = None
        rng = np.random.default_rng(0)
        # 14 clear items (score far from 4.0, low divergence) + 1 uncertain (3.9, high divergence)
        specs = [(1.5, 1.4)] * 7 + [(4.8, 4.7)] * 7 + [(3.9, 2.0)]
        for score, weighted in specs:
            opp = Opportunity(track="employment", opp_type="job_posting", org_id=org.id,
                              source_id=src.id, title="VP of IT", description_md="x",
                              stage="t2_done", first_seen_at="2026-07-20T00:00:00.000000+00:00")
            s.add(opp)
            await s.flush()
            vec = rng.normal(0, 1, size=16).astype(np.float32)
            s.add(OpportunityEmbedding(opportunity_id=opp.id, model="nomic-embed-text",
                                       vector=vec.tobytes(), dims=16))
            s.add(Evaluation(opportunity_id=opp.id, tier=2, track="employment",
                             verdict="consider", score=score, weighted_score=weighted))
            if score == 3.9:
                uncertain_id = opp.id
        await s.commit()
    async with sm() as s:
        ranked = await rank_uncertain(s, "employment", settings, limit=10)
    assert ranked[0].opportunity.id == uncertain_id  # near-threshold + divergent ranks first


async def test_drift_demotes_and_notifies(sm):
    async with sm() as s:
        await seed_dataset(s, n=250, signal=True)
    settings = get_settings()
    async with sm() as s:
        result = await rr.train_reranker(s, "employment", settings)
        assert result.promoted
    # inject 50 recent labels the promoted model no longer predicts (market shift)
    async with sm() as s:
        await seed_high_quality_labels_with_low_scores(s, n=50)
    async with sm() as s:
        report = await rr.check_drift(s, "employment", settings)
        assert report["demoted"] is True
        assert report["mae"] > settings.learning.drift_threshold
        active = (
            await s.execute(
                select(func.count()).select_from(MlModel).where(
                    MlModel.track == "employment", MlModel.is_active.is_(True)
                )
            )
        ).scalar_one()
        n_notes = (
            await s.execute(
                select(func.count()).select_from(Notification).where(
                    Notification.kind == "reranker_drift"
                )
            )
        ).scalar_one()
    assert active == 0  # demoted back to LLM Tier 1
    assert n_notes == 1


async def test_reranker_does_not_promote_by_beating_a_missing_llm(sm):
    """Reviewer fix: with no Tier 1 LLM verdicts on the held set, 'beats the LLM' is not
    established, so the reranker must NOT promote even when it fits the target well."""
    async with sm() as s:
        await seed_dataset(s, n=250, signal=True, tier1=False)  # no Tier 1 evaluations
    settings = get_settings()
    async with sm() as s:
        result = await rr.train_reranker(s, "employment", settings)
    assert result.metrics["llm_agreement"] is None
    assert result.criteria["beats_llm"] is False
    assert result.promoted is False


class _PerfectModel:
    """A model whose prediction is the row's own target (features are [target]), so recall/MAE are
    perfect. Isolates the promotion GATE from model quality."""

    def predict(self, x):
        return x[:, 0]


def _apply_row(target: float, verdict: str, llm: str = "keep") -> rr.Row:
    return rr.Row(0, np.array([target], dtype=float), float(target), True, verdict, llm)


def test_reranker_recall_gate_refuses_below_min_apply():
    """AFTERACTION 2.1/3.8: recall is measured on the operator's own apply verdict and is
    UNMEASURABLE below _MIN_APPLY_FOR_RECALL held apply items, so a perfect model cannot promote on
    one or two lucky observations (hard rule 6). The earlier gate keyed off target>=4.0 and passed
    on a single apply item."""
    keep = 3.0
    model = _PerfectModel()

    # 2 operator-apply held items + fillers, perfect recall -> still not measurable, gate refuses.
    held2 = [_apply_row(5.0, "apply"), _apply_row(5.0, "apply"),
             _apply_row(2.0, "pass"), _apply_row(2.5, "consider")]
    m2 = rr._evaluate(model, held2, keep_cutoff=keep)
    assert m2["n_apply"] == 2
    assert m2["recall_apply"] is None  # below the floor -> not rendered as solid
    recall_ok_2 = (m2["recall_apply"] is not None and m2["n_apply"] >= rr._MIN_APPLY_FOR_RECALL
                   and m2["recall_apply"] > 0.95)
    assert recall_ok_2 is False  # promotion criterion 4 refuses at n_apply=2 with perfect recall

    # 0 apply items -> also unmeasurable.
    m0 = rr._evaluate(model, [_apply_row(2.0, "pass")], keep_cutoff=keep)
    assert m0["n_apply"] == 0 and m0["recall_apply"] is None

    # The floor is five (hard rule 6): 4 of 4 perfect is still not a measurement.
    assert rr._MIN_APPLY_FOR_RECALL == 5
    m4 = rr._evaluate(model, [_apply_row(5.0, "apply") for _ in range(4)], keep_cutoff=keep)
    assert m4["n_apply"] == 4 and m4["recall_apply"] is None

    # At/above the floor the mechanism still works: 5 apply items, perfect model -> recall 1.0.
    held5 = [_apply_row(5.0, "apply") for _ in range(5)] + [_apply_row(2.0, "pass")]
    m5 = rr._evaluate(model, held5, keep_cutoff=keep)
    assert m5["n_apply"] == 5 and m5["recall_apply"] == 1.0
    recall_ok_5 = (m5["recall_apply"] is not None and m5["n_apply"] >= rr._MIN_APPLY_FOR_RECALL
                   and m5["recall_apply"] > 0.95)
    assert recall_ok_5 is True

    # Consulting's "pursue" verdict counts as an apply item too (track-aware).
    mp = rr._evaluate(model, [_apply_row(5.0, "pursue") for _ in range(5)], keep_cutoff=keep)
    assert mp["n_apply"] == 5


async def test_reranker_refuses_to_promote_on_a_tiny_held_out_set(sm):
    """With min_labels_for_reranker lowered, the 25% split can hand back one to four held-out rows.
    An MAE over that is not a measurement: the gate FAILS (not merely a display label) and the
    reason states the n (hard rule 6)."""
    async with sm() as s:
        await seed_dataset(s, n=12, signal=True)  # 25% held out -> n_held = 3
    settings = get_settings()
    settings.learning.min_labels_for_reranker = 3
    async with sm() as s:
        result = await rr.train_reranker(s, "employment", settings)
    assert result.metrics["n_held"] == 3
    assert result.criteria["enough_labels"] is True
    assert result.criteria["mae_ok"] is False  # refused on n, whatever the MAE happened to be
    assert result.promoted is False
    assert result.reason is not None
    assert "n_held=3" in result.reason and str(rr._MIN_HELD_FOR_MAE) in result.reason
    async with sm() as s:  # nothing was activated
        active = (
            await s.execute(
                select(func.count()).select_from(MlModel).where(MlModel.is_active.is_(True))
            )
        ).scalar_one()
    assert active == 0


def test_refusal_reason_states_n_apply_below_the_floor():
    settings = get_settings()
    metrics = {"n_held": 40, "mae": 0.3, "n_llm": 20, "n_apply": 3, "recall_apply": None}
    criteria = {"enough_labels": True, "mae_ok": True, "beats_llm": True, "recall_ok": False}
    reasons = rr._refusal_reasons(criteria, metrics, settings)
    assert len(reasons) == 1
    assert "n_apply=3" in reasons[0] and f"< {rr._MIN_APPLY_FOR_RECALL}" in reasons[0]


async def test_drift_window_ignores_labels_the_model_trained_on(sm):
    """Right after promotion the 50 most recent labels are the TRAINING rows, which the model fits
    by construction, so an unfiltered window could never fire. Labels dated before trained_at are
    excluded: with none after it, the check is insufficient, never a verdict."""
    async with sm() as s:
        await seed_dataset(s, n=250, signal=True)
    settings = get_settings()
    async with sm() as s:
        result = await rr.train_reranker(s, "employment", settings)
        assert result.promoted
    # 50 contradicting labels, but BACKDATED to before the model was trained: they are not
    # evidence about the model's behaviour since promotion and must not count.
    async with sm() as s:
        await seed_high_quality_labels_with_low_scores(
            s, n=50, created_at="2000-01-01T00:00:00.000000+00:00"
        )
    async with sm() as s:
        report = await rr.check_drift(s, "employment", settings)
    assert report["active"] is True
    assert report["demoted"] is False
    assert report["insufficient"] is True and report["n"] == 0
    assert "mae" not in report  # no number is reported from zero observations


async def test_drift_needs_five_post_training_labels_with_embeddings(sm):
    """Four post-training labels that all contradict the model are still insufficient (no
    demotion), and labels without an embedding do not count toward the five."""
    async with sm() as s:
        await seed_dataset(s, n=250, signal=True)
    settings = get_settings()
    async with sm() as s:
        assert (await rr.train_reranker(s, "employment", settings)).promoted
    async with sm() as s:
        await seed_high_quality_labels_with_low_scores(s, n=4)  # embedded, post-training
        await seed_high_quality_labels_with_low_scores(s, n=3, embed=False)  # dropped by filter
    async with sm() as s:
        report = await rr.check_drift(s, "employment", settings)
        assert report["demoted"] is False
        assert report["insufficient"] is True and report["n"] == 4
        active = (
            await s.execute(
                select(func.count()).select_from(MlModel).where(
                    MlModel.track == "employment", MlModel.is_active.is_(True)
                )
            )
        ).scalar_one()
    assert active == 1  # still active: four observations do not demote

    # One more embedded post-training label reaches the floor; now the same evidence demotes.
    async with sm() as s:
        await seed_high_quality_labels_with_low_scores(s, n=1)
    async with sm() as s:
        report = await rr.check_drift(s, "employment", settings)
    assert report["n"] == 5 and report["demoted"] is True


async def test_promoted_reranker_decides_tier1_without_the_llm(sm):
    """Reviewer fix + SPEC 5.4: once promoted, Tier 1 is a millisecond reranker call, not the LLM.
    A confident item is decided by the reranker; the LLM client is never invoked."""
    from kestrel.db.models import Evaluation as Ev
    from kestrel.pipeline.tier1 import handle_t1

    async with sm() as s:
        await seed_dataset(s, n=250, signal=True)
    settings = get_settings()
    async with sm() as s:
        assert (await rr.train_reranker(s, "employment", settings)).promoted
        # a fresh, clearly-excellent opportunity at t0_pass with an embedding encoding high dims
        org = (await s.execute(select(Org).limit(1))).scalar_one()
        opp = Opportunity(track="employment", opp_type="job_posting", org_id=org.id,
                          title="VP of IT", stage="t0_pass", remote_type="hybrid",
                          stage_reason="soft: title_contains", pre_score=2.0)
        s.add(opp)
        await s.flush()
        opp_id = opp.id
        vec = np.zeros(32, dtype=np.float32)
        vec[:5] = 15.0  # encodes ~5.0 dims -> reranker predicts ~5.0 -> confident keep
        s.add(OpportunityEmbedding(opportunity_id=opp.id, model="nomic-embed-text",
                                   vector=vec.tobytes(), dims=32))
        await s.commit()

    class _NoLLM:
        settings = get_settings()

        async def complete(self, **_):
            raise AssertionError("the LLM must not be called for a confident reranker decision")

    outcome = await handle_t1(sm, _NoLLM(), settings, opportunity_id=opp_id, profiles={})
    assert outcome.action == "t1_pass"
    async with sm() as s:
        ev = (
            await s.execute(select(Ev).where(Ev.opportunity_id == opp_id, Ev.tier == 1))
        ).scalar_one()
        assert ev.model.startswith("reranker:") and ev.forced_by == "reranker"


async def test_outcomes_none_on_insufficient_data(sm):
    async with sm() as s:
        org = Org(name="A", normalized_name="a")
        src = Source(track="employment", kind="greenhouse", name="s", enabled=True)
        s.add_all([org, src])
        await s.commit()
    async with sm() as s:
        report = await outcomes_mod.analyze_outcomes(s, "employment")
    assert report.discriminating is None  # never claim it works from no data (hard rule 6)


async def test_outcomes_report_non_discrimination(sm):
    async with sm() as s:
        org = Org(name="A", normalized_name="a")
        src = Source(track="employment", kind="greenhouse", name="s", enabled=True)
        s.add_all([org, src])
        await s.flush()
        rng = np.random.default_rng(3)
        # scores spread across bands, but response is INDEPENDENT of score (coin flip)
        for _ in range(120):
            score = float(rng.uniform(2.0, 5.0))
            opp = Opportunity(track="employment", opp_type="job_posting", org_id=org.id,
                              source_id=src.id, title="VP of IT", stage="t2_done",
                              remote_type="hybrid")
            s.add(opp)
            await s.flush()
            s.add(Evaluation(opportunity_id=opp.id, tier=2, track="employment",
                             verdict="consider", score=score))
            pur = Pursuit(opportunity_id=opp.id, track="employment", status="applied")
            s.add(pur)
            await s.flush()
            responded = rng.uniform() < 0.5  # independent of score
            s.add(Outcome(pursuit_id=pur.id, opportunity_id=opp.id,
                          days_to_first_response=int(rng.integers(3, 20)) if responded else None,
                          furthest_stage="applied"))
        await s.commit()
    async with sm() as s:
        report = await outcomes_mod.analyze_outcomes(s, "employment")
    assert report.discriminating is False
    assert "not discriminating" in report.headline.lower()


async def test_active_learning_skips_roles_without_a_description_or_archived(sm):
    """Calibrate asks the operator to judge a role, so a role with nothing to read (no
    description, or only whitespace) or one already hidden is never queued (#32)."""
    settings = get_settings()
    async with sm() as s:
        org = Org(name="A", normalized_name="a")
        src = Source(track="employment", kind="greenhouse", name="s", enabled=True)
        s.add_all([org, src])
        await s.flush()
        ids: dict[str, int] = {}
        cases = {
            "ok": dict(description_md="Own IT strategy."),
            "none": dict(description_md=None),
            "empty": dict(description_md=""),
            "blank": dict(description_md="  \n\t "),
            "archived": dict(description_md="Own IT.", archived_at="2026-07-21T00:00:00+00:00"),
        }
        for name, kw in cases.items():
            opp = Opportunity(track="employment", opp_type="job_posting", org_id=org.id,
                              source_id=src.id, title="VP of IT", stage="t2_done",
                              first_seen_at="2026-07-20T00:00:00.000000+00:00", **kw)
            s.add(opp)
            await s.flush()
            ids[name] = opp.id
            s.add(Evaluation(opportunity_id=opp.id, tier=2, track="employment",
                             verdict="consider", score=3.9, weighted_score=3.5))
        await s.commit()
    async with sm() as s:
        ranked = await rank_uncertain(s, "employment", settings, limit=10)
    assert [u.opportunity.id for u in ranked] == [ids["ok"]]
