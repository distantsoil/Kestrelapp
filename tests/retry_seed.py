"""Fictional failed roles for the Needs another try tests: the seven rows of the locked mockup
(the locked design mockup (kept privately)), written straight into the database.

`seed(...)` assumes the `_isolate_data_home` fixture (a throwaway data directory) and returns the
opportunity and quarantine ids by role title, so a test can post to the row it wants. Nothing
here touches the network or a model."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import Opportunity, Org, Quarantine
from tests import throwaway

# (role, company, tier, error as stored, raw reply, attempts, age)
ROWS = [
    ("Operations Analyst", "Larkspur Logistics", 2, "schema validation failed: verdict required",
     '{"fit": "strong", "summary": "The role centers on weekly reporting', 2,
     timedelta(minutes=20)),
    ("Supply Chain Planner", "Meadowbrook Foods", 1, "request error: ReadTimeout: timed out",
     "(no answer after 120 seconds)", 1, timedelta(minutes=23)),
    ("Clinical Reporting Analyst", "Northwind Health", 3, "HTTP 401: invalid api key",
     '{"error": {"message": "The API key was rejected by the service.", "status": 401}}', 3,
     timedelta(days=1, hours=2)),
    ("Process Improvement Lead", "Larkspur Logistics", 2, "schema validation failed: no object",
     "Sure! Based on the posting, I would rate this a 4 out of 5.", 2, timedelta(hours=2)),
    ("Inventory Coordinator", "Meadowbrook Foods", 1, "request error: ReadTimeout: timed out",
     "(no answer after 120 seconds)", 1, timedelta(hours=2, minutes=3)),
    ("Data Quality Specialist", "Northwind Health", 1, "schema validation failed: keep not bool",
     '{"keep": "probably yes"}', 2, timedelta(days=1, hours=3)),
    ("Operations Manager", "Vantage Mutual", 2, "generation loop detected",
     "the the the the", 1, timedelta(days=3)),
]


def _wipe_db() -> None:
    """Delete the database file. Refuses unless KESTREL_DATA_HOME points at a directory marked
    throwaway (tests/throwaway.py) that holds it: without that, db_path() may be the operator's
    real database. Holds outside pytest too, where no fixture isolates anything."""
    throwaway.require_throwaway_home("retry_seed")
    for suffix in ("", "-wal", "-shm"):
        p = paths.db_path().with_name(paths.db_path().name + suffix)
        p.unlink(missing_ok=True)


async def seed_async(rows=ROWS) -> dict[str, dict[str, int]]:
    await reset_engine()
    _wipe_db()
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    out: dict[str, dict[str, int]] = {}
    now = datetime.now(UTC)
    async with sm() as s:
        orgs: dict[str, Org] = {}
        for role, company, tier, error, raw, attempts, age in rows:
            org = orgs.get(company)
            if org is None:
                org = Org(name=company, normalized_name=company.lower())
                s.add(org)
                await s.flush()
                orgs[company] = org
            # Tier 1 promotes (hard rule 4); tiers 2 and 3 park the role.
            stage = "t1_pass" if tier == 1 else "quarantined"
            opp = Opportunity(
                track="employment", opp_type="job_posting", title=role, org_id=org.id,
                org_name=company, url=f"https://example.test/{role.replace(' ', '-')}",
                stage=stage, pre_score=0.5,
                description_md="A posting. " * 600,
            )
            s.add(opp)
            await s.flush()
            q = Quarantine(
                opportunity_id=opp.id, tier=tier, model="kestrel-tier2", prompt="[user]",
                raw_response=raw, error=error, attempts=attempts,
                created_at=(now - age).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            s.add(q)
            await s.flush()
            out[role] = {"opp": opp.id, "q": q.id}
        await s.commit()
    await engine.dispose()
    forget_engine()
    return out


def seed(rows=ROWS) -> dict[str, dict[str, int]]:
    return asyncio.run(seed_async(rows))
