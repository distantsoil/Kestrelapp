"""Fictional roles for the Role page tests: the two roles of the locked mockup
(the locked design mockup (kept privately)) plus a third to give Previous and Next something
to move between, a consulting lead, a pay corpus, a story, a document master with one render, a
company with research and a contact. Everything is written straight into the database.

`seed_async()` assumes the `_isolate_data_home` fixture (a throwaway data directory) and returns the
ids by name, so a test can open the role it wants. Nothing here touches the network or a model."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import (
    CompObservation,
    Contact,
    DocRender,
    Evaluation,
    Opportunity,
    Org,
    OrgResearch,
    Source,
    Story,
)
from tests import throwaway

# A one-page PDF is all the Download link needs to exist; the bytes are never parsed.
_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _wipe_db() -> None:
    """Delete the database file, only inside a directory marked throwaway (tests/throwaway.py)."""
    throwaway.require_throwaway_home("role_seed")
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)


def _iso(delta: timedelta) -> str:
    return (datetime.now(UTC) - delta).isoformat(timespec="seconds")


def _dims(values: list[float], whys: list[str]) -> dict:
    keys = ["role_fit", "domain_match", "company_context", "comp_logistics", "trajectory"]
    return {k: {"score": v, "why": w} for k, v, w in zip(keys, values, whys, strict=True)}


async def seed_async(*, extra_pay_obs: int = 6) -> dict[str, int]:
    await reset_engine()
    _wipe_db()
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    out: dict[str, int] = {}
    async with sm() as s:
        src = Source(track="employment", kind="indeed", enabled=True, name="Indeed")
        csrc = Source(track="consulting", kind="rss", enabled=True, name="Careers feed")
        s.add_all([src, csrc])
        lark = Org(
            name="Larkspur Logistics",
            normalized_name="larkspur logistics",
            sector="Transportation and logistics",
            size_band="201 to 500",
            hq_location="Austin, TX",
            ownership="private",
            url="https://www.larkspur.test",
            domain="larkspur.test",
            signals=[{"category": "growth", "statement": "Opened a third distribution center."}],
        )
        north = Org(name="Northwind Health", normalized_name="northwind health")
        meadow = Org(
            name="Meadowbrook Foods",
            normalized_name="meadowbrook foods",
            sector="Food manufacturer",
        )
        s.add_all([lark, north, meadow])
        await s.flush()

        # 1. The mockup's first role: a strong fit with every box filled in.
        o1 = Opportunity(
            track="employment",
            opp_type="job_posting",
            org_id=lark.id,
            source_id=src.id,
            title="Senior Operations Analyst",
            org_name="Larkspur Logistics",
            url="https://indeed.test/jobs/larkspur-1",
            stage="t2_done",
            description_md=(
                "About the role\nWe are looking for a Senior Operations Analyst to lead process "
                "improvement across our three distribution centers.\n\nWhat we are looking for\n"
                "5+ years in operations or supply chain. Comfortable with SQL and forecasting "
                "tools. Six Sigma Green Belt preferred."
            ),
            location_raw="Austin, TX",
            city="Austin",
            state="TX",
            country="US",
            remote_type="hybrid",
            comp_min=118000,
            comp_max=142000,
            comp_currency="USD",
            comp_interval="year",
            employment_type="Full-time",
            comp_source="parsed",
            seen_count=2,
            version=1,
            liveness="live",
            date_posted=_iso(timedelta(days=2)),
            first_seen_at=_iso(timedelta(days=2)),
            last_seen_at=_iso(timedelta(hours=3)),
        )
        o2 = Opportunity(
            track="employment",
            opp_type="job_posting",
            org_id=north.id,
            source_id=src.id,
            title="Process Improvement Lead",
            org_name="Northwind Health",
            url="https://indeed.test/jobs/northwind-1",
            stage="t2_done",
            description_md="Lead cross-functional teams to redesign processes.",
            location_raw="Remote (US)",
            state="TX",
            country="US",
            remote_type="remote",
            comp_min=157000,
            comp_max=185000,
            comp_currency="USD",
            comp_interval="year",
            seen_count=1,
            version=1,
            liveness="live",
            date_posted=_iso(timedelta(days=4)),
            first_seen_at=_iso(timedelta(days=4)),
        )
        o3 = Opportunity(
            track="employment",
            opp_type="job_posting",
            org_id=meadow.id,
            source_id=src.id,
            title="Supply Planner",
            org_name="Meadowbrook Foods",
            url="https://indeed.test/jobs/meadowbrook-1",
            stage="t2_done",
            description_md="Plan supply for three plants.",
            location_raw="Dayton, OH",
            remote_type="onsite",
            comp_min=90000,
            comp_max=105000,
            comp_currency="USD",
            comp_interval="year",
            seen_count=1,
            version=1,
            liveness="live",
            date_posted=_iso(timedelta(days=6)),
            first_seen_at=_iso(timedelta(days=6)),
        )
        # 4. A role nothing has scored yet (Being scored), for the unscored state.
        o4 = Opportunity(
            track="employment",
            opp_type="job_posting",
            org_id=meadow.id,
            source_id=src.id,
            title="Inventory Coordinator",
            org_name="Meadowbrook Foods",
            url="https://indeed.test/jobs/meadowbrook-2",
            stage="t1_pass",
            description_md="Coordinate inventory.",
            location_raw="Dayton, OH",
            seen_count=1,
            version=1,
            first_seen_at=_iso(timedelta(hours=1)),
        )
        lead = Opportunity(
            track="consulting",
            opp_type="contract_posting",
            org_id=meadow.id,
            source_id=csrc.id,
            title="Weekly reporting dashboard build, 6 weeks",
            org_name="Meadowbrook Foods",
            url="https://careers.test/reporting",
            stage="t2_done",
            description_md="Automate weekly operations reporting before the busy season.",
            location_raw="Remote (US)",
            remote_type="remote",
            comp_min=552,
            comp_max=650,
            comp_currency="USD",
            comp_interval="day",
            seen_count=1,
            version=1,
            liveness="live",
            date_posted=_iso(timedelta(days=2)),
            first_seen_at=_iso(timedelta(days=2)),
        )
        s.add_all([o1, o2, o3, o4, lead])
        await s.flush()

        s.add(
            Evaluation(
                opportunity_id=o1.id,
                tier=2,
                track="employment",
                verdict="apply",
                verdict_model="apply",
                score=4.6,
                weighted_score=4.6,
                needs_review=False,
                model="qwen3:14b",
                prompt_version="evaluate/v7",
                prompt_tokens=3100,
                created_at=_iso(timedelta(days=2)),
                dimensions=_dims(
                    [4.8, 4.3, 4.7, 4.1, 4.6],
                    [
                        "Process improvement across sites is the core of the job.",
                        "Supply chain is adjacent to your last role.",
                        "A growing regional carrier.",
                        "Pay sits above your target.",
                        "A clear step toward operations leadership.",
                    ],
                ),
                blocks={
                    "a_role_summary": {
                        "what_this_actually_is": (
                            "Leads a small team improving how work moves through the business: "
                            "mapping current processes across three distribution centers, finding "
                            "the bottlenecks, building SQL reporting and forecasts, and presenting "
                            "progress to the VP of Operations each week."
                        ),
                        "title_body_mismatch": None,
                    },
                    "b_fit": {
                        "matches": [
                            {
                                "requirement": "Process improvement across distribution centers",
                                "evidence": "Your Meadowbrook bullet: throughput up 22% in 2 sites",
                                "strength": "strong",
                            },
                            {
                                "requirement": "SQL and forecasting",
                                "evidence": "Used in the Demand forecast rebuild story",
                                "strength": "strong",
                            },
                            {
                                "requirement": "Supply chain domain",
                                "evidence": "Adjacent, from your last role",
                                "strength": "partial",
                            },
                        ],
                        "gaps": [
                            {
                                "requirement": "Six Sigma Green Belt preferred",
                                "severity": "addressable",
                                "mitigation": "Lead with your Lean results; it is only preferred, "
                                "not required.",
                            },
                            {
                                "requirement": "Broader scope than your recent roles",
                                "severity": "addressable",
                                "mitigation": "Point to the weekly review you run for the VP of "
                                "Operations.",
                            },
                        ],
                    },
                    "c_level": {
                        "relative_level": "at",
                        "scope_read": "Same level.",
                        "reports_to": "VP Operations",
                        "team_size": "4",
                    },
                    "d_comp": {
                        "stated": "$118,000 to $142,000",
                        "estimate_confidence": "high",
                        "estimate_basis": "posted",
                        "vs_floor": "above_target",
                        "geo_adjustment_risk": False,
                    },
                    "e_angle": {
                        "lead_with": "Your distribution center throughput project, with its "
                        "22% before-and-after number.",
                        "jd_citations": [],
                    },
                },
                blockers=[],
                legitimacy={"status": "clean", "confidence": "high", "signals": []},
            )
        )
        s.add(
            Evaluation(
                opportunity_id=o2.id,
                tier=2,
                track="employment",
                verdict="apply",
                verdict_model="apply",
                score=4.4,
                weighted_score=4.4,
                needs_review=False,
                model="qwen3:14b",
                prompt_version="evaluate/v7",
                prompt_tokens=2800,
                created_at=_iso(timedelta(days=3)),
                dimensions=_dims([4.6, 4.0, 4.4, 4.5, 4.3], ["Leads improvement teams."] * 5),
                blocks={
                    "a_role_summary": {
                        "what_this_actually_is": "Leads cross-functional teams.",
                        "title_body_mismatch": None,
                    },
                    "b_fit": {
                        "matches": [
                            {
                                "requirement": "Leads cross-functional teams",
                                "evidence": "Your Lean program across three sites",
                                "strength": "strong",
                            }
                        ],
                        "gaps": [
                            {
                                "requirement": "Healthcare experience preferred",
                                "severity": "addressable",
                                "mitigation": "Logistics carries.",
                            }
                        ],
                    },
                    "c_level": {"relative_level": "above", "scope_read": "A step up."},
                    "d_comp": {
                        "estimate_confidence": "high",
                        "estimate_basis": "posted",
                        "vs_floor": "above_target",
                        "geo_adjustment_risk": False,
                    },
                    "e_angle": {"lead_with": "The Lean program.", "jd_citations": []},
                },
                blockers=[],
                legitimacy={
                    "status": "suspect",
                    "confidence": "medium",
                    "signals": ["generic_boilerplate"],
                },
            )
        )
        s.add(
            Evaluation(
                opportunity_id=o3.id,
                tier=2,
                track="employment",
                verdict="consider",
                verdict_model="consider",
                score=3.4,
                weighted_score=3.4,
                needs_review=False,
                model="qwen3:14b",
                prompt_version="evaluate/v7",
                prompt_tokens=2500,
                created_at=_iso(timedelta(days=5)),
                dimensions=_dims([3.5, 3.4, 3.3, 3.0, 3.5], ["Partly relevant."] * 5),
                blocks={
                    "a_role_summary": {
                        "what_this_actually_is": "Plans supply.",
                        "title_body_mismatch": None,
                    },
                    "b_fit": {"matches": [], "gaps": []},
                    "c_level": {"relative_level": "below", "scope_read": "A step down."},
                    "d_comp": {
                        "estimate_confidence": "low",
                        "estimate_basis": "n/a",
                        "vs_floor": "at_floor",
                        "geo_adjustment_risk": False,
                    },
                    "e_angle": {"lead_with": "Planning.", "jd_citations": []},
                },
                blockers=[{"kind": "location", "detail": "On-site in Dayton, OH."}],
                legitimacy={"status": "clean", "confidence": "high", "signals": []},
            )
        )
        s.add(
            Evaluation(
                opportunity_id=lead.id,
                tier=2,
                track="consulting",
                verdict="pursue",
                verdict_model="pursue",
                score=4.5,
                weighted_score=4.5,
                needs_review=False,
                model="qwen3:14b",
                prompt_version="evaluate_consulting/v3",
                prompt_tokens=2400,
                created_at=_iso(timedelta(days=2)),
                dimensions={
                    k: {"score": v, "why": "Fits."}
                    for k, v in (
                        ("engagement_fit", 4.7),
                        ("buyer_access", 4.2),
                        ("economics", 4.6),
                        ("delivery_feasibility", 4.4),
                        ("strategic_value", 4.5),
                    )
                },
                blocks={
                    "a_situation": {
                        "stated": "Weekly operations reporting is assembled by hand.",
                        "inferred": "",
                        "speculative": "",
                        "timing_read": "",
                    },
                    "b_thesis": {
                        "engagement": "Automate the weekly report.",
                        "service_lines": ["Reporting automation"],
                        "phases": [],
                        "estimated_days": 18,
                        "estimated_weeks": 6,
                    },
                    "c_buyers": {
                        "economic_buyer": "Operations director",
                        "influencers": [],
                        "blockers": [],
                        "access_difficulty": "moderate",
                        "access_path": "Operations director named in the posting",
                    },
                    "d_economics": {
                        "rate_low": 552,
                        "rate_high": 650,
                        "confidence": "high",
                        "basis": "posted",
                        "engagement_value": 18000,
                        "renewal_likelihood": "medium",
                        "vs_floor": "above_target",
                    },
                    "e_approach": {
                        "opening_move": "Offer a two-week reporting sprint.",
                        "to_whom": "Operations director",
                        "referencing": "",
                        "channel": "email",
                        "why_now_line": "",
                    },
                    "f_delivery": {
                        "solo_feasible": True,
                        "gaps": ["Power BI named"],
                        "partner_needed": None,
                        "effort_shape": "About 3 days a week.",
                    },
                    "g_risk": {"status": "clean", "signals": []},
                },
                blockers=[],
                legitimacy={"status": "clean", "confidence": "high", "signals": []},
            )
        )

        for title, n in (
            ("senior operations analyst", extra_pay_obs),
            ("process improvement lead", 3),
        ):
            for i in range(n):
                lo = 108000 + i * 4000
                s.add(
                    CompObservation(
                        normalized_title=title,
                        geo_bucket="TX",
                        min=lo,
                        max=lo + 12000,
                        currency="USD",
                        interval="year",
                        source="test",
                    )
                )
        s.add(
            Story(
                title="Demand forecast rebuild",
                situation="Forecasts missed by a third.",
                action="Rebuilt the model.",
                result="Error fell by half.",
                is_canonical=True,
                operator_edited=True,
                themes=[],
            )
        )
        s.add(
            OrgResearch(
                org_id=lark.id,
                summary_md="Regional carrier with three centers.",
                signals=[],
                unknowns=[],
                angle="Operations",
                insufficient_sources=False,
            )
        )
        s.add(
            Contact(
                org_id=lark.id,
                name="Pat Lee",
                title="VP of Operations",
                contact_type="hiring_manager",
            )
        )

        from kestrel.docs.seed import seed_all

        master_id = (await seed_all(s))["resume"]
        pdf = paths.data_dir() / "documents" / "seed_resume.pdf"
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(_PDF)
        render = DocRender(
            opportunity_id=o1.id,
            doc_master_id=master_id,
            config={},
            filename="Rivera_Larkspur_Analyst.pdf",
            page_count=1,
            pdf_path=str(pdf),
            created_at=_iso(timedelta(hours=5)),
        )
        s.add(render)
        await s.flush()
        out.update(
            {
                "strong": o1.id,
                "second": o2.id,
                "maybe": o3.id,
                "unscored": o4.id,
                "lead": lead.id,
                "org": lark.id,
                "master": master_id,
                "render": render.id,
            }
        )
        await s.commit()
    await engine.dispose()
    forget_engine()
    return out
