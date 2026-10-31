"""Fictional rows for the Today tests: the roles of the locked mockup
(the locked design mockup (kept privately)), written straight into the database.

`seed(...)` assumes the `_isolate_data_home` fixture (a throwaway data directory) and returns the
ids by short name, so a test can post to the row it wants. Nothing here touches the network or a
model. Roles found "today" are first seen a few minutes ago, so they are today's on any clock
except in the first minutes after local midnight."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.base import iso
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import (
    DocMaster,
    Evaluation,
    Opportunity,
    Org,
    Pursuit,
    Run,
    ScrapeRun,
    Source,
)
from tests import throwaway

DESC = (
    "We are hiring to improve how work moves across distribution centers. You will own weekly "
    "reporting, forecasting and SQL analysis, and lead process improvement with site managers. "
    "Experience with Power BI is a plus; mysql alone is not enough."
)

# (key, title, company, place, remote_type, pay, estimate, score, verdict, kind)
TODAY = [
    ("strong", "Senior Operations Analyst", "Larkspur Logistics", "Austin, TX", "hybrid",
     (118000, 142000), False, 4.6, "apply"),
    ("good", "Process Improvement Lead", "Northwind Health", "Remote (US)", "remote",
     (157000, 185000), False, 4.4, "apply"),
    ("est", "Operations Program Manager", "Harbor Point Analytics", "Remote (US)", "remote",
     (140000, 165000), True, 4.1, "apply"),
    ("maybe", "Inventory Planning Manager", "Cedar Grove Supply", "Reno, NV", "onsite",
     (98000, 120000), False, 3.4, "consider"),
    ("nofit", "Warehouse Shift Supervisor", "Ironbridge Supply", "Reno, NV", "onsite",
     None, False, 1.8, "pass"),
]


def _local_midnight_safe(delta: timedelta) -> str:
    return iso(datetime.now(UTC) - delta)


def _guard() -> None:
    throwaway.require_throwaway_home("today_seed")


def _opp(orgs, src, key, title, company, place, remote, pay, estimate, **kw) -> Opportunity:
    base = dict(
        track="employment", opp_type="job_posting", org_id=orgs[company].id, source_id=src.id,
        title=title, org_name=company, url=f"https://example.test/{key}", stage="t2_done",
        first_seen_at=_local_midnight_safe(timedelta(minutes=5)), location_raw=place,
        remote_type=remote, description_md=DESC,
        comp_min=pay[0] if pay else None, comp_max=pay[1] if pay else None,
        comp_currency="USD" if pay else None, comp_interval="year" if pay else None,
        comp_source=("inferred" if estimate else "posted") if pay else "none",
    )
    base.update(kw)
    return Opportunity(**base)


def _ev(opp, score, verdict, **kw) -> Evaluation:
    base = dict(
        opportunity_id=opp.id, tier=2, track=opp.track, verdict=verdict, verdict_model=verdict,
        score=score, weighted_score=score, needs_review=False,
        created_at=_local_midnight_safe(timedelta(minutes=3)),
        blocks={
            "a_role_summary": {
                "what_this_actually_is": (
                    f"Leads improvement work at {opp.org_name}. It is mostly reporting and "
                    "process change."
                )
            },
            "b_fit": {
                "matches": [
                    {"requirement": "SQL and forecasting", "strength": "partial",
                     "evidence": "They want SQL and forecasting, which you list."},
                    {"requirement": "Process improvement", "strength": "strong",
                     "evidence": "Your process improvement work lines up with their main ask."},
                ],
                "gaps": [],
            },
        },
    )
    base.update(kw)
    return Evaluation(**base)


async def seed(*, with_run: bool = True) -> dict[str, int]:
    """The mockup's day: five roles found today (one with an estimated pay), one that only had its
    quick screen, one the rules set aside, an undecided role from an earlier day, a shortlisted
    one, and three applications needing action. Returns the ids by key."""
    _guard()
    await reset_engine()
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    ids: dict[str, int] = {}
    today = date.today()
    async with sm() as s:
        src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
        s.add(src)
        orgs: dict[str, Org] = {}

        def org_for(company: str) -> Org:
            if company not in orgs:
                orgs[company] = Org(name=company, normalized_name=company.lower())
                s.add(orgs[company])
            return orgs[company]

        for company in {r[2] for r in TODAY} | {
            "Vantage Mutual", "Ironbridge Supply", "Harbor Point Analytics", "Pinecrest Clinics",
        }:
            org_for(company)
        await s.flush()
        if with_run:
            s.add(Run(name="pipeline", status="done", mode="local",
                      started_at=_local_midnight_safe(timedelta(hours=2)),
                      finished_at=_local_midnight_safe(timedelta(hours=1)),
                      counts={"tier0": {"seen": 8, "pass": 6, "reject": 2, "geo_uncertain": 0}}))
            s.add(ScrapeRun(source_id=src.id, status="ok",
                            started_at=_local_midnight_safe(timedelta(hours=2)),
                            finished_at=_local_midnight_safe(timedelta(hours=1)),
                            counts={"fetched": 8, "new": 7, "reseen": 1}))
        made: dict[str, Opportunity] = {}
        for key, title, company, place, remote, pay, est, *_ in TODAY:
            made[key] = _opp(orgs, src, key, title, company, place, remote, pay, est)
        made["quick"] = _opp(orgs, src, "quick", "Business Analyst II", "Vantage Mutual",
                             "Remote (US)", "remote", None, False, stage="t1_pass")
        made["rule"] = _opp(orgs, src, "rule", "Night Guard", "Ironbridge Supply", "Reno, NV",
                            "onsite", None, False, stage="t0_reject",
                            stage_reason="blocker:geo: Reno")
        made["backlog"] = _opp(orgs, src, "backlog", "Director of Enterprise Applications",
                               "Harbor Point Analytics", "Remote (US)", "remote", (150000, 170000),
                               False, first_seen_at=_local_midnight_safe(timedelta(days=2)))
        made["listed"] = _opp(orgs, src, "listed", "Customer Operations Lead", "Vantage Mutual",
                              "Remote (US)", "remote", (110000, 130000), False,
                              first_seen_at=_local_midnight_safe(timedelta(days=2)))
        made["interview"] = _opp(orgs, src, "interview", "Chief Operations Lead",
                                 "Pinecrest Clinics", "Remote (US)", "remote", None, False,
                                 first_seen_at=_local_midnight_safe(timedelta(days=9)))
        made["quiet"] = _opp(orgs, src, "quiet", "Supply Chain Planning Lead",
                             "Ironbridge Supply", "Reno, NV", "onsite", None, False,
                             first_seen_at=_local_midnight_safe(timedelta(days=12)))
        s.add_all(made.values())
        await s.flush()
        for row in TODAY:
            s.add(_ev(made[row[0]], row[7], row[8]))
        s.add(_ev(made["quick"], None, "keep", tier=1, blocks={}, weighted_score=None))
        s.add(_ev(made["backlog"], 3.9, "apply"))
        s.add(_ev(made["listed"], 4.2, "apply"))
        await s.flush()
        s.add_all([
            Pursuit(opportunity_id=made["listed"].id, track="employment", status="shortlisted"),
            Pursuit(opportunity_id=made["interview"].id, track="employment",
                    status="interviewing", next_action="Interview",
                    next_action_at=today.isoformat(), last_activity_at=iso(datetime.now(UTC))),
            Pursuit(opportunity_id=made["quiet"].id, track="employment", status="applied",
                    next_action="Follow up", applied_at=_local_midnight_safe(timedelta(days=24)),
                    next_action_at=(today - timedelta(days=3)).isoformat(),
                    last_activity_at=_local_midnight_safe(timedelta(days=24))),
        ])
        s.add(DocMaster(track="employment", name="Resume", doc_kind="resume", is_active=True,
                        skill_groups=[{"key": "tools", "label": "Tools",
                                       "items": ["SQL", "Power BI", "Forecasting", "Lean"]}]))
        await s.commit()
        ids = {k: v.id for k, v in made.items()}
        by_opp = {v: k for k, v in ids.items()}
        for opp_id, pid in (await s.execute(select(Pursuit.opportunity_id, Pursuit.id))).all():
            ids[f"pursuit_{by_opp[opp_id]}"] = pid
    await engine.dispose()
    forget_engine()
    return ids
