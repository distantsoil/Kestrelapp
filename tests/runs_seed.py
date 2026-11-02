"""Fictional runs for the Runs page tests: a history of twelve runs (the four ways one reads), the
failed roles of `retry_seed`, and, on request, a run that is going right now. Written straight into
the database of a throwaway data directory (the `_isolate_data_home` fixture), so nothing touches
the network or a model. Returns the ids a test needs."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db.base import iso
from kestrel.db.engine import forget_engine, make_async_engine
from kestrel.db.models import Evaluation, Opportunity, Run, ScrapeRun, Source, Task
from tests import clock_seed, retry_seed

# (hours ago it started, minutes it took, status, mode, scrape counts, tier1, tier2, note)
HISTORY = [
    (
        26,
        11,
        "done",
        "local",
        {"completed": 5, "errors": 0, "new": 38},
        {"completed": 38, "errors": 0, "per_minute": 16.0},
        {"completed": 9, "errors": 0, "per_minute": 2.4},
        None,
    ),
    (
        50,
        9,
        "done",
        "local",
        {"completed": 5, "errors": 1, "new": 29},
        {"completed": 29, "errors": 0, "per_minute": 14.0},
        {"completed": 6, "errors": 0, "per_minute": 2.1},
        None,
    ),
    (
        74,
        6,
        "stopping",
        "local",
        {"completed": 3, "errors": 0, "new": 22},
        {"completed": 22, "errors": 0, "per_minute": 12.0},
        {"completed": 4, "errors": 0, "per_minute": 2.0},
        "Stopped by you at Rating 4 of 7.",
    ),
    (75, 1, "failed", "local", {"completed": 0, "errors": 5, "new": 0}, {}, {}, None),
    (
        98,
        12,
        "done",
        "hosted",
        {"completed": 5, "errors": 0, "new": 41},
        {"completed": 41, "errors": 0, "per_minute": 30.0},
        {"completed": 11, "errors": 1, "per_minute": 4.0},
        None,
    ),
    (
        122,
        10,
        "interrupted",
        "local",
        {"completed": 2, "errors": 0, "new": 5},
        {},
        {},
        "Closed at startup: the app stopped before this run finished.",
    ),
] + [
    (
        146 + 24 * i,
        8,
        "done",
        "local",
        {"completed": 5, "errors": 0, "new": 10 + i},
        {"completed": 10 + i, "errors": 0, "per_minute": 11.0},
        {"completed": 3 + i, "errors": 0, "per_minute": 1.9},
        None,
    )
    for i in range(6)
]


async def seed_async(*, active: bool = False) -> dict:
    roles = await retry_seed.seed_async()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    out: dict = {"runs": [], "roles": roles}
    async with sm() as s:
        sources = [
            Source(
                track="employment",
                kind="jobspy",
                name=f"Search {i}",
                enabled=True,
                config={"site_name": ["indeed"], "search_term": term},
            )
            for i, term in enumerate(("Operations Analyst", "Supply Chain Planner", "Data Analyst"))
        ]
        s.add_all(sources)
        await s.flush()
        for hours, minutes, status, mode, scrape, t1, t2, note in HISTORY:
            start = now - timedelta(hours=hours)
            run = Run(
                name="desktop",
                status=status,
                mode=mode,
                started_at=iso(start),
                finished_at=iso(start + timedelta(minutes=minutes)),
                notes=note,
                counts={"scrape": scrape, "tier1": t1, "tier2": t2},
            )
            s.add(run)
            await s.flush()
            out["runs"].append(run.id)
            if hours == 26:  # the latest finished run: its searches and ratings
                for i, src in enumerate(sources):
                    s.add(
                        ScrapeRun(
                            source_id=src.id,
                            status="partial" if i == 1 else "ok",
                            started_at=iso(start + timedelta(minutes=i)),
                            finished_at=iso(start + timedelta(minutes=i + 1)),
                            error="HTTP 429" if i == 1 else None,
                            counts={"new": 12 - i},
                        )
                    )
                for i in range(3):
                    opp = Opportunity(
                        track="employment",
                        opp_type="job_posting",
                        title=f"Rated role {i}",
                        url=f"https://example.test/rated-{i}",
                        stage="t2_done",
                        org_name="Larkspur Logistics",
                    )
                    s.add(opp)
                    await s.flush()
                    s.add(
                        Evaluation(
                            opportunity_id=opp.id,
                            tier=2,
                            track="employment",
                            verdict="apply",
                            verdict_model="apply",
                            score=4.2,
                            weighted_score=4.2,
                            created_at=iso(start + timedelta(minutes=6)),
                        )
                    )
        if active:
            start = clock_seed.ago_within(
                timedelta(minutes=8), clock_seed.local_day_start(now),
                horizon=timedelta(minutes=10), now=now)
            run = Run(
                name="desktop",
                status="running",
                mode="local",
                started_at=iso(start),
                counts={"scrape": {"completed": 2, "errors": 0, "new": 11}},
            )
            s.add(run)
            await s.flush()
            out["active_run"] = run.id
            for i, src in enumerate(sources[:2]):
                s.add(
                    ScrapeRun(
                        source_id=src.id,
                        status="ok",
                        started_at=iso(start + timedelta(minutes=i)),
                        finished_at=iso(start + timedelta(minutes=i + 1)),
                        counts={"new": 5 + i},
                    )
                )
            s.add(
                Task(
                    type="scrape",
                    status="pending",
                    payload={"source_id": sources[2].id},
                    created_at=iso(start),
                )
            )
            opps = []
            for i in range(4):
                opp = Opportunity(
                    track="employment",
                    opp_type="job_posting",
                    title=f"Queued role {i}",
                    url=f"https://example.test/q-{i}",
                    stage="t0_pass",
                    org_name="Northwind Health",
                )
                s.add(opp)
                await s.flush()
                opps.append(opp.id)
                s.add(
                    Task(
                        type="t1",
                        status="pending",
                        payload={"opportunity_id": opp.id},
                        created_at=iso(start + timedelta(minutes=2)),
                    )
                )
        await s.commit()
    await engine.dispose()
    forget_engine()
    return out


def seed(*, active: bool = False) -> dict:
    return asyncio.run(seed_async(active=active))
