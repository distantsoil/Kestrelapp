"""How precise is the found-board smart filter? A read-only report (#22, D-SRC-8).

D-SRC-8 measured recall (98% on 270 roles) but never precision. This reads the database and, for
roles from boards found in a search, splits them into the fast lane (the filter kept them) and the
background lane (it did not), then reports how each lane's scored roles came out:

  - fast-lane precision = fits / scored roles in the fast lane
  - lane miss rate      = fits that sat in the background lane / all fits

A fit is a role whose final verdict is apply, pursue or consider; a role scored `pass` (Tier 2) or
`kill` (Tier 1) is not a fit. A role with no final verdict yet, or one the rules set aside, is
counted as not scored and left out of every rate. This is a historical lane report: D-SRC-13
rechecks waiting rows and scores surviving matches locally only. Fewer than five observations are
insufficient and never rendered as a percentage (hard rule 6). It only reads (SELECT).

    uv run python -m kestrel.evals.smart_filter_report [--track employment|consulting]
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Evaluation, Opportunity, Source
from kestrel.ingest.board_discovery import DISCOVERED
from kestrel.pipeline.lane import LANE_REASON

MIN_N = 5  # hard rule 6: fewer observations than this is "insufficient"
FIT_VERDICTS = frozenset({"apply", "pursue", "consider"})
NOT_FIT_VERDICTS = frozenset({"pass", "kill"})


def _share(num: int, den: int) -> float | None:
    return num / den if den else None


def _lane(verdicts: list[str | None]) -> dict[str, Any]:
    """`verdicts` are the final verdicts (None when there is none) of one lane's roles."""
    fits = sum(1 for v in verdicts if v in FIT_VERDICTS)
    nots = sum(1 for v in verdicts if v in NOT_FIT_VERDICTS)
    scored = fits + nots
    return {
        "roles": len(verdicts),
        "scored": scored,
        "unscored": len(verdicts) - scored,
        "fits": fits,
        "not_fits": nots,
        "precision": _share(fits, scored),
        "precision_sufficient": scored >= MIN_N,
    }


async def build_report(session: AsyncSession, track: str) -> dict[str, Any]:
    opps = (
        await session.execute(
            select(Opportunity.id, Opportunity.stage_reason)
            .join(Source, Source.id == Opportunity.source_id)
            .where(Opportunity.track == track, Source.origin == DISCOVERED)
        )
    ).all()
    ids = [o.id for o in opps]
    final: dict[int, tuple[int, int, str | None]] = {}
    if ids:
        evals = (
            await session.execute(
                select(Evaluation.opportunity_id, Evaluation.tier, Evaluation.id,
                       Evaluation.verdict)
                .where(Evaluation.opportunity_id.in_(ids))
            )
        ).all()
        for oid, tier, eid, verdict in evals:
            if oid not in final or (tier, eid) > final[oid][:2]:
                final[oid] = (tier, eid, verdict)
    fast: list[str | None] = []
    lane: list[str | None] = []
    for o in opps:
        verdict = final.get(o.id, (0, 0, None))[2]
        (lane if LANE_REASON in (o.stage_reason or "") else fast).append(verdict)
    f, b = _lane(fast), _lane(lane)
    fits = f["fits"] + b["fits"]
    return {
        "track": track,
        "fast": f,
        "background": b,
        "miss_rate": _share(b["fits"], fits),
        "miss_sufficient": fits >= MIN_N,
        "fits_total": fits,
    }


def _pct(value: float | None, n: int, noun: str, ok: bool) -> str:
    if not ok or value is None:
        return f"insufficient ({n} {noun}, need at least {MIN_N})"
    return f"{round(value * 100)}% ({n} {noun})"


def render(r: dict[str, Any]) -> str:
    f, b = r["fast"], r["background"]
    out = [f"Smart-filter precision, {r['track']} track (found-board roles only)", ""]
    for label, lane in (("Fast lane (kept by the filter)", f),
                        ("Background lane (not kept)", b)):
        out.append(
            f"{label}: {lane['roles']} roles, {lane['scored']} scored, "
            f"{lane['unscored']} not scored yet or set aside by the rules"
        )
        out.append(f"  fits {lane['fits']}, not a fit {lane['not_fits']}")
    out.append("")
    out.append(
        "Fast-lane precision: "
        + _pct(f["precision"], f["scored"], "scored", f["precision_sufficient"])
    )
    out.append(
        "Background-lane fit share: "
        + _pct(b["precision"], b["scored"], "scored", b["precision_sufficient"])
    )
    out.append(
        f"Lane miss rate: {b['fits']} of {r['fits_total']} fits sat in the background lane"
        + (
            f" ({round(r['miss_rate'] * 100)}%)"
            if r["miss_sufficient"] and r["miss_rate"] is not None
            else f"; miss rate: insufficient (need at least {MIN_N} fits)"
        )
    )
    if b["unscored"]:
        out.append(
            "Legacy waiting roles are rechecked before local scoring; mismatches stay unscored."
        )
    return "\n".join(out)


async def _main(track: str) -> str:
    from kestrel import paths
    from kestrel.db.engine import make_async_engine

    if not paths.db_path().exists():
        return "No Kestrel database found yet, so there is nothing to report."
    engine = make_async_engine(paths.db_path())
    try:
        async with AsyncSession(engine) as s:
            return render(await build_report(s, track))
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--track", choices=("employment", "consulting"), default="employment")
    args = ap.parse_args(argv)
    print(asyncio.run(_main(args.track)))  # noqa: T201 -- this script's own output


if __name__ == "__main__":
    main()
