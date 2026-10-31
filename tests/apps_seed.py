"""Fictional applications for the Applications screen tests: the rows of the locked mockup
(the locked design mockup (kept privately)), written straight into the database
with dates relative to today.

`seed(track)` assumes the `_isolate_data_home` fixture (a throwaway data directory) and returns
the pursuit ids by role title. Nothing here touches the network or a model."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import Evaluation, Opportunity, Org, Outcome, Pursuit, StatusEvent
from kestrel.services.tracks import BOTH, forget_cache, save_enabled
from tests.retry_seed import _wipe_db

# (title, org, place, remote, comp_min, comp_max, comp_interval, score, status, priority,
#  applied (days ago), next action, due (days from today), closed (days ago))
EMPLOYMENT = [
    ("Product Operations Manager", "Ridgeline Software", "San Francisco, CA", "hybrid",
     150000, 175000, None, 4.3, "shortlisted", 1, None, "Apply", 2, None),
    ("Senior Data Analyst", "Brightwater Payments", "Remote (US)", "remote",
     120000, 140000, None, 4.0, "preparing", 0, None, "Finish the cover letter", 1, None),
    ("Senior Operations Analyst", "Larkspur Logistics", "Austin, TX", "hybrid",
     118000, 142000, None, 4.6, "applied", 1, 12, "Follow up", -5, None),
    ("Process Improvement Lead", "Northwind Health", "Remote (US)", "remote",
     157000, 185000, None, 4.4, "applied", 0, 4, "Follow up", 3, None),
    ("Supply Chain Planning Lead", "Vantage Mutual", "Seattle, WA", "hybrid",
     125000, 160000, None, 3.8, "screening", 0, 14, "Follow up", -2, None),
    ("Director of Enterprise Applications", "Harbor Point Analytics", "Remote (US)", "remote",
     149000, 175000, None, 3.9, "interviewing", 0, 21, "Interview (Video call)", 4, None),
    ("Operations Program Manager", "Cascadia Regional Health", "Portland, OR", "hybrid",
     140000, 165000, None, 4.1, "final", 0, 28, "Interview (On site)", 6, None),
    ("Business Operations Manager", "Meadowbrook Foods", "Dayton, OH", "hybrid",
     105000, 128000, None, 4.0, "offer", 0, 38, "Reply to the offer", 5, None),
    ("IT Systems Manager", "Cedar Grove Health", "Denver, CO", "hybrid",
     110000, 130000, None, 3.2, "rejected", 0, 44, None, None, 19),
    ("Customer Operations Lead", "Pinecrest Clinics", "Remote (US)", "remote",
     None, None, None, 3.1, "ghosted", 0, 52, None, None, 15),
    ("Inventory Planning Manager", "Ironbridge Supply", "Reno, NV", "onsite",
     98000, 120000, None, 3.4, "withdrawn", 0, 36, None, None, 12),
]
CONSULTING = [
    ("Weekly reporting dashboard build, 6 weeks", "Meadowbrook Foods", "Remote (US)", "remote",
     552, 650, "day", 4.5, "shortlisted", 1, None, "Reach out to the operations director", 1, None),
    ("Interim operations support, 3 months", "Pinecrest Clinics", "Remote (US)", "remote",
     600, 700, "day", 3.9, "researching", 0, None, None, None, None),
    ("Order-to-cash process documentation", "Tidewater Freight Group", "Columbus, OH", "hybrid",
     510, 600, "day", 4.0, "outreach_sent", 0, 12, "Follow up", -5, None),
    ("KPI framework refresh", "Larkspur Logistics", "Remote (US)", "remote",
     575, 575, "day", 4.2, "conversation", 0, 9, "Call (Discovery call)", 5, None),
    ("Warehouse metrics cleanup, 3 weeks", "Cedar Grove Supply", "Dayton, OH", "hybrid",
     488, 575, "day", 3.2, "proposal_sent", 0, 17, "Follow up on proposal", -2, None),
    ("Systems selection advisory", "Vantage Mutual", "Remote (US)", "remote",
     640, 640, "day", 3.6, "won", 0, 33, "Agree the start date", 3, None),
    ("Reporting audit, 2 weeks", "Ironbridge Supply", "Remote (US)", "remote",
     500, 500, "day", 3.0, "lost", 0, 39, None, None, 15),
    ("Board reporting pack, 4 weeks", "Harbor Point Analytics", "Remote (US)", "remote",
     None, None, None, 3.3, "no_response", 0, 45, None, None, 20),
]


async def seed_async(track: str = "employment", both: bool = False) -> dict[str, int]:
    await reset_engine()
    _wipe_db()
    migrate.run_migrations()
    forget_cache()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    out: dict[str, int] = {}
    today = date.today()
    rows = EMPLOYMENT if track == "employment" else CONSULTING
    async with sm() as s:
        if both or track == "consulting":
            await save_enabled(s, BOTH)
        orgs: dict[str, Org] = {}
        for (title, company, place, remote, lo, hi, interval, score, status, pri, applied_ago,
             action, due, closed_ago) in rows:
            org = orgs.get(company)
            if org is None:
                org = Org(name=company, normalized_name=company.lower())
                s.add(org)
                await s.flush()
                orgs[company] = org
            opp = Opportunity(
                track=track, opp_type="job_posting" if track == "employment" else "engagement",
                title=title, org_id=org.id, org_name=company, location_raw=place,
                remote_type=remote, comp_min=lo, comp_max=hi, comp_interval=interval,
                comp_currency="USD", url=f"https://example.test/{title.replace(' ', '-')}",
                stage="t2_done", description_md="A posting. " * 50,
            )
            s.add(opp)
            await s.flush()
            s.add(Evaluation(opportunity_id=opp.id, tier=2, track=track, verdict="apply",
                             score=score, weighted_score=score))
            applied = (
                f"{(today - timedelta(days=applied_ago)).isoformat()}T00:00:00+00:00"
                if applied_ago else None
            )
            last = (today - timedelta(days=(closed_ago if closed_ago is not None else 1)))
            p = Pursuit(
                opportunity_id=opp.id, track=track, status=status, priority=pri,
                applied_at=applied, next_action=action,
                next_action_at=(today + timedelta(days=due)).isoformat() if due is not None
                else None,
                last_activity_at=f"{last.isoformat()}T09:00:00+00:00",
                closed_at=f"{last.isoformat()}T09:00:00+00:00" if closed_ago is not None else None,
            )
            if action and action.startswith(("Interview", "Call")):
                p.next_action_at = f"{(today + timedelta(days=due)).isoformat()}T14:00"
            s.add(p)
            await s.flush()
            s.add(StatusEvent(pursuit_id=p.id, from_status=None, to_status="shortlisted"))
            if status != "shortlisted":
                s.add(StatusEvent(pursuit_id=p.id, from_status="shortlisted", to_status=status))
            if closed_ago is not None:
                s.add(Outcome(pursuit_id=p.id, opportunity_id=opp.id, final_status=status,
                              furthest_stage="screening", days_to_first_response=9))
            out[title] = p.id
        await s.commit()
    await engine.dispose()
    forget_engine()
    return out


def seed(track: str = "employment", both: bool = False) -> dict[str, int]:
    return asyncio.run(seed_async(track, both))
