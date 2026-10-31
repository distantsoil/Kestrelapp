"""Fictional offers for the Offers screen tests: the sample data of the locked mockup
(the locked design mockup (kept privately)), written straight into the database.

`seed(...)` assumes the `_isolate_data_home` fixture (a throwaway data directory) and returns the
pursuit and offer ids by company, so a test can post to the one it wants. Nothing here touches the
network or a model. It also writes a profile.yml with a pay floor and target, so the card's pay
band has something to draw against."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import (
    CompObservation,
    EngagementOffer,
    Offer,
    Opportunity,
    Org,
    Pursuit,
    Source,
)
from tests import retry_seed

PROFILE = """\
employment:
  employment_terms:
    comp_floor_usd: 120000
    comp_target_usd: 150000
consulting:
  consulting_terms:
    day_rate_floor: 450
    day_rate_target: 600
"""

# (company, role, status, base, bonus %, signing, equity, equity words, pto, start, days left,
#  level, reports to, where, posted low, posted high)
EMPLOYMENT = [
    ("Larkspur Logistics", "Senior Operations Analyst", "written", 136000, 10, 5000, 0, "None",
     20, 40, 9, "Senior Analyst", "VP of Operations", "Austin, TX · Hybrid, 3 days",
     118000, 142000),
    ("Northwind Health", "Process Improvement Lead", "verbal", 162000, 8, 0, 40000,
     "RSUs, $40,000 over 4 years", 18, 60, 5, "Lead", "Director of Operations", "Remote (US)",
     157000, 185000),
]
THIRD = ("Harbor Point Analytics", "Operations Program Manager", "verbal", 150000, 5, 10000, 0,
         "None", 22, 75, 18, "Manager", "COO", "Remote (US)", 140000, 165000)
# (company, role, status, day rate, days, weeks, payment, expenses, renewal, exclusivity, ip,
#  notice, days left, posted low, posted high)
CONSULTING = [
    ("Meadowbrook Foods", "Weekly reporting dashboard build", "written", 600, 30, 6, "Net 30",
     "Not covered", "Option: 3 months of support", "None", "Client owns what you build",
     "2 weeks", 7),
    ("Tidewater Freight Group", "Order-to-cash process documentation", "verbal", 560, 40, 10,
     "Net 45", "Covered", "None", "None during the engagement", "Client owns what you build",
     "1 week", 11),
]


def _day(offset: int) -> str:
    return (date.today() + timedelta(days=offset)).isoformat()


async def seed_async(*, third: bool = False, consulting: bool = True, past: bool = True,
                     market: bool = True, profile: bool = True) -> dict[str, dict[str, int]]:
    await reset_engine()
    retry_seed._wipe_db()
    migrate.run_migrations()
    if profile:
        paths.config_dir().mkdir(parents=True, exist_ok=True)
        (paths.config_dir() / "profile.yml").write_text(PROFILE, encoding="utf-8")
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    out: dict[str, dict[str, int]] = {}
    async with sm() as s:
        srcs = {}
        for track in ("employment", "consulting"):
            srcs[track] = Source(track=track, kind="greenhouse", name=f"{track}-src", enabled=True)
            s.add(srcs[track])
        await s.flush()

        async def pursuit(company, role, track, lo=None, hi=None, status="shortlisted"):
            org = Org(name=company, normalized_name=company.lower())
            s.add(org)
            await s.flush()
            opp = Opportunity(
                track=track,
                opp_type="job_posting" if track == "employment" else "contract_posting",
                source_id=srcs[track].id, org_id=org.id, org_name=company, title=role,
                stage="t3_pass", comp_min=lo, comp_max=hi,
            )
            s.add(opp)
            await s.flush()
            p = Pursuit(opportunity_id=opp.id, track=track, status=status)
            s.add(p)
            await s.flush()
            return p

        rows = list(EMPLOYMENT) + ([THIRD] if third else [])
        for (co, role, status, base, bonus, sign, eq, eqd, pto, start, left, level, rep, where,
             lo, hi) in rows:
            p = await pursuit(co, role, "employment", lo, hi, "offer")
            o = Offer(
                pursuit_id=p.id, base=base, bonus_target_pct=bonus, signing=sign,
                equity_value_estimate=eq or None, equity_description=eqd, pto_days=pto,
                start_date=_day(start), deadline=_day(left), level_title=level, reports_to=rep,
                location_policy=where, status=status,
            )
            s.add(o)
            await s.flush()
            out[co] = {"pursuit": p.id, "offer": o.id}
        if consulting:
            for (co, role, status, rate, days, weeks, pay, exp, renew, excl, ip, notice,
                 left) in CONSULTING:
                p = await pursuit(co, role, "consulting", status="negotiating")
                o = EngagementOffer(
                    pursuit_id=p.id, day_rate=rate, days_committed=days, duration_weeks=weeks,
                    payment_terms=pay, expenses=exp, renewal_terms=renew, exclusivity=excl,
                    ip_terms=ip, notice_period=notice, deadline=_day(left), status=status,
                )
                s.add(o)
                await s.flush()
                out[co] = {"pursuit": p.id, "offer": o.id}
        if past:
            for co, role, status, base, when in (
                ("Meadowbrook Foods", "Operations Analyst", "accepted", 104000, "2021-03-02"),
                ("Larkspur Logistics", "Business Analyst", "declined", 96000, "2021-06-14"),
            ):
                p = await pursuit(co + " (past)", role, "employment", status="accepted")
                s.add(Offer(pursuit_id=p.id, base=base, status=status, received_at=when))
        if market:
            for i in range(12):  # twelve similar roles: enough for a range (five are needed)
                s.add(CompObservation(
                    normalized_title="senior operations analyst", geo_bucket="TX",
                    min=108000 + i * 2500, max=118000 + i * 3000, currency="USD", interval="year",
                ))
            for i in range(3):  # three: too few
                s.add(CompObservation(
                    normalized_title="process improvement lead", geo_bucket="US",
                    min=140000 + i * 1000, max=170000 + i * 1000, currency="USD",
                    interval="year",
                ))
        await s.commit()
    await engine.dispose()
    forget_engine()
    return out


def seed(**kw) -> dict[str, dict[str, int]]:
    return asyncio.run(seed_async(**kw))
