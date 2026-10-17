"""Model history and comparison (SPEC-MODULES 7, SPEC.md 8.6): which model has been doing the work
at each tier, and how it has been doing, computed from the operator's own `evaluation` rows.

Every number here carries the sample size it rests on (hard rule 6). Agreement with the operator's
own labels is the only quality signal that is not self-reported by the model; any agreement rate
over fewer than five labeled evaluations is returned as None with `insufficient=True` and the n
stated, never as a number that reads as solid.

Cost is only known for hosted sends: `frontier_transmission` rows carry what the provider billed,
joined here by `evaluation_id`. A local model has no cost column to sum, so its cost is None and is
rendered as "local", never as $0.00. A hosted send whose provider did not report a cost is rendered
as unknown, never as free.

`kestrel bench` (kestrel/bench/harness.py) prints its comparison table and persists nothing, so
`bench_summary` reports the state of the gold sets it would run against rather than inventing a
stored result."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import iso
from kestrel.db.enums import TRACKS
from kestrel.db.models import Evaluation, FrontierTransmission, Label, Opportunity
from kestrel.web.redact import confidential, redact_name, redact_text

_MIN_N = 5  # hard rule 6: a rate over fewer than five observations is labeled insufficient
_UNKNOWN_MODEL = "(unknown model)"
_UNKNOWN_VERSION = "(unknown version)"


# ============================================================================= rows
@dataclass
class ModelRow:
    """One (tier, model, prompt_version) group of evaluations. `n` is the number of evaluations;
    `n_labeled` is how many of those have an operator label on the same opportunity and track,
    which is the denominator for `agreement_rate` and `mean_abs_score_error`."""

    tier: int
    model: str
    prompt_version: str
    n: int
    mean_score: float | None
    verdicts: dict[str, int]
    mean_latency_ms: float | None
    mean_prompt_tokens: float | None
    mean_completion_tokens: float | None
    n_labeled: int
    agreement_rate: float | None
    mean_abs_score_error: float | None
    insufficient: bool
    hosted: bool
    n_hosted_sends: int = 0
    n_costed_sends: int = 0
    cost_usd: float | None = None  # None: local (no cost to sum) or hosted with no reported cost

    @property
    def cost_label(self) -> str:
        if not self.hosted:
            return "local"
        if self.cost_usd is None:
            return "unknown"
        return f"${self.cost_usd:.4f}"


@dataclass
class HistoryRow:
    """One recent evaluation, with the opportunity it judged (redacted under confidential mode)."""

    evaluation_id: int
    created_at: str
    opportunity_id: int
    title: str | None
    org: str | None
    track: str
    tier: int
    model: str
    prompt_version: str
    verdict: str | None
    score: float | None
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int
    hosted: bool
    cost_usd: float | None
    operator_verdict: str | None  # the operator's label on this opportunity, if any
    agreed: bool | None  # None: no label (or no verdict on one side) to compare against

    @property
    def cost_label(self) -> str:
        if not self.hosted:
            return "local"
        return "unknown" if self.cost_usd is None else f"${self.cost_usd:.4f}"


@dataclass
class GoldSetStatus:
    track: str
    present: bool
    n_items: int
    placeholder: bool  # the shipped invented set, not the operator's own regenerated labels
    insufficient: bool


@dataclass
class BenchStatus:
    """`kestrel bench` prints its table and stores nothing, so there is no stored run to show.
    What can be stated honestly: that fact, and the state of the gold sets a run would use."""

    persisted: bool
    note: str
    goldsets: list[GoldSetStatus] = field(default_factory=list)


# ============================================================================= helpers
def _mean(values: list[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _since_iso(since_days: int | None) -> str | None:
    if since_days is None or since_days <= 0:
        return None
    return iso(datetime.now(UTC) - timedelta(days=since_days))


async def _evaluations(
    session: AsyncSession, *, track: str | None, since_days: int | None
) -> list[Evaluation]:
    stmt = select(Evaluation)
    if track:
        stmt = stmt.where(Evaluation.track == track)
    cutoff = _since_iso(since_days)
    if cutoff:
        stmt = stmt.where(Evaluation.created_at >= cutoff)
    return list((await session.execute(stmt)).scalars().all())


async def _latest_labels(session: AsyncSession) -> dict[tuple[int, str], Label]:
    """(opportunity_id, track) -> the operator's most recent label. One label per evaluation keeps
    `n_labeled` equal to a count of evaluations rather than of pairings, so the n reads plainly."""
    rows = (await session.execute(select(Label).order_by(Label.created_at, Label.id))).scalars()
    latest: dict[tuple[int, str], Label] = {}
    for lbl in rows:
        latest[(lbl.opportunity_id, lbl.track)] = lbl  # later rows overwrite earlier ones
    return latest


async def _transmissions(
    session: AsyncSession, evaluation_ids: list[int]
) -> dict[int, list[FrontierTransmission]]:
    if not evaluation_ids:
        return {}
    rows = (
        await session.execute(
            select(FrontierTransmission).where(
                FrontierTransmission.evaluation_id.in_(evaluation_ids)
            )
        )
    ).scalars()
    out: dict[int, list[FrontierTransmission]] = defaultdict(list)
    for t in rows:
        if t.evaluation_id is not None:
            out[t.evaluation_id].append(t)
    return out


def _agreement(
    pairs: list[tuple[Evaluation, Label]],
) -> tuple[int, float | None, float | None, bool]:
    """(n_labeled, agreement_rate, mean_abs_score_error, insufficient). Rates are None under five
    labeled evaluations; the n is still returned so the caller can state it."""
    n = len(pairs)
    if n < _MIN_N:
        return n, None, None, True
    comparable = [(ev, lbl) for ev, lbl in pairs if ev.verdict and lbl.verdict]
    agree = sum(1 for ev, lbl in comparable if ev.verdict == lbl.verdict)
    rate = (agree / len(comparable)) if comparable else None
    errs = [
        abs(ev.score - lbl.score)
        for ev, lbl in pairs
        if ev.score is not None and lbl.score is not None
    ]
    return n, rate, _mean(errs), False


# ============================================================================= summary
async def model_summary(
    session: AsyncSession, *, track: str | None = None, since_days: int | None = None
) -> list[ModelRow]:
    """Evaluations grouped by (tier, model, prompt_version), sorted by tier then n descending.
    `track=None` means both tracks; `since_days=None` means all time."""
    evaluations = await _evaluations(session, track=track, since_days=since_days)
    if not evaluations:
        return []
    labels = await _latest_labels(session)
    sends = await _transmissions(session, [ev.id for ev in evaluations])

    groups: dict[tuple[int, str, str], list[Evaluation]] = defaultdict(list)
    for ev in evaluations:
        key = (ev.tier, ev.model or _UNKNOWN_MODEL, ev.prompt_version or _UNKNOWN_VERSION)
        groups[key].append(ev)

    rows: list[ModelRow] = []
    for (tier, model, version), evs in groups.items():
        pairs = [
            (ev, labels[(ev.opportunity_id, ev.track)])
            for ev in evs
            if (ev.opportunity_id, ev.track) in labels
        ]
        n_labeled, rate, mae, insufficient = _agreement(pairs)
        group_sends = [t for ev in evs for t in sends.get(ev.id, ())]
        costed = [t.cost_usd for t in group_sends if t.cost_usd is not None]
        rows.append(
            ModelRow(
                tier=tier,
                model=model,
                prompt_version=version,
                n=len(evs),
                mean_score=_mean([ev.score for ev in evs if ev.score is not None]),
                verdicts=dict(Counter(ev.verdict or "(none)" for ev in evs)),
                mean_latency_ms=_mean([float(ev.latency_ms or 0) for ev in evs]),
                mean_prompt_tokens=_mean([float(ev.prompt_tokens or 0) for ev in evs]),
                mean_completion_tokens=_mean([float(ev.completion_tokens or 0) for ev in evs]),
                n_labeled=n_labeled,
                agreement_rate=rate,
                mean_abs_score_error=mae,
                insufficient=insufficient,
                hosted=bool(group_sends),
                n_hosted_sends=len(group_sends),
                n_costed_sends=len(costed),
                cost_usd=sum(costed) if costed else None,
            )
        )
    rows.sort(key=lambda r: (r.tier, -r.n, r.model, r.prompt_version))
    return rows


def best_by_tier(rows: list[ModelRow]) -> dict[int, ModelRow | None]:
    """Per tier, the row with the highest agreement rate among rows with enough labels, or None
    when no row at that tier has five labeled evaluations yet. The caller states the n either
    way (`max_labeled_by_tier`)."""
    out: dict[int, ModelRow | None] = {}
    for row in rows:
        current = out.get(row.tier)
        if row.agreement_rate is None:
            out.setdefault(row.tier, None)
            continue
        if current is None or (current.agreement_rate or 0.0) < row.agreement_rate:
            out[row.tier] = row
    return out


def max_labeled_by_tier(rows: list[ModelRow]) -> dict[int, int]:
    out: dict[int, int] = {}
    for row in rows:
        out[row.tier] = max(out.get(row.tier, 0), row.n_labeled)
    return out


# ============================================================================= history
async def recent_history(
    session: AsyncSession, *, limit: int = 100, track: str | None = None
) -> list[HistoryRow]:
    """The most recent evaluations, newest first, with the opportunity's title and org redacted
    under confidential mode the same way every list view redacts them."""
    stmt = (
        select(Evaluation, Opportunity)
        .join(Opportunity, Opportunity.id == Evaluation.opportunity_id)
        .order_by(Evaluation.created_at.desc(), Evaluation.id.desc())
        .limit(max(1, limit))
    )
    if track:
        stmt = stmt.where(Evaluation.track == track)
    pairs = (await session.execute(stmt)).all()
    if not pairs:
        return []
    enabled, mapping = await confidential(session)
    labels = await _latest_labels(session)
    sends = await _transmissions(session, [ev.id for ev, _ in pairs])

    out: list[HistoryRow] = []
    for ev, opp in pairs:
        lbl = labels.get((ev.opportunity_id, ev.track))
        agreed: bool | None = None
        if lbl is not None and ev.verdict and lbl.verdict:
            agreed = ev.verdict == lbl.verdict
        ev_sends = sends.get(ev.id, [])
        costed = [t.cost_usd for t in ev_sends if t.cost_usd is not None]
        out.append(
            HistoryRow(
                evaluation_id=ev.id,
                created_at=ev.created_at,
                opportunity_id=opp.id,
                title=redact_text(opp.title, mapping, enabled=enabled),
                org=redact_name(opp.org_name, mapping, enabled=enabled),
                track=ev.track,
                tier=ev.tier,
                model=ev.model or _UNKNOWN_MODEL,
                prompt_version=ev.prompt_version or _UNKNOWN_VERSION,
                verdict=ev.verdict,
                score=ev.score,
                latency_ms=ev.latency_ms or 0,
                prompt_tokens=ev.prompt_tokens or 0,
                completion_tokens=ev.completion_tokens or 0,
                hosted=bool(ev_sends),
                cost_usd=sum(costed) if costed else None,
                operator_verdict=lbl.verdict if lbl is not None else None,
                agreed=agreed,
            )
        )
    return out


# ============================================================================= bench
_BENCH_NOTE = (
    "`kestrel bench` prints its comparison table to the terminal and stores no rows, so there is "
    "no stored benchmark run to show here. The gold sets it would run against are listed below; "
    "the live overlap between stored evaluations and operator labels is on the Bench page."
)


def _count_jsonl(path: Path) -> int:
    try:
        with path.open(encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def bench_summary(gold_dir: Path | str | None = None) -> BenchStatus:
    """What can be said about benchmarking without a stored run: per track, whether a gold set
    exists, how many items it holds, and whether it is still the shipped placeholder (the same
    test `kestrel bench` applies before it prints agreement). Never raises."""
    from kestrel import paths

    try:
        directory = Path(gold_dir) if gold_dir is not None else paths.evals_dir()
    except OSError:
        directory = None
    statuses: list[GoldSetStatus] = []
    for track in TRACKS:
        if directory is None:
            statuses.append(GoldSetStatus(track, False, 0, False, True))
            continue
        path = directory / f"goldset.{track}.jsonl"
        regenerated = directory / f"goldset.{track}.regenerated.jsonl"
        present = path.is_file()
        n = _count_jsonl(path) if present else 0
        statuses.append(
            GoldSetStatus(
                track=track,
                present=present,
                n_items=n,
                placeholder=present and not regenerated.exists(),
                insufficient=n < _MIN_N,
            )
        )
    return BenchStatus(persisted=False, note=_BENCH_NOTE, goldsets=statuses)
