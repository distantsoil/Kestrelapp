"""Documents editor round (operator report): keys hidden from the tree, add and remove roles and
bullets in the operator's own words (D-UI-29); layouts that look different, a font choice and
header alignment (D-DOC-1); and the resume assessment, advisory and schema-bound per master
(D-DOC-2, PROMPTS.md P13). Offline: a fake LLM client, no browser, no network."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import func, select

from kestrel import paths
from kestrel.db.models import DocLayout, DocMaster, DocSection, DocSlot, DocVariant, Quarantine
from kestrel.llm.client import ChatResult

ORIGINAL = "Cut costs 20% across 300 staff."


# ------------------------------------------------------------------------------------ helpers
def _app(monkeypatch, fake_cls=None, *, two_slots: bool = False):
    """A fresh DB with one resume master (one role, one or two bullets) and the web app."""
    from starlette.testclient import TestClient

    from kestrel.db import migrate
    from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
    from kestrel.web.app import build_app

    if fake_cls is not None:
        monkeypatch.setattr("kestrel.llm.client.LLMClient", fake_cls)
    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations(paths.db_path())
    forget_engine()

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
            for i, key in enumerate(("r1_enterprise_technology", "r1_sso")[: 2 if two_slots
                                                                            else 1]):
                slot = DocSlot(doc_section_id=sec.id, key=key, sort_order=i, optional=False,
                               default_variant_key="original")
                s.add(slot)
                await s.flush()
                s.add(DocVariant(doc_slot_id=slot.id, key="original", derived=False,
                                 tags=["cost"], text=ORIGINAL if i == 0 else "Rolled out SSO."))
            await s.commit()
            return m.id

    master_id = asyncio.run(seed())
    forget_engine()
    return TestClient(build_app(worker=False)), master_id


def _rows(model, **where):
    from kestrel.db.engine import get_sessionmaker, reset_engine

    async def go():
        await reset_engine()
        async with get_sessionmaker()() as s:
            stmt = select(model)
            for k, v in where.items():
                stmt = stmt.where(getattr(model, k) == v)
            out = list((await s.execute(stmt)).scalars().all())
        await reset_engine()
        return out

    return asyncio.run(go())


def _visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "textarea"]):
        tag.decompose()
    return soup.get_text(" ")


# ------------------------------------------------------------------ D-UI-29: the tree itself
def test_slot_and_wording_keys_are_tooltips_not_text(monkeypatch):
    client, _ = _app(monkeypatch)
    html = client.get("/documents", headers={"HX-Request": "true"}).text
    text = _visible_text(html)
    assert "r1_enterprise_technology" not in text and "original" not in text
    assert "Bullet 1 · Enterprise technology" in text
    assert 'title="Internal key: r1_enterprise_technology"' in html  # still reachable
    assert 'data-slot-key="r1_enterprise_technology"' in html
    assert 'data-variant-key="original"' in html
    soup = BeautifulSoup(html, "html.parser")
    assert soup.select_one(".role-head .role-name").get_text(strip=True) == "Acme · CIO"
    # Bullets are grouped inside the role, not siblings of its header.
    assert soup.select_one("details.section .role-bullets .slot") is not None


def test_add_role_with_its_first_bullet_in_the_operators_words(monkeypatch):
    client, master_id = _app(monkeypatch)
    text = "Led   the migration of 12 sites to Microsoft 365, on time."
    r = client.post(f"/documents/{master_id}/roles", data={
        "org_label": "Beta Hotels", "title": "IT Manager", "start_date": "2015",
        "end_date": "2019", "context_line": "", "text": text})
    assert r.status_code == 200 and "Role added." in r.text and "IT Manager" in r.text
    secs = _rows(DocSection, doc_master_id=master_id)
    new = max(secs, key=lambda x: x.sort_order)
    assert new.section_kind == "role" and new.org_label == "Beta Hotels"
    assert new.sort_order > min(x.sort_order for x in secs)  # at the end
    assert new.context_line is None
    (slot,) = _rows(DocSlot, doc_section_id=new.id)
    assert slot.key.startswith("r2_") and slot.default_variant_key == "original"
    (v,) = _rows(DocVariant, doc_slot_id=slot.id)
    assert v.key == "original" and v.derived is False and v.text == text  # byte for byte


def test_a_role_without_its_first_bullet_is_refused(monkeypatch):
    client, master_id = _app(monkeypatch)
    r = client.post(f"/documents/{master_id}/roles", data={"org_label": "X", "text": "  "})
    assert "needs its first bullet" in r.text
    assert len(_rows(DocSection, doc_master_id=master_id)) == 1


def test_add_bullet_gets_a_unique_key_and_an_original_variant(monkeypatch):
    client, master_id = _app(monkeypatch)
    (sec,) = _rows(DocSection, doc_master_id=master_id)
    for _ in range(2):  # the same words twice still make two distinct keys
        r = client.post(f"/documents/section/{sec.id}/bullets",
                        data={"text": "Enterprise technology roadmap owner."})
        assert "Bullet added." in r.text
    keys = [s.key for s in _rows(DocSlot, doc_section_id=sec.id)]
    assert len(keys) == len(set(keys)) == 3
    assert "r1_enterprise_technology_roadmap_owner" in keys
    assert "r1_enterprise_technology_roadmap_owner_2" in keys
    assert "Bullet 3 · Enterprise technology roadmap owner" in _visible_text(r.text)
    empty = client.post(f"/documents/section/{sec.id}/bullets", data={"text": ""})
    assert "type the bullet" in empty.text


def test_remove_bullet_and_remove_role(monkeypatch):
    client, master_id = _app(monkeypatch, two_slots=True)
    (sec,) = _rows(DocSection, doc_master_id=master_id)
    html = client.get("/documents").text
    assert 'hx-confirm="Remove bullet 1' in html and 'hx-confirm="Remove this role' in html
    slots = _rows(DocSlot, doc_section_id=sec.id)
    r = client.post(f"/documents/slot/{slots[0].id}/delete")
    assert "Bullet removed." in r.text
    assert [s.key for s in _rows(DocSlot, doc_section_id=sec.id)] == ["r1_sso"]
    assert _rows(DocVariant, doc_slot_id=slots[0].id) == []
    r = client.post(f"/documents/section/{sec.id}/delete")
    assert "Role removed." in r.text and "no roles yet" in r.text
    assert _rows(DocSection, doc_master_id=master_id) == [] and _rows(DocSlot) == []
    assert _rows(DocVariant) == []  # an empty master is allowed


def test_the_help_panel_names_the_add_controls(monkeypatch):
    client, _ = _app(monkeypatch)
    html = client.get("/documents").text
    assert '<details class="help panel" data-testid="doc-help">' in html
    assert 'data-testid="add-role"' in html and 'data-testid="add-bullet"' in html
    assert "—" not in _visible_text(html)


# ------------------------------------------------------------- D-DOC-1: layouts and fonts
def _doc(layout: str | None, **config):
    from types import SimpleNamespace as NS

    from kestrel.docs.catalog import MasterCatalog, SectionView, SlotView, resolve

    sec = NS(section_kind="role", org_label="Acme", org_label_redacted=None, title="CIO",
             start_date="2019", end_date="2024", context_line="300 staff.")
    variant = NS(key="original", text=ORIGINAL, tags=[])
    layouts = [NS(key=k, section_order=["summary", "experience", "skills"],
                  max_roles_shown=None, description="")
               for k in ("executive", "condensed", "technical")]
    master = NS(doc_kind="resume", track="employment", header={"name": "Pat Example",
                "headline": "IT leader"}, summary_variants=[], skill_groups=[])
    catalog = MasterCatalog(master=master, sections=[SectionView(sec, [
        SlotView("r1_cost", False, "original", [variant])])], layouts=layouts)
    return resolve(catalog, {"layout": layout, **config} if layout else config)


def _render(layout: str | None, **config) -> BeautifulSoup:
    from kestrel.docs.render import render_html

    return BeautifulSoup(render_html(_doc(layout, **config)), "html.parser")


def test_each_resume_layout_has_its_own_look_and_default_font():
    looks = {k: _render(k).body for k in ("executive", "condensed", "technical")}
    classes = {k: tuple(b["class"]) for k, b in looks.items()}
    assert len(set(classes.values())) == 3  # three genuinely different treatments
    assert "align-center" in classes["executive"]  # centered name and headline
    assert "align-left" in classes["condensed"] and "align-left" in classes["technical"]
    assert looks["executive"]["data-font"] == "Cambria"
    assert looks["condensed"]["data-font"] == "Calibri"
    assert looks["technical"]["data-font"] == "Segoe UI"
    # Condensed puts the dates inline with the title; the others keep a right-hand date column.
    assert "(2019 - 2024)" in looks["condensed"].select_one(".row").get_text()
    assert looks["executive"].select_one(".row > .dates") is not None
    css = _render("technical").style.get_text()
    assert "#D4622A" in css and "small-caps" in css


def test_font_and_alignment_override_the_layout_and_unknown_values_do_not():
    body = _render("executive", font="Arial", header_align="left").body
    assert body["data-font"] == "Arial" and "align-left" in body["class"]
    assert '"Arial", "Helvetica"' in _render("executive", font="Arial").style.get_text()
    odd = _render("executive", font="</style><script>x</script>", header_align="diagonal").body
    assert odd["data-font"] == "Cambria" and "align-center" in odd["class"]
    assert _render(None).body["class"] == ["style-classic", "align-left"]


@pytest.mark.parametrize("layout", ["executive", "condensed", "technical", None])
def test_every_look_stays_ats_safe_and_prints_the_text_byte_for_byte(layout):
    from kestrel.docs.render import bullets_in_html, render_html

    html = render_html(_doc(layout, font="Garamond"))
    soup = BeautifulSoup(html, "html.parser")
    assert soup.find("table") is None and soup.find("img") is None and soup.find("svg") is None
    assert bullets_in_html(html) == [("original", ORIGINAL)]
    assert soup.find("meta", attrs={"name": "generator"}) is None
    assert "Acme" in soup.select_one(".org").get_text()


def test_shipped_layout_rows_are_updated_in_place_and_seeding_is_idempotent():
    from kestrel.db import migrate
    from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
    from kestrel.docs.seed import _LAYOUTS, _ensure_layouts

    async def go():
        await reset_engine()
        for suffix in ("", "-wal", "-shm"):
            paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
        migrate.run_migrations(paths.db_path())
        forget_engine()
        async with get_sessionmaker()() as s:
            s.add(DocLayout(key="executive", label="Old", description="old words",
                            section_order=["experience"], max_roles_shown=1))
            s.add(DocLayout(key="mine", label="Mine", description="the operator's own",
                            section_order=["experience"]))
            await s.commit()
        for _ in range(2):
            async with get_sessionmaker()() as s:
                await _ensure_layouts(s)
                await s.commit()
        async with get_sessionmaker()() as s:
            rows = {r.key: r for r in (await s.execute(select(DocLayout))).scalars().all()}
            n = (await s.execute(select(func.count()).select_from(DocLayout))).scalar_one()
        await reset_engine()
        return rows, n

    rows, n = asyncio.run(go())
    spec = next(x for x in _LAYOUTS if x["key"] == "executive")
    assert rows["executive"].description == spec["description"] and "Centered" in spec[
        "description"]
    assert rows["executive"].section_order == spec["section_order"]
    assert rows["mine"].description == "the operator's own"  # never touched
    assert n == len(_LAYOUTS) + 1


def test_documents_tab_offers_font_and_alignment_and_the_preview_uses_them(monkeypatch):
    client, master_id = _app(monkeypatch)
    html = client.get("/documents").text
    soup = BeautifulSoup(html, "html.parser")
    fonts = [o["value"] for o in soup.select("select#font option")]
    assert fonts[0] == "" and {"Georgia", "Calibri", "Segoe UI"} <= set(fonts)
    assert soup.select("select#header_align option")
    r = client.post(f"/documents/{master_id}/preview",
                    data={"font": "Arial", "header_align": "center"})
    inner = BeautifulSoup(BeautifulSoup(r.text, "html.parser").iframe["srcdoc"], "html.parser")
    assert inner.body["data-font"] == "Arial" and "align-center" in inner.body["class"]


def test_test_render_and_the_review_carry_the_look(monkeypatch):
    from types import SimpleNamespace as NS

    seen: dict = {}

    async def fake_render(session, settings, master_id, *, config, opportunity_id=None, **kw):
        seen.update(config)
        r = DocRender(doc_master_id=master_id, config=config, rationale={})
        session.add(r)
        await session.commit()
        return NS(doc_render_id=r.id)

    from kestrel.db.models import DocRender

    monkeypatch.setattr("kestrel.docs.service.render_document", fake_render)
    client, master_id = _app(monkeypatch)
    r = client.get(f"/renders/new?master_id={master_id}&font=Calibri&header_align=center"
                   "&layout=nope", follow_redirects=False)
    assert r.status_code == 303 and seen["font"] == "Calibri" and seen["header_align"] == "center"
    rid = int(r.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/renders/{rid}").text
    assert 'data-testid="render-look"' in page and "<iframe" in page
    r = client.post(f"/renders/{rid}/look", data={"layout": "", "font": "Georgia",
                                                  "header_align": ""})
    assert r.status_code == 200 and "Showing Georgia" in r.text
    (row,) = _rows(DocRender, id=rid)
    assert row.config["font"] == "Georgia" and "header_align" not in row.config
    bad = client.post(f"/renders/{rid}/look", data={"layout": "<b>nope</b>"})
    assert "nothing was changed" in bad.text and "<b>" not in bad.text  # escaped, shown


def test_review_keeps_the_way_back_and_speaks_of_applications_and_leads(monkeypatch):
    """A look change on a render with a PDF starts a new render; the redirect keeps `from` (from
    the form or the page the request came from). The attach button and its result say
    application, or lead on the consulting track, never pursuit."""
    from kestrel.db.engine import get_sessionmaker, reset_engine
    from kestrel.db.models import DocRender, Opportunity, Pursuit

    client, master_id = _app(monkeypatch)

    async def seed(track):
        await reset_engine()
        async with get_sessionmaker()() as s:
            o = Opportunity(track=track, opp_type="job_posting", title="CIO", org_name="Acme",
                            url=f"https://x/{track}", stage="t2_done")
            s.add(o)
            await s.flush()
            s.add(Pursuit(opportunity_id=o.id, track=track, status="shortlisted"))
            r = DocRender(doc_master_id=master_id, opportunity_id=o.id, config={},
                          rationale={}, pdf_path="x.pdf")
            s.add(r)
            await s.commit()
            rid = r.id
        await reset_engine()
        return rid

    rid = asyncio.run(seed("employment"))
    page = client.get(f"/renders/{rid}?from=/pursuits").text
    assert "Use for this application" in page and "pursuit" not in _visible_text(page).lower()
    assert '<input type="hidden" name="from" value="/pursuits">' in page
    r = client.post(f"/renders/{rid}/look", data={"layout": "", "font": "Georgia",
                                                  "from": "/pursuits"})
    assert "from=%2Fpursuits" in r.headers["HX-Redirect"]
    r = client.post(f"/renders/{rid}/look", data={"font": "Georgia"},
                    headers={"HX-Current-URL": f"http://t/renders/{rid}?from=/opportunities/1"})
    assert "from=%2Fopportunities%2F1" in r.headers["HX-Redirect"]
    attached = client.post(f"/renders/{rid}/attach").text
    assert attached.endswith("Attached to this application.</span>")
    lead = asyncio.run(seed("consulting"))
    assert "Use for this lead" in client.get(f"/renders/{lead}").text
    assert "Attached to this lead." in client.post(f"/renders/{lead}/attach").text


# --------------------------------------------------------------- D-DOC-2: the assessment
GOOD = {
    "overall_fit": "Strong operator evidence for VP of IT; light on board-level strategy.",
    "strengths": [{"point": "Cost discipline with a number.",
                   "slot_keys": ["r1_enterprise_technology"]}],
    "gaps": [{"gap": "No board reporting", "why_it_matters": "VP roles report to the board."}],
    "strengthen": [{"slot_key": "r1_sso", "reason": "No scope or outcome."}],
}


class _AssessLLM:
    replies: list = []
    sent: list = []

    def __init__(self, settings, http=None, **_):
        self.settings = settings

    async def complete(self, **kw):
        type(self).sent.append(kw)
        reply = type(self).replies.pop(0) if type(self).replies else GOOD
        return ChatResult(content=json.dumps(reply), model="fake")


@pytest.fixture
def assess_llm():
    _AssessLLM.replies, _AssessLLM.sent = [], []
    return _AssessLLM


def test_the_schema_enum_is_this_masters_slot_keys(monkeypatch, assess_llm):
    client, master_id = _app(monkeypatch, assess_llm, two_slots=True)
    r = client.post(f"/documents/{master_id}/assess")
    assert r.status_code == 200 and 'data-testid="assessment-result"' in r.text
    sent = assess_llm.sent[0]
    assert sent["tier"] == "tier2" and sent["schema_name"] == "resume_assess_v1"
    blob = json.dumps(sent["schema"])
    assert '"enum": ["r1_enterprise_technology", "r1_sso"]' in blob
    assert "const" not in blob and "oneOf" not in blob
    user = sent["messages"][-1]["content"]
    assert f"[r1_enterprise_technology] {ORIGINAL}" in user
    text = _visible_text(r.text)
    assert "CIO, bullet 2 (Sso)" in text and "No scope or outcome." in text
    assert "r1_sso" not in text  # internal keys are tooltips here too
    assert "Ran on" in r.text and "Advice only" in r.text


def test_a_hallucinated_key_is_repaired_once_then_quarantined_never_shown(monkeypatch,
                                                                           assess_llm):
    bad = json.loads(json.dumps(GOOD))
    bad["strengthen"][0]["slot_key"] = "r9_invented"
    assess_llm.replies = [bad, bad]
    client, master_id = _app(monkeypatch, assess_llm, two_slots=True)
    r = client.post(f"/documents/{master_id}/assess")
    assert r.status_code == 200 and 'data-testid="assessment-error"' in r.text
    assert "could not be used" in r.text and "r9_invented" not in r.text
    assert len(assess_llm.sent) == 2  # the ask and one repair
    (q,) = _rows(Quarantine)
    assert q.tier == 13 and "r9_invented" in (q.raw_response or "")


def test_the_assessment_writes_nothing_to_the_document(monkeypatch, assess_llm):
    client, master_id = _app(monkeypatch, assess_llm, two_slots=True)
    before = [(v.id, v.key, v.text, v.derived) for v in _rows(DocVariant)]
    client.post(f"/documents/{master_id}/assess")
    assert [(v.id, v.key, v.text, v.derived) for v in _rows(DocVariant)] == before
    assert len(_rows(DocSlot)) == 2


def test_a_model_that_is_down_is_a_message_not_a_500(monkeypatch):
    class _Down(_AssessLLM):
        async def complete(self, **kw):
            raise ConnectionError("refused")

    client, master_id = _app(monkeypatch, _Down)
    r = client.post(f"/documents/{master_id}/assess")
    assert r.status_code == 200 and "Could not reach the model" in r.text


def test_dynamic_model_rejects_unknown_keys_and_one_key_is_an_enum():
    from kestrel.docs.assess import assess_model_for, assess_schema

    m = assess_model_for(["r1_a", "r1_b"])
    m.model_validate(GOOD | {"strengths": [], "strengthen": [{"slot_key": "r1_b",
                                                              "reason": "x"}]})
    with pytest.raises(ValueError):
        m.model_validate(GOOD | {"strengthen": [{"slot_key": "nope", "reason": "x"}]})
    one = json.dumps(assess_schema(assess_model_for(["r1_only"])))
    assert '"enum": ["r1_only"]' in one and "const" not in one
    with pytest.raises(ValueError):
        assess_model_for([])


def test_targets_come_only_from_the_operators_own_profile_blocks():
    from kestrel.docs.assess import profile_targets
    from kestrel.services import profile_edit as pe

    pe.profile_path().unlink(missing_ok=True)
    assert profile_targets("employment") == ([], [])  # the example's titles never count
    pe.save_section("employment", "about", pe.section_from_form(
        "about", {"full_name": "Pat Example", "must_have": "budget ownership"}))
    assert profile_targets("employment") == ([], ["budget ownership"])
    pe.save_section("employment", "employment", pe.section_from_form(
        "employment", {"target_titles": "VP of IT\nCIO", "seniority_floor": "vp"}))
    assert profile_targets("employment") == (["VP of IT", "CIO"], ["budget ownership"])
    pe.profile_path().unlink(missing_ok=True)


def test_prompt_p13_is_registered_and_says_it_never_writes_resume_text():
    from kestrel.llm.prompts import load_prompt
    from kestrel.llm.schemas import P13ResumeAssess

    p = load_prompt("resume_assess/v1")
    assert p.id == "P13" and p.tier == "tier2" and p.model_cls is P13ResumeAssess
    assert p.reasoning == "low" and p.max_tokens == 2500
    assert "never write resume text" in p.system and "—" not in p.system.split("never")[0][-200:]


# ------------------------------------------------------------- Bench and Calibrate purpose
@pytest.mark.parametrize("name,words", [("bench.html", "choose which model runs each tier"),
                                         ("calibrate.html", "scoring learns your taste")])
def test_bench_and_calibrate_say_what_they_are_for(name, words):
    body = (Path(paths.web_templates_dir()) / name).read_text(encoding="utf-8")
    purpose = body.split('data-testid="page-purpose">')[1].split("</p>")[0]
    assert words in purpose and "Profile &gt; Documents" in purpose and "—" not in purpose
