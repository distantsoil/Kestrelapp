"""The quick round (issue 171, D-UI-272, D-LEARN-2): yes or no calibration cards.

Three small jobs live here, none of which calls a model or touches the network:

1. Saving an answer. A Quick answer is one `label` row with `source="quick"`: Top pick 5, Yes 4,
   Not for me 2, the verdict derived from the score exactly as a Detailed rating derives it.
   "Not sure" writes nothing, so the role stays in the queue. Undo is `ledger.delete_label`.
2. The offer: when Today's strip asks for a round. Its state is a few `setting` rows (never
   browser storage): the toggle, the cadence, and per track the finished-run count at which the
   strip may show again. Snooze, Stop asking and Undo only move those rows.
3. The meter: the next unlock (15, 30, 200) with the real counts.

Kestrel's score is never read here for the round: the queue order is `active.rank_uncertain`,
unchanged (served through `uncertain_cache`), and the answer path stores the operator's word
and nothing else."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.config import Settings
from kestrel.db.enums import TRACKS
from kestrel.db.models import Run, Setting
from kestrel.learning import uncertain_cache
from kestrel.learning.active import UncertainItem
from kestrel.learning.capture import labeled_examples, record_label
from kestrel.pipeline.scoring import verdict_from_score

SOURCE = "quick"
ROUND_SIZE = 10
MIN_OFFER = 5  # fewer roles than this and the strip does not show
SNOOZE_RUNS = 3
WEEK = timedelta(days=7)

# The answer words, in the order of the keys (Left, Right, Up, Down).
ANSWER_SCORES: dict[str, float] = {"top": 5.0, "yes": 4.0, "no": 2.0}
UNSURE = "unsure"
ANSWERS = (*ANSWER_SCORES, UNSURE)

CADENCES: dict[str, str] = {
    "every3": "After the first run, then every 3 runs",
    "every": "After every run",
    "weekly": "Once a week",
}
DEFAULT_CADENCE = "every3"

ENABLED_KEY = "ui.quick.enabled"
CADENCE_KEY = "ui.quick.cadence"


def _runs_key(track: str) -> str:
    return f"ui.quick.next_runs.{track}"


def _at_key(track: str) -> str:
    return f"ui.quick.next_at.{track}"


def _taken_key(track: str) -> str:
    """When the last round was taken: {"runs": finished runs then, "at": the time}, so a change of
    "How often" re-anchors the pending wait to the new choice (D-LEARN-10)."""
    return f"ui.quick.taken.{track}"


# ------------------------------------------------------------------------------ the answers
async def save_answer(
    session: AsyncSession,
    *,
    opportunity_id: int,
    track: str,
    answer: str,
    settings: Settings,
    commit: bool = True,
):
    """Save one Quick answer as a label and return it, or None for "Not sure" (nothing is
    written). The caller has already checked the role exists and is on `track`. With
    commit=False the label is only flushed, for a caller that commits it with the offer."""
    if answer == UNSURE:
        return None
    score = ANSWER_SCORES[answer]  # KeyError for an unknown word: the route validates first
    return await record_label(
        session,
        opportunity_id=opportunity_id,
        track=track,
        score=score,
        verdict=verdict_from_score(score, track, settings),
        source=SOURCE,
        commit=commit,
    )


# ------------------------------------------------------------------------------ the queue
async def round_items(
    session: AsyncSession, track: str, settings: Settings, *, seen: list[int],
    limit: int = ROUND_SIZE,
) -> list[UncertainItem]:
    """The next roles for a round: the active-learning order, minus the roles already shown in
    this round (a "Not sure" has no label, so the queue alone would offer it again)."""
    skip = set(seen)
    # The same order as rank_uncertain, cached until a new evaluation, label or embedding
    # (the strip asks on every poll; uncertain_cache).
    pool = await uncertain_cache.ranked(session, track, settings, limit=limit + len(skip))
    return [u for u in pool if u.opportunity.id not in skip][:limit]


async def answer_count(session: AsyncSession, track: str) -> int:
    """The answers that count toward the thresholds: the same figure Detailed and the nav use."""
    return len(await labeled_examples(session, track))


async def examples_count(session: AsyncSession, track: str, settings: Settings) -> int | None:
    """How many roles the Tier 2 prompt can draw examples from now: the roles with a `label` row
    and a current-model embedding, the pool `tier2._gather_few_shot` selects from. None when
    "use examples" is off in Settings, so no copy promises a re-rank that will not happen."""
    if not settings.learning.use_examples:
        return None
    from kestrel.db.models import Label, OpportunityEmbedding
    from kestrel.llm.embeddings import current_model

    return int(await session.scalar(
        select(func.count(func.distinct(Label.opportunity_id)))
        .join(OpportunityEmbedding, OpportunityEmbedding.opportunity_id == Label.opportunity_id)
        .where(Label.track == track, OpportunityEmbedding.model == current_model(settings))
    ) or 0)


# ------------------------------------------------------------------------------- the meter
@dataclass
class Meter:
    n: int
    target: int | None  # the next unlock, None once past the last
    lead: str  # the bold part: "6 of 15"
    rest: str  # the plain part after it
    pct: int

    @property
    def text(self) -> str:
        return f"{self.lead}{self.rest}"


def meter_for(n: int, settings: Settings, examples: int | None = -1) -> Meter:
    """The next unlock with the real counts. One function, so the Today strip and the Quick tab
    can never word it differently (D-UI-273). `examples` is `examples_count`: the few-shot step
    is reached by that pool, not by every answer, and is left out when it is None (examples off).
    -1 (not given) counts it by `n`."""
    lr = settings.learning
    shots = n if examples == -1 else examples
    steps = (
        *(((lr.min_labels_for_few_shot, shots,
            "At {t}, Kestrel ranks new roles using your examples."),) if shots is not None
          else ()),
        (lr.min_labels_for_calibration, n, "At {t}, Kestrel suggests a scoring tune-up."),
        (lr.min_labels_for_reranker, n, "At {t}, Kestrel learns your ranking."),
    )
    for target, have, line in steps:
        if have < target:
            return Meter(
                have, target, f"{have} of {target}", " answers. " + line.format(t=target),
                min(100, have * 100 // target),
            )
    return Meter(n, None, str(n), " answers. Kestrel is learning your ranking from them.", 100)


# -------------------------------------------------------------------------------- the offer
@dataclass
class OfferState:
    enabled: bool = True
    next_runs: int = 1  # the strip may show once this many runs have finished
    next_at: str | None = None  # ...and, for "Once a week", not before this time

    def as_params(self) -> dict[str, str]:
        return {
            "enabled": "1" if self.enabled else "0",
            "runs": str(self.next_runs),
            "at": self.next_at or "",
        }


async def _get(session: AsyncSession, key: str):
    row = await session.get(Setting, key)
    return row.value if row is not None else None


async def _put(session: AsyncSession, key: str, value) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value


async def finished_runs(session: AsyncSession) -> int:
    """Runs that finished normally (status "done", D-LEARN-8). A failed, interrupted or stopped
    run brought no new roles worth asking about, so it does not move the offer."""
    stmt = select(func.count()).select_from(Run).where(
        Run.finished_at.is_not(None), Run.status == "done")
    return int((await session.execute(stmt)).scalar_one() or 0)


async def cadence(session: AsyncSession) -> str:
    value = await _get(session, CADENCE_KEY)
    return value if isinstance(value, str) and value in CADENCES else DEFAULT_CADENCE


async def read_state(session: AsyncSession, track: str) -> OfferState:
    enabled = await _get(session, ENABLED_KEY)
    runs = await _get(session, _runs_key(track))
    at = await _get(session, _at_key(track))
    return OfferState(
        enabled=enabled is not False,
        next_runs=runs if isinstance(runs, int) and not isinstance(runs, bool) and runs >= 0 else 1,
        next_at=at if isinstance(at, str) and at else None,
    )


async def write_state(
    session: AsyncSession, track: str, state: OfferState, *, commit: bool = True
) -> None:
    await _put(session, ENABLED_KEY, bool(state.enabled))
    await _put(session, _runs_key(track), int(state.next_runs))
    await _put(session, _at_key(track), state.next_at or None)
    if commit:
        await session.commit()


def _parse(value: str | None) -> datetime | None:
    try:
        when = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    return when.replace(tzinfo=UTC) if when is not None and when.tzinfo is None else when


async def is_due(session: AsyncSession, track: str, *, now: datetime | None = None) -> bool:
    """Whether the offer may show, apart from the roles and the run state: switched on, a run has
    finished, and the snooze (runs, and for the weekly cadence time) has passed."""
    state = await read_state(session, track)
    if not state.enabled:
        return False
    runs = await finished_runs(session)
    if runs < 1 or runs < state.next_runs:
        return False
    waiting_until = _parse(state.next_at)
    return waiting_until is None or (now or datetime.now(UTC)) >= waiting_until


async def mark_taken(
    session: AsyncSession, track: str, *, now: datetime | None = None
) -> OfferState:
    """A round was started (an answer was saved): the next offer follows the chosen cadence."""
    runs = await finished_runs(session)
    when = now or datetime.now(UTC)
    state = await read_state(session, track)
    _follow(state, await cadence(session), runs, when)
    await _put(session, _taken_key(track), {"runs": runs, "at": when.isoformat()})
    await write_state(session, track, state)
    return state


def _follow(state: OfferState, kind: str, runs: int, when: datetime) -> None:
    """The wait after a round taken at `runs` finished runs and time `when`, for cadence `kind`."""
    if kind == "every":
        state.next_runs, state.next_at = runs + 1, None
    elif kind == "weekly":
        state.next_runs, state.next_at = runs, (when + WEEK).isoformat()
    else:
        state.next_runs, state.next_at = runs + SNOOZE_RUNS, None


async def snooze(session: AsyncSession, track: str) -> OfferState:
    """"Ask after the next 3 runs": the strip waits for three more finished runs."""
    state = await read_state(session, track)
    state.next_runs, state.next_at = await finished_runs(session) + SNOOZE_RUNS, None
    await write_state(session, track, state)
    return state


async def stop_asking(session: AsyncSession, track: str) -> OfferState:
    state = await read_state(session, track)
    state.enabled = False
    await write_state(session, track, state)
    return state


async def set_enabled(session: AsyncSession, track: str, enabled: bool) -> OfferState:
    """The Calibrate toggle. Turning it back on makes the strip eligible at the next finished
    run, never retroactively for the one that already passed. The toggle is global, so the
    wait is set for every track, not only the one the toggle was pressed on (D-UI-281)."""
    state = await read_state(session, track)
    state.enabled = enabled
    if enabled:
        nxt = max(1, await finished_runs(session) + 1)
        for other in TRACKS:
            if other != track:  # a later snooze on the other track still stands
                theirs = await read_state(session, other)
                theirs.enabled, theirs.next_runs = True, max(theirs.next_runs, nxt)
                await write_state(session, other, theirs, commit=False)
        state.next_runs, state.next_at = nxt, None
    await write_state(session, track, state)
    return state


async def set_cadence(session: AsyncSession, kind: str) -> bool:
    """Save "How often". A different choice re-anchors each track's pending wait to the last
    round taken, as if that round had been taken under the new choice (D-LEARN-10): a week's
    wait does not outlive a switch to "After every run", and the reverse. A track with no round
    taken yet, or with the offer switched off, keeps its state."""
    if kind not in CADENCES:
        return False
    changed = await cadence(session) != kind
    await _put(session, CADENCE_KEY, kind)
    if changed:
        for track in TRACKS:
            taken = await _get(session, _taken_key(track))
            state = await read_state(session, track)
            got = taken if isinstance(taken, dict) else {}
            at, runs = got.get("at"), got.get("runs")
            when = _parse(at) if isinstance(at, str) else None
            if not state.enabled or when is None or not isinstance(runs, int):
                continue
            _follow(state, kind, runs, when)
            await write_state(session, track, state, commit=False)
    await session.commit()
    return True
