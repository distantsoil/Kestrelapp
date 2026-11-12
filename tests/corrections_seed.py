"""A fictional Profile with four corrections, for the Profile > Corrections tests
(the locked design mockup (kept privately)).

Builds on tests/profile_seed.py (the saved profile), then writes three roles (one archived),
their reads, two stories (one drafted by Kestrel, one the person wrote) and one correction of each
kind the tab words differently.

Assumes the `_isolate_data_home` fixture. Nothing here touches the network or a model."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db.engine import forget_engine, make_async_engine
from kestrel.db.models import Evaluation, Opportunity, RequirementCorrection, Story
from kestrel.learning import corrections as corr
from tests import profile_seed

REQ_CARRIER = "Carrier and vendor management"
REQ_LEAN = "Lean or Six Sigma certification"
REQ_TEAMS = "Managing teams of 20 or more"
REQ_PLAN = "Quarterly planning with an executive team"
REQS = (REQ_CARRIER, REQ_LEAN, REQ_TEAMS, REQ_PLAN)


def _blocks() -> dict:
    return {"b_fit": {
        "matches": [
            {"requirement": REQ_CARRIER, "strength": "strong",
             "evidence": "Cut freight cost across carriers."},
            {"requirement": REQ_TEAMS, "strength": "strong",
             "evidence": "Ran a night shift turnaround."},
        ],
        "gaps": [
            {"requirement": REQ_LEAN, "severity": "material", "mitigation": "Ask about it."},
            {"requirement": REQ_PLAN, "severity": "minor", "mitigation": "Ask about it."},
        ]}}


async def seed_async() -> dict[str, int]:
    master_id = await profile_seed.seed_async()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    out: dict[str, int] = {"master": master_id}
    async with sm() as s:
        roles = {}
        for key, title, org, archived in (
            ("a", "Senior Operations Analyst", "Larkspur Logistics", False),
            ("b", "Warehouse Operations Manager", "Pinegate Supply", True),
            ("c", "Operations Program Lead", "Westbrook Health", False),
        ):
            o = Opportunity(
                track="employment", opp_type="job_posting", title=title, org_name=org,
                stage="t2_done", stage_reason="t2 consider",
                archived_at="2026-01-01T00:00:00.000000+00:00" if archived else None)
            s.add(o)
            await s.flush()
            s.add(Evaluation(opportunity_id=o.id, tier=2, track="employment", score=3.6,
                             verdict="consider", blocks=_blocks()))
            roles[key] = o.id
        drafted = Story(title="Night shift turnaround", action="Rebuilt the shift plan.",
                        result="Cut overtime.", is_canonical=True, operator_edited=False)
        mine = Story(title="Lean program across two sites", action="Ran the program.",
                     result="Both sites certified.", is_canonical=True, operator_edited=True)
        s.add_all([drafted, mine])
        await s.commit()
        out.update({f"role_{k}": v for k, v in roles.items()})
        out["drafted"], out["mine"] = drafted.id, mine.id
        # Oldest first: the list shows the newest on top.
        await corr.save_correction(
            s, opportunity_id=roles["c"], requirement_text=REQ_PLAN, kestrel_state="gap",
            reason="some", basis="none")
        await corr.save_correction(
            s, opportunity_id=roles["b"], requirement_text=REQ_TEAMS, kestrel_state="met",
            reason="overstated", story_id=drafted.id, basis="quoted",
            note="The story says 30 people. It was 12.")
        await corr.save_correction(
            s, opportunity_id=roles["a"], requirement_text=REQ_LEAN, kestrel_state="gap",
            reason="all", story_id=mine.id, basis="quoted")
        await corr.save_correction(
            s, opportunity_id=roles["a"], requirement_text=REQ_CARRIER, kestrel_state="met",
            reason="not_me", basis="inferred",
            note="I chose carriers once; someone else ran them.")
        row = (await s.execute(RequirementCorrection.__table__.select().where(
            RequirementCorrection.requirement_text == REQ_LEAN))).first()
        out["lean_id"] = row.id
        lean = await s.get(RequirementCorrection, row.id)
        lean.agrees_now = True
        await s.commit()
        for c in (await s.execute(RequirementCorrection.__table__.select())).all():
            out[f"corr_{c.requirement_text}"] = c.id
    await engine.dispose()
    forget_engine()
    return out


def seed() -> dict[str, int]:
    return asyncio.run(seed_async())
