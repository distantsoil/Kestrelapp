"""Prep packs (SPEC-MODULES 3.2). Assembled on demand for a shortlisted opportunity from data the
system already holds: the role summary, the research signals, the Block B gaps and their
mitigations, the mapped canonical stories for the likely questions, questions the operator should
ask (drawn from research UNKNOWNS, which is what makes them specific rather than generic), and any
known panel members from `contact`. Practically, this is what the operator reads before going in.

Assembly is pure and offline; the PDF render (via the Phase 8 Playwright pipeline) is a separate,
local step so the assembly is fully testable without a browser."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel import paths
from kestrel.db.models import Contact, Evaluation, Opportunity, Org, OrgResearch, PrepPack, Story
from kestrel.interview.questions import active_set, set_questions
from kestrel.research.connections import not_imported_connection

_PANEL_TYPES = ("hiring_manager", "exec", "board", "peer", "recruiter", "pe_operator")


@dataclass
class _Assembled:
    role_summary: str
    signals: list[dict]
    gaps: list[dict]
    questions_map: list[dict]  # [{question, story_id|None, story_title|None}]
    questions_to_ask: list[str]
    panel: list[dict]


async def _latest_eval(session: AsyncSession, opportunity_id: int) -> Evaluation | None:
    return (
        await session.execute(
            select(Evaluation)
            .where(
                Evaluation.opportunity_id == opportunity_id,
                Evaluation.tier == 2,
                Evaluation.track == "employment",
            )
            .order_by(Evaluation.created_at.desc(), Evaluation.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _latest_research(session: AsyncSession, org_id: int | None) -> OrgResearch | None:
    if org_id is None:
        return None
    return (
        await session.execute(
            select(OrgResearch)
            .where(OrgResearch.org_id == org_id, OrgResearch.insufficient_sources.is_(False))
            .order_by(OrgResearch.researched_at.desc(), OrgResearch.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _map_stories(f_interview: list, stories: list[Story]) -> list[dict]:
    """Map each likely question to a canonical story by shared proof_point_key. A question with no
    confident match is left unmapped rather than pointed at an arbitrary story (hard rule 2's
    spirit: do not assert a mapping the data does not support)."""
    by_key = {s.proof_point_key: s for s in stories if s.proof_point_key}
    out: list[dict] = []
    for item in f_interview or []:
        if not isinstance(item, dict):
            continue
        q = (item.get("likely_question") or "").strip()
        if not q:
            continue
        story = by_key.get(item.get("proof_point_key"))
        out.append(
            {"question": q, "story_id": story.id if story else None,
             "story_title": story.title if story else None}
        )
    return out


def _map_question_set(questions: list[dict], stories: list[Story]) -> list[dict]:
    """The P14 set's questions with their story: the story the model linked by id, else the one
    that shares the question's proof point key, else none (never an arbitrary story)."""
    by_id = {s.id: s for s in stories}
    by_key = {s.proof_point_key: s for s in stories if s.proof_point_key}
    out: list[dict] = []
    for item in questions:
        story = by_id.get(item.get("story_id")) or by_key.get(item.get("proof_point_key"))
        out.append(
            {"question": str(item["question"]).strip(), "story_id": story.id if story else None,
             "story_title": story.title if story else None}
        )
    return out


def _questions_to_ask(unknowns: list) -> list[str]:
    """Turn research unknowns into specific questions to ask. Their specificity is the whole value:
    they come from what the research could NOT resolve, so they are about this company alone."""
    out: list[str] = []
    for u in unknowns or []:
        u = (u or "").strip() if isinstance(u, str) else ""
        if not u:
            continue
        out.append(u if u.endswith("?") else f"Can you help me understand: {u}")
    return out


async def _assemble(session: AsyncSession, opportunity: Opportunity, org: Org | None) -> _Assembled:
    ev = await _latest_eval(session, opportunity.id)
    blocks = ev.blocks if ev and isinstance(ev.blocks, dict) else {}
    a = blocks.get("a_role_summary") or {}
    role_summary = (a.get("lead_with") if isinstance(a, dict) else None) or (
        f"{opportunity.title or 'Role'}" + (f" at {org.name}" if org else "")
    )
    gaps = [
        g for g in ((blocks.get("b_fit") or {}).get("gaps") or []) if isinstance(g, dict)
    ]

    research = await _latest_research(session, org.id if org else None)
    signals = [s for s in (research.signals if research else []) if isinstance(s, dict)]
    unknowns = research.unknowns if research else []

    stories = (
        (await session.execute(select(Story).where(Story.is_canonical.is_(True)))).scalars().all()
    )
    # Once the role was marked Applied and its P14 set exists, that set supersedes Block F
    # (D-INTERVIEW-1); Block F stays the fallback for every other role.
    qset = set_questions(await active_set(session, opportunity.id))
    if qset:
        questions_map = _map_question_set(qset, list(stories))
    else:
        questions_map = _map_stories(blocks.get("f_interview") or [], list(stories))

    panel = []
    if org is not None:
        contacts = (
            (
                await session.execute(
                    select(Contact).where(
                        Contact.org_id == org.id,
                        Contact.contact_type.in_(_PANEL_TYPES),
                        not_imported_connection(),
                    )
                )
            )
            .scalars()
            .all()
        )
        panel = [
            {"name": c.name, "title": c.title, "contact_type": c.contact_type} for c in contacts
        ]

    return _Assembled(
        role_summary=role_summary,
        signals=signals,
        gaps=gaps,
        questions_map=questions_map,
        questions_to_ask=_questions_to_ask(unknowns),
        panel=panel,
    )


def _content_md(org_name: str | None, role: str | None, a: _Assembled) -> str:
    lines = [f"# Interview prep: {role or 'Role'}" + (f", {org_name}" if org_name else ""), ""]
    lines += ["## Role summary", a.role_summary, ""]
    if a.signals:
        lines += ["## Research signals"]
        lines += [f"- ({s.get('category')}) {s.get('statement')}" for s in a.signals] + [""]
    if a.gaps:
        lines += ["## Gaps and mitigations"]
        for g in a.gaps:
            mit = g.get("mitigation") or "no mitigation drafted"
            lines.append(f"- [{g.get('severity')}] {g.get('requirement')}: {mit}")
        lines += [""]
    if a.questions_map:
        lines += ["## Likely questions and your stories"]
        for m in a.questions_map:
            tail = f" -> {m['story_title']}" if m.get("story_title") else " -> (no mapped story)"
            lines.append(f"- {m['question']}{tail}")
        lines += [""]
    if a.questions_to_ask:
        lines += ["## Questions to ask them"]
        lines += [f"- {q}" for q in a.questions_to_ask] + [""]
    if a.panel:
        lines += ["## Known panel"]
        lines += [f"- {p['name']} ({p.get('title') or p['contact_type']})" for p in a.panel] + [""]
    return "\n".join(lines).strip()


@dataclass
class LiveView:
    """What the Interview prep page shows beside the stored pack, read fresh each time so a story
    the operator just wrote shows up at once: the role in one line, the likely questions with the
    story Kestrel suggested for each (never invented: a question with no confident match has
    none), the gaps worth handling, and the company notes the PDF carries."""

    role_summary: str
    questions: list[dict]  # [{question, story_id, why, kind}]
    gaps: list[dict]  # [{requirement, severity, mitigation}], noise left out
    notes: list[str]  # public company facts from research, for the PDF's company notes


_KIND_OF_CATEGORY = {"gap_probe": "gap", "technical": "requirement", "role_specific": "requirement"}


async def live_view(session: AsyncSession, opportunity_id: int) -> LiveView | None:
    opportunity = await session.get(Opportunity, opportunity_id)
    if opportunity is None:
        return None
    org = await session.get(Org, opportunity.org_id) if opportunity.org_id else None
    ev = await _latest_eval(session, opportunity_id)
    blocks = ev.blocks if ev and isinstance(ev.blocks, dict) else {}
    a = blocks.get("a_role_summary") or {}
    summary = (a.get("lead_with") if isinstance(a, dict) else None) or (
        f"{opportunity.title or 'Role'}" + (f" at {org.name}" if org else "")
    )
    gaps = [
        g for g in ((blocks.get("b_fit") or {}).get("gaps") or [])
        if isinstance(g, dict) and g.get("severity") != "noise"
    ]
    stories = list(
        (await session.execute(select(Story).where(Story.is_canonical.is_(True)))).scalars().all()
    )
    by_id = {s.id: s for s in stories}
    by_key = {s.proof_point_key: s for s in stories if s.proof_point_key}
    questions: list[dict] = []
    qset = set_questions(await active_set(session, opportunity_id))
    if qset:
        for item in qset:
            story = by_id.get(item.get("story_id")) or by_key.get(item.get("proof_point_key"))
            questions.append({
                "question": str(item["question"]).strip(),
                "story_id": story.id if story else None,
                "why": str(item.get("why_asked") or "").strip(),
                "kind": _KIND_OF_CATEGORY.get(str(item.get("category")), "common"),
            })
    else:
        for item in blocks.get("f_interview") or []:
            text = str(item.get("likely_question") or "").strip() if isinstance(item, dict) else ""
            if not text:
                continue
            story = by_key.get(item.get("proof_point_key"))
            questions.append({
                "question": text, "story_id": story.id if story else None,
                "why": str(item.get("why_asked") or "").strip(), "kind": "requirement",
            })
    research = await _latest_research(session, org.id if org else None)
    notes = [
        str(s.get("statement")).strip()
        for s in (research.signals if research else [])
        if isinstance(s, dict) and str(s.get("statement") or "").strip()
    ]
    return LiveView(role_summary=summary, questions=questions, gaps=gaps, notes=notes)


async def assemble_prep_pack(session: AsyncSession, opportunity_id: int) -> PrepPack | None:
    """Build and persist a prep pack row (no PDF yet). None if the opportunity is missing."""
    opportunity = await session.get(Opportunity, opportunity_id)
    if opportunity is None:
        return None
    org = await session.get(Org, opportunity.org_id) if opportunity.org_id else None
    a = await _assemble(session, opportunity, org)

    row = PrepPack(
        opportunity_id=opportunity_id,
        content_md=_content_md(org.name if org else None, opportunity.title, a),
        stories={m["question"]: m["story_id"] for m in a.questions_map if m["story_id"]},
        questions_to_ask=a.questions_to_ask,
        panel=a.panel,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def render_prep_pack_pdf(
    session: AsyncSession, prep_pack_id: int, *, model: dict | None = None
) -> str | None:
    """Render a stored prep pack to PDF via the Phase 8 pipeline (Playwright/Edge, empty
    header/footer, scrubbed metadata) and record its path. Local-required (needs Edge).
    With `model` (the Interview prep page's paged pack) the PDF is those same pages; without it
    the stored text is rendered."""
    from kestrel.docs.render import render_pdf
    from kestrel.interview.render import pack_pages_html, prep_pack_html

    row = await session.get(PrepPack, prep_pack_id)
    if row is None:
        return None
    opportunity = await session.get(Opportunity, row.opportunity_id)
    org = await session.get(Org, opportunity.org_id) if opportunity and opportunity.org_id else None

    html = (
        pack_pages_html(model) if model is not None
        else prep_pack_html(row, org_name=org.name if org else None,
                            role=opportunity.title if opportunity else None)
    )
    rendered = await render_pdf(html)

    out_dir = paths.data_dir() / "prep_packs"
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / f"{row.id}_prep_pack.pdf"
    pdf_path.write_bytes(rendered.pdf)
    row.pdf_path = str(pdf_path)
    await session.commit()
    return str(pdf_path)
