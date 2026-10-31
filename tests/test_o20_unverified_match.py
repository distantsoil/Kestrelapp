"""O20 (owner-reported): a role scored 2.7, "Not a fit", yet the Evaluation tab said "No gaps
found" and the Match map read "5 of 5" met while every "In your favor" item said the model
could not verify it, each evidence row said "Source not recorded", and the reasons quoted raw
input fields ("Remote OK True", "JD required skills bullet 1").

The fix is structural (hard rule 3, never regex the model's prose): P2's match `strength` gains
"unverified" for something the profile neither shows nor rules out, the prompt says when to use
it and to write plain sentences, the profile renders yes or no instead of `True`, and the page
shows an unverified match as Partly met under Gaps, never as Met or as proof (hard rule 4).
The fixture is invented data in the shape of the owner's case."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kestrel import paths
from kestrel.db.engine import make_async_engine
from kestrel.db.models import Evaluation, Opportunity
from kestrel.llm.client import json_schema_for
from kestrel.llm.prompts import load_prompt
from kestrel.llm.schemas import P2Eval
from kestrel.pipeline.context import profile_compact
from kestrel.web import pipeline_view as pv
from kestrel.web import role_view as rv
from tests import role_seed

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "tests/fixtures/eval/o20_remote_director.json").read_text("utf-8"))
OPP = SimpleNamespace(track="employment")


def _reply() -> dict:
    """The fixture as the pipeline stores it: validated by the P2 schema first."""
    return P2Eval.model_validate(FIXTURE).model_dump(mode="json")


def test_the_p2_schema_accepts_unverified_and_sends_it_to_the_model():
    ev = _reply()
    assert [m["strength"] for m in ev["blocks"]["b_fit"]["matches"]] == ["unverified"] * 5
    schema = json.dumps(json_schema_for(P2Eval))
    assert '"unverified"' in schema


def test_unverified_requirements_are_partly_met_gaps_never_met_or_proof():
    ev = _reply()
    reqs = rv.requirements(OPP, ev)
    assert [r["state"] for r in reqs] == [rv.PART] * 5
    w = rv.wheel(reqs)
    assert w["center"] == "0 of 5" and (w["met"], w["part"], w["gap"]) == (0, 5, 0)
    assert all(r["how"] == rv.UNVERIFIED_HOW for r in reqs)
    # Not proof: no Evidence row (so no "Source not recorded"), and not a reason it fits.
    assert rv.evidence_rows(reqs, {}) == []
    card = rv.read_card(OPP, ev, "not a fit")
    assert "cannot be verified" not in card["text"]
    assert "The level is a step below your current role." in card["text"]
    # The Pipeline side panel tells the same story.
    assert pv.fit_reasons(ev) == []
    panel = pv.gap_rows(ev)  # four rows: three named, then "2 more" that says why
    assert [g["how"] for g in panel[:3]] == [pv.UNVERIFIED_HOW] * 3
    assert panel[3]["text"] == "2 more" and "not shown in your profile yet" in panel[3]["how"]


def test_strong_partial_and_unverified_mix_counts_only_strong_as_met():
    ev = {
        "blocks": {
            "b_fit": {
                "matches": [
                    {"requirement": "Remote, United States", "evidence": "You are open to "
                     "remote work.", "strength": "strong"},
                    {"requirement": "ERP", "evidence": "Close to your CRM work.",
                     "strength": "adjacent"},
                    {"requirement": "Vendors", "evidence": "Not shown.", "strength": "unverified"},
                ],
                "gaps": [],
            }
        }
    }
    reqs = rv.requirements(OPP, ev)
    assert [r["state"] for r in reqs] == [rv.MET, rv.PART, rv.PART]
    assert rv.wheel(reqs)["center"] == "1 of 3"
    assert [e["quote"] for e in rv.evidence_rows(reqs, {})] == ["Remote, United States", "ERP"]
    assert [r["text"] for r in pv.fit_reasons(ev)] == ["Remote, United States", "ERP"]


def test_the_prompt_asks_for_unverified_and_plain_sentences_in_step_with_prompts_md():
    system = load_prompt("evaluate/v2").system
    md = (ROOT / "PROMPTS.md").read_text("utf-8")
    for phrase in (
        'list it under matches with strength "unverified"',
        'Never mark a requirement "strong" that you cannot point at.',
        "Never copy input labels, field\n   names or true/false values into them",
        'Never cite by position ("bullet 1")',
    ):
        assert phrase in system, phrase
        assert phrase in md, phrase
    assert '"enum": ["strong", "partial", "adjacent", "unverified"]' in md


def test_the_profile_says_yes_or_no_never_a_program_value():
    profile = {"geo_rules": {"remote_ok": True, "relocation_acceptable": False}}
    text = profile_compact(profile)
    assert "Open to remote work: yes." in text and "Open to relocating: no." in text
    assert "True" not in text and "False" not in text and "Remote OK" not in text
    assert "Open to remote work: unspecified." in profile_compact({"geo_rules": {}})


# ------------------------------------------------------------------------------ the page itself
@pytest.mark.usefixtures("_isolate_data_home")
def test_the_evaluation_tab_tells_one_story_for_the_o20_case():
    ids = asyncio.run(role_seed.seed_async())
    ev = _reply()

    async def put():
        from sqlalchemy.ext.asyncio import async_sessionmaker

        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            opp = await s.get(Opportunity, ids["strong"])
            opp.title = "Director, Enterprise Applications"
            row = (
                await s.execute(select(Evaluation).where(Evaluation.opportunity_id == opp.id))
            ).scalars().first()
            row.blocks, row.dimensions = ev["blocks"], ev["dimensions"]
            row.score = row.weighted_score = 2.7
            row.verdict = row.verdict_model = "pass"
            await s.commit()
        await engine.dispose()

    asyncio.run(put())
    from kestrel.web.app import build_app

    with TestClient(build_app(worker=False)) as c:
        html = c.get(f"/opportunities/{ids['strong']}").text
    assert ">2.7<" in html
    assert ">0 of 5<" in html and "5 partly met" in html and "0 met" in html
    assert "No gaps found" not in html
    # No operator profile.yml in this test, so the profile reads thin (#64): the unverified
    # rows point to the fix instead of "can't tell".
    assert html.count(pv.UNVERIFIED_HOW_THIN.replace(">", "&gt;")) == 5
    assert "Nothing the posting asks for is fully covered yet." in html
    assert "Source not recorded" not in html and "cannot be verified" not in html
    assert "Remote OK" not in html and "bullet 1" not in html


# ------------------------------------------------------------- the consulting read (P2C), too
LEAD = SimpleNamespace(track="consulting")


def _lead_blocks(thesis: dict) -> dict:
    return {
        "blocks": {
            "b_thesis": {"engagement": "Automate the weekly report.", **thesis},
            "f_delivery": {"solo_feasible": True, "gaps": ["Data warehouse build"],
                           "partner_needed": None, "effort_shape": "Six weeks, part time."},
        }
    }


def test_the_p2c_schema_grades_each_need_and_sends_unverified_to_the_model():
    from kestrel.llm.schemas import P2CEval, P2CThesis

    t = P2CThesis.model_validate(
        {"engagement": "x", "service_lines": ["Reporting"], "phases": [], "estimated_days": 1,
         "estimated_weeks": 1, "matches": [
             {"requirement": "Report automation", "evidence": "Not shown.",
              "strength": "unverified"}]}
    )
    assert t.matches[0].strength == "unverified"
    schema = json_schema_for(P2CEval)
    thesis = schema["$defs"]["P2CThesis"]
    assert "matches" in thesis["required"]
    assert '"unverified"' in json.dumps(schema)


def test_a_leads_unverified_need_is_partly_met_under_gaps_never_met():
    ev = _lead_blocks(
        {"service_lines": ["Reporting automation"], "matches": [
            {"requirement": "Power BI dashboards", "evidence": "You built these for two "
             "clients.", "strength": "strong"},
            {"requirement": "Finance close process", "evidence": "Your profile has no proof "
             "points yet, so this cannot be verified.", "strength": "unverified"},
        ]}
    )
    reqs = rv.requirements(LEAD, ev)
    assert [(r["text"], r["state"]) for r in reqs] == [
        ("Power BI dashboards", rv.MET),
        ("Finance close process", rv.PART),
        ("Can be delivered on your own", rv.MET),
        ("Data warehouse build", rv.GAP),
    ]
    assert reqs[1]["how"] == rv.UNVERIFIED_HOW and reqs[1]["evidence"] == ""
    assert [e["quote"] for e in rv.evidence_rows(reqs, {})] == [
        "Power BI dashboards", "Can be delivered on your own"]


def test_an_older_lead_reads_bare_service_lines_as_partly_met_not_met():
    reqs = rv.requirements(LEAD, _lead_blocks({"service_lines": ["Reporting automation"]}))
    assert (reqs[0]["text"], reqs[0]["state"]) == ("Reporting automation work", rv.PART)
    assert reqs[0]["how"] == rv.LEGACY_SERVICE_HOW
    assert rv.wheel(reqs)["center"] == "1 of 3"


def test_the_p2c_prompt_asks_for_proof_and_plain_sentences_in_step_with_prompts_md():
    system = load_prompt("evaluate_consulting/v2").system
    md = (ROOT / "PROMPTS.md").read_text("utf-8")
    p2c = md[md.index("## P2C"):md.index("## P4C")]
    for phrase in (
        'use strength\n   "unverified" and say in evidence what the profile does not show.',
        "Never mark a need\n   \"strong\" that you cannot point at.",
        "Never copy input labels, field names or true/false values into\n   them",
        'position ("bullet 1"); the practitioner never sees those labels.',
    ):
        assert phrase in system, phrase
        assert phrase in p2c, phrase
    assert '"enum": ["strong", "partial", "adjacent", "unverified"]' in p2c
    assert '"required": ["engagement", "service_lines", "matches",' in p2c


def test_the_consulting_profile_says_yes_or_no_never_a_program_value():
    text = profile_compact(
        {"consulting_terms": {"service_lines": ["Reporting"]},
         "geo_rules": {"remote_ok": False, "relocation_acceptable": True}}
    )
    assert "Practice:" in text
    assert "Open to remote work: no." in text and "Open to relocating: yes." in text
    assert "True" not in text and "False" not in text


# --------------------------------------------- review follow-ups: every consumer agrees (O20)
def test_a_score_from_the_old_prompt_version_says_score_again():
    opp = SimpleNamespace(track="employment", ghost_score=0.0, liveness=None)
    old = SimpleNamespace(tier=2, prompt_version="evaluate/v1", needs_review=False,
                          legitimacy=None)
    assert "Score again to refresh it." in (pv.warning_text(opp, old) or "")
    new = SimpleNamespace(tier=2, prompt_version="evaluate/v2", needs_review=False,
                          legitimacy=None)
    assert pv.warning_text(opp, new) is None
    lead = SimpleNamespace(track="consulting", ghost_score=0.0, liveness=None)
    old_c = SimpleNamespace(tier=2, prompt_version="evaluate_consulting/v1",
                            needs_review=False, legitimacy=None)
    assert "Score again" in (pv.warning_text(lead, old_c) or "")


def test_tailoring_never_links_or_counts_an_unverified_match_as_covered():
    from kestrel.web.tailor_view import Line
    from kestrel.web.tailor_view import requirements as tailor_reqs

    blocks = {"b_fit": {"matches": [
        {"requirement": "ERP implementation", "evidence": "ERP implementation",
         "strength": "unverified"},
        {"requirement": "Vendor contracts", "evidence": "x", "strength": "partial"},
    ], "gaps": []}}
    lines = [Line("b1", "Led an ERP implementation across three sites."),
             Line("b2", "Negotiated vendor contracts and renewals.")]
    rows = tailor_reqs(blocks, lines)
    assert rows[0] == {"text": "ERP implementation", "status": "n", "slot": "",
                       "note": pv.UNVERIFIED_HOW}
    assert rows[1]["status"] == "p" and rows[1]["slot"] == "b2"


def test_document_selection_gets_no_unverified_match_as_a_match_or_a_gap():
    from kestrel.docs.select import _render_user

    spec = SimpleNamespace(user="M: {block_b.matches[].requirement}\n"
                                "G: {block_b.gaps[].requirement}\n{DOC_SLOT_CATALOG}")
    catalog = SimpleNamespace(sections=[], summary_variants=[], skill_groups=[], layouts=[])
    ev = SimpleNamespace(blocks={"b_fit": {
        "matches": [{"requirement": "Remote, US", "strength": "strong"},
                    {"requirement": "ERP implementation", "strength": "unverified"}],
        "gaps": [{"requirement": "CPA license", "severity": "addressable"}],
    }})
    text = _render_user(spec, opp=None, ev=ev, catalog=catalog)
    assert "M: Remote, US\n" in text and "G: CPA license\n" in text
    assert "ERP implementation" not in text


def _many_gaps(n_real: int, n_unverified: int) -> dict:
    return {"blocks": {"b_fit": {
        "gaps": [{"requirement": f"Gap {i}", "severity": "addressable", "mitigation": "Do X"}
                 for i in range(n_real)],
        "matches": [{"requirement": f"Unknown {i}", "evidence": "-", "strength": "unverified"}
                    for i in range(n_unverified)],
    }}}


def test_the_panel_never_drops_unverified_items_silently():
    rows = pv.gap_rows(_many_gaps(4, 2))
    assert [r["text"] for r in rows] == ["Gap 0", "Gap 1", "Gap 2", "3 more"]
    assert rows[3]["how"].startswith("2 of them are not shown in your profile yet")
    rows = pv.gap_rows(_many_gaps(1, 2))
    assert [r["text"] for r in rows] == ["Gap 0", "Unknown 0", "Unknown 1"]
    rows = pv.gap_rows(_many_gaps(5, 0))
    assert rows[3] == {"text": "2 more", "how": "The Evaluation tab lists them all.",
                       "fatal": False, "unverified": False}


def test_worth_watching_names_an_unverified_item_like_the_panel():
    ev = _many_gaps(0, 1)
    card = rv.read_card(OPP, {**ev, "blocks": {**ev["blocks"],
                                               "d_comp": {"vs_floor": "above_target"}}}, "maybe")
    assert "Worth watching: Unknown 0." in card["text"]


def test_today_never_uses_an_unverified_match_as_why_it_fits():
    from kestrel.web.today_view import why_it_fits

    opp = SimpleNamespace(track="employment")
    ev = {"blocks": {"b_fit": {"matches": [
        {"requirement": "ERP", "evidence": "Your profile has no proof points yet.",
         "strength": "unverified"},
        {"requirement": "Vendors", "evidence": "You ran vendor renewals for six years.",
         "strength": "adjacent"},
    ], "gaps": []}}}
    text = why_it_fits(opp, ev)
    assert "You ran vendor renewals for six years." in text
    assert "no proof points" not in text


def test_the_version_before_it_stays_runnable_for_the_model_check():
    # SPEC-INTERFACES /models check offers "the version before it"; the O20 bump must not remove it.
    from kestrel.llm.prompts import all_versions
    from kestrel.web.pages.bench import has_previous_instructions

    versions = set(all_versions())
    assert {"evaluate/v1", "triage/v1", "evaluate/v2", "triage/v2"} <= versions
    # The files are kept; the choice stays off until runs pass the version through (O21).
    assert not has_previous_instructions()
