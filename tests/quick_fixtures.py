"""Shared plumbing for the quick-round tests (issue 171): a throwaway database holding fictional
evaluated roles, finished runs and answers. Cloud-safe: no model, no network. Assumes the
`_isolate_data_home` fixture."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import (
    Evaluation,
    Label,
    Opportunity,
    OpportunityEmbedding,
    Org,
    Run,
    Setting,
    Source,
)

DIMS = ["role_fit", "domain_match", "company_context", "comp_logistics", "trajectory"]
FINISHED = "2026-11-01T10:00:00.000000+00:00"


def run(fn):
    """Run `fn(session)` on a fresh engine and return its result."""

    async def go():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                return await fn(s)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def seed(n_roles: int = 12, *, track: str = "employment", finished_runs: int = 0,
         n_labeled: int = 0, embed_labeled: bool = False) -> list[int]:
    """n_roles evaluated roles with descriptions (so Calibrate may ask about them), the first
    n_labeled already answered, and `finished_runs` finished run rows. `embed_labeled` gives the
    answered roles a current-model embedding, so they are in the few-shot pool."""

    async def prepare():
        await reset_engine()
        for suffix in ("", "-wal", "-shm"):
            paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
        migrate.run_migrations()

    asyncio.run(prepare())
    forget_engine()

    async def fill(s):
        org = Org(name="Acme Freight", normalized_name="acme freight")
        src = Source(track=track, kind="greenhouse", name="b", enabled=True)
        s.add_all([org, src])
        await s.flush()
        ids = []
        for i in range(n_roles):
            opp = Opportunity(
                track=track, opp_type="job_posting", org_id=org.id, source_id=src.id,
                title=f"Operations Analyst {i}", org_name="Acme Freight",
                description_md=f"Own weekly reporting {i}.", stage="t2_done",
                location_raw="Columbus, OH", remote_type="hybrid", comp_min=90000,
                comp_max=110000, comp_currency="USD", comp_interval="yearly",
                url=f"https://example.test/{i}", first_seen_at="2026-10-20T00:00:00.000000+00:00",
            )
            s.add(opp)
            await s.flush()
            ids.append(opp.id)
            s.add(Evaluation(
                opportunity_id=opp.id, tier=2, track=track, verdict="consider",
                score=3.4 + (i % 4) * 0.2, weighted_score=3.4, blocks={},
                dimensions={k: {"score": 1 + (i + j) % 5, "why": f"reason {k}"}
                            for j, k in enumerate(DIMS)},
            ))
            if i < n_labeled:
                s.add(Label(opportunity_id=opp.id, track=track, score=4.0, verdict="consider",
                            source="quick", created_at=f"2026-10-0{1 + i % 9}T00:00:00+00:00"))
                if embed_labeled:
                    from kestrel.config import get_settings
                    from kestrel.llm.embeddings import current_model

                    s.add(OpportunityEmbedding(
                        opportunity_id=opp.id, model=current_model(get_settings()), dims=4,
                        vector=b"\x00\x00\x80\x3f" * 4))
        for _ in range(finished_runs):
            s.add(Run(name="desktop", status="done", mode="local", finished_at=FINISHED))
        await s.commit()
        return ids

    ids = run(fill)
    forget_engine()
    return ids


def add_finished_runs(n: int) -> None:
    async def go(s):
        for _ in range(n):
            s.add(Run(name="desktop", status="done", mode="local", finished_at=FINISHED))
        await s.commit()

    run(go)


def client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def labels() -> list[Label]:
    async def go(s):
        return list((await s.execute(select(Label).order_by(Label.id))).scalars())

    return run(go)


def setting(key: str):
    async def go(s):
        row = await s.get(Setting, key)
        return row.value if row is not None else None

    return run(go)
