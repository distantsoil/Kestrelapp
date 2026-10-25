"""Fictional data for the Tailor screen tests: the sample resume and posting of the locked mockup
(the locked design mockup (kept privately)), written straight into the database.

`seed(...)` assumes the `_isolate_data_home` fixture (a throwaway data directory) and returns the
ids a test needs. Nothing here touches the network, a browser or a model."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import (
    DocLayout,
    DocMaster,
    DocRender,
    DocSection,
    DocSlot,
    DocVariant,
    Evaluation,
    Opportunity,
    Org,
    Pursuit,
    Source,
)
from tests import throwaway

POSTING = """\
## About the role
Larkspur Logistics moves goods for regional grocers and food makers across Texas.

## What you will do
- Lead process improvement across sites, from mapping the flow to measuring the result.
- Build KPI dashboards that site managers actually open each morning.
- Use SQL and forecasting to predict demand and cut excess inventory.
- Present to senior leaders every month.

## What we are looking for
- Solid supply chain domain knowledge, ideally in food or grocery.
- Experience with a WMS and ERP systems.

## Nice to have
- A Six Sigma Green Belt or similar training.
"""

SUMMARY = [
    {"key": "sum_a", "text": "Operations analyst who turns messy spreadsheets into weekly "
                             "reports a team can act on."},
    {"key": "sum_b", "text": "Operations analyst with six years in distribution, focused on "
                             "forecasting and Lean improvement."},
]
SKILLS = [
    {"key": "analysis", "label": "Analysis", "items": ["SQL", "Forecasting", "Excel"],
     "default_visible": True},
    {"key": "tools", "label": "Tools", "items": ["Tableau", "Power BI"], "default_visible": True},
    {"key": "languages", "label": "Languages", "items": ["Spanish (conversational)"],
     "default_visible": False},
]
# (slot key, optional, default, [(variant key, text, tags)])
SLOTS = [
    ("m_throughput", False, "t1", [
        ("t1", "Led process improvement across two distribution centers, raising throughput 22%.",
         ["process improvement", "throughput"]),
        ("t2", "Raised throughput 22% across two distribution centers by leading a Lean "
               "improvement program.", ["lean"]),
    ]),
    ("m_forecast", False, "f1", [
        ("f1", "Rebuilt demand forecasting in SQL, cutting excess inventory 18%.",
         ["sql", "forecasting"]),
    ]),
    ("m_review", False, "r1", [
        ("r1", "Ran the weekly operations review for the VP of Operations.", ["leadership"]),
        ("r2", "Prepared and ran the weekly operations review for the VP of Operations and four "
               "site leads.", ["leadership"]),
    ]),
    ("r_dash", False, "d1", [
        ("d1", "Built KPI dashboards in SQL and Tableau used by four site managers.",
         ["kpi", "dashboards"]),
    ]),
    ("r_o2c", True, "o1", [
        ("o1", "Mapped order-to-cash and removed two manual handoffs.", ["order to cash"]),
    ]),
]

EVAL_BLOCKS = {
    "b_fit": {
        "matches": [
            {"requirement": "Process improvement across sites",
             "evidence": "Led process improvement across two distribution centers",
             "strength": "strong"},
            {"requirement": "SQL and forecasting", "evidence": "Rebuilt demand forecasting in SQL",
             "strength": "strong"},
            {"requirement": "KPI dashboards", "evidence": "Built KPI dashboards",
             "strength": "partial"},
        ],
        "gaps": [
            {"requirement": "Six Sigma Green Belt (preferred)", "severity": "noise"},
        ],
    }
}


async def seed_async(
    *, track: str = "employment", pdf_pages: int | None = 1, evaluation: bool = True,
    pursuit: bool = True, redact_label: str = "A regional food distributor",
    config: dict | None = None, rationale: dict | None = None, opportunity: bool = True,
) -> dict[str, int]:
    throwaway.require_throwaway_home("tailor_seed")
    await reset_engine()
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    out: dict[str, int] = {}
    async with sm() as s:
        s.add(DocLayout(key="classic", label="Classic", description="Classic", section_order=[
            "summary", "experience", "skills", "education"]))
        s.add(DocLayout(key="modern", label="Modern", description="Modern", section_order=[
            "summary", "experience", "skills", "education"]))
        master = DocMaster(
            track=track, name="Operations resume", doc_kind="resume",
            header={"name": "Jordan A. Rivera", "email": "jordan.rivera@example.com"},
            summary_variants=[dict(v) for v in SUMMARY],
            skill_groups=[dict(g) for g in SKILLS], is_active=True,
        )
        s.add(master)
        await s.flush()
        out["master"] = master.id
        roles = [
            ("Meadowbrook Foods", redact_label, "Operations Analyst", "2021", "present",
             SLOTS[:3]),
            ("Ridgeway Distribution", None, "Business Analyst", "2018", "2021", SLOTS[3:]),
        ]
        for i, (org_label, redacted, title, start, end, slots) in enumerate(roles):
            sec = DocSection(
                doc_master_id=master.id, section_kind="role", org_label=org_label,
                org_label_redacted=redacted, title=title, start_date=start, end_date=end,
                sort_order=i,
            )
            s.add(sec)
            await s.flush()
            for j, (key, optional, default, variants) in enumerate(slots):
                slot = DocSlot(doc_section_id=sec.id, key=key, sort_order=j, optional=optional,
                               default_variant_key=default)
                s.add(slot)
                await s.flush()
                for vk, text, tags in variants:
                    s.add(DocVariant(doc_slot_id=slot.id, key=vk, text=text, tags=tags,
                                     derived=False))
        opp_id = None
        if opportunity:
            org = Org(name="Larkspur Logistics", normalized_name="larkspur logistics")
            src = Source(track=track, kind="greenhouse", name="board", enabled=True)
            s.add_all([org, src])
            await s.flush()
            opp = Opportunity(
                track=track,
                opp_type="job_posting" if track == "employment" else "contract_posting",
                org_id=org.id, source_id=src.id, org_name="Larkspur Logistics",
                title="Senior Operations Analyst", url="https://jobs.example.test/larkspur/1",
                location_raw="Austin, TX", remote_type="hybrid", comp_min=118000,
                comp_max=142000, comp_currency="USD", description_md=POSTING,
                stage="t2_done", date_posted="2026-10-09",
            )
            s.add(opp)
            await s.flush()
            opp_id = opp.id
            out["opp"] = opp.id
            if evaluation:
                s.add(Evaluation(opportunity_id=opp.id, tier=2, track=track, weighted_score=4.6,
                                 blocks=EVAL_BLOCKS))
            if pursuit:
                p = Pursuit(opportunity_id=opp.id, track=track, status="shortlisted")
                s.add(p)
                await s.flush()
                out["pursuit"] = p.id
        base = {
            "layout": "classic", "summary": "sum_a",
            "slots": {k: d for k, _o, d, _v in SLOTS},
            "skill_groups": {"analysis": True, "tools": True, "languages": False},
        }
        base.update(config or {})
        render = DocRender(
            doc_master_id=master.id, opportunity_id=opp_id, config=base,
            rationale=rationale if rationale is not None else {}, template="classic",
            filename="Rivera_Larkspur-Logistics_Senior-Operations-Analyst.pdf",
            page_count=pdf_pages,
        )
        s.add(render)
        await s.flush()
        out["render"] = render.id
        await s.commit()
    await engine.dispose()
    forget_engine()
    return out


def seed(**kw) -> dict[str, int]:
    return asyncio.run(seed_async(**kw))
