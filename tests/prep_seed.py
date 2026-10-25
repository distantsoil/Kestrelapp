"""Fictional roles, stories and research for the Interview prep tests: the sample data of the
locked mockup (the locked design mockup (kept privately)), written straight into the
database.

`seed_async()` assumes the `_isolate_data_home` fixture (a throwaway data directory) and returns
the opportunity and pursuit ids by company. Nothing here touches the network or a model."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import (
    Contact,
    Evaluation,
    Opportunity,
    Org,
    OrgResearch,
    Pursuit,
    Source,
    Story,
)
from tests import retry_seed

STORIES = [
    ("Throughput turnaround at two sites", ["turnaround", "influence_without_authority"], 4,
     "Two of our distribution centers missed same-day targets most weeks, and nobody owned the "
     "fix.",
     "I mapped the pick and pack flow with the shift leads and moved slotting to match order "
     "volume.",
     "Throughput rose 22% in two quarters, and both sites hit target 9 weeks out of 10.",
     "pp_throughput"),
    ("Rebuilding the demand forecast in SQL", ["technical_judgment", "prioritization"], 3,
     "Our forecast lived in a spreadsheet one person understood, and we kept overstocking.",
     "I rebuilt it in SQL with weekly seasonality and walked the buyers through each change.",
     "Excess inventory fell 18% in a year.", "pp_forecast"),
    ("Weekly review for the VP of Operations", ["difficult_stakeholder"], 2,
     "The VP got three versions of the same numbers and trusted none of them.",
     "I built one weekly report and ran the review myself, with site leads in the room.",
     "The review became the one place decisions got made, and it still runs.", "pp_review"),
    ("Onboarding two new analysts", ["team_building"], 1,
     "We hired two analysts in one month and had no training plan.",
     "I wrote a four-week plan and paired each of them with a site for their first project.",
     "Both shipped their first report in week three.", "pp_onboard"),
]

# (question, why, proof key, category)
QUESTIONS = [
    ("Tell us about a time you improved how work moved through a warehouse or site.",
     "Their main ask: process improvement across distribution centers", "pp_throughput",
     "behavioral"),
    ("How have you used SQL or forecasting to change a decision?",
     "Listed requirement: SQL and forecasting", "pp_forecast", "technical"),
    ("Describe working with a senior leader who did not trust the numbers.",
     "They want one trusted weekly review", "pp_review", "behavioral"),
    ("Our domain is supply chain. How quickly can you get up to speed?",
     "Gap: supply chain domain, adjacent from your last role", None, "gap_probe"),
    ("Tell me about a project that did not go to plan.", "Common question", None,
     "behavioral"),
]


async def seed_async(*, with_pack: bool = False, consulting: bool = False) -> dict[str, dict]:
    await reset_engine()
    retry_seed._wipe_db()
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    out: dict[str, dict] = {}
    track = "consulting" if consulting else "employment"
    when = [(date.today() + timedelta(days=5), "10:00", "Interview (Panel interview)"),
            (date.today() + timedelta(days=11), "13:30", "Interview (Phone screen)")]
    roles = [("Senior Operations Analyst", "Larkspur Logistics"),
             ("Process Improvement Lead", "Northwind Health")]
    async with sm() as s:
        src = Source(track=track, kind="greenhouse", name="board", enabled=True)
        s.add(src)
        await s.flush()
        for k, (role, company) in enumerate(roles):
            org = Org(name=company, normalized_name=company.lower())
            s.add(org)
            await s.flush()
            opp = Opportunity(
                track=track, opp_type="job_posting", org_id=org.id, source_id=src.id, title=role,
                org_name=company, url=f"https://example.test/{k}", stage="t2_done",
                description_md="A posting. " * 50,
            )
            s.add(opp)
            await s.flush()
            day, clock, step = when[k]
            pur = Pursuit(
                opportunity_id=opp.id, track=track, status="interviewing",
                next_action_at=f"{day.isoformat()}T{clock}:00+00:00", next_action=step,
            )
            s.add(pur)
            await s.flush()
            s.add(Evaluation(
                opportunity_id=opp.id, tier=2, track="employment", verdict="apply",
                verdict_model="apply", score=4.0, weighted_score=4.0, needs_review=False,
                dimensions={},
                blocks={
                    "a_role_summary": {"lead_with": "Lead process improvement across three "
                                                    "distribution centers and own the weekly "
                                                    "operations review."},
                    "b_fit": {"gaps": [
                        {"requirement": "Supply chain domain: adjacent, from your last role",
                         "severity": "addressable",
                         "mitigation": "Lead with the distribution center work."},
                        {"requirement": "Six Sigma Green Belt preferred", "severity": "fatal",
                         "mitigation": "Point to your Lean results."},
                        {"requirement": "Noise item", "severity": "noise", "mitigation": None},
                    ]},
                    "f_interview": [
                        {"likely_question": q, "why_asked": why, "proof_point_key": key,
                         "star_r": {}} for q, why, key, _c in QUESTIONS
                    ],
                },
                blockers=[], legitimacy={"status": "clean", "signals": []},
            ))
            s.add(OrgResearch(
                org_id=org.id, summary_md="A regional operator.",
                signals=[{"category": "news", "statement": "Opened a second distribution "
                          "center on Oct 14", "source_index": 1, "url": "https://x/news"}],
                unknowns=["Who owns the weekly operations numbers now?",
                          "Which of the three sites is furthest from target today, and why?",
                          "What would a strong first 90 days look like to you?",
                          "How is the second distribution center changing the team?"],
                angle="Lead with throughput.", sources=[], insufficient_sources=False,
            ))
            s.add(Contact(org_id=org.id, name="Maya Ruiz", title="VP of Operations",
                          contact_type="hiring_manager"))
            out[company] = {"opp": opp.id, "pursuit": pur.id}
        for title, themes, used, sit, act, res, key in STORIES:
            s.add(Story(title=title, themes=themes, use_count=used, situation=sit, action=act,
                        result=res, proof_point_key=key, is_canonical=True,
                        operator_edited=True))
        await s.commit()
    await engine.dispose()
    forget_engine()
    if with_pack:
        from kestrel.interview.prep import assemble_prep_pack

        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            for ids in out.values():
                await assemble_prep_pack(s, ids["opp"])
        await engine.dispose()
        forget_engine()
    return out


def seed(**kw) -> dict[str, dict]:
    return asyncio.run(seed_async(**kw))
