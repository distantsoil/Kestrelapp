"""The learning ledger (Settings > Learning): what the operator's Agree, Adjust, Override and
Calibrate decisions teach, where that data lives, and how to view, export and reset it.

Two tables hold the operator's judgments. `verdict_override` is written by Agree / Adjust /
Override on an opportunity (web/queries.record_override); `label` is written by the Calibrate
queue (learning/capture.record_label). Calibration and the reranker consume both through
`labeled_examples`; few-shot examples (pipeline/tier2._gather_few_shot) draw on `label` rows that
have an embedding. This module reads both tables as one ledger so the operator sees every decision
that teaches anything, and it is the one place that deletes any of it.

Nothing here changes a local model's weights: the model is never trained. Kestrel changes what the
model is shown (few-shot examples), how its dimension scores are combined (accepted calibration
weights) and whether a learned reranker gates Tier 1. Reset removes exactly those three things and
touches no opportunity, evaluation, pursuit or document.

Hard rule 6: every rate is stated with its n and labeled insufficient under five observations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import sklearn
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel import paths
from kestrel.config import ConsultingWeights, EmploymentWeights, Settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import (
    Calibration,
    Evaluation,
    Label,
    MlModel,
    Notification,
    Opportunity,
    OpportunityEmbedding,
    Org,
    Setting,
    VerdictOverride,
)
from kestrel.learning.capture import labeled_examples
from kestrel.web.redact import confidential, redact_name, redact_text

TRACKS: tuple[str, ...] = ("employment", "consulting")
KINDS: tuple[str, ...] = ("label", "override")

# Hard rule 6: a rate from fewer than five observations is labeled insufficient, never solid.
INSUFFICIENT_N = 5

# Accepted calibrations are applied as setting-table rows under this prefix
# (learning/calibration.accept_calibration); reset removes exactly those rows.
WEIGHT_PREFIX = "scoring.weights."

# The only files Kestrel writes under paths.ml_dir() (learning/reranker._save_model). Reset removes
# files matching these patterns plus any artifact an ml_model row points at inside ml_dir, and
# nothing else: an operator's own file in that folder is never touched.
ML_FILE_PATTERNS: tuple[str, ...] = ("reranker_*.pkl", "features_*.pkl")


# ============================================================================ data shapes
@dataclass
class LedgerRow:
    """One operator decision, from either table, as the page and the export render it."""

    kind: str  # "label" (Calibrate queue) | "override" (Agree / Adjust / Override)
    id: int
    created_at: str
    track: str
    opportunity_id: int
    title: str | None
    org: str | None
    model_verdict: str | None
    model_score: float | None
    your_verdict: str | None
    your_score: float | None
    rationale: str | None
    source: str
    feeds_few_shot: bool  # a `label` row whose opportunity has an embedding

    @property
    def agreed(self) -> bool | None:
        if not self.model_verdict or not self.your_verdict:
            return None
        return self.model_verdict == self.your_verdict


@dataclass
class Agreement:
    n: int
    agreed: int
    rate: float | None  # None under INSUFFICIENT_N
    insufficient: bool

    @property
    def text(self) -> str:
        if self.n == 0:
            return "no decisions with a model verdict to compare against yet"
        if self.insufficient:
            return f"too few to judge ({self.n} of {INSUFFICIENT_N} needed)"
        return f"{(self.rate or 0.0) * 100:.0f}% (from {self.n})"


@dataclass
class Floor:
    key: str  # few_shot | calibration | reranker
    label: str
    n: int
    needed: int
    active: bool
    note: str

    @property
    def reached(self) -> bool:
        return self.n >= self.needed


@dataclass
class WeightRow:
    key: str
    current: float
    default: float
    overridden: bool


@dataclass
class RerankerState:
    active: bool
    trained_at: str | None
    n_samples: int | None
    sklearn_version: str | None
    loadable: bool
    labels_since_training: int
    last_drift_demotion_at: str | None


@dataclass
class TrackSummary:
    track: str
    n_labels: int  # `label` rows (Calibrate queue)
    n_overrides: int  # `verdict_override` rows (Agree / Adjust / Override)
    agreement: Agreement
    floors: list[Floor]
    calibrations_accepted: int
    calibrations_proposed: int
    weights: list[WeightRow]
    reranker: RerankerState

    @property
    def n_total(self) -> int:
        return self.n_labels + self.n_overrides


@dataclass
class MlFile:
    name: str
    size: int
    track: str | None
    trained_at: str | None
    sklearn_version: str | None
    active: bool
    kestrel_wrote_it: bool


@dataclass
class LedgerSummary:
    db_path: str
    ml_dir: str
    sklearn_version: str
    tracks: list[TrackSummary]
    files: list[MlFile]
    confidential: bool

    @property
    def n_total(self) -> int:
        return sum(t.n_total for t in self.tracks)


# ============================================================================ reading rows
async def _latest_eval_map(session: AsyncSession, opp_ids: list[int]) -> dict[int, Evaluation]:
    """Latest evaluation per opportunity (any tier; the newest row is what the operator saw when
    they decided, which is also what record_override compares against)."""
    if not opp_ids:
        return {}
    rows = (
        await session.execute(
            select(Evaluation)
            .where(Evaluation.opportunity_id.in_(opp_ids))
            .order_by(Evaluation.opportunity_id, Evaluation.id)
        )
    ).scalars()
    latest: dict[int, Evaluation] = {}
    for ev in rows:  # ascending id: the last write per opportunity wins
        latest[ev.opportunity_id] = ev
    return latest


async def _embedded_ids(
    session: AsyncSession, opp_ids: list[int], model: str | None = None
) -> set[int]:
    """Opportunities with a vector, counting only the current embed model's when `model` is given:
    after a model switch the old vectors are not usable, and promotion_status excludes them, so
    the ledger must not count them either (D-EMBED-2 review)."""
    if not opp_ids:
        return set()
    q = select(OpportunityEmbedding.opportunity_id).where(
        OpportunityEmbedding.opportunity_id.in_(opp_ids)
    )
    if model:
        q = q.where(OpportunityEmbedding.model == model)
    rows = await session.execute(q)
    return {r[0] for r in rows}


async def _rows(
    session: AsyncSession, *, track: str | None, limit: int | None, offset: int,
    model: str | None = None,
) -> tuple[list[LedgerRow], int]:
    """The merged ledger, newest first, plus the total across both tables for the filter. Each
    table contributes at most offset+limit rows so a long history never loads whole for one page."""
    lbl_stmt = select(Label, Opportunity).join(Opportunity, Opportunity.id == Label.opportunity_id)
    ov_stmt = select(VerdictOverride, Opportunity).join(
        Opportunity, Opportunity.id == VerdictOverride.opportunity_id
    )
    lbl_count = select(func.count()).select_from(Label)
    ov_count = (
        select(func.count())
        .select_from(VerdictOverride)
        .join(Opportunity, Opportunity.id == VerdictOverride.opportunity_id)
    )
    if track:
        lbl_stmt = lbl_stmt.where(Label.track == track)
        lbl_count = lbl_count.where(Label.track == track)
        ov_stmt = ov_stmt.where(Opportunity.track == track)
        ov_count = ov_count.where(Opportunity.track == track)
    lbl_stmt = lbl_stmt.order_by(Label.created_at.desc(), Label.id.desc())
    ov_stmt = ov_stmt.order_by(VerdictOverride.created_at.desc(), VerdictOverride.id.desc())
    if limit is not None:
        lbl_stmt = lbl_stmt.limit(offset + limit)
        ov_stmt = ov_stmt.limit(offset + limit)

    labels = (await session.execute(lbl_stmt)).all()
    overrides = (await session.execute(ov_stmt)).all()
    total = (await session.execute(lbl_count)).scalar_one() + (
        await session.execute(ov_count)
    ).scalar_one()

    evals = await _latest_eval_map(session, [o.id for _, o in labels])
    embedded = await _embedded_ids(session, [o.id for _, o in labels], model)
    org_ids = sorted({o.org_id for _, o in labels + overrides if o.org_id})
    orgs: dict[int, Org] = {}
    if org_ids:
        orgs = {
            o.id: o
            for o in (await session.execute(select(Org).where(Org.id.in_(org_ids)))).scalars()
        }

    def _org_name(opp: Opportunity) -> str | None:
        org = orgs.get(opp.org_id) if opp.org_id else None
        return org.name if org else opp.org_name

    out: list[LedgerRow] = []
    for lb, opp in labels:
        ev = evals.get(opp.id)
        out.append(
            LedgerRow(
                kind="label", id=lb.id, created_at=lb.created_at or "", track=lb.track,
                opportunity_id=opp.id, title=opp.title, org=_org_name(opp),
                model_verdict=ev.verdict if ev else None, model_score=ev.score if ev else None,
                your_verdict=lb.verdict, your_score=lb.score, rationale=lb.rationale,
                source=lb.source or "manual", feeds_few_shot=opp.id in embedded,
            )
        )
    for ov, opp in overrides:
        out.append(
            LedgerRow(
                kind="override", id=ov.id, created_at=ov.created_at or "", track=opp.track,
                opportunity_id=opp.id, title=opp.title, org=_org_name(opp),
                model_verdict=ov.system_verdict, model_score=ov.system_score,
                your_verdict=ov.operator_verdict, your_score=ov.operator_score,
                rationale=ov.reason, source="agree_adjust_override", feeds_few_shot=False,
            )
        )
    out.sort(key=lambda r: (r.created_at, r.id), reverse=True)
    if limit is not None:
        out = out[offset : offset + limit]
    return out, total


def _redact_row(row: LedgerRow, mapping: dict[str, str], *, enabled: bool) -> LedgerRow:
    if not enabled:
        return row
    row.org = redact_name(row.org, mapping, enabled=True)
    row.title = redact_text(row.title, mapping, enabled=True)
    row.rationale = redact_text(row.rationale, mapping, enabled=True)
    return row


async def recent_labels(
    session: AsyncSession, *, track: str | None = None, limit: int = 50, offset: int = 0
) -> tuple[list[LedgerRow], int]:
    """One page of the ledger, newest first, with the current employer redacted when confidential
    mode is on (the same map every list view uses). Returns (rows, total for this filter)."""
    rows, total = await _rows(session, track=track, limit=limit, offset=offset)
    enabled, mapping = await confidential(session)
    return [_redact_row(r, mapping, enabled=enabled) for r in rows], total


async def export_labels(session: AsyncSession, *, redact: bool = False) -> list[dict]:
    """Every decision in both tables, oldest first, as JSON-serializable dicts. Not redacted by
    default: the old Learning tab's export was the operator's own data leaving by their own
    click. Settings > Scoring and learning passes `redact=True`, so with Confidential mode on the
    current employer is replaced exactly as in every other output (SPEC-INTERFACES 2.6)."""
    rows, _ = await _rows(session, track=None, limit=None, offset=0)
    if redact:
        enabled, mapping = await confidential(session)
        rows = [_redact_row(r, mapping, enabled=enabled) for r in rows]
    out = [asdict(r) | {"agreed": r.agreed} for r in reversed(rows)]
    return out


# ============================================================================ summary
def _agreement(rows: list[LedgerRow]) -> Agreement:
    comparable = [r for r in rows if r.agreed is not None]
    n = len(comparable)
    agreed = sum(1 for r in comparable if r.agreed)
    if n < INSUFFICIENT_N:
        return Agreement(n=n, agreed=agreed, rate=None, insufficient=True)
    return Agreement(n=n, agreed=agreed, rate=agreed / n, insufficient=False)


def _default_weights(track: str) -> dict[str, float]:
    model = ConsultingWeights() if track == "consulting" else EmploymentWeights()
    return model.model_dump()


def _artifact_loadable(ml: MlModel) -> bool:
    """Mirrors reranker.load_active_reranker's two refusal conditions, read-only."""
    if ml.sklearn_version != sklearn.__version__:
        return False
    return all(p and Path(p).exists() for p in (ml.artifact_path, ml.pca_artifact_path))


async def _reranker_state(
    session: AsyncSession, track: str, labeled_n_after: Callable[[str], int]
) -> RerankerState:
    active = (
        await session.execute(
            select(MlModel)
            .where(MlModel.track == track, MlModel.kind == "reranker", MlModel.is_active.is_(True))
            .order_by(MlModel.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    drift = (
        await session.execute(
            select(Notification.created_at)
            .where(Notification.kind == "reranker_drift", Notification.title.contains(track))
            .order_by(Notification.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if active is None:
        return RerankerState(
            active=False, trained_at=None, n_samples=None, sklearn_version=None, loadable=False,
            labels_since_training=0, last_drift_demotion_at=drift,
        )
    return RerankerState(
        active=True,
        trained_at=active.trained_at,
        n_samples=active.n_samples,
        sklearn_version=active.sklearn_version,
        loadable=_artifact_loadable(active),
        labels_since_training=labeled_n_after(active.trained_at or ""),
        last_drift_demotion_at=drift,
    )


async def _track_summary(
    session: AsyncSession, track: str, settings: Settings, weight_overrides: set[str]
) -> TrackSummary:
    from kestrel.llm.embeddings import current_model

    rows, _ = await _rows(session, track=track, limit=None, offset=0,
                          model=current_model(settings))
    n_labels = sum(1 for r in rows if r.kind == "label")
    n_overrides = len(rows) - n_labels
    learning = settings.learning

    # Few-shot: `label` rows with an embedding, the exact pool tier2._gather_few_shot selects from.
    few_shot_n = sum(1 for r in rows if r.kind == "label" and r.feeds_few_shot)
    # Calibration and the reranker: the unified judgments (most recent per opportunity, with a
    # score), as labeled_examples hands them to both.
    examples = await labeled_examples(session, track)
    embedded = await _embedded_ids(session, [ex.opportunity_id for ex in examples],
                                   current_model(settings))
    usable_for_reranker = sum(1 for ex in examples if ex.opportunity_id in embedded)

    def _labels_after(trained_at: str) -> int:
        return sum(1 for ex in examples if ex.created_at > trained_at)

    reranker = await _reranker_state(session, track, _labels_after)
    track_overrides = {k for k in weight_overrides if k.startswith(f"{WEIGHT_PREFIX}{track}.")}

    cal_counts = dict(
        (
            await session.execute(
                select(Calibration.status, func.count())
                .where(Calibration.track == track)
                .group_by(Calibration.status)
            )
        ).all()
    )

    floors = [
        Floor(
            key="few_shot", label="Few-shot examples", n=few_shot_n,
            needed=learning.min_labels_for_few_shot,
            active=few_shot_n >= learning.min_labels_for_few_shot,
            note=(
                "Calibrate-queue labels with an embedding; shown to the model as examples in "
                "Tier 2 once the floor is reached"
            ),
        ),
        Floor(
            key="calibration", label="Weight calibration", n=len(examples),
            needed=learning.min_labels_for_calibration,
            active=bool(track_overrides),
            note=(
                "judgments with a score, one per opportunity; a proposal appears at the floor "
                "and applies only when you accept it"
            ),
        ),
        Floor(
            key="reranker", label="Learned reranker", n=usable_for_reranker,
            needed=learning.min_labels_for_reranker,
            active=reranker.active and reranker.loadable,
            note=(
                "judgments with an embedding; trains at the floor and gates Tier 1 only after "
                "it passes every promotion criterion"
            ),
        ),
    ]
    current = getattr(settings.scoring.weights, track).model_dump()
    defaults = _default_weights(track)
    weights = [
        WeightRow(
            key=k, current=float(current.get(k, 0.0)), default=float(v),
            overridden=f"{WEIGHT_PREFIX}{track}.{k}" in track_overrides,
        )
        for k, v in defaults.items()
    ]
    return TrackSummary(
        track=track,
        n_labels=n_labels,
        n_overrides=n_overrides,
        agreement=_agreement(rows),
        floors=floors,
        calibrations_accepted=int(cal_counts.get("accepted", 0)),
        calibrations_proposed=int(cal_counts.get("proposed", 0)),
        weights=weights,
        reranker=reranker,
    )


async def _weight_override_keys(session: AsyncSession) -> set[str]:
    rows = await session.execute(select(Setting.key).where(Setting.key.like(f"{WEIGHT_PREFIX}%")))
    return {r[0] for r in rows}


def _kestrel_wrote(path: Path, recorded: set[str]) -> bool:
    return str(path) in recorded or any(path.match(p) for p in ML_FILE_PATTERNS)


async def _ml_files(session: AsyncSession) -> list[MlFile]:
    models = list((await session.execute(select(MlModel))).scalars())
    by_path: dict[str, MlModel] = {}
    for m in models:
        for p in (m.artifact_path, m.pca_artifact_path):
            if p:
                by_path[str(Path(p))] = m
    folder = paths.ml_dir()
    if not folder.is_dir():
        return []
    out: list[MlFile] = []
    for path in sorted(p for p in folder.iterdir() if p.is_file()):
        m = by_path.get(str(path))
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        out.append(
            MlFile(
                name=path.name, size=size, track=m.track if m else None,
                trained_at=m.trained_at if m else None,
                sklearn_version=m.sklearn_version if m else None,
                active=bool(m and m.is_active),
                kestrel_wrote_it=_kestrel_wrote(path, set(by_path)),
            )
        )
    return out


async def track_summary(
    session: AsyncSession, track: str, settings: Settings
) -> TrackSummary:
    """One track's summary, without the other track's queries or the ml folder listing."""
    return await _track_summary(session, track, settings, await _weight_override_keys(session))


async def summary(session: AsyncSession, settings: Settings) -> LedgerSummary:
    """Everything the Learning tab states: per track counts, agreement (n stated), the three
    floors, calibration state with current vs shipped weights, the reranker, and the ml folder."""
    overrides = await _weight_override_keys(session)
    tracks = [await _track_summary(session, t, settings, overrides) for t in TRACKS]
    return LedgerSummary(
        db_path=str(paths.db_path()),
        ml_dir=str(paths.ml_dir()),
        sklearn_version=sklearn.__version__,
        tracks=tracks,
        files=await _ml_files(session),
        confidential=settings.confidential_mode,
    )


# ============================================================================ writes
async def delete_label(session: AsyncSession, label_id: int, *, kind: str = "label") -> bool:
    """Delete one decision from whichever table holds it. The opportunity and its evaluations stay;
    only the operator's judgment goes. Returns False when there is no such row."""
    if kind not in KINDS:
        return False
    model = Label if kind == "label" else VerdictOverride
    row = await session.get(model, label_id)
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    return True


@dataclass
class ResetResult:
    labels_deleted: int = 0
    overrides_deleted: int = 0
    ml_files_removed: list[str] = field(default_factory=list)
    ml_models_deleted: int = 0
    calibrations_reset: int = 0
    weight_overrides_removed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def nothing(self) -> bool:
        return not any(
            (
                self.labels_deleted, self.overrides_deleted, self.ml_files_removed,
                self.ml_models_deleted, self.calibrations_reset, self.weight_overrides_removed,
            )
        )


async def reset_learning(
    session: AsyncSession,
    settings: Settings,
    *,
    labels: bool = True,
    reranker: bool = True,
    calibrations: bool = True,
) -> dict:
    """Remove what the learning loop has accumulated, by part, and report counts.

    labels: every `label` and `verdict_override` row.
    reranker: every `ml_model` row, and under paths.ml_dir() only the files Kestrel wrote (the
      reranker and PCA pickles those rows point at, plus any `reranker_*.pkl` / `features_*.pkl`
      left behind by a deleted row). Any other file in the folder is left alone.
    calibrations: accepted and still-proposed calibration rows are marked rejected (the rows stay
      as history) and the `scoring.weights.*` setting rows they wrote are removed, so the shipped
      weights apply on the next cycle.

    Opportunities, evaluations, pursuits, documents, outcomes and settings outside those keys are
    not touched. `settings` is accepted for symmetry with summary() and future floors; nothing is
    read from it today."""
    _ = settings
    result = ResetResult()

    if labels:
        for model in (Label, VerdictOverride):
            rows = list((await session.execute(select(model))).scalars())
            for row in rows:
                await session.delete(row)
            if model is Label:
                result.labels_deleted = len(rows)
            else:
                result.overrides_deleted = len(rows)

    if reranker:
        models = list((await session.execute(select(MlModel))).scalars())
        folder = paths.ml_dir().resolve()
        recorded: set[Path] = set()
        for m in models:
            for p in (m.artifact_path, m.pca_artifact_path):
                if p:
                    recorded.add(Path(p))
            await session.delete(m)
        result.ml_models_deleted = len(models)
        candidates: set[Path] = set()
        for p in recorded:
            try:
                if p.resolve().is_relative_to(folder):
                    candidates.add(p.resolve())
            except OSError:
                continue
        if folder.is_dir():
            for pattern in ML_FILE_PATTERNS:
                candidates.update(q.resolve() for q in folder.glob(pattern) if q.is_file())
        for p in sorted(candidates):
            try:
                p.unlink()
            except FileNotFoundError:
                continue
            result.ml_files_removed.append(p.name)

    if calibrations:
        now = utcnow_iso()
        cals = list(
            (
                await session.execute(
                    select(Calibration).where(Calibration.status.in_(("accepted", "proposed")))
                )
            ).scalars()
        )
        for cal in cals:
            cal.status = "rejected"
            cal.decided_at = now
        result.calibrations_reset = len(cals)
        keys = sorted(await _weight_override_keys(session))
        for key in keys:
            row = await session.get(Setting, key)
            if row is not None:
                await session.delete(row)
        result.weight_overrides_removed = keys

    await session.commit()
    return result.as_dict()
