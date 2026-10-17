"""Prep pack assembly (offline): pulls the role summary, research signals, Block B gaps, mapped
canonical stories, questions-to-ask drawn from research UNKNOWNS, and known panel members, and
produces content_md plus the structured maps. The PDF render is a separate local step (test below is
skipped without Edge). The HTML converter is exercised purely."""

from __future__ import annotations

from kestrel.db.models import (
    Contact,
    Evaluation,
    Opportunity,
    Org,
    OrgResearch,
    Source,
    Story,
)
from kestrel.interview.prep import assemble_prep_pack, render_prep_pack_pdf
from kestrel.interview.render import prep_pack_html


async def _seed(sm) -> int:
    async with sm() as s:
        org = Org(name="Beacon Health", normalized_name="beacon health", sector="healthcare")
        src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
        s.add_all([org, src])
        await s.flush()
        opp = Opportunity(
            track="employment", opp_type="job_posting", org_id=org.id, source_id=src.id,
            title="VP of IT", org_name="Beacon Health", url="https://x/1",
        )
        s.add(opp)
        await s.flush()
        s.add(
            Evaluation(
                opportunity_id=opp.id, tier=2, track="employment", verdict="apply",
                verdict_model="apply", score=4.0, weighted_score=4.0, needs_review=False,
                dimensions={},
                blocks={
                    "a_role_summary": {"lead_with": "Own IT and security for a health system."},
                    "b_fit": {"gaps": [
                        {"requirement": "HL7/FHIR experience", "severity": "addressable",
                         "mitigation": "Led an EHR integration adjacent to it."}
                    ]},
                    "f_interview": [
                        {"likely_question": "Tell me about a turnaround.", "why_asked": "ownership",
                         "proof_point_key": "pp_turnaround", "star_r": {}}
                    ],
                },
                blockers=[], legitimacy={"status": "clean", "signals": []},
            )
        )
        s.add(
            OrgResearch(
                org_id=org.id, summary_md="A regional health system.",
                signals=[{"category": "leadership_change", "statement": "CIO departed in May",
                          "source_index": 1, "url": "https://x/news"}],
                unknowns=["Who owns the EHR roadmap now?", "Is the security team in-house"],
                angle="Position as a steady hand post-departure.", sources=[],
                insufficient_sources=False,
            )
        )
        s.add(Story(title="Payments turnaround", proof_point_key="pp_turnaround",
                    themes=["turnaround"], is_canonical=True, result="Recovered reliability."))
        s.add(Contact(org_id=org.id, name="Dana Lee", title="CEO", contact_type="exec"))
        await s.commit()
        return opp.id


async def test_prep_pack_assembles_from_existing_data(sm):
    opp_id = await _seed(sm)
    async with sm() as s:
        pack = await assemble_prep_pack(s, opp_id)

    assert pack is not None
    assert pack.pdf_path is None  # assembly only; no browser touched
    md = pack.content_md
    assert "Own IT and security for a health system." in md  # role summary
    assert "HL7/FHIR experience" in md  # Block B gap
    assert "CIO departed in May" in md  # research signal
    assert "Dana Lee" in md  # known panel member

    # The likely question maps to the canonical story via the shared proof_point_key.
    assert pack.stories.get("Tell me about a turnaround.") is not None
    # Questions to ask are drawn from research unknowns (specific, not generic).
    assert any("EHR roadmap" in q for q in pack.questions_to_ask)
    assert any(q.endswith("?") for q in pack.questions_to_ask)  # a non-question was phrased as one
    assert any(p["name"] == "Dana Lee" for p in pack.panel)


async def test_prep_pack_html_converter_is_offline_and_structured(sm):
    opp_id = await _seed(sm)
    async with sm() as s:
        pack = await assemble_prep_pack(s, opp_id)
        html = prep_pack_html(pack, org_name="Beacon Health", role="VP of IT")
    assert "<h1>" in html and "<h2>" in html and "<li>" in html
    assert "Beacon Health" in html
    assert "<title>" in html and "4F5BD5" in html  # house palette (indigo, D-UI-53), titled


async def test_prep_pack_pdf_renders_locally(sm, browser_fallback):
    """Local gate: the prep pack renders to a real PDF via Edge. Skipped where Edge is absent."""
    from pathlib import Path

    opp_id = await _seed(sm)
    async with sm() as s:
        pack = await assemble_prep_pack(s, opp_id)
        path = await render_prep_pack_pdf(s, pack.id)

    assert path is not None
    pdf = Path(path)
    assert pdf.exists() and pdf.stat().st_size > 0
    assert pdf.read_bytes()[:4] == b"%PDF"
