"""D-PRIV-1: confidential mode is settable, matches carefully, and never rewrites operator text.

Before D-PRIV-1 nothing in the UI or the import set `org_label_redacted`, so confidential mode
redacted nothing for a real user. These tests drive the operator's paths (the role editor, the
import, Settings, the Tailor editor, suggested wordings, the assessment) and pin the design rules:

A. Operator text is never round-tripped: editors show and save the real text.
B. Text sent to a model carries a random per-call code; a reply has only that code restored.
C. Suggested wordings go through the hosted path and are refused when nothing can be hidden.
D. Import under confidential mode on an online model always needs a bound confirmation.
E. With nothing marked, the newest open-ended role is hidden until confirmed (fail closed).
F. Matching: exactly the operator's listed names, case-sensitive, other employers protected.
G. A rename carries the mark to the promotion rows that shared the old name."""

from __future__ import annotations

import asyncio
import copy
import html
import json

from sqlalchemy import select

from kestrel import paths
from kestrel.db.models import (
    DocMaster,
    DocRender,
    DocSection,
    DocSlot,
    DocVariant,
    Evaluation,
    Opportunity,
    Org,
    Setting,
)
from kestrel.privacy import Employer, EmployerMap, from_pairs

_EMPLOYER = "Northwind Freight"
_PAST = "Ridgeline Utilities"
_SHOWN = "a freight company"


def _app(*, confidential: bool = True, opportunity: bool = False, cap_marked: bool = True,
         cap_org: str = "Harbor Logistics", cur_end: str = "Present", promo: bool = False,
         with_docs: bool = True):
    """A fresh DB: a resume (current role at Northwind, past role at Ridgeline, optionally an
    earlier Northwind role from a promotion) and a capability statement with one engagement;
    optionally a scored Northwind opportunity."""
    from starlette.testclient import TestClient

    from kestrel.db import migrate
    from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
    from kestrel.web.app import build_app

    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations(paths.db_path())
    forget_engine()

    async def seed() -> dict[str, int]:
        async with get_sessionmaker()() as s:
            ids: dict[str, int] = {}
            if with_docs:
                resume = DocMaster(track="employment", name="Resume", doc_kind="resume",
                                   summary_variants=[], skill_groups=[])
                cap = DocMaster(track="consulting", name="Capability", doc_kind="capability",
                                summary_variants=[], skill_groups=[])
                s.add_all([resume, cap])
                await s.flush()
                cur = DocSection(doc_master_id=resume.id, section_kind="role",
                                 org_label=_EMPLOYER, title="Director of IT",
                                 start_date="2021", end_date=cur_end, sort_order=0)
                past = DocSection(doc_master_id=resume.id, section_kind="role",
                                  org_label=_PAST, title="IT Manager", start_date="2015",
                                  end_date="2019", sort_order=2)
                other = DocSection(doc_master_id=cap.id, section_kind="proof_engagement",
                                   org_label=cap_org,
                                   org_label_redacted="a logistics firm" if cap_marked else None,
                                   sort_order=0)
                s.add_all([cur, past, other])
                if promo:
                    earlier = DocSection(doc_master_id=resume.id, section_kind="role",
                                         org_label=_EMPLOYER, title="IT Manager",
                                         start_date="2019", end_date="2021", sort_order=1)
                    s.add(earlier)
                await s.flush()
                for sec, text in ((cur, f"Led IT for {_EMPLOYER} across 300 staff."),
                                  (past, "Migrated 12 sites to Microsoft 365.")):
                    slot = DocSlot(doc_section_id=sec.id, key=f"r{sec.sort_order + 1}_a",
                                   sort_order=0, optional=False, default_variant_key="original")
                    s.add(slot)
                    await s.flush()
                    s.add(DocVariant(doc_slot_id=slot.id, key="original", derived=False,
                                     tags=[], text=text))
                ids.update(resume=resume.id, cap=cap.id, cur=cur.id, past=past.id,
                           other=other.id)
                if promo:
                    ids["earlier"] = earlier.id
            if opportunity:
                org = Org(name=_EMPLOYER, normalized_name=_EMPLOYER.lower())
                s.add(org)
                await s.flush()
                opp = Opportunity(track="employment", opp_type="job_posting",
                                  title="VP of IT", org_id=org.id, org_name=_EMPLOYER,
                                  url="https://example.test/p-1", stage="t2_done")
                s.add(opp)
                await s.flush()
                s.add(Evaluation(opportunity_id=opp.id, tier=2, track="employment",
                                 verdict="apply", verdict_model="apply", score=4.5,
                                 weighted_score=4.4, needs_review=False, dimensions={},
                                 blocks={}, blockers=[],
                                 legitimacy={"status": "clean", "signals": []}))
                ids["opp"] = opp.id
            if confidential:
                s.add(Setting(key="confidential_mode", value=True))
            await s.commit()
            return ids

    ids = asyncio.run(seed())
    forget_engine()
    return TestClient(build_app(worker=False)), ids


def _db(fn):
    from kestrel.db.engine import get_sessionmaker, reset_engine

    async def go():
        await reset_engine()
        async with get_sessionmaker()() as s:
            out = await fn(s)
        await reset_engine()
        return out

    return asyncio.run(go())


def _labels() -> dict[int, str | None]:
    async def fn(s):
        return {r.id: r.org_label_redacted
                for r in (await s.execute(select(DocSection))).scalars().all()}

    return _db(fn)


def _hidden() -> dict[str, str]:
    """{employer: shown as} of the live map (marks, else the inference)."""
    from kestrel.web.redact import redaction_map

    async def fn(s):
        return {e.name: e.shown_as for e in (await redaction_map(s)).employers}

    return _db(fn)


def _doc_hidden(master_id: int) -> dict[str, str]:
    from kestrel.docs.current_employer import load_employer_map

    async def fn(s):
        emap = await load_employer_map(s, master_id=master_id)
        return {e.name: e.shown_as for e in emap.employers}

    return _db(fn)


def _texts(slot_id: int) -> set[str]:
    async def fn(s):
        return set((await s.execute(select(DocVariant.text).where(
            DocVariant.doc_slot_id == slot_id))).scalars())

    return _db(fn)


def _slot_id(section_id: int) -> int:
    async def fn(s):
        return (await s.execute(select(DocSlot.id).where(
            DocSlot.doc_section_id == section_id))).scalar_one()

    return _db(fn)


def _mark(client, section_id: int, org: str, shown: str | None, **extra):
    data = {"org_label": org, "title": "Director of IT", "current_employer_form": "1", **extra}
    if shown is not None:
        data.update(current_employer="on", org_label_redacted=shown)
    return client.post(f"/documents/section/{section_id}", data=data)


def _hosted(monkeypatch):
    """Every tier counts as an online model under Hosted mode (the plan, not the transport)."""
    monkeypatch.setattr("kestrel.pipeline._shared.tier_is_hosted", lambda settings, tier: True)
    monkeypatch.setattr("kestrel.web.pages.models._provider_label", lambda name: "OpenRouter")


def _code_in(text: str) -> str:
    """The live stand-in code a send carries (D-PRIV-1)."""
    from kestrel.privacy import _LIVE

    return next(c for c in reversed(_LIVE) if c in text)


def _code_in_or_none(text: str) -> str | None:
    from kestrel.privacy import _LIVE

    return next((c for c in _LIVE if c in text), None)


# ------------------------------------------------------------------------- the matcher
def test_only_listed_names_match_exactly_and_other_employers_are_protected():
    emap = EmployerMap([Employer("Acme", "a software firm", ("Acme", "ACME"))],
                       protected=["Acme Labs Inc."])
    assert emap.redact("Joined Acme Labs, then Acme; ACME too; acme stays.") == (
        "Joined Acme Labs, then a software firm; a software firm too; acme stays.")
    sq = EmployerMap([Employer("Block, Inc.", "a fintech firm")])
    assert sq.redact("Square footage and SQUARE FEET at Block.") == (
        "Square footage and SQUARE FEET at a fintech firm.")
    listed = EmployerMap([Employer("Block, Inc.", "a fintech firm", ("Block", "Square"))])
    assert listed.redact("Square footage, SQUARE FEET, Squares.") == (
        "a fintech firm footage, SQUARE FEET, Squares.")
    ford = EmployerMap([Employer("Ford Motor Company", "an automaker")])
    assert ford.redact("Harrison Ford visited Ford Motor.") == (
        "Harrison Ford visited an automaker.")
    assert EmployerMap([Employer("Ford Motor Company", "an automaker",
                                 ("Ford Motor Company", "Ford"))]).redact("Harrison Ford") == (
        "Harrison an automaker")
    web = EmployerMap([Employer("Acme", "a software firm", ("Acme", "acme.com"))])
    assert web.redact("Mail jane@acme.com today; see acme.com/jobs.") == (
        "Mail jane@a software firm today; see a software firm/jobs.")
    assert EmployerMap([Employer("Acme", "x", ("Acme",))]).redact("jane@acme.com") == (
        "jane@acme.com")


def test_default_names_are_the_label_stripped_and_all_caps():
    from kestrel.privacy import default_names

    assert default_names("Meta Platforms, Inc.") == [
        "Meta Platforms, Inc.", "Meta Platforms", "META PLATFORMS, INC.", "META PLATFORMS"]
    assert default_names("Box Inc") == ["Box Inc", "Box", "BOX INC", "BOX"]


def test_codes_are_fresh_per_call_and_restore_only_the_exact_code():
    emap = from_pairs([(_EMPLOYER, "a company in fintech")])
    one, two = emap.codebook(), emap.codebook()
    sent = one.encode(f"Led IT at {_EMPLOYER}; sold to a company in fintech.")
    two.encode(_EMPLOYER)
    code = one.codes[0]
    assert one.codes != two.codes and len(code) == 8 and code.isalnum()
    assert _EMPLOYER not in sent and code in sent and "a company in fintech" in sent
    assert one.decode(f"Ran {code} IT; sold to a company in fintech.") == (
        f"Ran {_EMPLOYER} IT; sold to a company in fintech.")
    restored, err = one.restore_raw(json.dumps(f"Ran {code[:6]} IT"))
    assert restored is None and code[:5] in err  # a piece of a code is a failed parse
    assert one.restore_raw(json.dumps("Apply at [Employer] today."))[1] is None


def test_each_hidden_spelling_has_its_own_code_and_round_trips_byte_for_byte():
    emap = EmployerMap([Employer("Acme Inc.", "a software firm", ("Acme Inc.", "ACME",
                                                                  "acme.com"))])
    book = emap.codebook()
    text = "Shipped Widgetron at ACME; jo@acme.com"
    sent = book.encode(text)
    assert "ACME" not in sent and "acme.com" not in sent and len(set(book.codes)) == 2
    assert book.decode(sent) == text
    restored, err = book.restore_raw(json.dumps(sent, ensure_ascii=False))
    assert err is None and json.loads(restored) == text


# ------------------------------------------------------------------------ the role editor
def test_the_role_editor_offers_the_box_and_a_suggested_replacement():
    client, ids = _app()
    with client:
        html = client.get(f"/documents?master_id={ids['resume']}").text
    assert "This is my current employer" in html and "Show it as" in html
    assert 'value="a company in your industry"' in html
    assert f'id="role-{ids["cur"]}"' in html


def test_ticking_sets_the_mark_and_only_one_role_per_document_is_current():
    client, ids = _app()
    with client:
        _mark(client, ids["cur"], _EMPLOYER, _SHOWN)
        assert _labels()[ids["cur"]] == _SHOWN and _hidden()[_EMPLOYER] == _SHOWN
        r = _mark(client, ids["past"], _PAST, "a utility")
    labels = _labels()
    assert labels[ids["past"]] == "a utility" and labels[ids["cur"]] is None
    assert labels[ids["other"]] == "a logistics firm", "another document's mark was touched"
    assert r.headers.get("HX-Retarget") == "#tree"


def test_a_ticked_box_needs_a_replacement_and_a_details_post_keeps_the_mark():
    client, ids = _app()
    with client:
        r = _mark(client, ids["cur"], _EMPLOYER, "   ")
        assert "Say how your current employer should be shown" in r.text
        _mark(client, ids["cur"], _EMPLOYER, _SHOWN)
        client.post(f"/documents/section/{ids['cur']}", data={"title": "CIO"})
    assert _labels()[ids["cur"]] == _SHOWN


def test_a_rename_keeps_both_promotion_rows_redacted():
    client, ids = _app(promo=True)
    with client:
        _mark(client, ids["cur"], _EMPLOYER, _SHOWN)
        assert _labels()[ids["earlier"]] == _SHOWN  # the promotion row is marked with it
        _mark(client, ids["cur"], "Northwind Freight Group", _SHOWN)
        labels = _labels()
        assert labels[ids["cur"]] == _SHOWN and labels[ids["earlier"]] == _SHOWN
        # An older client posting only the details carries the mark too.
        client.post(f"/documents/section/{ids['cur']}", data={"org_label": "NW Freight"})
        page = _render_page(client, ids)
    labels = _labels()
    assert labels[ids["cur"]] == _SHOWN and labels[ids["earlier"]] == _SHOWN
    doc = _preview_doc(page)
    assert [el.get_text() for el in doc.select(".role .org")] == [_SHOWN, _SHOWN, _PAST]


def test_a_stale_cross_document_mark_stops_hiding_once_unticked_everywhere():
    client, ids = _app(cap_org=_EMPLOYER)
    with client:
        _mark(client, ids["cur"], _EMPLOYER, _SHOWN)
        assert _labels()[ids["other"]] == _SHOWN  # one name, one "Show it as"
        # The job ended: untick it on the resume. The capability statement still marks it.
        r = _mark(client, ids["cur"], _EMPLOYER, None, end_date="2024")
        assert "Still hidden in Capability, which also marks this employer" in r.text
        # The resume is now the newest document naming it, as a past role: the combined map
        # (lists, API, MCP) no longer hides it; the capability statement's own map still does.
        assert _EMPLOYER not in _hidden()
        assert _EMPLOYER in _doc_hidden(ids["cap"])
        client.post(f"/documents/section/{ids['other']}",
                    data={"org_label": _EMPLOYER, "current_employer_form": "1"})
        assert _EMPLOYER not in _doc_hidden(ids["cap"])
        page = _render_page(client, ids)
    orgs = [el.get_text() for el in _preview_doc(page).select(".role .org")]
    assert orgs == [_EMPLOYER, _PAST], "a past employer must print as written"


# ---------------------------------------------------------------- the redaction reaches out
def test_pipeline_list_hides_the_name_once_it_is_set():
    client, ids = _app(opportunity=True, cur_end="2024")
    with client:
        before = client.get("/opportunities").text
        _mark(client, ids["cur"], _EMPLOYER, _SHOWN)
        after = client.get("/opportunities").text
    assert _EMPLOYER in before, "fixture: nothing marked and nothing to infer"
    assert _EMPLOYER not in after and _SHOWN in after


def _render_page(client, ids) -> str:
    async def fn(s):
        r = DocRender(doc_master_id=ids["resume"], config={}, rationale={})
        s.add(r)
        await s.commit()
        return r.id

    return client.get(f"/renders/{_db(fn)}").text


def _preview_doc(page: str):
    from bs4 import BeautifulSoup

    frame = BeautifulSoup(page, "html.parser").select_one("iframe[srcdoc]")
    return BeautifulSoup(frame["srcdoc"], "html.parser")


def test_a_render_hides_only_the_current_employer_and_the_editor_keeps_real_text():
    client, ids = _app()
    with client:
        _mark(client, ids["cur"], _EMPLOYER, _SHOWN)
        page = _render_page(client, ids)
    doc = _preview_doc(page)
    assert [el.get_text() for el in doc.select(".role .org")] == [_SHOWN, _PAST]
    assert "Confidential" not in doc.get_text()
    bullets = [li.get_text() for li in doc.select("li[data-vk]")]
    assert f"Led IT for {_SHOWN} across 300 staff." in bullets
    assert _EMPLOYER not in str(doc)
    # Rule A: the Tailor rows are an editor and show the real wording.
    assert f"Led IT for {_EMPLOYER} across 300 staff." in page


def test_tailor_edit_wording_saves_exactly_what_was_typed():
    """No round trip: the stand-in text, a word containing it, and ALL CAPS are all stored as
    typed. Only the preview is redacted."""
    client, ids = _app()
    slot = _slot_id(ids["cur"])
    with client:
        _mark(client, ids["cur"], _EMPLOYER, "Bank")
        _render_page(client, ids)
        rid = _db(lambda s: _last_render(s))
        typed = "Sold to a company in fintech; Banking at Bank; LED NORTHWIND FREIGHT IT."
        r = client.post(f"/renders/{rid}/wording", data={"slot_key": "r1_a", "text": typed})
    assert r.status_code == 200
    assert typed in _texts(slot)
    preview = _preview_doc(r.text).get_text()
    assert "Banking at Bank" in preview and "LED Bank IT" in preview


async def _last_render(s) -> int:
    return (await s.execute(select(DocRender.id).order_by(DocRender.id.desc()))).scalars().first()


def test_resolve_substitutes_only_marked_names_in_titles_bullets_summary_and_context():
    from types import SimpleNamespace as NS

    from kestrel.docs.catalog import MasterCatalog, SectionView, SlotView, resolve

    cur = NS(section_kind="role", org_label="Acme", org_label_redacted="a software firm",
             title="CIO, Acme", start_date="2021", end_date="Present",
             context_line="Acme, 300 staff.")
    lab = NS(section_kind="role", org_label="Acme Labs", org_label_redacted=None,
             title="IT Manager", start_date="2015", end_date="2021", context_line=None)
    master = NS(doc_kind="resume", track="employment", look={}, header={"name": "Pat Example"},
                summary_variants=[{"key": "s", "text": "Led ACME and Acme Labs IT."}],
                skill_groups=[], education=[], certifications=[], licenses=[], projects=None)
    cat = MasterCatalog(master=master, layouts=[], sections=[
        SectionView(cur, [SlotView("r1", False, "a", [
            NS(key="a", text="Moved Acme to the cloud; we hit target; acme stays.", tags=[])])]),
        SectionView(lab, [SlotView("r2", False, "a", [
            NS(key="a", text="Ran Acme Labs IT.", tags=[])])]),
    ])
    doc = resolve(cat, {"summary": "s", "redact_orgs": True})
    roles = [r for b in doc.blocks for r in (b.roles or [])]
    assert [r.org for r in roles] == ["a software firm", "Acme Labs"]
    assert roles[0].title == "CIO, a software firm"
    assert roles[0].context == "a software firm, 300 staff."
    assert roles[0].bullets[0].text == (
        "Moved a software firm to the cloud; we hit target; acme stays.")
    assert roles[1].bullets[0].text == "Ran Acme Labs IT."
    assert [b.text for b in doc.blocks if b.type == "summary"] == [
        "Led a software firm and Acme Labs IT."]


# ------------------------------------------------------------------- E: fail closed + Settings
def test_with_nothing_marked_the_open_ended_role_is_hidden_until_confirmed():
    client, ids = _app(opportunity=True, cap_marked=False)
    with client:
        rows = client.get("/opportunities").text
        docs = client.get(f"/documents?master_id={ids['resume']}").text
        settings = client.get("/settings").text
        r = client.post(f"/documents/section/{ids['cur']}/confirm-current",
                        data={"shown_as": "a company in your industry"})
        after = client.get("/settings").text
    assert _EMPLOYER not in rows and "a company in your industry" in rows
    banner = (f"Kestrel is hiding <b>{_EMPLOYER}</b> as <b>a company in your industry</b> "
              "because it looks like your current job. Confirm or change.")
    for page in (docs, settings):
        assert banner in " ".join(page.split())
    assert "Confirmed" in r.text and _labels()[ids["cur"]] == "a company in your industry"
    assert "because it looks like your current job" not in after
    assert f"Hiding <b>{_EMPLOYER}</b> as <b>a company in your industry</b>" in after


def test_settings_says_so_when_there_is_nothing_to_hide():
    client, ids = _app(cap_marked=False, cur_end="2024")
    with client:
        inert = client.get("/settings").text
        _mark(client, ids["cur"], _EMPLOYER, _SHOWN)
        set_ = client.get("/settings").text
        # No open-ended role: a render prints the real names (nothing to infer).
    assert "redacting nothing" in inert and "Choose your current employer" in inert
    assert "documents show every employer's real name" in " ".join(inert.split())
    href = f"/documents?master_id={ids['resume']}&amp;section={ids['cur']}#role-{ids['cur']}"
    assert href in inert
    assert f"Hiding <b>{_EMPLOYER}</b> as <b>{_SHOWN}</b>" in set_
    help_line = " ".join(set_.split("Confidential mode replaces", 1)[1].split("</p>", 1)[0].split())
    assert "anything sent to an online model" in help_line and "everywhere" not in help_line
    assert "other employers show as written" in help_line and "Importing a resume" in help_line
    assert "Names match exactly as listed" in help_line


# ----------------------------------------------------------------------- suggest wordings
class _SuggestLLM:
    sent: list[str] = []

    def __init__(self, settings, http=None, **_):
        self.settings = settings

    async def complete(self, **kw):
        from kestrel.llm.client import ChatResult

        blob = json.dumps(kw.get("messages"), ensure_ascii=False)
        _SuggestLLM.sent.append(blob)
        code = _code_in(blob)
        return ChatResult(content=json.dumps({"variants": [
            {"text": f"Sold to a company in fintech for {code}, 300 staff.", "tags": []}]}),
            model="fake")


def test_suggest_sends_a_code_and_restores_only_the_code(monkeypatch):
    _hosted(monkeypatch)
    monkeypatch.setattr("kestrel.llm.client.LLMClient", _SuggestLLM)
    _SuggestLLM.sent = []
    client, ids = _app(cap_marked=False)
    slot = _slot_id(ids["cur"])
    with client:
        _mark(client, ids["cur"], _EMPLOYER, "a company in fintech")
        shown = client.post(f"/documents/slot/{slot}/suggest").text
        restored = f"Sold to a company in fintech for {_EMPLOYER}, 300 staff."
        client.post(f"/documents/slot/{slot}/variants", data={"text": restored, "tags": ""})
    assert _SuggestLLM.sent and all(_EMPLOYER not in m for m in _SuggestLLM.sent)
    assert restored in shown
    assert restored in _texts(slot)  # the customer bullet's stand-in text saved verbatim


def test_suggest_refuses_when_nothing_can_be_hidden(monkeypatch):
    _hosted(monkeypatch)
    monkeypatch.setattr("kestrel.llm.client.LLMClient", _SuggestLLM)
    _SuggestLLM.sent = []
    client, ids = _app(cap_marked=False, cur_end="2024")
    slot = _slot_id(ids["cur"])
    with client:
        r = client.post(f"/documents/slot/{slot}/suggest").text
    assert "Not sent" in r and not _SuggestLLM.sent


# ------------------------------------------------------------------------------ assessment
def test_assess_runs_with_confidential_mode_on_and_sends_a_code(monkeypatch):
    from kestrel.docs.assess import assess_master, document_text
    from kestrel.docs.catalog import load_catalog
    from kestrel.llm.client import ChatResult

    _hosted(monkeypatch)
    client, ids = _app(cap_marked=False)
    with client:
        _mark(client, ids["cur"], _EMPLOYER, _SHOWN)
    sent: list[str] = []

    class _Fake:
        def __init__(self):
            from kestrel.config import get_settings

            self.settings = get_settings().model_copy(deep=True)
            self.settings.confidential_mode = True  # as load_settings_with_db reads the row

        async def complete(self, **kw):
            sent.append(json.dumps(kw.get("messages"), ensure_ascii=False))
            return ChatResult(content="not json", model="fake")

    async def fn(s):
        catalog = await load_catalog(s, ids["resume"])
        assert _EMPLOYER in document_text(catalog, redact=True)  # real text; the send codes it
        return await assess_master(_Fake(), catalog, session=s, redact=True)

    out = _db(fn)
    assert out.error and sent and all(_EMPLOYER not in m for m in sent)
    assert _code_in(sent[0])


# --------------------------------------------------------------------------------- import
_RESUME = """# Pat Example
## Experience
### Acme Media, Director of IT
2019 - Present
- Cut infrastructure costs 20% by consolidating Northwind Freight data centers.
### Beta Hotels, IT Manager
2015 - 2019
- Migrated 12 sites to Microsoft 365.
"""
_OUTLINE = {
    "summary_blocks": [],
    "roles": [
        {"header_lines": [3, 4], "first_line": 3, "last_line": 5, "company_label": "Acme Media",
         "title": "Director of IT", "start_date": "2019", "end_date": "Present"},
        {"header_lines": [6, 7], "first_line": 6, "last_line": 8, "company_label": "Beta Hotels",
         "title": "IT Manager", "start_date": "2015", "end_date": "2019"},
    ],
    "skill_groups": [],
}
_ROLES = [
    {"context_lines": [], "bullets": [{"key": "cost", "lines": [5], "optional": False,
                                       "tags": []}], "proof_points": []},
    {"context_lines": [], "bullets": [{"key": "m365", "lines": [8], "optional": False,
                                       "tags": []}], "proof_points": []},
]


class _ImportFake:
    sent: list[str] = []

    def __init__(self, outline=_OUTLINE, settings=None, http=None, **_):
        self.outline = outline if isinstance(outline, dict) else _OUTLINE
        self.settings = settings

    async def complete(self, *, tier, messages, schema=None, schema_name="", **kw):
        from kestrel.llm.client import ChatResult

        _ImportFake.sent.append(json.dumps(messages, ensure_ascii=False))
        if "outline" in schema_name:
            return ChatResult(content=json.dumps(self.outline), model="fake")
        i = 0 if "Acme" in messages[1]["content"] else 1
        return ChatResult(content=json.dumps(_ROLES[i]), model="fake")


def _proposal(outline=_OUTLINE):
    from kestrel.docs.imports import propose_import

    return asyncio.run(propose_import(_ImportFake(outline), _RESUME, source_format="md"))


def test_import_offers_the_opt_in_only_for_an_open_ended_newest_role():
    from kestrel.web.deps import templates
    from kestrel.web.pages.documents import _current_offer

    prop = _proposal()
    assert prop.value is not None and prop.flags.clean
    offer = _current_offer(prop, "employment")
    assert offer == {"company": "Acme Media", "hint": "a company in your industry"}
    html = templates().get_template("partials/import_proposal.html").render(
        proposal=prop, markdown=_RESUME, track="employment", doc_kind="resume",
        current_offer=offer)
    assert "Mark Acme Media as my current employer" in html
    assert "checked" not in html.split('name="current_employer"', 1)[1].split(">", 1)[0]
    closed = copy.deepcopy(_OUTLINE)
    closed["roles"][0]["end_date"] = "2024"
    assert _current_offer(_proposal(closed), "employment") is None


def test_import_sets_nothing_unless_ticked_and_sets_the_newest_role_when_ticked():
    client, _ids = _app(confidential=False, with_docs=False)
    prop = _proposal()
    base = {"proposal_json": prop.value.model_dump_json(), "text": _RESUME,
            "track": "employment", "doc_kind": "resume"}
    with client:
        assert "Created." in client.post("/api/documents/import/accept", data=base).text
        assert not any(_labels().values())
        r = client.post("/api/documents/import/accept",
                        data={**base, "current_employer": "on",
                              "current_employer_as": "a media company"})
    assert "Created." in r.text
    assert sorted(v for v in _labels().values() if v) == ["a media company"]


def _confirm_value(page: str) -> str:
    import re

    m = re.search(r'name="confirm_unredacted" value="([0-9a-f]{64})"', page)
    assert m, "the step carries no confirmation"
    return m.group(1)


def test_import_without_a_mark_needs_a_confirmation_bound_to_text_and_model(monkeypatch):
    _hosted(monkeypatch)
    monkeypatch.setattr("kestrel.llm.client.LLMClient", _ImportFake)
    _ImportFake.sent = []
    client, _ids = _app(with_docs=False)
    with client:
        read = client.post("/api/documents/import/read", data={"text": _RESUME}).text
        good = _confirm_value(read)
        direct = client.post("/api/documents/import", data={"text": _RESUME}).text
        flag = client.post("/api/documents/import",
                           data={"text": _RESUME, "confirm_unredacted": "1"}).text
        other_text = client.post("/api/documents/import",
                                 data={"text": _RESUME + "\nExtra line.",
                                       "confirm_unredacted": good}).text
        monkeypatch.setattr("kestrel.web.pages.models._provider_label", lambda name: "OtherAI")
        other_provider = client.post("/api/documents/import",
                                     data={"text": _RESUME, "confirm_unredacted": good}).text
        assert not _ImportFake.sent, "an unconfirmed import reached the model"
        assert _confirm_value(other_provider) != good  # a FRESH confirmation is offered
        monkeypatch.setattr("kestrel.web.pages.models._provider_label",
                            lambda name: "OpenRouter")
        ok = client.post("/api/documents/import",
                         data={"text": _RESUME, "confirm_unredacted": good}).text
    warning = ("This sends your whole resume to OpenRouter, including your current employer's "
               "name. Confidential mode can hide it only after you mark your current employer.")
    assert warning in " ".join(html.unescape(read).split()) and 'hx-trigger="load"' not in read
    for refused in (direct, flag, other_text, other_provider):
        assert "Not sent." in refused
    assert _ImportFake.sent and "Proposed import" in ok


def test_import_with_a_mark_still_asks_and_sends_coded_lines(monkeypatch):
    _hosted(monkeypatch)
    monkeypatch.setattr("kestrel.llm.client.LLMClient", _ImportFake)
    _ImportFake.sent = []
    client, ids = _app(cap_marked=False)
    with client:
        _mark(client, ids["cur"], _EMPLOYER, _SHOWN)
        read = client.post("/api/documents/import/read", data={"text": _RESUME}).text
        refused = client.post("/api/documents/import", data={"text": _RESUME}).text
        assert "Not sent." in refused and not _ImportFake.sent  # always asked
        r = client.post("/api/documents/import",
                        data={"text": _RESUME, "confirm_unredacted": _confirm_value(read)})
    assert "This sends your whole resume to OpenRouter" in " ".join(read.split())
    assert _ImportFake.sent and all(_EMPLOYER not in m for m in _ImportFake.sent)
    assert any(_code_in_or_none(m) for m in _ImportFake.sent)
    # The proposal copies the ORIGINAL lines locally: verbatim, real name included.
    assert "consolidating Northwind Freight data centers." in r.text
    assert "Every bullet is verbatim from your source" in r.text


# ----------------------------------------------------------------- 1: per-document maps
def _job_change() -> dict[str, int]:
    """An old resume that marks OldCo (then current), and a new resume where NewCo is current
    and OldCo has ended."""
    async def fn(s):
        old = DocMaster(track="employment", name="Old resume", doc_kind="resume",
                        summary_variants=[], skill_groups=[])
        new = DocMaster(track="employment", name="New resume", doc_kind="resume",
                        summary_variants=[], skill_groups=[])
        s.add_all([old, new])
        await s.flush()
        s.add(DocSection(doc_master_id=old.id, section_kind="role", org_label="OldCo Shipping",
                         org_label_redacted="a shipping firm", end_date="Present", sort_order=0))
        newco = DocSection(doc_master_id=new.id, section_kind="role", org_label="NewCo Robotics",
                           title="CIO", end_date="Present", sort_order=0)
        oldco = DocSection(doc_master_id=new.id, section_kind="role", org_label="OldCo Shipping",
                           title="Director", start_date="2019", end_date="2023", sort_order=1)
        s.add_all([newco, oldco])
        await s.flush()
        for sec, text in ((newco, "Built the NewCo Robotics data team."),
                          (oldco, "Ran OldCo Shipping IT.")):
            slot = DocSlot(doc_section_id=sec.id, key=f"r{sec.sort_order + 1}_a", sort_order=0,
                           optional=False, default_variant_key="original")
            s.add(slot)
            await s.flush()
            s.add(DocVariant(doc_slot_id=slot.id, key="original", derived=False, tags=[],
                             text=text))
        await s.commit()
        return {"old": old.id, "new": new.id}

    return _db(fn)


def test_a_job_change_hides_the_new_employer_and_prints_the_old_one(monkeypatch):
    from kestrel.docs.assess import assess_master
    from kestrel.docs.catalog import load_catalog
    from kestrel.llm.client import ChatResult

    client, _ = _app(with_docs=False)
    ids = _job_change()

    async def add_render(s):
        r = DocRender(doc_master_id=ids["new"], config={}, rationale={})
        s.add(r)
        await s.commit()
        return r.id

    rid = _db(add_render)
    with client:
        page = client.get(f"/renders/{rid}").text
    doc = _preview_doc(page)
    assert [el.get_text() for el in doc.select(".role .org")] == [
        "a company in your industry", "OldCo Shipping"]
    assert "NewCo" not in str(doc) and "Ran OldCo Shipping IT." in doc.get_text()

    _hosted(monkeypatch)
    sent: list[str] = []

    class _Fake:
        def __init__(self):
            from kestrel.config import get_settings

            self.settings = get_settings().model_copy(deep=True)
            self.settings.confidential_mode = True

        async def complete(self, **kw):
            sent.append(json.dumps(kw.get("messages"), ensure_ascii=False))
            return ChatResult(content="not json", model="fake")

    async def fn(s):
        return await assess_master(_Fake(), await load_catalog(s, ids["new"]), session=s)

    _db(fn)
    assert sent and all("NewCo" not in m for m in sent)
    assert any("OldCo Shipping" in m for m in sent), "a past employer goes as written"


def test_every_open_ended_role_is_inferred_and_sample_marks_stay_in_the_sample():
    from kestrel.docs.seed import seed_all

    client, ids = _app(cap_marked=False)

    async def fn(s):
        s.add(DocSection(doc_master_id=ids["resume"], section_kind="role",
                         org_label="Side Studio", end_date="", sort_order=5))
        await seed_all(s)  # the fictional sample marks its own (fictional) employers
        await s.commit()

    _db(fn)
    hidden = _hidden()
    assert hidden.get(_EMPLOYER) == "a company in your industry"
    assert hidden.get("Side Studio") == "a company in your industry"
    assert not {"Lakeside Distribution", "Brightline Foods"} & set(hidden), (
        "the sample's marks leaked into the operator's own outputs")


def test_the_banner_shows_above_every_settings_tab_and_today_asks_until_confirmed():
    from kestrel.analytics.today import system_asks
    from kestrel.config import get_settings

    client, ids = _app(cap_marked=False)
    with client:
        page = client.get("/settings").text
        before = _db(lambda s: system_asks(s, get_settings(), None))
        client.post(f"/documents/section/{ids['cur']}/confirm-current",
                    data={"shown_as": "a company in your industry"})
        after = _db(lambda s: system_asks(s, get_settings(), None))
    assert page.index('data-testid="employer-inferred"') < page.index('class="tabs"')
    assert "redaction_confirm" in [a["key"] for a in before]
    assert "redaction_confirm" not in [a["key"] for a in after]


def test_load_catalog_skips_the_map_when_confidential_mode_is_off():
    from kestrel.docs.catalog import load_catalog

    _client, ids = _app(confidential=False)

    async def fn(s):
        return (await load_catalog(s, ids["resume"])).redaction

    assert _db(fn) is None


# ---------------------------------------------------------------- names chosen in the editor
def test_the_editor_adds_a_name_and_warns_about_everyday_words():
    client, ids = _app(cap_marked=False, cur_end="2024")
    with client:
        _mark(client, ids["cur"], "Meta Platforms, Inc.", "a social media company")
        assert "Meta" not in _db(_names_hidden)
        r = _mark(client, ids["cur"], "Meta Platforms, Inc.", "a social media company",
                  hide_name=["Meta Platforms, Inc.", "Meta Platforms"], hide_name_new="Meta")
    assert _db(_names_hidden) == ["Meta Platforms, Inc.", "Meta Platforms", "Meta"]
    assert "This is also an everyday word" in r.text  # "Meta" is in the common-word list
    emap = _db(_live_map)
    assert emap.redact("Meta ships; metadata stays.") == "a social media company ships; " \
        "metadata stays."


async def _names_hidden(s):
    from kestrel.web.redact import redaction_map

    return list((await redaction_map(s)).employers[0].hide())


async def _live_map(s):
    from kestrel.web.redact import redaction_map

    return await redaction_map(s)


def test_a_past_employer_sharing_a_word_survives_in_bullets():
    client, ids = _app(cap_marked=False)
    with client:
        client.post(f"/documents/section/{ids['past']}", data={"org_label": "Acme Labs"})
        _mark(client, ids["cur"], "Acme", _SHOWN)
    emap = _db(_live_map)
    assert emap.redact("Joined Acme Labs in 2015, then Acme.") == (
        f"Joined Acme Labs in 2015, then {_SHOWN}.")


# -------------------------------------------------------------------------- code hygiene
class _CodeFake:
    """Replies built from the code in what it was sent: `make(code)` per attempt."""

    def __init__(self, *makers):
        from kestrel.config import get_settings

        self.makers = list(makers)
        self.settings = get_settings()

    async def complete(self, **kw):
        from kestrel.llm.client import ChatResult

        code = _code_in(json.dumps(kw["messages"], ensure_ascii=False))
        return ChatResult(content=self.makers.pop(0)(code), model="fake")


def _suggest_reply(text: str) -> str:
    return json.dumps({"variants": [{"text": text, "tags": []}]}, ensure_ascii=False)


def test_a_leftover_code_is_repaired_once_then_quarantined_and_a_posting_bracket_is_fine():
    from kestrel.llm.guard import run_prompt
    from kestrel.llm.prompts import load_prompt

    spec = load_prompt("variant_suggest/v1")
    emap = from_pairs([(_EMPLOYER, _SHOWN)])
    msgs = [{"role": "user", "content": f"Apply at [Employer]; I work at {_EMPLOYER}."}]
    quarantined: list = []

    async def hook(messages, raw, error, model):
        quarantined.append((raw, error))

    def run(*makers):
        book = emap.codebook()
        return asyncio.run(run_prompt(_CodeFake(*makers), spec, book.encode_messages(msgs),
                                      reply_filter=book, on_quarantine=hook))

    ok = run(lambda c: _suggest_reply(f"Ran {c[:6]}X IT."), lambda c: _suggest_reply(f"Ran {c}."))
    assert ok.ok and ok.repaired and ok.value.variants[0].text == f"Ran {_EMPLOYER}."
    bad = run(lambda c: _suggest_reply(f"Ran {c[1:]} IT."), lambda c: _suggest_reply(c[2:7]))
    assert not bad.ok and bad.quarantined and quarantined and quarantined[-1][0]
    fine = run(lambda c: _suggest_reply("Apply at [Employer] today."))
    assert fine.ok and not fine.repaired


def test_a_wording_with_a_live_code_is_never_saved():
    client, ids = _app()
    slot = _slot_id(ids["cur"])
    book = from_pairs([(_EMPLOYER, _SHOWN)]).codebook()
    book.encode(_EMPLOYER)
    code = book.codes[0]
    with client:
        r = client.post(f"/documents/slot/{slot}/variants",
                        data={"text": f"Led IT for {code} across 300 staff.", "tags": ""})
    assert "stand-in code" in r.text
    assert not any(code in t for t in _texts(slot))


# ------------------------------------------------------------- final review: exact spellings
class _EchoBulletLLM:
    """Replies with the bullet it was sent, coded exactly as sent, in a new frame."""

    def __init__(self, settings, http=None, **_):
        self.settings = settings

    async def complete(self, **kw):
        import re

        from kestrel.llm.client import ChatResult

        user = kw["messages"][-1]["content"]
        bullet = re.search(r"BULLET: (.*)", user).group(1)
        return ChatResult(content=json.dumps({"variants": [
            {"text": f"Owned it: {bullet}", "tags": []}]}), model="fake")


def test_a_suggested_wording_keeps_the_spelling_the_operator_wrote(monkeypatch):
    _hosted(monkeypatch)
    monkeypatch.setattr("kestrel.llm.client.LLMClient", _EchoBulletLLM)
    client, ids = _app(cap_marked=False)
    slot = _slot_id(ids["cur"])

    async def caps(s):
        v = (await s.execute(select(DocVariant).where(DocVariant.doc_slot_id == slot))).scalar_one()
        v.text = "Led IT for NORTHWIND FREIGHT across 300 staff."
        await s.commit()

    _db(caps)
    with client:
        _mark(client, ids["cur"], _EMPLOYER, _SHOWN)
        shown = client.post(f"/documents/slot/{slot}/suggest").text
        wording = "Owned it: Led IT for NORTHWIND FREIGHT across 300 staff."
        assert wording in shown
        client.post(f"/documents/slot/{slot}/variants", data={"text": wording, "tags": ""})
    assert wording in _texts(slot) and not any(
        f"for {_EMPLOYER} across" in t and t.startswith("Owned") for t in _texts(slot))


def test_the_pipeline_follows_a_job_change():
    client, _ = _app(with_docs=False)
    _job_change()

    async def opps(s):
        for name in ("OldCo Shipping", "NewCo Robotics"):
            org = Org(name=name, normalized_name=name.lower())
            s.add(org)
            await s.flush()
            opp = Opportunity(track="employment", opp_type="job_posting", title=f"VP at {name}",
                              org_id=org.id, org_name=name, url=f"https://example.test/{org.id}",
                              stage="t2_done")
            s.add(opp)
            await s.flush()
            s.add(Evaluation(opportunity_id=opp.id, tier=2, track="employment",
                             verdict="apply", verdict_model="apply", score=4.5,
                             weighted_score=4.4, needs_review=False, dimensions={}, blocks={},
                             blockers=[], legitimacy={"status": "clean", "signals": []}))
        await s.commit()

    _db(opps)
    with client:
        rows = client.get("/opportunities").text
    # NewCo is current in the newest resume: hidden. OldCo is a past role there: shown, even
    # though the older resume still marks it.
    assert "NewCo Robotics" not in rows and "OldCo Shipping" in rows


def test_wizard_resume_step_approves_then_saves_and_continues():
    """D-UI-65: in the setup wizard the proposal carries ONE approval and ONE primary button,
    "Save resume and continue", disabled until the text is approved; accepting moves the wizard
    on. Outside the wizard the proposal is unchanged."""
    from kestrel.web.deps import templates

    prop = _proposal()
    common = dict(proposal=prop, markdown=_RESUME, track="employment", doc_kind="resume")
    wiz = templates().get_template("partials/import_proposal.html").render(ob=True, **common)
    box = wiz.split('data-testid="ob-resume-approve"')[1].split("</div>")[0]
    assert "I approve this resume text" in box and ">Save resume and continue</button>" in box
    assert ':disabled="!ok" disabled' in box and "Approve your resume text first" in box
    assert "Accept and create master" not in wiz and 'name="ob" value="1"' in wiz
    plain = templates().get_template("partials/import_proposal.html").render(**common)
    assert "Accept and create master" in plain and "ob-resume-approve" not in plain

    client, _ids = _app(confidential=False, with_docs=False)
    base = {"proposal_json": prop.value.model_dump_json(), "text": _RESUME,
            "track": "employment", "doc_kind": "resume", "ob": "1"}
    with client:
        # Finding 11: without the approval nothing is written and the hint shows in place.
        r = client.post("/api/documents/import/accept", data=base,
                        headers={"HX-Request": "true"})
        assert r.headers.get("HX-Retarget") == "#ob-approve-error"
        assert "HX-Redirect" not in r.headers
        r = client.post("/api/documents/import/accept", data={**base, "approved": "1"},
                        headers={"HX-Request": "true"})
    assert r.headers.get("HX-Redirect", "").startswith("/onboarding?step=")
