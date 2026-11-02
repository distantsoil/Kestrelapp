"""P7 v2, import by line reference (D-IMPORT-3, PROMPTS.md P7). The model answers in line numbers;
the app copies the lines. These tests pin the properties that make that safe: copied text is the
source's own, invented or out-of-role references are surfaced as errors, unplaced bullets are
shown, and the per-call schema only admits line numbers that exist."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from kestrel.docs.imports import (
    LAYOUT_WIDTH,
    _view,
    flag_proposal,
    line_ref_schema,
    number_lines,
    propose_import,
)
from kestrel.llm.client import ChatResult
from kestrel.llm.schemas import P7Outline, P7RoleMap

RESUME = """# Pat Example
pat@example.com
## Summary
IT leader with 15 years across retail and logistics.
## Experience
### Acme Retail, Director of IT
2019 - 2024
Led IT for 300 staff across three countries.
- Cut infrastructure costs 20% by consolidating
  three data centers into one.
- Ran the M&A integration of two acquired warehouses.
- Rolled out SSO to 300 staff.
### Beta Freight, IT Manager
2015 - 2019
- Migrated 12 sites to Microsoft 365.
## Skills
Identity: Entra ID, Okta; Intune
"""

OUTLINE = {
    "summary_blocks": [{"lines": [4]}],
    "roles": [
        {"header_lines": [6, 7], "first_line": 6, "last_line": 12, "company_label": "Acme Retail",
         "title": "Director of IT", "start_date": "2019", "end_date": "2024"},
        {"header_lines": [13, 14], "first_line": 13, "last_line": 15,
         "company_label": "Beta Freight", "title": "IT Manager", "start_date": "2015",
         "end_date": "2019"},
    ],
    "skill_groups": [{"label": "Identity", "lines": [17]}],
}
ROLE1 = {
    "context_lines": [8],
    "bullets": [
        {"key": "cost_reduction", "lines": [9, 10], "optional": False, "tags": ["cost"]},
        {"key": "m_and_a", "lines": [11], "optional": True, "tags": ["integration"]},
    ],
    "proof_points": [{"key": "cost", "lines": [9, 10], "metric": "20%"}],
}
ROLE2 = {
    "context_lines": [],
    "bullets": [{"key": "m365", "lines": [15], "optional": False, "tags": ["cloud"]}],
    "proof_points": [{"key": "m365", "lines": [15], "metric": "15 sites"}],  # not in the text
}


class _Fake:
    """Answers the outline and each role call from fixed payloads, recording what it was sent."""

    def __init__(self, outline=OUTLINE, roles=(ROLE1, ROLE2), fail_role: int | None = None):
        self.outline, self.roles, self.fail_role = outline, list(roles), fail_role
        self.sent: list[dict] = []

    async def complete(self, *, tier, messages, schema=None, schema_name="", **kw):
        self.sent.append({"schema_name": schema_name, "schema": schema,
                          "user": messages[-1]["content"], "first": messages[1]["content"],
                          **kw})
        if "outline" in schema_name:
            return ChatResult(content=json.dumps(self.outline), model="fake")
        i = 0 if "Acme" in messages[1]["content"] else 1  # the original ask, not a re-ask
        if self.fail_role == i:
            return ChatResult(content="not json", model="fake")
        return ChatResult(content=json.dumps(self.roles[i]), model="fake")


def _run(fake) -> object:
    return asyncio.run(propose_import(fake, RESUME, source_format="md"))


def test_text_is_copied_from_the_source_and_the_proposal_is_clean():
    prop = _run(_Fake())
    assert prop.error is None
    acme, beta = prop.value.roles
    cost, mna = acme.slots
    assert cost.key == "r1_cost_reduction" and cost.default_variant_key == "original"
    # A wrapped bullet becomes one variant, joined with a space, marker dropped, words untouched.
    assert cost.variants[0].text == (
        "Cut infrastructure costs 20% by consolidating three data centers into one."
    )
    assert all(not v.derived for s in acme.slots + beta.slots for v in s.variants)
    assert mna.optional and acme.context_line == "Led IT for 300 staff across three countries."
    assert prop.value.summary_variants[0].text.startswith("IT leader with 15 years")
    assert prop.value.skill_groups[0].items == ["Entra ID", "Okta", "Intune"]
    assert prop.flags.clean and flag_proposal(prop.value, RESUME).clean


def test_a_bullet_no_slot_took_is_shown_not_dropped():
    prop = _run(_Fake())
    assert prop.flags.unplaced == ["- Rolled out SSO to 300 staff."]
    assert prop.flags.clean  # shown to the operator; not a reason to refuse the import


def test_a_metric_not_in_the_bullet_is_dropped_not_kept():
    proofs = {p.key: p for p in _run(_Fake()).value.proof_points}
    assert proofs["r1_cost"].metric == "20%" and proofs["r1_cost"].evidence.startswith("Acme")
    assert proofs["r2_m365"].metric is None  # "15 sites" is not what the bullet says


def test_an_invented_line_number_is_an_error_never_defaulted():
    bad = json.loads(json.dumps(ROLE1))
    bad["bullets"][0]["lines"] = [99]
    prop = _run(_Fake(roles=(bad, ROLE2)))
    assert prop.value is None and "do not exist" in prop.error and "99" in prop.error


def test_a_line_from_another_role_is_an_error():
    bad = json.loads(json.dumps(ROLE1))
    bad["bullets"][1]["lines"] = [15]  # Beta's bullet, claimed under Acme
    prop = _run(_Fake(roles=(bad, ROLE2)))
    assert prop.value is None and "role 1" in prop.error


def test_a_role_that_runs_into_the_next_is_clipped_and_double_placed_lines_are_errors():
    """A role whose range runs into the next role's lines is pulled back to end before it
    (D-IMPORT-5); it is no longer a failed import. Two bullets on one line still are."""
    outline = json.loads(json.dumps(OUTLINE))
    outline["roles"][0]["last_line"] = 14  # Acme over-runs into Beta's header
    prop = _run(_Fake(outline=outline))
    assert prop.error is None
    acme, beta = prop.value.roles
    assert acme.company_label == "Acme Retail" and beta.company_label == "Beta Freight"
    dup = json.loads(json.dumps(ROLE1))
    dup["bullets"][1]["lines"] = [10]
    assert "two bullets" in _run(_Fake(roles=(dup, ROLE2))).error


def test_a_failed_role_names_the_role():
    prop = _run(_Fake(fail_role=1))
    assert prop.value is None and prop.error.startswith("Role 2 (Beta Freight")


def test_each_call_is_schema_bound_to_the_lines_it_may_use():
    fake = _Fake()
    _run(fake)
    outline_call = next(c for c in fake.sent if "outline" in c["schema_name"])
    role_calls = [c for c in fake.sent if "role" in c["schema_name"]]
    assert len(role_calls) == 2
    acme = next(c for c in role_calls if "Acme" in c["user"])
    text = json.dumps(acme["schema"])
    assert '"enum": [6, 7, 8, 9, 10, 11, 12]' in text and "15" not in text.split("enum")[1][:40]
    assert '"enum": [1, 2, 3' in json.dumps(outline_call["schema"])
    assert acme.get("reasoning_hint") == "none"  # extraction asks for no thinking


def test_the_layout_view_shortens_long_lines_but_keeps_numbers():
    from kestrel.docs.imports import HEADER_WIDTH

    lines = number_lines("- " + "y" * 300 + "\nAcme " + "z" * 200 + " 2019 - 2024\n")
    bullet, header = _view(lines, sorted(lines), LAYOUT_WIDTH).splitlines()
    assert bullet.startswith("[1] - yyy") and bullet.endswith(" ...")
    assert len(bullet) < LAYOUT_WIDTH + 12
    assert header.endswith("2019 - 2024") and len(header) < HEADER_WIDTH  # dates survive


def test_line_ref_schema_restricts_only_line_fields():
    s = json.dumps(line_ref_schema(P7RoleMap, [3, 4]))
    assert s.count('"enum": [3, 4]') == 3  # context_lines, bullet lines, proof lines
    o = json.dumps(line_ref_schema(P7Outline, [1, 2]))
    assert o.count('"enum": [1, 2]') == 5  # summary, header, first, last, skills


def test_output_is_small_whatever_the_resume_length():
    """The point of the redesign: a role call's answer is bounded by its bullet count, never by
    the length of the text, so a 12-page CV costs more calls, not bigger ones."""
    long_role = {"context_lines": [], "proof_points": [],
                 "bullets": [{"key": f"b{n}", "lines": [n], "optional": False,
                              "tags": ["a", "b"]} for n in range(1, 41)]}
    assert len(json.dumps(long_role)) / 4 < 1200  # 40 bullets in well under 1,200 tokens


# ------------------------------------------------------------------------------- PDF cleanup
def test_pdf_furniture_and_page_numbers_are_dropped_and_listed():
    from kestrel.docs.convert import _drop_page_furniture

    pages = ["Pat Example | 555-0100\nAcme Retail\n- Cut costs 20%.\nPage 1 of 2",
             "Pat Example | 555-0100\nBeta Freight\n- Migrated 12 sites.\nPage 2 of 2"]
    out, removed = _drop_page_furniture(pages)
    assert removed == ["Pat Example | 555-0100", "Page 1 of 2", "Page 2 of 2"]
    assert "- Cut costs 20%." in out[0] and "- Migrated 12 sites." in out[1]
    single, none = _drop_page_furniture(["Pat Example\n- One page only."])
    assert none == [] and "Pat Example" in single[0]  # one page: no header to detect


def test_pdf_cleanup_keeps_content_that_only_looks_repeated():
    """Review findings: bullets differing only in digits, date lines, and a number in the middle
    of a page are content, never furniture."""
    from kestrel.docs.convert import _drop_page_furniture

    pages = ["Pat Example\nAcme\n2015 - 2019\n12\nLed IT.\n- Grew revenue 10%",
             "Pat Example\nBeta\n2019 - 2021\n12\nRan ops.\n- Grew revenue 25%"]
    out, removed = _drop_page_furniture(pages)
    assert removed == ["Pat Example"]
    joined = "\n".join(out)
    for kept in ("2015 - 2019", "2019 - 2021", "- Grew revenue 10%", "- Grew revenue 25%"):
        assert kept in joined
    assert joined.count("\n12\n") == 2  # a mid-page number is not a page number


def test_a_wrapped_bullet_is_rejoined_but_a_heading_after_it_is_not():
    from kestrel.docs.convert import _rejoin_wrapped_bullets

    assert _rejoin_wrapped_bullets(["- Cut costs by consolidating", "three data centers.",
                                    "Beta Freight", "- Next"]) == [
        "- Cut costs by consolidating three data centers.", "Beta Freight", "- Next"]


# ------------------------------------------------------------------------- P7c suggestions
def test_facts_changed_compares_values_with_their_scale():
    from kestrel.docs.imports import facts_changed

    orig = "Cut costs 20% ($1.2M) across 300 staff."
    assert facts_changed(orig, "Reduced spend 20% ($1.2 million) for 300+ staff.") == []
    assert facts_changed(orig, "Reduced spend 25% ($1.2M) for 300 staff.") == ["20%", "25%"]
    assert facts_changed(orig, "Reduced spend for 300 staff.") == ["$1,200,000", "20%"]
    assert facts_changed("Saved $40 million.", "Saved $40 billion.") != []
    assert facts_changed("In 2019, led 12 sites.", "Led 12 sites in 2019.") == []


def test_suggestions_with_changed_numbers_are_marked_not_offered():
    from kestrel.docs.imports import suggest_variants

    class _S:
        async def complete(self, **kw):
            return ChatResult(content=json.dumps({"variants": [
                {"text": "Reduced spend 20% by merging three data centers.", "tags": ["cost"]},
                {"text": "Reduced spend 30% by merging data centers.", "tags": ["cost"]},
                {"text": "Cut costs 20% by consolidating three data centers.", "tags": []},
            ]}), model="fake")

    out, err = asyncio.run(suggest_variants(
        _S(), role_label="Acme", text="Cut costs 20% by consolidating three data centers.",
        tags=["cost"]))
    assert err is None and len(out) == 2  # capped at two; an unchanged copy never counts
    assert out[0].problem is None and "30%" in (out[1].problem or "")


def test_reasoning_hint_reaches_openrouter_only_when_the_operator_set_none():
    from kestrel.config import get_settings
    from kestrel.llm.client import build_request_body

    be = get_settings().llm.backends["frontier"]
    msgs = [{"role": "user", "content": "x"}]
    body = build_request_body(model="m", messages=msgs, backend=be, temperature=0,
                              max_tokens=10, reasoning_hint="low")
    assert body["reasoning"] == {"effort": "low"}
    be2 = be.model_copy(deep=True)
    be2.hosted.reasoning_effort = "high"
    body = build_request_body(model="m", messages=msgs, backend=be2, temperature=0,
                              max_tokens=10, reasoning_hint="low")
    assert body["reasoning"] == {"effort": "high"}  # the operator's setting wins


def _slot_app(monkeypatch, fake_cls):
    from starlette.testclient import TestClient

    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
    from kestrel.db.models import DocMaster, DocSection, DocSlot, DocVariant
    from kestrel.web.app import build_app

    monkeypatch.setattr("kestrel.llm.client.LLMClient", fake_cls)
    migrate.run_migrations(paths.db_path())
    asyncio.run(reset_engine())

    async def seed() -> int:
        async with get_sessionmaker()() as s:
            m = DocMaster(track="employment", name="t", doc_kind="resume",
                          summary_variants=[], skill_groups=[])
            s.add(m)
            await s.flush()
            sec = DocSection(doc_master_id=m.id, section_kind="role", org_label="Acme",
                             title="CIO", sort_order=0)
            s.add(sec)
            await s.flush()
            slot = DocSlot(doc_section_id=sec.id, key="r1_cost", sort_order=0, optional=False,
                           default_variant_key="original")
            s.add(slot)
            await s.flush()
            s.add(DocVariant(doc_slot_id=slot.id, key="original", derived=False, tags=["cost"],
                             text="Cut costs 20% across 300 staff."))
            await s.commit()
            return slot.id

    slot_id = asyncio.run(seed())
    # The seed's engine lived on asyncio.run's loop, now closed; the app builds its own on the
    # TestClient loop. Reusing the dead one leaked a connection (SAWarning) once the Documents
    # page ran one more query.
    forget_engine()
    return TestClient(build_app(worker=False)), slot_id


class _SuggestLLM:
    def __init__(self, settings, http=None, **_):
        self.settings = settings

    async def complete(self, **kw):
        return ChatResult(content=json.dumps({"variants": [
            {"text": "Reduced spend 20% for 300 staff.", "tags": ["cost"]}]}), model="fake")


def test_slot_suggest_and_add_routes_recheck_the_numbers_and_the_limit(monkeypatch):
    client, slot_id = _slot_app(monkeypatch, _SuggestLLM)
    r = client.post(f"/documents/slot/{slot_id}/suggest")
    assert 'data-testid="variant-suggestions"' in r.text and "Reduced spend 20%" in r.text
    add = f"/documents/slot/{slot_id}/variants"
    bad = client.post(add, data={"text": "Reduced spend 35% for 300 staff.", "tags": "cost"})
    assert "changes the numbers" in bad.text  # a forged form cannot change a fact
    ok = client.post(add, data={"text": "Reduced spend 20% for 300 staff.", "tags": "cost"})
    assert "Reduced spend 20% for 300 staff." in ok.text
    client.post(add, data={"text": "Lowered costs 20% for 300 staff.", "tags": ""})
    third = client.post(add, data={"text": "Spend down 20% for 300 staff.", "tags": ""})
    assert "already has 2" in third.text


# ------------------------------------------------------------------- review follow-ups
def test_markdown_bold_is_dropped_from_copied_text_and_still_verbatim():
    md = "### Acme, CIO\n- **Led** the migration of 300 staff.\n"
    outline = {"summary_blocks": [], "skill_groups": [], "roles": [
        {"header_lines": [1], "first_line": 1, "last_line": 2, "company_label": "Acme",
         "title": "CIO", "start_date": "", "end_date": ""}]}
    role = {"context_lines": [], "proof_points": [],
            "bullets": [{"key": "mig", "lines": [2], "optional": False, "tags": []}]}
    prop = asyncio.run(propose_import(_Fake(outline=outline, roles=(role, role)), md))
    assert prop.value.roles[0].slots[0].variants[0].text == "Led the migration of 300 staff."
    assert prop.flags.clean


def test_two_bullets_joined_into_one_get_one_corrective_retry_then_fail():
    bad = json.loads(json.dumps(ROLE1))
    bad["bullets"] = [{"key": "both", "lines": [11, 12], "optional": False, "tags": []}]
    fake = _Fake(roles=(bad, ROLE2))
    prop = _run(fake)
    assert prop.value is None and "separate bullets" in prop.error
    acme_calls = [c for c in fake.sent if "role" in c["schema_name"] and "Acme" in c["first"]]
    assert len(acme_calls) == 2  # the original ask and one corrective re-ask


def test_a_corrected_answer_on_retry_is_accepted():
    bad = json.loads(json.dumps(ROLE1))
    bad["bullets"][0]["lines"] = [99]

    class _FixOnRetry(_Fake):
        async def complete(self, *, messages, **kw):
            if "Acme" in messages[1]["content"] and len(messages) > 2:
                self.roles[0] = ROLE1
            return await super().complete(messages=messages, **kw)

    prop = _run(_FixOnRetry(roles=(bad, ROLE2)))
    assert prop.error is None and prop.value.roles[0].slots[0].key == "r1_cost_reduction"


def test_a_missed_role_is_unplaced_and_an_unmapped_wrap_rejoins_its_bullet():
    outline = json.loads(json.dumps(OUTLINE))
    outline["roles"] = outline["roles"][:1]  # the outline missed Beta Freight
    role1 = json.loads(json.dumps(ROLE1))
    role1["bullets"][0]["lines"] = [9]  # the wrapped second line (10) left unmapped
    prop = _run(_Fake(outline=outline, roles=(role1, ROLE2)))
    # D-IMPORT-6: line 10 starts lowercase after an unfinished line, so it is the same bullet.
    assert "three data centers into one." not in prop.flags.unplaced
    assert prop.value.roles[0].slots[0].variants[0].text == (
        "Cut infrastructure costs 20% by consolidating three data centers into one."
    )
    assert "- Migrated 12 sites to Microsoft 365." in prop.flags.unplaced
    assert prop.flags.clean


def test_a_wrap_carries_its_proof_point_and_a_capitalized_line_is_never_joined():
    from kestrel.docs.imports import _continues

    role1 = json.loads(json.dumps(ROLE1))
    role1["bullets"][0]["lines"] = [9]
    role1["proof_points"][0]["lines"] = [9]
    prop = _run(_Fake(roles=(role1, ROLE2)))
    assert prop.value.proof_points[0].claim.endswith("three data centers into one.")
    # A capitalized line is its own bullet unless the line before plainly runs on (review).
    assert not _continues("Led IT for 300 staff", "Rolled out SSO to 300 staff")
    assert not _continues("Cut costs by 20%", "\uf0b7 Ran the M&A integration")
    assert _continues("Built the platform for", "Microsoft 365 and Entra ID")
    assert _continues("Consolidated data centers,", "Networks and identity")
    assert _continues("Rolled out SSO to", "300 staff")


def test_the_operator_places_unplaced_lines_in_skills_or_a_role():
    import pytest

    from kestrel.docs.imports import ImportRejected, place_unplaced

    prop = _run(_Fake())
    value = place_unplaced(prop.value, RESUME, [
        ("- Rolled out SSO to 300 staff.", "role:0"),
        ("Identity: Entra ID, Okta; Intune", "skills"),
        ("## Experience", ""),  # left out
    ])
    added = value.roles[0].slots[-1]
    assert added.variants[0].text == "Rolled out SSO to 300 staff."
    assert not added.variants[0].derived and added.key not in {
        s.key for s in value.roles[0].slots[:-1]}
    extra = value.skill_groups[-1]
    assert extra.label == "Additional skills" and extra.items == ["Entra ID", "Okta", "Intune"]
    assert flag_proposal(value, RESUME).clean
    assert len(prop.value.roles[0].slots) == 2  # the reviewed proposal itself is not changed
    with pytest.raises(ImportRejected):  # a line the operator never wrote is refused
        place_unplaced(prop.value, RESUME, [("- Invented a moon base.", "role:0")])


def test_role_details_not_in_the_header_are_flagged():
    outline = json.loads(json.dumps(OUTLINE))
    outline["roles"][0]["start_date"] = "2018"  # the header says 2019
    prop = _run(_Fake(outline=outline))
    # A date comes from the role's own header line, never from the model: the header's is used
    # and the review says so (the old substring check passed a date found anywhere in the header).
    assert prop.flags.unverified == []
    assert prop.flags.dates_taken == ["role 1 start date: 2019 (the model said 2018)"]
    assert prop.value.roles[0].start_date == "2019"


def test_a_raising_role_task_is_reported_not_raised():
    class _Boom(_Fake):
        async def complete(self, *, messages, **kw):
            if "role" in kw.get("schema_name", "") and "Beta" in messages[1]["content"]:
                raise RuntimeError("hook failed")
            return await super().complete(messages=messages, **kw)

    prop = _run(_Boom())
    # The exception's text stays in the log; the page gets a fixed sentence (D-SEC-4).
    assert prop.value is None and "Role 2" in prop.error and "hook failed" not in prop.error
    assert "details are in the log" in prop.error


def test_a_year_starting_a_line_is_not_a_list_marker():
    from kestrel.docs.imports import _join

    assert _join({1: "2019. Promoted to VP."}, [1]) == "2019. Promoted to VP."
    assert _join({1: "3) Cut costs."}, [1]) == "Cut costs."


# ------------------------------------------------------------- Documents page (D-UI-18)
def test_documents_page_reads_in_the_operators_terms_and_role_details_save(monkeypatch):
    from kestrel.web.pages.documents import slot_label

    assert slot_label("r2_m_and_a_integration") == "M and a integration"
    assert slot_label("r12_cost") == "Cost" and slot_label("") == ""
    client, slot_id = _slot_app(monkeypatch, _SuggestLLM)
    html = client.get("/documents").text
    assert "Bullet 1 · Cost" in html and "r1_cost" in html  # readable first, key still shown
    assert "Your wording" in html and 'data-testid="doc-help"' in html
    assert "Edit role details" in html and 'name="org_label"' in html
    assert "—" not in html
    sec_id = int(html.split('hx-post="/documents/section/')[1].split('"')[0])
    r = client.post(f"/documents/section/{sec_id}", data={
        "org_label": "Acme Retail", "title": "Director of IT", "start_date": "2019",
        "end_date": "2024", "context_line": ""})
    assert r.status_code == 200 and 'class="saved"' in r.text and 'value="Acme Retail"' in r.text
    assert "Director of IT" in client.get("/documents").text


def test_saving_a_wording_shows_saved_and_a_preview_failure_is_visible(monkeypatch):
    client, slot_id = _slot_app(monkeypatch, _SuggestLLM)
    html = client.get("/documents").text
    vid = int(html.split('hx-post="/documents/variant/')[1].split('"')[0])
    r = client.post(f"/documents/variant/{vid}", data={"text": "Cut costs 20% across 300 staff.",
                                                        "tags": "cost"})
    assert r.status_code == 200 and 'class="saved"' in r.text
    monkeypatch.setattr("kestrel.docs.render.render_html",
                        lambda doc: (_ for _ in ()).throw(RuntimeError("no template")))
    import re

    mid = int(re.search(r"/documents/(\d+)/preview", html).group(1))
    r = client.post(f"/documents/{mid}/preview")
    assert r.status_code == 200 and "Preview failed: RuntimeError." in r.text
    assert "no template" not in r.text  # exception text stays in the log


def test_pursuits_links_to_the_applied_import_and_render_review_links_back(monkeypatch):
    client, _ = _slot_app(monkeypatch, _SuggestLLM)
    assert 'data-testid="applied-import-link"' in client.get("/pursuits").text
    assert 'data-testid="render-back"' in client.get("/renders/new").text



PROMOTED = """# Pat Example
## Experience
Harborview Clinics | Clinic Director Springfield, ZZ | May 2022 - Apr 2023
Senior Nurse Coordinator Jun 2021 - May 2022
Moved up after one year of steady results.
- Opened two satellite clinics.
- Reduced annual supply spend by 20%.
Tidewater Care | Program Lead Shelbyville, ZZ | Sep 2016 - Mar 2021
- Trained more than 50 volunteers.
"""


def test_a_promotion_mapped_as_two_overlapping_roles_is_merged_into_one():
    """A free model answered one promoted role as two roles over the same body, which failed the
    import with "overlaps another role". Two titles on consecutive header lines are one role
    (D-IMPORT-5)."""
    outline = {"summary_blocks": [], "skill_groups": [], "roles": [
        {"header_lines": [3], "first_line": 3, "last_line": 7,
         "company_label": "Harborview Clinics", "title": "Clinic Director",
         "start_date": "May 2022", "end_date": "Apr 2023"},
        {"header_lines": [4], "first_line": 4, "last_line": 7,
         "company_label": "Harborview Clinics", "title": "Senior Nurse Coordinator",
         "start_date": "Jun 2021",
         "end_date": "May 2022"},
        {"header_lines": [8], "first_line": 8, "last_line": 9, "company_label": "Tidewater Care",
         "title": "Program Lead", "start_date": "Sep 2016", "end_date": "Mar 2021"},
    ]}
    clinic = {"context_lines": [5], "bullets": [
        {"key": "clinics", "lines": [6], "optional": False, "tags": []},
        {"key": "cost", "lines": [7], "optional": False, "tags": []}], "proof_points": []}
    care = {"context_lines": [], "bullets": [
        {"key": "volunteers", "lines": [9], "optional": False, "tags": []}], "proof_points": []}

    class _ByCompany(_Fake):
        async def complete(self, *, tier, messages, schema=None, schema_name="", **kw):
            if "outline" in schema_name:
                return ChatResult(content=json.dumps(self.outline), model="fake")
            body = clinic if "Harborview" in messages[1]["content"] else care
            return ChatResult(content=json.dumps(body), model="fake")

    prop = asyncio.run(propose_import(_ByCompany(outline=outline), PROMOTED))
    assert prop.error is None, prop.error
    roles = prop.value.roles
    assert [r.company_label for r in roles] == ["Harborview Clinics", "Tidewater Care"]
    assert roles[0].title == "Clinic Director" and roles[0].start_date == "Jun 2021"
    assert roles[0].end_date == "Apr 2023"
    texts = [v.text for s in roles[0].slots for v in s.variants]
    assert "Opened two satellite clinics." in texts  # verbatim, nothing invented


def test_accept_writes_the_operators_placements_and_the_proposal_offers_them(monkeypatch):
    from sqlalchemy import select

    from kestrel.db.engine import get_sessionmaker
    from kestrel.db.models import DocSlot, DocVariant

    client, _slot_id = _slot_app(monkeypatch, _SuggestLLM)
    prop = _run(_Fake())
    r = client.post("/api/documents/import/accept", data={
        "proposal_json": prop.value.model_dump_json(), "text": RESUME, "track": "employment",
        "doc_kind": "resume",
        "unplaced_line": ["- Rolled out SSO to 300 staff."], "unplaced_to": ["role:0"],
    })
    assert "Created." in r.text

    async def texts():
        async with get_sessionmaker()() as s:
            return set((await s.execute(
                select(DocVariant.text).join(DocSlot, DocVariant.doc_slot_id == DocSlot.id)
            )).scalars())

    assert "Rolled out SSO to 300 staff." in asyncio.run(texts())

    from kestrel.web.deps import templates

    html = templates().get_template("partials/import_proposal.html").render(
        proposal=prop, markdown=RESUME, track="employment", doc_kind="resume")
    assert 'name="unplaced_to"' in html and 'value="role:1"' in html
    assert 'value="skills"' in html and "Add to Beta Freight" in html


def _wrapped_context_import(md: str, header_lines: list[int], context_lines=()):
    """Run the import on `md` with a model that mapped one promoted role (header lines as given)
    and left the context lines out, as the owner's free model did."""
    outline = {"summary_blocks": [], "skill_groups": [], "roles": [
        {"header_lines": header_lines, "first_line": 3, "last_line": 8,
         "company_label": "Larkspur Library Network", "title": "Branch Manager",
         "start_date": "Mar 2017", "end_date": "Jan 2024"},
        {"header_lines": [9], "first_line": 9, "last_line": 10,
         "company_label": "Quillfeather Books", "title": "Buyer",
         "start_date": "Jun 2014", "end_date": "Feb 2017"},
    ]}
    lib = {"context_lines": list(context_lines), "bullets": [
        {"key": "reading", "lines": [7], "optional": False, "tags": []},
        {"key": "notices", "lines": [8], "optional": False, "tags": []}], "proof_points": []}
    buyer = {"context_lines": [], "bullets": [
        {"key": "catalog", "lines": [10], "optional": False, "tags": []}], "proof_points": []}

    class _ByCompany(_Fake):
        async def complete(self, *, tier, messages, schema=None, schema_name="", **kw):
            if "outline" in schema_name:
                return ChatResult(content=json.dumps(outline), model="fake")
            body = lib if "Larkspur" in messages[1]["content"] else buyer
            return ChatResult(content=json.dumps(body), model="fake")

    return asyncio.run(propose_import(_ByCompany(), md, source_format="md"))


def _fixture_md() -> str:
    from pathlib import Path

    return (Path(__file__).parent / "fixtures" / "docs" / "promotion_wrapped_context.md"
            ).read_text(encoding="utf-8")


_WRAP1 = ("Moved into branch management after three years on the front desk of a nine-branch "
          "public")
_WRAP2 = ("library system serving a mid-sized county. Ran a staff of 6 and set the yearly "
          "programming calendar.")


def test_a_wrapped_context_paragraph_under_a_promotion_header_is_one_placed_line():
    """#84: two header lines (a promotion), then a context paragraph wrapped over two printed
    lines that the role map left out. It used to be reported as two unplaced lines."""
    prop = _wrapped_context_import(_fixture_md(), [3, 4])
    assert prop.error is None, prop.error
    assert prop.flags.unplaced == []
    assert prop.value.roles[0].context_line == f"{_WRAP1} {_WRAP2}"
    assert prop.flags.clean  # verbatim apart from the single-space join
    assert prop.value.roles[1].context_line == ""  # nothing invented for a role without one


def test_a_missed_second_title_line_misplaces_nothing_and_the_warning_still_lists_it():
    """header_lines=[3]: the model left the second title line (4) out. The line under the header
    is then a title and date line, which is never context; everything stays in the warning."""
    prop = _wrapped_context_import(_fixture_md(), [3])
    assert prop.error is None, prop.error
    assert prop.value.roles[0].context_line == ""
    assert prop.flags.unplaced == [
        "Circulation Supervisor                                                  "
        "Mar 2017 \u2013 Jan 2020",
        _WRAP1, _WRAP2,
    ]


def test_a_lone_date_a_location_or_a_sub_heading_under_a_header_stays_unplaced():
    md = _fixture_md()
    head = md.splitlines()
    for replaced in ("Mar 2017 \u2013 Jan 2020", "Dunmore, ZZ", "Key accomplishments:",
                     "Selected Achievements"):
        lines = list(head)
        lines[3] = replaced  # line 4 of the file is the second header line; now the candidate
        lines[4] = "and ran a staff of 6 in the branch."  # reads as its continuation
        prop = _wrapped_context_import("\n".join(lines) + "\n", [3], context_lines=())
        assert prop.error is None, prop.error
        assert prop.value.roles[0].context_line == "", replaced
        assert replaced in prop.flags.unplaced, replaced
    # The same shapes directly: a prose line is context, these are not.
    from kestrel.docs.imports import _not_context

    assert not _not_context(_WRAP1)
    assert _not_context("Jun 2021 \u2013 Present") and _not_context("2019 - 2024")
    assert _not_context("Key accomplishments:") and _not_context("Selected Achievements")
    assert _not_context("Dunmore, ZZ  |  Feb 2020")
    # A line that ends on a month and year or a bare year, with or without a dash, is a date line.
    for dated in ("Senior Manager, Software Systems and Support  Jun 2021 \u2013", "May 2022",
                  "Coordinator of Intake Services  2019 -", "Lead Planner  Sep 2016"):
        assert _not_context(dated), dated
    assert not _not_context("Grew the program from 3 sites to 9 by 2022 with the same staff.")


# ------------------------------------------------------------------ #102: a role never vanishes
def _five_roles():
    """The invented one-column resume, its numbered lines, and what a correct outline holds."""
    from kestrel.docs.imports import number_lines

    md = (Path(__file__).parent / "fixtures" / "docs" / "five_roles_one_column.md"
          ).read_text(encoding="utf-8")
    lines = number_lines(md)
    heads = [n for n, t in lines.items() if " | " in t and "20" in t and "@" not in t]
    heads = [n for n in heads if not t_is_edu(lines[n])]
    sec = {t.strip(): n for n, t in lines.items() if t.startswith("## ")}
    ends = [*(h - 1 for h in heads[1:]), sec["## Skills"] - 1]
    roles = [{"header_lines": [h], "first_line": h, "last_line": e,
              "company_label": lines[h].split(" | ")[0], "title": "Staff",
              "start_date": "", "end_date": ""} for h, e in zip(heads, ends, strict=True)]
    summary = [{"lines": [sec["## Summary"] + 1]}]
    skills = [{"label": "Planning", "lines": [sec["## Skills"] + 1]}]
    return md, lines, heads, roles, summary, skills


def t_is_edu(text: str) -> bool:
    return text.startswith("B.S.")


class _OutlineFake:
    """Answers the outline from `answers` (one per call, the last repeating) and every role call
    from the numbered lines it is shown, so no line numbers are written by hand."""

    def __init__(self, answers):
        self.answers, self.outline_calls, self.asked = list(answers), 0, []

    async def complete(self, *, tier, messages, schema=None, schema_name="", **kw):
        text = messages[-1]["content"]
        if "outline" in schema_name:
            self.asked.append(text)
            i = min(self.outline_calls, len(self.answers) - 1)
            self.outline_calls += 1
            return ChatResult(content=json.dumps(self.answers[i]), model="fake")
        rows = [(int(a), b) for a, b in re.findall(r"^\[(\d+)\] (.*)$", messages[1]["content"],
                                                    re.M)]
        body = {"context_lines": [n for n, t in rows[1:] if not t.startswith("- ")],
                "bullets": [{"key": f"b{n}", "lines": [n], "optional": False, "tags": []}
                            for n, t in rows if t.startswith("- ")],
                "proof_points": []}
        return ChatResult(content=json.dumps(body), model="fake")


def _outline(roles, summary, skills):
    return {"summary_blocks": summary, "roles": roles, "skill_groups": skills}


def test_a_complete_outline_gets_no_reask_and_no_missed_role_warning():
    md, lines, heads, roles, summary, skills = _five_roles()
    fake = _OutlineFake([_outline(roles, summary, skills)])
    prop = asyncio.run(propose_import(fake, md, source_format="md"))
    assert prop.error is None, prop.error
    assert fake.outline_calls == 1 and prop.flags.missed_roles == []
    assert len(prop.value.roles) == 5
    # Everything else is still reported, never dropped: the education line is not a group.
    assert prop.flags.unplaced == ["B.S. in Business, State University, 2009"]


def test_a_missed_role_triggers_one_reask_and_the_full_answer_is_placed():
    md, lines, heads, roles, summary, skills = _five_roles()
    fake = _OutlineFake([_outline(roles[:1], summary, skills), _outline(roles, summary, skills)])
    prop = asyncio.run(propose_import(fake, md, source_format="md"))
    assert prop.error is None, prop.error
    assert fake.outline_calls == 2
    for h in heads[1:]:  # the re-ask names the four lines it is about
        assert f"[{h}] " in fake.asked[1]
    assert len(prop.value.roles) == 5 and prop.flags.missed_roles == []
    assert [r.company_label for r in prop.value.roles][-1] == "Elm Street Hardware"


def test_roles_still_missing_after_the_reask_are_warned_and_their_lines_unplaced():
    from structlog.testing import capture_logs

    md, lines, heads, roles, summary, skills = _five_roles()
    fake = _OutlineFake([_outline(roles[:1], summary, skills)])
    with capture_logs() as logs:
        prop = asyncio.run(propose_import(fake, md, source_format="md"))
    assert prop.error is None, prop.error
    assert fake.outline_calls == 2 and len(prop.value.roles) == 1
    assert prop.flags.missed_roles == [lines[h].strip() for h in heads[1:]]
    assert len(prop.flags.missed_roles) == 4
    unplaced = prop.flags.unplaced
    for h in heads[1:]:
        assert lines[h].strip() in unplaced
    assert "- Managed the paper budget for two plants." in unplaced
    assert "- Kept the paint counter stocked." in unplaced
    # The import logs counts and outcomes, never resume text.
    events = {e["event"]: e for e in logs}
    assert events["resume_import_outline"]["reasked"] is True
    assert events["resume_import_outline"]["missed_roles"] == 4
    assert events["resume_import_result"]["unplaced"] == len(unplaced)
    assert "Alder" not in repr(logs) and "paper budget" not in repr(logs)


def test_both_review_templates_show_the_missed_role_warning():
    tpl_dir = Path(__file__).parent.parent / "kestrel" / "web" / "templates" / "partials"
    for name in ("import_proposal.html", "onboarding_proposal.html"):
        src = (tpl_dir / name).read_text(encoding="utf-8")
        assert "proposal.flags.missed_roles" in src and "import-missed-roles" in src, name
        assert "may have missed" in src, name


# ------------------------------------------- merged roles are split; a real promotion is not
def test_five_roles_merged_into_one_range_are_reasked_then_split_on_their_headers():
    md, lines, heads, roles, summary, skills = _five_roles()
    merged = dict(roles[0], last_line=roles[-1]["last_line"])  # one range over all five
    fake = _OutlineFake([_outline([merged], summary, skills)])
    prop = asyncio.run(propose_import(fake, md, source_format="md"))
    assert prop.error is None, prop.error
    assert fake.outline_calls == 2  # one re-ask, which named the four headers
    for h in heads[1:]:
        assert f"[{h}] " in fake.asked[1]
    assert [r.company_label for r in prop.value.roles] == [
        "Alder Foods", "Bramble Logistics", "Cobalt Print Works", "Dovetail Supply",
        "Elm Street Hardware"]
    assert (prop.value.roles[1].start_date, prop.value.roles[1].end_date) == (
        "Feb 2018", "Dec 2020")
    assert prop.flags.split_roles == 4 and prop.flags.missed_roles == []
    texts = [v.text for s in prop.value.roles[4].slots for v in s.variants]
    assert "Kept the paint counter stocked." in texts and prop.flags.clean


def test_a_reask_that_separates_the_merged_roles_needs_no_split():
    md, lines, heads, roles, summary, skills = _five_roles()
    merged = dict(roles[0], last_line=roles[-1]["last_line"])
    fake = _OutlineFake([_outline([merged], summary, skills), _outline(roles, summary, skills)])
    prop = asyncio.run(propose_import(fake, md, source_format="md"))
    assert fake.outline_calls == 2 and len(prop.value.roles) == 5
    assert prop.flags.split_roles == 0


def test_a_genuine_promotion_stays_one_role_with_no_reask():
    md = (
        "# Pat Example\n## Experience\n"
        "Harborview Clinics | Springfield, ZZ  May 2022 – Apr 2023\n"
        "Harborview Clinics | Senior Nurse Coordinator  Jun 2021 – May 2022\n"
        "Nurse Aide                                          Jan 2019 – May 2021\n"
        "- Opened two satellite clinics.\n"
    ).replace("\n", "\n")
    outline = _outline([{"header_lines": [3, 4, 5], "first_line": 3, "last_line": 6,
                         "company_label": "Harborview Clinics", "title": "Clinic Director",
                         "start_date": "Jan 2019", "end_date": "Apr 2023"}], [], [])
    fake = _OutlineFake([outline])
    prop = asyncio.run(propose_import(fake, md, source_format="md"))
    assert prop.error is None, prop.error
    assert fake.outline_calls == 1 and len(prop.value.roles) == 1
    assert prop.flags.split_roles == 0 and prop.flags.missed_roles == []


def test_both_review_templates_note_a_split():
    tpl_dir = Path(__file__).parent.parent / "kestrel" / "web" / "templates" / "partials"
    for name in ("import_proposal.html", "onboarding_proposal.html"):
        src = (tpl_dir / name).read_text(encoding="utf-8")
        assert "proposal.flags.split_roles" in src and "the model had merged" in src, name


def test_the_outline_prompt_states_the_promotion_rule_in_spec_and_prompt():
    root = Path(__file__).parent.parent
    prompt = (root / "kestrel/llm/prompts/resume_outline.v1.md").read_text(encoding="utf-8")
    spec = (root / "PROMPTS.md").read_text(encoding="utf-8")
    assert "version: resume_outline/v1" in prompt  # no version bump: the file is not hashed
    assert "always starts a new role" in prompt and "always starts a new role" in spec
    assert "name the same\n  employer" in prompt


# ---------------------------------------------------------- a wrapped proof point is one claim
_WRAPPED_PROOF = """# Pat Example
## Experience
### Acme Retail, Director of IT
2019 - 2024
- Closed renewals worth ~
$450K across twelve accounts.
- Ran the desk.
"""
_PROOF_OUTLINE = {"summary_blocks": [], "skill_groups": [], "roles": [
    {"header_lines": [3, 4], "first_line": 3, "last_line": 7, "company_label": "Acme Retail",
     "title": "Director of IT", "start_date": "2019", "end_date": "2024"}]}


def _proof_run(bullet_lines, proofs):
    role = {"context_lines": [], "bullets": [
        {"key": "renewals", "lines": bullet_lines, "optional": False, "tags": []},
        {"key": "desk", "lines": [7], "optional": False, "tags": []}],
        "proof_points": [{"key": f"p{i}", "lines": ln, "metric": None}
                         for i, ln in enumerate(proofs)]}

    class _One(_Fake):
        async def complete(self, *, tier, messages, schema=None, schema_name="", **kw):
            body = self.outline if "outline" in schema_name else role
            return ChatResult(content=json.dumps(body), model="fake")

    return asyncio.run(propose_import(_One(outline=_PROOF_OUTLINE), _WRAPPED_PROOF,
                                      source_format="md"))


def test_a_bullet_wrapped_at_a_dollar_figure_gives_one_proof_point_with_both_lines():
    claim = "Closed renewals worth ~ $450K across twelve accounts."
    # The model took only the first printed line of the bullet; the wrap was left unplaced.
    prop = _proof_run([5], [[5]])
    assert prop.error is None, prop.error
    assert [p.claim for p in prop.value.proof_points] == [claim]
    assert prop.flags.unplaced == [] and prop.flags.clean
    # The model took the whole bullet but cited only its first line.
    prop = _proof_run([5, 6], [[5]])
    assert [p.claim for p in prop.value.proof_points] == [claim]
    # The model split the one claim into two proof points: one claim remains.
    prop = _proof_run([5, 6], [[5], [6]])
    assert [p.claim for p in prop.value.proof_points] == [claim]


def test_a_proof_point_that_joins_two_separate_bullets_is_refused_like_a_bullet_would_be():
    prop = _proof_run([5, 6], [[5, 7]])
    assert prop.error is not None and "not consecutive" in prop.error


# ------------------------------------- a header outside its own range is repaired, not fatal
def test_a_header_just_outside_its_roles_range_is_repaired_and_the_import_succeeds():
    """#102: "role 2 header lines [28] fall outside the role" failed the whole import."""
    md, lines, heads, roles, summary, skills = _five_roles()
    bad = [dict(r) for r in roles]
    bad[1] = dict(bad[1], first_line=heads[1] + 1)  # its header sits one line above its range
    fake = _OutlineFake([_outline(bad, summary, skills)])
    prop = asyncio.run(propose_import(fake, md, source_format="md"))
    assert prop.error is None, prop.error
    assert [r.company_label for r in prop.value.roles][:3] == [
        "Alder Foods", "Bramble Logistics", "Cobalt Print Works"]
    assert len(prop.value.roles) == 5 and prop.flags.missed_roles == []


def test_a_header_inside_another_roles_range_pulls_that_role_back_and_splits_the_roles():
    md, lines, heads, roles, summary, skills = _five_roles()
    bad = [dict(r) for r in roles]
    bad[0] = dict(bad[0], last_line=heads[1])  # role 1 runs over role 2's header line
    bad[1] = dict(bad[1], first_line=heads[1] + 1)
    fake = _OutlineFake([_outline(bad, summary, skills)])
    prop = asyncio.run(propose_import(fake, md, source_format="md"))
    assert prop.error is None, prop.error
    assert len(prop.value.roles) == 5 and prop.flags.missed_roles == []
    first = [v.text for s in prop.value.roles[0].slots for v in s.variants]
    assert "Moved the weekly order from phone calls to a shared sheet." in first
    assert prop.value.roles[1].company_label == "Bramble Logistics"


def test_an_inconsistent_outline_is_a_warning_with_unplaced_lines_not_a_failed_import():
    md, lines, heads, roles, summary, skills = _five_roles()
    bad = [dict(roles[0]), dict(roles[2])]
    # Role 2's header cannot be reached: role 3's header lies between it and its range.
    bad.append(dict(roles[3], header_lines=[heads[1]], company_label="Dovetail Supply"))
    fake = _OutlineFake([_outline(bad, summary, skills)])
    prop = asyncio.run(propose_import(fake, md, source_format="md"))
    assert prop.error is None, prop.error
    assert lines[heads[1]].strip() in prop.flags.missed_roles  # the unreachable header is named
    assert "- Reduced late deliveries by 9% in one year." in prop.flags.unplaced
    assert prop.value is not None and len(prop.value.roles) >= 3


# ----------------------------- dates from the header; a header found by its date range alone
def _headers_fixture():
    md = (Path(__file__).parent / "fixtures" / "docs" / "five_roles_headers.md"
          ).read_text(encoding="utf-8")
    from kestrel.docs.imports import number_lines

    lines = number_lines(md)
    heads = [n for n, t in lines.items() if re.search(r"\d{4} \u2013 (?:\w+ \d{4}|Present)$", t)
             and "Example State" not in t]
    sec = {t.strip(): n for n, t in lines.items() if t.startswith("## ")}
    return md, lines, heads, sec


def _role(lines, head, last, company, start="", end=""):
    return {"header_lines": [head], "first_line": head, "last_line": last,
            "company_label": company, "title": "Staff", "start_date": start, "end_date": end}


def test_a_two_role_outline_is_split_into_five_with_dates_from_each_header():
    md, lines, heads, sec = _headers_fixture()
    assert len(heads) == 5
    exp_end = sec["## Skills"] - 1
    outline = _outline(
        # role 1 carries a start date that belongs to role 2; role 1's range runs over role 2's
        # header, and role 3's over roles 4 and 5
        [_role(lines, heads[0], heads[2] - 1, "Marlin Cove Services", "Sep 2019", "Present"),
         _role(lines, heads[2], exp_end, "Pinewood Labs", "Jun 2017", "Aug 2019")],
        [{"lines": [sec["## Summary"] + 1]}],
        [{"label": "Tools", "lines": [sec["## Skills"] + 1]}])
    fake = _OutlineFake([outline])
    prop = asyncio.run(propose_import(fake, md, source_format="md"))
    assert prop.error is None, prop.error
    assert fake.outline_calls == 2  # one re-ask, then the split in code
    roles = prop.value.roles
    assert len(roles) == 5 and prop.flags.split_roles == 3
    # The header inside role 1's range is a role of its own.
    assert roles[1].start_date == "Sep 2019" and roles[1].end_date == "Jan 2022"
    # "Title | Company, City" is ambiguous (which side is the company?), so the company stays
    # empty and the review's note asks the person; a title is never taken for a company.
    assert "Larch Hollow" in lines[heads[1]] and roles[1].company_label == ""
    assert any(u.startswith("role 2 company: not clear") for u in prop.flags.unverified)
    # Role 1's date comes from its header, and the review says so.
    assert (roles[0].start_date, roles[0].end_date) == ("Mar 2022", "Present")
    assert any(d.startswith("role 1 start date: Mar 2022 (the model said Sep 2019)")
               for d in prop.flags.dates_taken)
    assert [r.end_date for r in roles] == ["Present", "Jan 2022", "Aug 2019", "May 2017",
                                           "Dec 2014"]
    # The Education line has a date range and is not a job.
    edu = "B.A. in Communication, Example State University, 2008 \u2013 2012"
    assert edu in prop.flags.unplaced and prop.flags.missed_roles == []
    texts = [v.text for s in roles[4].slots for v in s.variants]
    assert "Kept the stock room tidy." in texts and prop.flags.clean


def test_a_start_date_that_is_the_roles_own_end_date_is_replaced_by_the_headers():
    """The old check looked for the date anywhere in the header, so a start date that was really
    the end date, or any other date in that header, passed."""
    md, lines, heads, sec = _headers_fixture()
    exp_end = sec["## Skills"] - 1
    bounds = [*heads[1:], exp_end + 1]
    roles = [_role(lines, h, bounds[i] - 1, lines[h].split(" | ")[0], "", "")
             for i, h in enumerate(heads)]
    roles[0] = dict(roles[0], start_date="Present", end_date="Mar 2022")
    prop = asyncio.run(propose_import(_OutlineFake([_outline(roles, [], [])]), md,
                                      source_format="md"))
    assert prop.error is None, prop.error
    first = prop.value.roles[0]
    assert (first.start_date, first.end_date) == ("Mar 2022", "Present")
    assert not any("date" in u for u in prop.flags.unverified)
    assert len(prop.flags.dates_taken) >= 2
    assert all("title" in u for u in prop.flags.unverified)  # the invented title "Staff"


def test_both_review_templates_show_the_dates_taken_note():
    tpl_dir = Path(__file__).parent.parent / "kestrel" / "web" / "templates" / "partials"
    for name in ("import_proposal.html", "onboarding_proposal.html"):
        src = (tpl_dir / name).read_text(encoding="utf-8")
        assert "dates_taken" in src and "Dates taken from the role's header line" in src, name


def test_a_dated_line_under_education_or_in_prose_is_not_a_job_header():
    from kestrel.docs.imports import _dated, _non_role_lines

    assert _dated("Help Desk Intern | Paranet Solutions, Addison, TX Sep 2019 \u2013 Jan 2021")
    assert _dated("Acme Corp  2015 to Present") and _dated("2015 - 2019  Acme Corp")
    assert not _dated("- Led the 2015 - 2019 migration programme")  # a bullet
    assert not _dated("From 2015 to 2019 the team grew from three people to forty over time")
    lines = {1: "## Experience", 2: "Acme | Town  2015 - 2019", 3: "## Education",
             4: "B.A., X University 2015 - 2019", 5: "Certifications", 6: "Cert A 2016 - 2017"}
    assert _non_role_lines(lines) == {3, 4, 5, 6}


# ------------------------- #102 on a real machine: nothing outside the roles drops silently
def test_a_summary_the_outline_left_out_is_reported_with_every_other_unplaced_line():
    """A plain-text resume (no markdown headings): name and contact, a PROFESSIONAL SUMMARY of
    three lines, five roles, SKILLS, and a trailing section. The outline found no summary and
    only one skills group; the summary and the last section used to vanish with no warning."""
    md = (Path(__file__).parent / "fixtures" / "docs" / "plain_text_resume.txt"
          ).read_text(encoding="utf-8")
    from kestrel.docs.imports import number_lines

    lines = number_lines(md)
    assert len(lines) == 35
    heads = [n for n, t in lines.items() if re.search(r"\d{4} – (?:\w+ \d{4}|Present)$", t)]
    assert heads == [9, 18, 21, 25, 28]
    ends = [*(h - 1 for h in heads[1:]), 30]
    roles = [{"header_lines": [h], "first_line": h, "last_line": e,
              "company_label": lines[h].split(" | ")[0], "title": "", "start_date": "",
              "end_date": ""} for h, e in zip(heads, ends, strict=True)]
    outline = _outline(roles, [], [{"label": "Skills", "lines": [32]}])  # no summary block
    prop = asyncio.run(propose_import(_OutlineFake([outline]), md, source_format="txt"))
    assert prop.error is None, prop.error
    unplaced = prop.flags.unplaced
    for n in (4, 5, 6, 7, 33, 34, 35):  # the summary and its heading; the trailing section
        assert lines[n].strip() in unplaced, n
    # Not noise: name and contact, a heading over a section that was read, and the wrapped tails
    # that joined their bullets.
    for n in (1, 2, 3, 8, 31, 32, 11, 15):
        assert lines[n].strip() not in unplaced, n
    assert len(prop.value.roles) == 5


# ------------------------ #102 on a real machine: wrapped tails, short ranges, education
_TAILS = """# Pat Example
## Experience
### Acme Retail, Director of IT
2019 - 2024
- Cut vendor spend across the group by renegotiating our supplier contracts
IBM and Oracle teams signed the new terms in the second half of the same year
- Trained the staff
- Moved the archive to a new platform with no downtime for anyone
(see the migration notes for the full list of steps taken that week)
- Closed the audit.
Another note that is not part of the bullet above.
"""


def test_a_wrapped_tail_joins_its_bullet_whatever_character_it_starts_with():
    outline = {"summary_blocks": [], "skill_groups": [], "roles": [
        {"header_lines": [3, 4], "first_line": 3, "last_line": 10, "company_label": "Acme Retail",
         "title": "Director of IT", "start_date": "2019", "end_date": "2024"}]}
    role = {"context_lines": [], "proof_points": [], "bullets": [
        {"key": "spend", "lines": [5], "optional": False, "tags": []},
        {"key": "staff", "lines": [7], "optional": False, "tags": []},
        {"key": "archive", "lines": [8], "optional": False, "tags": []},
        {"key": "audit", "lines": [10], "optional": False, "tags": []}]}

    class _One(_Fake):
        async def complete(self, *, tier, messages, schema=None, schema_name="", **kw):
            body = self.outline if "outline" in schema_name else role
            return ChatResult(content=json.dumps(body), model="fake")

    prop = asyncio.run(propose_import(_One(outline=outline), _TAILS, source_format="md"))
    assert prop.error is None, prop.error
    texts = [v.text for s in prop.value.roles[0].slots for v in s.variants]
    assert ("Cut vendor spend across the group by renegotiating our supplier contracts "
            "IBM and Oracle teams signed the new terms in the second half of the same year"
            ) in texts
    assert ("Moved the archive to a new platform with no downtime for anyone "
            "(see the migration notes for the full list of steps taken that week)") in texts
    # A bullet that ended with a full stop is never extended: the next line is listed instead.
    assert "Closed the audit." in texts
    assert prop.flags.unplaced == ["Another note that is not part of the bullet above."]
    assert prop.flags.clean


def test_a_role_range_that_stops_one_bullet_early_is_extended_to_the_next_header():
    md, lines, heads, roles, summary, skills = _five_roles()
    short = [dict(r) for r in roles]
    short[2] = dict(short[2], last_line=short[2]["last_line"] - 1)  # drops its last bullet
    fake = _OutlineFake([_outline(short, summary, skills)])
    prop = asyncio.run(propose_import(fake, md, source_format="md"))
    assert prop.error is None, prop.error
    texts = [v.text for s in prop.value.roles[2].slots for v in s.variants]
    assert "Coordinated vendor audits." in texts
    assert "- Coordinated vendor audits." not in prop.flags.unplaced
    assert len(prop.value.roles) == 5 and prop.flags.missed_roles == []


def test_an_education_section_and_its_dated_line_are_reported_not_dropped():
    md = ("Pat Example\npat@example.com\nSUMMARY\nA short profile line.\nEXPERIENCE\n"
          "Acme | Town, ST  Jan 2020 – Dec 2022\n- Ran the desk.\nEDUCATION\n"
          "B.A. in History | State University  Sep 2014 – May 2018\n")
    outline = _outline([{"header_lines": [6], "first_line": 6, "last_line": 7,
                         "company_label": "Acme", "title": "", "start_date": "",
                         "end_date": ""}], [], [])
    prop = asyncio.run(propose_import(_OutlineFake([outline]), md, source_format="txt"))
    assert prop.error is None, prop.error
    unplaced = prop.flags.unplaced
    # The summary and the education section (heading and dated line) are listed; the name and
    # contact lines above the first heading and the EXPERIENCE heading over a read role are not.
    assert unplaced == ["SUMMARY", "A short profile line.", "EDUCATION",
                        "B.A. in History | State University  Sep 2014 – May 2018"]
    assert prop.flags.missed_roles == []  # a degree is not a job


# ------------------------------------------------------------ Opus review fixes (D-IMPORT-6)
def test_title_case_section_headings_after_the_last_role_stay_out_of_it():
    md = ("Pat Example\npat@example.com\nExperience\n"
          "Acme | Town, ST  Jan 2020 – Dec 2022\n- Ran the desk.\n- Kept the books.\n"
          "Education\nB.A. in History, State University, 2018\nSkills\nScheduling, budgets\n")
    outline = _outline([{"header_lines": [4], "first_line": 4, "last_line": 5,
                         "company_label": "Acme", "title": "", "start_date": "",
                         "end_date": ""}], [], [{"label": "Skills", "lines": [10]}])
    prop = asyncio.run(propose_import(_OutlineFake([outline]), md, source_format="txt"))
    assert prop.error is None, prop.error
    texts = [v.text for s in prop.value.roles[0].slots for v in s.variants]
    assert texts == ["Ran the desk.", "Kept the books."]  # the range reached line 6, not 8 or 10
    assert "B.A. in History, State University, 2018" in prop.flags.unplaced
    assert "Education" in prop.flags.unplaced


def test_a_role_range_that_covers_the_skills_lines_is_refused():
    md = ("Pat Example\n## Experience\nAcme | Town, ST  Jan 2020 – Dec 2022\n- Ran it.\n"
          "## Skills\nScheduling\n")
    outline = _outline([{"header_lines": [3], "first_line": 3, "last_line": 6,
                         "company_label": "Acme", "title": "", "start_date": "",
                         "end_date": ""}], [], [{"label": "Skills", "lines": [6]}])
    prop = asyncio.run(propose_import(_OutlineFake([outline]), md, source_format="md"))
    assert prop.value is None and "overlaps the summary or skills lines" in prop.error


def test_a_dated_sentence_is_not_a_header_and_a_bare_title_gets_no_company():
    from kestrel.docs.imports import _dated

    assert not _dated("Managed the ERP rollout for finance, 2021 - 2022")
    assert not _dated("Led a team of nine engineers across two sites and three product lines "
                      "through the migration, 2020 - 2021")
    assert _dated("Manager, Operations 2017 - 2019")
    assert _dated("Director of Operations and Finance  Jan 2018 – Present")
    md, lines, heads, sec = _headers_fixture()
    merged = _role(lines, heads[0], sec["## Skills"] - 1, "Marlin Cove Services")
    # A bare "Title Mon YYYY - Mon YYYY" line in the middle of a role splits with no company.
    mid = ("Pat\n## Experience\nAcme | Town, ST  Jan 2020 – Dec 2022\n- Ran it.\n"
           "Manager, Operations 2017 - 2019\n- Ran the floor.\n## Skills\nx\n")
    outline = _outline([{"header_lines": [3], "first_line": 3, "last_line": 6,
                         "company_label": "Acme", "title": "", "start_date": "",
                         "end_date": ""}], [], [{"label": "Skills", "lines": [8]}])
    prop = asyncio.run(propose_import(_OutlineFake([outline]), mid, source_format="md"))
    assert prop.error is None, prop.error
    assert [r.company_label for r in prop.value.roles] == ["Acme", ""]
    assert any(u.startswith("role 2 company: not clear") for u in prop.flags.unverified)
    assert merged["company_label"]


def test_a_profile_sentence_above_the_first_heading_is_reported_not_exempted():
    md = ("Morgan Example\nI coordinate operations for small teams and write things down.\n"
          "morgan@example.com | 555-0100\nEXPERIENCE\n"
          "Acme | Town, ST  Jan 2020 – Dec 2022\n- Ran the desk.\n")
    outline = _outline([{"header_lines": [5], "first_line": 5, "last_line": 6,
                         "company_label": "Acme", "title": "", "start_date": "",
                         "end_date": ""}], [], [])
    prop = asyncio.run(propose_import(_OutlineFake([outline]), md, source_format="txt"))
    assert prop.flags.unplaced == [
        "I coordinate operations for small teams and write things down."]


def test_a_tail_that_starts_with_a_capitalized_word_is_never_fused_into_the_bullet():
    from kestrel.docs.imports import _not_context, _wraps

    assert not _wraps("- Led the team of five", "Reduced costs by 10%")
    assert _wraps("- Led the team of five", "and cut costs by 10%")
    assert _wraps("- Moved the archive to a new platform", "(see the notes)")
    assert _wraps("- Cut spend with our suppliers", "IBM and Oracle signed the new terms")
    assert _wraps("- Grew revenue to", "450K a year")
    # A Title Case sub-heading of six or more words is not context.
    assert _not_context("Key Accomplishments And Notable Results This Year")


def test_new_date_shapes_are_dates():
    from kestrel.docs.imports import _dated, _header_dates

    for line in ("Acme | Town, ST  01/2019 - 03/2021", "Acme | Town, ST  Summer 2019 – Fall 2020",
                 "Acme | Town, ST  Jan '19 – Mar '21"):
        assert _dated(line), line
    assert _header_dates({1: "Acme | Town, ST  01/2019 - 03/2021"}, [1]) == ("01/2019", "03/2021")
