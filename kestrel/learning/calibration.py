"""Weight recalibration (SPEC-MODULES 5.2). At min_labels_for_calibration, fit the dimension weights
that best predict the operator's OWN score from Σ(dimension × weight), using ridge regression under
non-negativity and a sum-to-one constraint. Report current vs proposed with cross-validated MAE for
each. THE OPERATOR ACCEPTS OR REJECTS; weights are never applied silently. Runs per track."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize
from sklearn.model_selection import KFold
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.config import Settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Calibration, Setting
from kestrel.learning.capture import LabeledExample, labeled_examples

_DIMS = {
    "employment": ["role_fit", "domain_match", "company_context", "comp_logistics", "trajectory"],
    "consulting": [
        "engagement_fit",
        "buyer_access",
        "economics",
        "delivery_feasibility",
        "strategic_value",
    ],
}
_RIDGE_ALPHA = 0.02


@dataclass
class CalibrationProposal:
    track: str
    n_samples: int
    previous_weights: dict[str, float]
    proposed_weights: dict[str, float]
    cv_mae_previous: float
    cv_mae_proposed: float


def dimension_keys(track: str) -> list[str]:
    return _DIMS.get(track, _DIMS["employment"])


def _matrix(examples: list[LabeledExample], keys: list[str]) -> tuple[np.ndarray, np.ndarray]:
    xs, ys = [], []
    for ex in examples:
        dims = ex.dimensions or {}
        row = []
        ok = True
        for k in keys:
            d = dims.get(k)
            score = d.get("score") if isinstance(d, dict) else d
            if not isinstance(score, (int, float)):
                ok = False
                break
            row.append(float(score))
        if ok:
            xs.append(row)
            ys.append(float(ex.operator_score))
    if not xs:
        return np.empty((0, len(keys))), np.empty((0,))
    return np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)


def fit_weights(x: np.ndarray, y: np.ndarray, *, alpha: float = _RIDGE_ALPHA) -> np.ndarray:
    """Non-negative, sum-to-one ridge weights minimizing mean((Xw - y)^2) + alpha*||w||^2."""
    d = x.shape[1]

    def obj(w):
        resid = x @ w - y
        return float(np.mean(resid**2) + alpha * np.sum(w**2))

    w0 = np.full(d, 1.0 / d)
    res = minimize(
        obj,
        w0,
        method="SLSQP",
        bounds=[(0.0, 1.0)] * d,
        constraints=[{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}],
        options={"maxiter": 500, "ftol": 1e-9},
    )
    w = np.clip(res.x, 0.0, None)
    total = w.sum()
    return w / total if total > 0 else np.full(d, 1.0 / d)


def _mae(x: np.ndarray, y: np.ndarray, w: np.ndarray) -> float:
    return float(np.mean(np.abs(x @ w - y)))


def _cv_mae(x: np.ndarray, y: np.ndarray, current: np.ndarray) -> tuple[float, float]:
    """5-fold CV MAE for the CURRENT (fixed) weights and the PROPOSED (fit per fold) weights, on the
    same held-out folds so the comparison is fair."""
    n = len(y)
    splits = min(5, n)
    if splits < 2:
        w = fit_weights(x, y)
        return _mae(x, y, current), _mae(x, y, w)
    kf = KFold(n_splits=splits, shuffle=True, random_state=0)
    prev, prop = [], []
    for train_idx, test_idx in kf.split(x):
        w = fit_weights(x[train_idx], y[train_idx])
        prev.append(_mae(x[test_idx], y[test_idx], current))
        prop.append(_mae(x[test_idx], y[test_idx], w))
    return float(np.mean(prev)), float(np.mean(prop))


def _current_weights(track: str, settings: Settings) -> dict[str, float]:
    return getattr(settings.scoring.weights, track).model_dump()


async def propose_calibration(
    session: AsyncSession, track: str, settings: Settings
) -> Calibration | None:
    """Fit and STORE a calibration proposal (status 'proposed'), or None if there are too few
    labeled examples or too few with usable dimensions. Never applies the weights."""
    keys = dimension_keys(track)
    examples = await labeled_examples(session, track)
    if len(examples) < settings.learning.min_labels_for_calibration:
        return None
    x, y = _matrix(examples, keys)
    if len(y) < settings.learning.min_labels_for_calibration:
        return None

    current = np.asarray([_current_weights(track, settings)[k] for k in keys], dtype=float)
    proposed = fit_weights(x, y)
    cv_prev, cv_prop = _cv_mae(x, y, current)

    row = Calibration(
        track=track,
        created_at=utcnow_iso(),
        n_samples=len(y),
        previous_weights={k: float(v) for k, v in zip(keys, current, strict=True)},
        proposed_weights={k: round(float(v), 4) for k, v in zip(keys, proposed, strict=True)},
        cv_mae_previous=round(cv_prev, 4),
        cv_mae_proposed=round(cv_prop, 4),
        status="proposed",
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def accept_calibration(session: AsyncSession, calibration_id: int) -> bool:
    """Apply a proposal: write the proposed weights as setting-table rows (the highest-precedence
    config layer) and mark it accepted. The operator's explicit act, never automatic."""
    cal = await session.get(Calibration, calibration_id)
    if cal is None or cal.status != "proposed":
        return False
    for key, value in (cal.proposed_weights or {}).items():
        setting_key = f"scoring.weights.{cal.track}.{key}"
        existing = await session.get(Setting, setting_key)
        if existing is None:
            session.add(Setting(key=setting_key, value=value))
        else:
            existing.value = value
    cal.status = "accepted"
    cal.decided_at = utcnow_iso()
    await session.commit()
    return True


async def reject_calibration(session: AsyncSession, calibration_id: int) -> bool:
    cal = await session.get(Calibration, calibration_id)
    if cal is None or cal.status != "proposed":
        return False
    cal.status = "rejected"
    cal.decided_at = utcnow_iso()
    await session.commit()
    return True


async def latest_proposals(session: AsyncSession) -> list[Calibration]:
    return list(
        (
            await session.execute(
                select(Calibration)
                .where(Calibration.status == "proposed")
                .order_by(Calibration.id.desc())
            )
        ).scalars()
    )
