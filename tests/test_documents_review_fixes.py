"""Review findings on the document engine and the Tailor page: confidential mode redacts the
employer in the summary, skills and header text (PDF preview and Tailor rows alike), and an
override's model_choice is the model's own selection for the render, never an earlier operator
swap. Offline: a migrated SQLite file, the test client and in-memory catalogs."""

from __future__ import annotations

from types import SimpleNamespace as NS

from sqlalchemy import select

from kestrel.db.models import DocRender, SelectionOverride, Setting
from tests.test_stream3_flow import _give_master, _read, _seed_tailor
from tests.test_stream_c_pages import _client
from tests.test_web_phase13 import _dispose_engines  # noqa: F401  (autouse fixture)


def _catalog():
    from kestrel.docs.catalog import MasterCatalog, SectionView, SlotView

    sec = NS(section_kind="role", org_label="Acme Corp", org_label_redacted="A software firm",
             title="CIO", start_date="2019", end_date="2024", context_line=None)
    master = NS(
        doc_kind="resume", track="employment", look={},
        header={"name": "Pat Example", "email": "pat@acme-corp.example",
                "headline": "CIO at Acme Corp", "contact": "Acme Corp HQ, Boston"},
        summary_variants=[{"key": "s", "text": "Turned Acme Corp around in two years."}],
        skill_groups=[{"key": "erp", "label": "Acme Corp stack", "items": ["Acme Corp ERP"],
                       "default_visible": True}],
        education=[], certifications=[], licenses=[], projects=None,
    )
    return MasterCatalog(master=master, sections=[SectionView(sec, [
        SlotView("r1", False, "a", [NS(key="a", text="Led the IT function.", tags=[])])])],
        layouts=[])


def test_confidential_resolve_redacts_summary_skills_and_header_text():
    from kestrel.docs.catalog import resolve

    def texts(redact: bool):
        doc = resolve(_catalog(), {"summary": "s", "redact_orgs": redact})
        summary = [b.text for b in doc.blocks if b.type == "summary"]
        skills = [g for b in doc.blocks if b.type == "skills" for g in b.skills]
        return doc.header, summary, skills

    header, summary, skills = texts(False)
    assert header["contact"] == "Acme Corp HQ, Boston" and "Acme Corp" in summary[0]
    header, summary, skills = texts(True)
    assert header["contact"] == "A software firm HQ, Boston"
    assert header["headline"] == "CIO at A software firm"
    assert header["email"] == "pat@acme-corp.example"  # an identifier is never rewritten
    assert summary == ["Turned A software firm around in two years."]
    assert skills == [{"label": "A software firm stack", "items": ["A software firm ERP"]}]
    # Bullets stay byte-for-byte (hard rule 2).
    doc = resolve(_catalog(), {"summary": "s", "redact_orgs": True})
    assert [b.text for blk in doc.blocks for r in (blk.roles or []) for b in r.bullets] == [
        "Led the IT function."]


def test_tailor_rows_show_real_text_and_only_the_preview_is_redacted():
    """D-PRIV-1 rule A: the Tailor rows are an editor (their Edit wording box saves what it
    shows), so they show the operator's real text; confidential mode redacts the preview."""
    render_id, master_id = _seed_tailor()
    _give_master(
        master_id,
        summary_variants=[{"key": "summary_1", "text": "Rebuilt Acme Corp IT."},
                          {"key": "summary_2", "text": "Ran Acme Corp ops."}],
        skill_groups=[{"key": "identity", "label": "Identity", "items": ["Acme Corp SSO"],
                       "default_visible": True}],
    )
    c = _client()
    assert "Acme Corp" in c.get(f"/renders/{render_id}").text  # mode off: the real name

    async def _on(s):
        s.add(Setting(key="confidential_mode", value=True))
        await s.commit()

    _read(_on)
    html = _client().get(f"/renders/{render_id}").text
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    frame = soup.select_one("iframe[srcdoc]")
    preview = BeautifulSoup(frame["srcdoc"], "html.parser").get_text()
    frame.decompose()
    rows = soup.get_text()
    assert "Acme Corp" not in preview
    assert "Rebuilt [Confidential] IT." in preview and "[Confidential] SSO" in preview
    assert "Rebuilt Acme Corp IT." in rows  # the editor keeps the real wording


def test_model_choice_is_the_models_pick_even_after_an_earlier_swap():
    render_id, _ = _seed_tailor()
    c = _client()
    assert c.post(f"/renders/{render_id}/swap",
                  data={"slot_key": "s1", "variant_key": "b"}).status_code == 200
    assert c.post(f"/renders/{render_id}/wording",
                  data={"slot_key": "s1", "text": "My own words."}).status_code == 200
    assert c.post(f"/renders/{render_id}/swap",
                  data={"slot_key": "summary", "variant_key": "summary_2"}).status_code == 200
    assert c.post(f"/renders/{render_id}/wording",
                  data={"slot_key": "summary", "text": "Mine."}).status_code == 200

    async def _rows(s):
        rows = (await s.execute(select(SelectionOverride).order_by(SelectionOverride.id)))
        cfg = (await s.get(DocRender, render_id)).config
        return [(o.slot_key, o.model_choice, o.operator_choice) for o in rows.scalars()], cfg

    rows, cfg = _read(_rows)
    assert rows == [("s1", "a", "b"), ("s1", "a", "mine1"),
                    ("summary", "summary_1", "summary_2"), ("summary", "summary_1", "mine1")]
    assert cfg["slots"]["s1"] == "mine1" and cfg["summary"] == "mine1"


def test_dialog_backdrops_use_the_theme_token_in_light_and_dark():
    import re
    from pathlib import Path

    from kestrel import paths

    css = (Path(paths.web_static_dir()) / "css" / "kestrel.css").read_text(encoding="utf-8")
    assert css.count("--backdrop: rgba(") == 3  # light, dark (media query), dark (explicit)
    rules = re.findall(r"::backdrop\s*\{([^}]*)\}", css)
    assert rules and all("var(--backdrop)" in r and "rgba" not in r for r in rules)


def test_the_specs_name_the_indigo_brand_not_orange():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    ui = (root / "SPEC-INTERFACES.md").read_text(encoding="utf-8")
    web = ui.split("## 2. Web UI", 1)[1][:600]
    assert "#4F5BD5" in web and "#D4622A" not in web and "Orange" not in web


def test_hostile_slot_key_error_comes_back_as_plain_text_never_html():
    render_id, _ = _seed_tailor()
    r = _client().post(f"/renders/{render_id}/swap",
                       data={"slot_key": "<img src=x onerror=alert(1)>", "variant_key": "a"})
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("text/plain")
    assert "<img" in r.text  # shown as text by the toast, which only uses textContent
    assert _client().get("/renders/999999/download").headers["content-type"].startswith(
        "text/plain")
