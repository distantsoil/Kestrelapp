"""The learned reranker (SPEC-MODULES 5.4). Replaces the Tier 1 LLM once it earns the right on a
held-out split of the operator's own labels. HistGradientBoostingRegressor (no loky worker pool
re-executing sys.executable in a frozen app). The PCA is persisted WITH the model; pickles are
sklearn-version-locked and a mismatched artifact is refused (fall back to LLM Tier 1 + notification)
rather than silently scoring garbage. Drift over the last 50 post-training labels demotes it."""

from __future__ import annotations

import asyncio
import pickle
from dataclasses import dataclass, field

import numpy as np
import sklearn
import structlog
from sklearn.ensemble import HistGradientBoostingRegressor
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel import paths
from kestrel.config import Settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import (
    Evaluation,
    MlModel,
    Notification,
    Opportunity,
    OpportunityEmbedding,
    Org,
    Source,
    VerdictOverride,
)
from kestrel.learning.capture import labeled_examples
from kestrel.learning.features import FittedFeatures, embedding_of, fit_features
from kestrel.llm.embeddings import current_model
from kestrel.pipeline.scoring import (
    _TOP_VERDICT,  # single source of truth for the per-track verdict
)

log = structlog.get_logger()
LEGACY_EMBED_MODEL = "nomic-embed-text"


@dataclass
class Row:
    opp_id: int
    features: np.ndarray
    target: float
    labeled: bool
    operator_verdict: str | None
    llm_verdict: (
        str | None
    )  # the Tier 1 LLM verdict for this opp, for the "beats the LLM" criterion


@dataclass
class TrainResult:
    track: str
    n_labeled: int
    promoted: bool
    criteria: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    ml_model_id: int | None = None
    reason: str | None = None


async def _corpus_embeddings(
    session: AsyncSession, opp_ids: list[int], model: str | None = None
) -> dict[int, np.ndarray]:
    """Vectors by opportunity. With `model`, only that embed model's rows: vectors from two models
    live in different spaces (and usually widths), so a corpus must never mix them (D-EMBED-4)."""
    if not opp_ids:
        return {}
    stmt = select(OpportunityEmbedding).where(OpportunityEmbedding.opportunity_id.in_(opp_ids))
    if model is not None:
        stmt = stmt.where(OpportunityEmbedding.model == model)
    rows = (
        (
            await session.execute(stmt)
        )
        .scalars()
        .all()
    )
    return {r.opportunity_id: embedding_of(r.vector) for r in rows}


async def build_rows(
    session: AsyncSession, track: str, fitted: FittedFeatures, model: str | None = None
) -> list[Row]:
    """One row per opportunity that has a training TARGET and an embedding: the operator score where
    they judged it, else the Tier 2 holistic where they did not override it (SPEC-MODULES 5.4)."""
    opps = (
        (await session.execute(select(Opportunity).where(Opportunity.track == track)))
        .scalars()
        .all()
    )
    opp_by_id = {o.id: o for o in opps}
    ids = list(opp_by_id)
    emb = await _corpus_embeddings(session, ids, model)

    labeled = {ex.opportunity_id: ex for ex in await labeled_examples(session, track)}
    override_ids = {
        oid
        for (oid,) in (
            await session.execute(
                select(VerdictOverride.opportunity_id).where(
                    VerdictOverride.operator_score.is_not(None)
                )
            )
        ).all()
    }
    # tier-2 holistic and tier-1 verdict maps
    t2, t1 = {}, {}
    for ev in (
        (
            await session.execute(
                select(Evaluation).where(Evaluation.opportunity_id.in_(ids)).order_by(Evaluation.id)
            )
        )
        .scalars()
        .all()
    ):
        if ev.tier == 2 and ev.score is not None:
            t2[ev.opportunity_id] = ev.score
        if ev.tier == 1:
            t1[ev.opportunity_id] = ev.verdict
    orgs = {o.id: o for o in (await session.execute(select(Org))).scalars().all()}
    src_kind = {s.id: s.kind for s in (await session.execute(select(Source))).scalars().all()}

    rows: list[Row] = []
    for oid, opp in opp_by_id.items():
        vec = emb.get(oid)
        if vec is None:
            continue
        if oid in labeled:
            target = labeled[oid].operator_score
            is_labeled, op_verdict = True, labeled[oid].verdict
        elif oid in t2 and oid not in override_ids:
            target = t2[oid]
            is_labeled, op_verdict = False, None
        else:
            continue
        opp._source_kind = src_kind.get(opp.source_id)
        feats = fitted.transform(opp, orgs.get(opp.org_id), vec)
        rows.append(Row(oid, feats, float(target), is_labeled, op_verdict, t1.get(oid)))
    return rows


def _keep(score: float, cutoff: float) -> bool:
    return score >= cutoff


# Sample floors for the promotion gate (hard rule 6: a number from fewer than five observations
# is insufficient, and here insufficient means the gate FAILS, not merely that a label is shown).
_MIN_APPLY_FOR_RECALL = 5  # held-out apply/pursue items before recall means anything
_MIN_HELD_FOR_MAE = 5  # held-out rows before the MAE is a measurement at all
_MIN_LLM_FOR_COMPARISON = 5  # need a few LLM verdicts to claim we beat the LLM
_MIN_DRIFT_WINDOW = 5  # post-training labels before a rolling MAE can demote
# recall_ok requires recall_apply to clear this bar, not merely be present. Named so the /calibrate
# progress panel (promotion_status, below) can state the exact threshold without re-typing 0.95 and
# risking the UI's copy drifting from the real gate.
_RECALL_MIN_THRESHOLD = 0.95
# The operator verdicts that mean "pursue this opportunity", derived from the single source of
# truth (scoring._TOP_VERDICT: employment "apply", consulting "pursue") so the two never drift.
# Recall is measured against the operator's OWN decision, not a target-score proxy, so a reranker
# is judged on the items the operator would actually chase.
_APPLY_VERDICTS = tuple(_TOP_VERDICT.values())


def _evaluate(model, held: list[Row], *, keep_cutoff: float) -> dict:
    if not held:
        return {"mae": 999.0, "reranker_agreement": 0.0,
                "reranker_agreement_on_llm_set": None, "llm_agreement": None,
                "recall_apply": None, "n_held": 0, "n_apply": 0, "n_llm": 0}
    x = np.vstack([r.features for r in held])
    preds = model.predict(x)
    y = np.asarray([r.target for r in held])
    mae = float(np.mean(np.abs(preds - y)))

    op_keep = y >= keep_cutoff
    rr_keep = preds >= keep_cutoff
    reranker_agreement = float(np.mean(rr_keep == op_keep))

    # Criterion 3 must be measured on the SAME held items the LLM judged. Without an LLM verdict to
    # compare against, "beats the LLM" is not established (None) rather than vacuously true.
    llm_idx = [i for i, r in enumerate(held) if r.llm_verdict in ("keep", "kill")]
    if len(llm_idx) >= _MIN_LLM_FOR_COMPARISON:
        llm_keep = np.asarray([held[i].llm_verdict == "keep" for i in llm_idx])
        rr_keep_llm = rr_keep[llm_idx]
        op_keep_llm = op_keep[llm_idx]
        llm_agreement = float(np.mean(llm_keep == op_keep_llm))
        reranker_agreement_on_llm = float(np.mean(rr_keep_llm == op_keep_llm))
    else:
        llm_agreement = None
        reranker_agreement_on_llm = None

    # "Apply items" are the held rows the OPERATOR verdicted as pursue-worthy, not rows whose score
    # crossed a threshold. Recall = fraction of those the reranker would still keep (never drop a
    # real apply). It is None (not measurable) below _MIN_APPLY_FOR_RECALL held apply items, so the
    # promotion gate cannot pass on one or two lucky observations (hard rule 6).
    apply_mask = np.asarray([r.operator_verdict in _APPLY_VERDICTS for r in held])
    n_apply = int(apply_mask.sum())
    recall_apply = (
        float(np.mean(preds[apply_mask] >= keep_cutoff))
        if n_apply >= _MIN_APPLY_FOR_RECALL
        else None
    )
    return {
        "mae": round(mae, 4),
        "reranker_agreement": round(reranker_agreement, 4),
        "reranker_agreement_on_llm_set": (
            round(reranker_agreement_on_llm, 4) if reranker_agreement_on_llm is not None else None
        ),
        "llm_agreement": round(llm_agreement, 4) if llm_agreement is not None else None,
        "recall_apply": round(recall_apply, 4) if recall_apply is not None else None,
        "n_held": len(held),
        "n_apply": n_apply,
        "n_llm": len(llm_idx),
    }


def _split_labeled(rows: list[Row], *, holdout_frac: float = 0.25, seed: int = 0):
    labeled = [r for r in rows if r.labeled]
    unlabeled = [r for r in rows if not r.labeled]
    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(labeled))
    n_held = max(1, int(len(labeled) * holdout_frac))
    held = [labeled[i] for i in idx[:n_held]]
    train_labeled = [labeled[i] for i in idx[n_held:]]
    return train_labeled + unlabeled, held


async def train_reranker(
    session: AsyncSession, track: str, settings: Settings, *, promoted_by: str = "auto"
) -> TrainResult:
    if not settings.learning.build_model:
        return TrainResult(
            track, 0, promoted=False, reason="switched off in Settings > Scoring and learning"
        )
    # fit the PCA on the corpus (all track embeddings)
    opp_ids = [
        o
        for (o,) in (
            await session.execute(select(Opportunity.id).where(Opportunity.track == track))
        ).all()
    ]
    embed_model = current_model(settings)
    emb_map = await _corpus_embeddings(session, opp_ids, embed_model)
    emb_matrix = (
        np.vstack([v for v in emb_map.values() if v is not None]) if emb_map else np.empty((0, 0))
    )
    fitted = await asyncio.to_thread(fit_features, track, emb_matrix)
    rows = await build_rows(session, track, fitted, embed_model)
    # "labeled examples" that are actually USABLE (have an embedding, so they train/validate). The
    # promotion bar is on these, not on labels dropped for lack of an embedding (else the 200-label
    # bar gives false confidence while validation runs on a handful).
    n_labeled = sum(1 for r in rows if r.labeled)

    if n_labeled < settings.learning.min_labels_for_reranker:
        return TrainResult(
            track,
            n_labeled,
            promoted=False,
            reason=f"insufficient usable labels (n={n_labeled} < "
            f"{settings.learning.min_labels_for_reranker})",
        )

    train_rows, held = _split_labeled(rows)
    x_train = np.vstack([r.features for r in train_rows])
    y_train = np.asarray([r.target for r in train_rows])
    model = await asyncio.to_thread(
        HistGradientBoostingRegressor(random_state=0, max_iter=300).fit, x_train, y_train,
    )

    keep_cutoff = settings.thresholds.consider
    metrics = await asyncio.to_thread(_evaluate, model, held, keep_cutoff=keep_cutoff)

    # Every criterion must be POSITIVELY established on the held-out set. A None (not measurable)
    # fails, so a reranker never promotes by beating a non-existent LLM or on zero apply items.
    beats_llm = (
        metrics["llm_agreement"] is not None
        and metrics["reranker_agreement_on_llm_set"] is not None
        and metrics["reranker_agreement_on_llm_set"] > metrics["llm_agreement"]
    )
    # recall_apply is None below _MIN_APPLY_FOR_RECALL held apply items; the explicit n_apply guard
    # restates that so the floor is obvious and cannot be lost to a future refactor of _evaluate.
    recall_ok = (
        metrics["recall_apply"] is not None
        and metrics["n_apply"] >= _MIN_APPLY_FOR_RECALL
        and metrics["recall_apply"] > _RECALL_MIN_THRESHOLD
    )
    # An MAE over one or two held-out rows is not a measurement. With a lowered
    # min_labels_for_reranker the split can hand back a single row; the gate fails there.
    held_ok = metrics["n_held"] >= _MIN_HELD_FOR_MAE
    criteria = {
        "enough_labels": n_labeled >= settings.learning.min_labels_for_reranker,
        "mae_ok": held_ok and metrics["mae"] < settings.learning.reranker_max_mae,
        "beats_llm": beats_llm,
        "recall_ok": recall_ok,
    }
    promoted = all(criteria.values())
    reason = None
    if not promoted:
        reason = "; ".join(_refusal_reasons(criteria, metrics, settings))

    ml_id = await _save_model(
        session,
        track=track,
        model=model,
        fitted=fitted,
        n_labeled=n_labeled,
        # embed_model: the reranker's PCA was fitted in that model's space; it is refused under
        # any other (load_active_reranker), since its features would be meaningless there.
        metrics={**metrics, "criteria": criteria, "embed_model": embed_model},
        promoted=promoted,
        promoted_by=promoted_by,
    )
    return TrainResult(track, n_labeled, promoted, criteria, metrics, ml_id, reason)


def _refusal_reasons(criteria: dict, metrics: dict, settings: Settings) -> list[str]:
    """Why the gate refused, with every sample size stated, so a refusal on too few observations
    reads as exactly that and not as a model-quality verdict."""
    reasons: list[str] = []
    if not criteria["enough_labels"]:
        reasons.append("insufficient labels")
    if not criteria["mae_ok"]:
        if metrics["n_held"] < _MIN_HELD_FOR_MAE:
            reasons.append(
                f"held-out set too small to judge MAE (n_held={metrics['n_held']} < "
                f"{_MIN_HELD_FOR_MAE})"
            )
        else:
            reasons.append(
                f"MAE {metrics['mae']} not below {settings.learning.reranker_max_mae} "
                f"(n_held={metrics['n_held']})"
            )
    if not criteria["beats_llm"]:
        if metrics["n_llm"] < _MIN_LLM_FOR_COMPARISON:
            reasons.append(
                f"too few LLM-verdicted held-out items to compare (n_llm={metrics['n_llm']} < "
                f"{_MIN_LLM_FOR_COMPARISON})"
            )
        else:
            reasons.append(f"does not beat the LLM (n_llm={metrics['n_llm']})")
    if not criteria["recall_ok"]:
        if metrics["n_apply"] < _MIN_APPLY_FOR_RECALL:
            reasons.append(
                f"too few held-out apply/pursue items to judge recall (n_apply="
                f"{metrics['n_apply']} < {_MIN_APPLY_FOR_RECALL})"
            )
        else:
            reasons.append(
                f"recall {metrics['recall_apply']} not above {_RECALL_MIN_THRESHOLD} "
                f"(n_apply={metrics['n_apply']})"
            )
    return reasons


async def _save_model(
    session: AsyncSession,
    *,
    track: str,
    model,
    fitted: FittedFeatures,
    n_labeled: int,
    metrics: dict,
    promoted: bool,
    promoted_by: str,
) -> int:
    paths.ml_dir().mkdir(parents=True, exist_ok=True)
    version = utcnow_iso().replace(":", "").replace(".", "")
    model_path = paths.ml_dir() / f"reranker_{track}_{version}.pkl"
    feat_path = paths.ml_dir() / f"features_{track}_{version}.pkl"
    with open(model_path, "wb") as fh:
        pickle.dump(model, fh)
    with open(feat_path, "wb") as fh:
        pickle.dump(fitted, fh)

    if promoted:  # only one active reranker per track
        for m in (
            (
                await session.execute(
                    select(MlModel).where(
                        MlModel.track == track,
                        MlModel.kind == "reranker",
                        MlModel.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        ):
            m.is_active = False

    row = MlModel(
        kind="reranker",
        track=track,
        version=version,
        trained_at=utcnow_iso(),
        n_samples=n_labeled,
        metrics=metrics,
        artifact_path=str(model_path),
        pca_artifact_path=str(feat_path),
        sklearn_version=sklearn.__version__,
        feature_names=fitted.feature_names(),
        is_active=promoted,
        promoted_at=utcnow_iso() if promoted else None,
        promoted_by=promoted_by if promoted else None,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row.id


@dataclass
class Criterion:
    """One promotion-gate condition rendered as data for the /calibrate progress panel: whether it
    currently holds, the value it was measured at, and the threshold it must clear. `met` is None
    when the condition cannot yet be evaluated (no completed training run for this track, or the
    measurement itself is below its own sample floor) rather than a guessed True/False -- a number
    from too few observations is labeled insufficient, never rendered as if it were solid (hard rule
    6)."""

    key: str
    label: str
    met: bool | None
    current: str
    required: str


@dataclass
class PromotionStatus:
    track: str
    reranker_active: bool
    n_labeled: int
    min_labels_for_reranker: int
    last_trained_at: str | None
    last_trained_n_labeled: int | None
    criteria: list[Criterion]
    ready: bool
    next_step: str


# Hard rule 6's display floor: any number derived from fewer than five observations is labeled
# insufficient rather than rendered as if it were solid. The promotion gate's own floors
# (_MIN_APPLY_FOR_RECALL, _MIN_HELD_FOR_MAE) sit at the same value, so nothing the gate accepts
# is ever displayed as insufficient; this constant is kept separate so a future change to one
# cannot silently move the other.
_HONEST_SAMPLE_FLOOR = 5


def _artifact_is_loadable(ml: MlModel) -> bool:
    """Would load_active_reranker accept this row? Read-only and cheap: no unpickling, no writes,
    no Notification. Mirrors that function's two refusal conditions so the /calibrate panel cannot
    claim a reranker is active when loading it would in fact fall back to the LLM."""
    from pathlib import Path

    if ml.sklearn_version != sklearn.__version__:
        return False
    for path in (ml.artifact_path, ml.pca_artifact_path):
        if not path or not Path(path).exists():
            return False
    return True


def _rate_text(value: float | None, n: int, min_n: int) -> str:
    """A rate is never shown bare: it is always beside the n it was computed from, and it is
    labeled insufficient rather than rendered as solid whenever it is below its own gate floor OR
    below the hard-rule-6 floor of five observations (hard rule 6)."""
    if value is None or n < min_n:
        return f"too few to judge ({n} of {min_n} needed)"
    if n < _HONEST_SAMPLE_FLOOR:
        # Above the gate's floor but still too few to read as established.
        return (
            f"{value * 100:.0f}% (from {n}; too few to judge, {n} of "
            f"{_HONEST_SAMPLE_FLOOR} needed)"
        )
    return f"{value * 100:.0f}% (from {n})"


def _mae_text(mae: float | None, n_held: int) -> str:
    """MAE beside its n, labeled insufficient below the hard-rule-6 floor (same rule as _rate_text).
    An error averaged over one held-out row is not a measurement."""
    if mae is None:
        return f"not measured ({n_held} held out)"
    if n_held < _HONEST_SAMPLE_FLOOR:
        return (
            f"MAE {mae:.2f} ({n_held} held out; too few to judge, {n_held} of "
            f"{_HONEST_SAMPLE_FLOOR} needed)"
        )
    return f"MAE {mae:.2f} ({n_held} held out)"


def _beats_llm_text(metrics: dict, n_llm: int) -> str:
    min_n = _MIN_LLM_FOR_COMPARISON
    reranker = _rate_text(metrics.get("reranker_agreement_on_llm_set"), n_llm, min_n)
    llm = _rate_text(metrics.get("llm_agreement"), n_llm, min_n)
    return f"reranker {reranker} vs LLM {llm}"


async def promotion_status(
    session: AsyncSession, track: str, settings: Settings
) -> PromotionStatus:
    """Read-only snapshot of the SAME gate `train_reranker` enforces, for the /calibrate progress
    panel. Reads the constants train_reranker itself reads (_MIN_APPLY_FOR_RECALL,
    _MIN_LLM_FOR_COMPARISON, _RECALL_MIN_THRESHOLD, settings.learning.min_labels_for_reranker,
    settings.learning.reranker_max_mae), so the panel's thresholds cannot silently drift from the
    real gate.

    Deliberately does NOT train a model: training writes a pickle, adds a MlModel row, and can flip
    which model is active, none of which belongs to a page view. The label count is recomputed
    fresh, since it changes every time the operator labels an item; the other three criteria
    (mae_ok, beats_llm, recall_ok) can only be established by an actual held-out evaluation, so they
    report the most recently completed training run's numbers for this track, or "not yet
    evaluated" if the reranker has never been trained."""
    labeled = await labeled_examples(session, track)
    opp_ids = [ex.opportunity_id for ex in labeled]
    emb = await _corpus_embeddings(session, opp_ids, current_model(settings))
    # "usable" must mirror build_rows EXACTLY: `emb.get(oid) is not None`, not `oid in emb`.
    # _corpus_embeddings maps rows through embedding_of, which returns None for a zero-length blob
    # while LEAVING THE KEY PRESENT. Counting keys therefore let the panel report "enough labels,
    # go train" while train_reranker refused with "insufficient usable labels", which is the drift
    # this count exists to avoid.
    n_labeled = sum(1 for ex in labeled if emb.get(ex.opportunity_id) is not None)

    min_labels = settings.learning.min_labels_for_reranker
    enough_labels = n_labeled >= min_labels

    latest = (
        await session.execute(
            select(MlModel)
            .where(MlModel.track == track, MlModel.kind == "reranker")
            .order_by(MlModel.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    # `is_active` alone is not proof the reranker is running. load_active_reranker refuses a model
    # whose pickle was written under a different sklearn (pickles are version locked) or whose
    # artifact is missing, and it falls back to the LLM WITHOUT clearing is_active. Reporting
    # "active" in that state would tell the operator a learned model is making Tier 1 decisions
    # while every decision is actually being made by the LLM. These are the same two conditions
    # load_active_reranker checks, and both are cheap and read-only, so the panel checks them too
    # rather than claiming more than it knows.
    # order_by/limit, NOT scalar_one_or_none: _save_model's deactivate-then-insert is not atomic
    # against a concurrent train, so two is_active rows are possible. load_active_reranker copes
    # with that by taking the newest; scalar_one_or_none would raise MultipleResultsFound and take
    # the whole panel down while the reranker ran normally. The panel must not be more brittle than
    # the thing it reports on.
    active_row = (
        await session.execute(
            select(MlModel)
            .where(MlModel.track == track, MlModel.kind == "reranker", MlModel.is_active.is_(True))
            .order_by(MlModel.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    active_exists = active_row is not None and _artifact_is_loadable(active_row)
    # The ACTIVE model and the LATEST training run are not always the same row: a failed retrain
    # leaves the previously promoted model active while becoming `latest`. The criteria below
    # describe `latest`, so when they differ the panel must say so rather than let the operator
    # read the failed run's numbers as the running model's.
    stale_metrics = (
        active_row is not None and latest is not None and active_row.id != latest.id
    )

    m = latest.metrics if latest is not None and isinstance(latest.metrics, dict) else {}
    stored_criteria = m.get("criteria") if isinstance(m.get("criteria"), dict) else {}
    not_trained = "not yet evaluated (train the reranker to measure this)"
    n_llm = int(m.get("n_llm") or 0)
    n_apply = int(m.get("n_apply") or 0)
    n_held = int(m.get("n_held") or 0)

    criteria = [
        Criterion(
            key="enough_labels",
            label="Enough labeled examples",
            met=enough_labels,
            current=f"{n_labeled} labeled",
            required=f">= {min_labels}",
        ),
        Criterion(
            key="mae_ok",
            label="Held-out prediction error",
            met=(stored_criteria.get("mae_ok") if latest is not None else None),
            # Goes through the same hard-rule-6 floor as the other rates. An earlier cut built this
            # row inline and rendered "MAE 0.41 (n=1 held-out)" bare, with a green tick, in the very
            # panel added to satisfy that rule. n_held can be 1 whenever min_labels_for_reranker is
            # small, and that setting is operator-overridable.
            current=(_mae_text(m.get("mae"), n_held) if latest is not None else not_trained),
            required=(
                f"MAE < {settings.learning.reranker_max_mae} on >= {_MIN_HELD_FOR_MAE} "
                "held-out items"
            ),
        ),
        Criterion(
            key="beats_llm",
            label="Beats the LLM tier on the same held-out items",
            met=(stored_criteria.get("beats_llm") if latest is not None else None),
            current=(_beats_llm_text(m, n_llm) if latest is not None else not_trained),
            required=(
                f"reranker agreement > LLM agreement, both on >= {_MIN_LLM_FOR_COMPARISON} "
                "LLM-verdicted held-out items"
            ),
        ),
        Criterion(
            key="recall_ok",
            label="Recall on the operator's own apply/pursue decisions",
            met=(stored_criteria.get("recall_ok") if latest is not None else None),
            current=(
                _rate_text(m.get("recall_apply"), n_apply, _MIN_APPLY_FOR_RECALL)
                if latest is not None
                else not_trained
            ),
            required=(
                f"recall > {_RECALL_MIN_THRESHOLD * 100:.0f}% on >= {_MIN_APPLY_FOR_RECALL} "
                "held-out apply/pursue items"
            ),
        ),
    ]

    # None (not yet measurable) is falsy, same as False: ready only when every criterion is
    # POSITIVELY established, mirroring train_reranker's own `promoted = all(criteria.values())`.
    ready = latest is not None and all(c.met for c in criteria)

    # Whichever branch fires, this sentence must match reality. Saying "heuristic scoring is in
    # charge" while a promoted model is gating Tier 1 is a false claim about what decided the
    # operator's results, and it was reachable whenever a retrain failed after a promotion.
    in_charge = (
        "The learned ranking is currently active for the quick screen."
        if active_exists
        else "The rule-based quick screen is in charge."
    )
    stale_note = (
        " The criteria below describe the most recent TRAINING RUN, which is not the model "
        "currently active, so those numbers do not describe the model making decisions now."
        if stale_metrics
        else ""
    )

    if n_labeled == 0 and labeled:
        # Labels exist but none is usable, which today means no embedding was ever written for
        # them. Telling the operator to "label more" would be advice that cannot possibly work.
        next_step = (
            f"{len(labeled)} labeled {track} item(s) exist but none has an embedding, so the "
            "reranker cannot train on any of them. Labeling more will not change that until the "
            f"embedding backlog is processed. {in_charge}"
        )
    elif not enough_labels:
        remaining = min_labels - n_labeled
        next_step = (
            f"Label {remaining} more {track} item{'s' if remaining != 1 else ''} to reach the "
            f"training floor. {in_charge}"
        )
    elif latest is None:
        next_step = (
            "Enough labeled examples to attempt training; train the reranker to measure the "
            f"remaining criteria. {in_charge}"
        )
    elif ready and active_exists:
        next_step = (
            "Every criterion is met; the learned reranker is active for new Tier 1 decisions."
        )
    elif ready and not active_exists:
        next_step = (
            "The last training run met every criterion, but the reranker is not currently active "
            "(it may have since been demoted by drift, or its artifact refused). Retrain to "
            "refresh it. The rule-based quick screen is in charge in the meantime."
        )
    else:
        failing = [c.label for c in criteria if c.met is False]
        detail = f" ({'; '.join(failing)})" if failing else ""
        next_step = (
            f"The most recent training run was not promoted{detail}. Record more labels and "
            f"outcomes, then retrain. {in_charge}"
        )
    next_step += stale_note

    return PromotionStatus(
        track=track,
        reranker_active=active_exists,
        n_labeled=n_labeled,
        min_labels_for_reranker=min_labels,
        last_trained_at=latest.trained_at if latest is not None else None,
        last_trained_n_labeled=latest.n_samples if latest is not None else None,
        criteria=criteria,
        ready=ready,
        next_step=next_step,
    )


@dataclass
class LoadedReranker:
    ml_model: MlModel
    model: object
    fitted: FittedFeatures


async def _notify(
    session: AsyncSession, kind: str, title: str, *, severity: str, body=None
) -> None:
    session.add(
        Notification(kind=kind, title=title, body=body, severity=severity, delivered_channels=[])
    )
    await session.commit()


async def load_active_reranker(
    session: AsyncSession, track: str, *, embed_model: str | None = None
) -> LoadedReranker | None:
    """Load the active reranker, or None (fall back to the LLM Tier 1). A pickle trained under a
    different sklearn version is REFUSED with a notification rather than loaded (version-locked).
    With `embed_model`, a reranker trained on another embedding model is also refused (quietly:
    it is expected right after an engine switch, until the next retrain)."""
    ml = (
        await session.execute(
            select(MlModel)
            .where(MlModel.track == track, MlModel.kind == "reranker", MlModel.is_active.is_(True))
            .order_by(MlModel.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if ml is None:
        return None
    trained_on = (ml.metrics or {}).get("embed_model") if isinstance(ml.metrics, dict) else None
    # A reranker from before embed_model was recorded was trained on the only engine there was,
    # Ollama's nomic-embed-text: treat it as such, so drift never feeds it 384-dim vectors
    # through a 768-dim PCA (D-EMBED-2 review).
    trained_on = trained_on or LEGACY_EMBED_MODEL
    if embed_model is not None and trained_on != embed_model:
        log.info("reranker_embed_model_mismatch", track=track, trained_on=trained_on,
                 current=embed_model)
        return None
    if ml.sklearn_version != sklearn.__version__:
        await _notify(
            session,
            "reranker_refused",
            f"Reranker for {track} refused: trained under sklearn {ml.sklearn_version}, "
            f"running {sklearn.__version__}. Falling back to LLM Tier 1.",
            severity="warning",
        )
        return None
    try:
        with open(ml.artifact_path, "rb") as fh:
            model = pickle.load(fh)
        with open(ml.pca_artifact_path, "rb") as fh:
            fitted = pickle.load(fh)
    except (OSError, pickle.UnpicklingError) as exc:
        await _notify(
            session,
            "reranker_refused",
            f"Reranker for {track} artifact unreadable ({exc}); falling back to LLM Tier 1.",
            severity="warning",
        )
        return None
    return LoadedReranker(ml_model=ml, model=model, fitted=fitted)


def predict_score(loaded: LoadedReranker, opp: Opportunity, org, embedding) -> float:
    opp._source_kind = getattr(opp, "_source_kind", None)
    x = loaded.fitted.transform(opp, org, embedding).reshape(1, -1)
    return float(loaded.model.predict(x)[0])


async def check_drift(session: AsyncSession, track: str, settings: Settings) -> dict:
    """Rolling MAE over the last 50 labels recorded AFTER the active model was trained; if it rises
    past drift_threshold, DEMOTE the reranker to LLM Tier 1 and raise a notification
    (SPEC-MODULES 5.4). Labels the model trained on are excluded: right after promotion the
    unfiltered window is mostly training rows the model fits by construction, so drift could not
    fire until ~50 new labels had accrued. Fewer than _MIN_DRIFT_WINDOW post-training labels (with
    embeddings) is insufficient and never demotes (hard rule 6)."""
    embed_model = current_model(settings)
    loaded = await load_active_reranker(session, track, embed_model=embed_model)
    if loaded is None:
        return {"active": False, "demoted": False}
    trained_at = loaded.ml_model.trained_at or ""
    recent = [ex for ex in await labeled_examples(session, track) if ex.created_at > trained_at]
    recent = recent[-50:]
    if len(recent) < _MIN_DRIFT_WINDOW:
        return {"active": True, "demoted": False, "n": len(recent), "insufficient": True}

    opp_ids = [ex.opportunity_id for ex in recent]
    emb = await _corpus_embeddings(session, opp_ids, embed_model)
    orgs = {o.id: o for o in (await session.execute(select(Org))).scalars().all()}
    src_kind = {s.id: s.kind for s in (await session.execute(select(Source))).scalars().all()}
    errs = []
    for ex in recent:
        opp = await session.get(Opportunity, ex.opportunity_id)
        vec = emb.get(ex.opportunity_id)
        if opp is None or vec is None:
            continue
        opp._source_kind = src_kind.get(opp.source_id)
        pred = predict_score(loaded, opp, orgs.get(opp.org_id), vec)
        errs.append(abs(pred - ex.operator_score))
    if len(errs) < _MIN_DRIFT_WINDOW:  # the embedding filter can leave a handful; not enough
        return {"active": True, "demoted": False, "n": len(errs), "insufficient": True}
    mae = float(np.mean(errs))
    demoted = mae > settings.learning.drift_threshold
    if demoted:
        loaded.ml_model.is_active = False
        await _notify(
            session,
            "reranker_drift",
            f"Reranker for {track} demoted: rolling MAE {mae:.2f} over last {len(errs)} labels "
            f"exceeds drift threshold {settings.learning.drift_threshold}. Back to LLM Tier 1.",
            severity="warning",
        )
    return {"active": True, "demoted": demoted, "mae": round(mae, 4), "n": len(errs)}
