"""A fictional seed doc_master per track plus the eight shipped layouts (SPEC-MODULES 1.2), so
`kestrel render` and the gate are verifiable now, before the /documents UI (Phase 13) exists. All
content is fictional (privacy: the shipped example never carries a real person or organization)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import DocLayout, DocMaster, DocSection, DocSlot, DocVariant

# The fictional sample's contact address. It also marks a document as the sample, whose
# confidential-mode marks apply to itself only (current_employer.is_sample, D-PRIV-1).
SAMPLE_EMAIL = "jordan.rivera@example.com"

_LAYOUTS = [
    {
        "key": "executive",
        "label": "Executive",
        "max_roles_shown": None,
        "description": "Summary, full experience, skills, education. Two pages. Centered name, "
        "small caps headings, generous spacing.",
        "section_order": ["summary", "experience", "skills", "education", "certifications",
                          "licenses", "projects"],
    },
    {
        "key": "condensed",
        "label": "Condensed",
        "max_roles_shown": 2,
        "description": "One page, top two roles, no summary. Smaller type, tight spacing, dates "
        "inline.",
        "section_order": ["experience", "skills", "education", "certifications", "licenses",
                          "projects"],
    },
    {
        "key": "technical",
        "label": "Technical",
        "max_roles_shown": None,
        "description": "Skills promoted above experience. Sans serif, accent-colored headings.",
        "section_order": ["summary", "skills", "experience", "projects", "certifications",
                          "education", "licenses"],
    },
    {
        "key": "classic",
        "label": "Classic",
        "max_roles_shown": None,
        "description": "Traditional single column: summary, experience, education, "
        "credentials, skills. Serif, centered name over a double rule, plain headings.",
        "section_order": ["summary", "experience", "education", "certifications", "licenses",
                          "projects", "skills"],
    },
    {
        "key": "modern",
        "label": "Modern",
        "max_roles_shown": None,
        "description": "Clean single column: summary, experience, projects, skills, education. "
        "Sans serif, name in the accent color, short accent bars under headings.",
        "section_order": ["summary", "experience", "projects", "skills", "education",
                          "certifications", "licenses"],
    },
    {
        "key": "compact",
        "label": "Compact",
        "max_roles_shown": None,
        "description": "Dense one-page fit: summary, skills, experience, then credentials. Small "
        "type, tight spacing, dates inline.",
        "section_order": ["summary", "skills", "experience", "education", "certifications",
                          "licenses", "projects"],
    },
    {
        "key": "capability",
        "label": "Capability",
        "max_roles_shown": None,
        "description": "Positioning, service lines, proof engagements, differentiators. Serif, "
        "simple rules.",
        "section_order": ["positioning", "service_lines", "proof_engagements", "differentiators"],
    },
    {
        "key": "capability_short",
        "label": "Capability (short)",
        "max_roles_shown": 2,
        "description": "One page, three service lines, two proofs. Smaller type, tight spacing.",
        "section_order": ["positioning", "service_lines", "proof_engagements"],
    },
]


async def _ensure_layouts(session: AsyncSession) -> None:
    """Insert any missing shipped layout and bring an existing shipped row up to date (label,
    description, section order, role cap), so a description that now names the look reaches an
    install seeded by an older version (D-DOC-1). Idempotent; a layout the operator added under
    another key is never touched."""
    for spec in _LAYOUTS:
        existing = (
            await session.execute(select(DocLayout).where(DocLayout.key == spec["key"]))
        ).scalar_one_or_none()
        if existing is None:
            session.add(DocLayout(**spec))
            continue
        for name in ("label", "description", "section_order", "max_roles_shown"):
            if getattr(existing, name) != spec[name]:
                setattr(existing, name, spec[name])
    await session.flush()


async def _add_slots(session: AsyncSession, section_id: int, slots: list) -> None:
    for key, order, default, variants, optional in slots:
        slot = DocSlot(
            doc_section_id=section_id,
            key=key,
            sort_order=order,
            optional=optional,
            default_variant_key=default,
        )
        session.add(slot)
        await session.flush()
        for k, t, tags in variants:
            session.add(
                DocVariant(doc_slot_id=slot.id, key=k, text=t, tags=list(tags), derived=False)
            )


async def seed_resume_master(session: AsyncSession) -> int:
    master = DocMaster(
        track="employment",
        name="Jordan Rivera — Resume",
        doc_kind="resume",
        header={
            "name": "Jordan A. Rivera",
            "email": SAMPLE_EMAIL,
            "phone": "+1-614-555-0142",
            "location": "Columbus, OH",
            "links": ["linkedin.com/in/example"],
        },
        summary_variants=[
            {
                "key": "transformation",
                "text": "Operations analyst who turns messy spreadsheets into "
                "reports a team can act on.",
            },
            {
                "key": "operator",
                "text": "Practical analyst focused on accuracy, cost, and "
                "measurable improvements.",
            },
        ],
        skill_groups=[
            {
                "key": "analysis",
                "label": "Analysis & Reporting",
                "default_visible": True,
                "items": ["Excel", "SQL", "Power BI", "Tableau"],
            },
            {
                "key": "process",
                "label": "Process & Quality",
                "default_visible": True,
                "items": ["Lean", "Six Sigma Green Belt", "SOP writing", "Root cause analysis"],
            },
            {
                "key": "systems",
                "label": "Systems",
                "default_visible": False,
                "items": ["NetSuite", "SAP basics", "Jira"],
            },
        ],
    )
    session.add(master)
    await session.flush()

    role1 = DocSection(
        doc_master_id=master.id,
        section_kind="role",
        org_label="Lakeside Distribution",
        org_label_redacted="Regional distribution company",
        title="Operations Analyst",
        start_date="2022",
        end_date="Present",
        context_line="Three warehouses, 400 staff, one ERP.",
        sort_order=0,
    )
    role2 = DocSection(
        doc_master_id=master.id,
        section_kind="role",
        org_label="Brightline Foods",
        org_label_redacted="Regional food producer",
        title="Business Analyst",
        start_date="2018",
        end_date="2022",
        context_line="Two plants, a 30-person planning team.",
        sort_order=1,
    )
    edu = DocSection(
        doc_master_id=master.id,
        section_kind="credential",
        org_label="State University",
        title="B.S. Business Administration",
        start_date="",
        end_date="2014",
        context_line=None,
        sort_order=2,
    )
    session.add_all([role1, role2, edu])
    await session.flush()

    await _add_slots(
        session,
        role1.id,
        [
            (
                "r1b1",
                0,
                "cost",
                [
                    (
                        "cost",
                        "Cut weekly reporting time from two days to two hours with an automated "
                        "report.",
                        ["cost"],
                    ),
                    (
                        "scale",
                        "Standardized shipping and receiving metrics across three warehouses.",
                        ["scale"],
                    ),
                ],
                False,
            ),
            (
                "r1b2",
                1,
                "accuracy",
                [
                    (
                        "accuracy",
                        "Raised inventory accuracy from 92% to 98% by redesigning the cycle count.",
                        ["accuracy"],
                    ),
                    (
                        "compliance",
                        "Documented 25 standard procedures ahead of a customer audit with no "
                        "open findings.",
                        ["compliance"],
                    ),
                ],
                False,
            ),
            (
                "r1b3",
                2,
                "team",
                [
                    (
                        "team",
                        "Trained eight new supervisors on the shared reporting tools.",
                        ["team"],
                    )
                ],
                True,
            ),
        ],
    )
    await _add_slots(
        session,
        role2.id,
        [
            (
                "r2b1",
                0,
                "process",
                [
                    (
                        "process",
                        "Led a process review that cut order-entry errors by 40%.",
                        ["accuracy"],
                    ),
                    (
                        "ontime",
                        "Improved on-time delivery from 88% to 95% by tracking late orders weekly.",
                        ["reliability"],
                    ),
                ],
                False,
            ),
            (
                "r2b2",
                1,
                "migration",
                [
                    (
                        "migration",
                        "Moved the planning team from spreadsheets to a shared database with "
                        "no missed shipments.",
                        ["cloud"],
                    )
                ],
                True,
            ),
        ],
    )
    await _add_slots(
        session,
        edu.id,
        [
            (
                "e1",
                0,
                "degree",
                [
                    (
                        "degree",
                        "B.S. Business Administration, State University, 2014.",
                        ["education"],
                    )
                ],
                True,
            ),
        ],
    )
    await session.commit()
    return master.id


async def seed_capability_master(session: AsyncSession) -> int:
    master = DocMaster(
        track="consulting",
        name="Jordan Rivera — Capability",
        doc_kind="capability_statement",
        header={
            "name": "Jordan A. Rivera",
            "email": SAMPLE_EMAIL,
            "phone": "+1-614-555-0142",
            "location": "Columbus, OH",
            "links": [],
        },
        summary_variants=[
            {
                "key": "positioning",
                "text": "Independent operations consultant who fixes reporting and "
                "order-to-cash processes for small and mid-size companies.",
            },
        ],
        skill_groups=[
            {
                "key": "differentiators",
                "label": "Differentiators",
                "default_visible": True,
                "items": [
                    "Outcome-scoped engagements",
                    "Small trusted delivery bench",
                    "Manufacturing and distribution experience",
                ],
            },
        ],
    )
    session.add(master)
    await session.flush()

    sl1 = DocSection(
        doc_master_id=master.id,
        section_kind="service_line",
        org_label="Reporting and dashboard setup",
        title=None,
        start_date="",
        end_date="",
        context_line="Replace manual spreadsheets with one shared weekly report.",
        sort_order=0,
    )
    pe1 = DocSection(
        doc_master_id=master.id,
        section_kind="proof_engagement",
        org_label="Distribution project",
        org_label_redacted="Regional distribution company",
        title="Operations consultant",
        start_date="2024",
        end_date="2024",
        context_line="Three warehouses, eight weeks, one dashboard.",
        sort_order=1,
    )
    session.add_all([sl1, pe1])
    await session.flush()

    await _add_slots(
        session,
        sl1.id,
        [
            (
                "s1b1",
                0,
                "scope",
                [
                    (
                        "scope",
                        "A shared dashboard and a written process the team can run on its own.",
                        ["scope"],
                    )
                ],
                False,
            ),
        ],
    )
    await _add_slots(
        session,
        pe1.id,
        [
            (
                "p1b1",
                0,
                "result",
                [
                    (
                        "result",
                        "Built one weekly dashboard for three warehouses in eight weeks.",
                        ["result"],
                    ),
                    (
                        "cost",
                        "Cut weekly reporting time from two days to two hours.",
                        ["cost"],
                    ),
                ],
                False,
            ),
        ],
    )
    await session.commit()
    return master.id


async def seed_all(session: AsyncSession) -> dict[str, int]:
    await _ensure_layouts(session)
    resume = await seed_resume_master(session)
    capability = await seed_capability_master(session)
    return {"resume": resume, "capability": capability}
