"""Fictional rated roles and stored Model check results for the Models and Model check screen
tests. Writes through the app's own sessionmaker, so it only ever touches the test's throwaway
data directory (the autouse _isolate_data_home fixture)."""

from __future__ import annotations

import asyncio
from typing import Any

from kestrel.bench import check
from kestrel.bench.check import StepResult
from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import Label, Opportunity
from tests import throwaway


def seed_labels(n: int) -> None:
    """`n` rated roles, a third of them passed on."""
    throwaway.require_throwaway_home("model_check_seed")

    async def go() -> None:
        async with get_sessionmaker()() as s:
            for i in range(n):
                o = Opportunity(track="employment", opp_type="posting", title=f"Role {i}")
                s.add(o)
                await s.flush()
                s.add(Label(opportunity_id=o.id, track="employment", score=4.0,
                            verdict="pass" if i % 3 == 0 else "apply"))
            await s.commit()

    asyncio.run(go())


def result(step: str, model: str, agree: float | None, *, n: int = 38, online: bool = False,
           ms: float = 1000.0, backend: str = "local") -> StepResult:
    return StepResult(step, model, online, n, agree, ms, 0.95, backend=backend)


def seed_check(*, changes: int = 1, n: int = 38, online_best: bool = False,
               best_model: str = "gemma3:12b",
               at: str = "2026-10-12T10:00:00+00:00") -> dict[str, Any]:
    """Store one finished check. With `changes` 1 Rating has a better model (gemma3:12b, or an
    online model when `online_best`); Quick screen always keeps its model."""
    throwaway.require_throwaway_home("model_check_seed")
    best = ("vendor/big-model", 0.60, True, "frontier") if online_best else (
        best_model, 0.43, False, "local")
    rating = [result("rating", "cur-rate", 0.37, n=n, ms=2000.0)]
    if changes:
        rating.append(result("rating", best[0], best[1], n=n, ms=4400.0, online=best[2],
                             backend=best[3]))
    quick = [result("quick_screen", "cur-quick", 0.80, n=n, ms=900.0)]
    rows = [*rating, *quick]
    verdicts = [
        check.asdict(check.recommend_step("rating", rows, "cur-rate", "local")),
        check.asdict(check.recommend_step("quick_screen", rows, "cur-quick", "local")),
    ]
    summary, count = check.build_summary(
        [check.recommend_step("rating", rows, "cur-rate", "local"),
         check.recommend_step("quick_screen", rows, "cur-quick", "local")])
    stored = {
        "id": "seeded", "at": at, "summary": summary, "changes": count, "steps": verdicts,
        "results": [check._row(r) for r in rows], "answer_key_roles": n, "rated_roles": n,
    }

    async def go() -> None:
        async with get_sessionmaker()() as s:
            await check._store_check(s, stored)
            await s.commit()

    asyncio.run(go())
    return stored
