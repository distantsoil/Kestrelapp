"""P7 v2, import by line reference (D-IMPORT-3, PROMPTS.md P7). The model answers in line numbers;
the app copies the lines. These tests pin the properties that make that safe: copied text is the
source's own, invented or out-of-role references are surfaced as errors, unplaced bullets are
shown, and the per-call schema only admits line numbers that exist."""

from __future__ import annotations

import asyncio
import json

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
    assert prop.flags.unverified == ["role 1 start date: '2018'"]


def test_a_raising_role_task_is_reported_not_raised():
    class _Boom(_Fake):
        async def complete(self, *, messages, **kw):
            if "role" in kw.get("schema_name", "") and "Beta" in messages[1]["content"]:
                raise RuntimeError("hook failed")
            return await super().complete(messages=messages, **kw)

    prop = _run(_Boom())
    assert prop.value is None and "Role 2" in prop.error and "hook failed" in prop.error


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
Brightline | Director of IT Columbus, OH | May 2022 - Apr 2023
Manager, Software, Systems & Support Jun 2021 - May 2022
Promoted to Director within 12 months.
- Led successive releases of Rails.
- Reduced annual IT expense by 20%.
Northfield | Operations Director Cleveland, OH | Sep 2016 - Mar 2021
- Served as virtual CIO to more than 50 organizations.
"""


def test_a_promotion_mapped_as_two_overlapping_roles_is_merged_into_one():
    """The operator's import with a free model failed: "role 3 (lines 37-47) overlaps another
    role". Brightline has two titles on consecutive header lines and the model answered two roles
    over the same body. That is one role (D-IMPORT-5)."""
    outline = {"summary_blocks": [], "skill_groups": [], "roles": [
        {"header_lines": [3], "first_line": 3, "last_line": 7, "company_label": "Brightline",
         "title": "Director of IT", "start_date": "May 2022", "end_date": "Apr 2023"},
        {"header_lines": [4], "first_line": 4, "last_line": 7, "company_label": "Brightline",
         "title": "Manager, Software, Systems & Support", "start_date": "Jun 2021",
         "end_date": "May 2022"},
        {"header_lines": [8], "first_line": 8, "last_line": 9, "company_label": "Northfield",
         "title": "Operations Director", "start_date": "Sep 2016", "end_date": "Mar 2021"},
    ]}
    rail = {"context_lines": [5], "bullets": [
        {"key": "rails", "lines": [6], "optional": False, "tags": []},
        {"key": "cost", "lines": [7], "optional": False, "tags": []}], "proof_points": []}
    inet = {"context_lines": [], "bullets": [
        {"key": "vcio", "lines": [9], "optional": False, "tags": []}], "proof_points": []}
    class _ByCompany(_Fake):
        async def complete(self, *, tier, messages, schema=None, schema_name="", **kw):
            if "outline" in schema_name:
                return ChatResult(content=json.dumps(self.outline), model="fake")
            body = rail if "Brightline" in messages[1]["content"] else inet
            return ChatResult(content=json.dumps(body), model="fake")

    prop = asyncio.run(propose_import(_ByCompany(outline=outline), PROMOTED))
    assert prop.error is None, prop.error
    roles = prop.value.roles
    assert [r.company_label for r in roles] == ["Brightline", "Northfield"]
    assert roles[0].title == "Director of IT" and roles[0].start_date == "Jun 2021"
    assert roles[0].end_date == "Apr 2023"
    texts = [v.text for s in roles[0].slots for v in s.variants]
    assert "Led successive releases of Rails." in texts  # verbatim, nothing invented


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
