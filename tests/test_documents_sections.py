"""Documents round two (operator feedback): optional sections (education, certifications,
licenses, projects; D-DOC-3), an accent color per document (D-DOC-4), roles collapsed in the
editor, and three more shipped layouts (classic, modern, compact; D-DOC-5). Offline: no browser,
no network, no model."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import inspect as sa_inspect

from kestrel.db.models import DocMaster, DocSection
from tests.test_documents_editor import ORIGINAL, _app, _rows

EDU = {"institution": "State University", "credential": "B.S.", "field": "Computer Science",
       "dates": "2008", "detail": "Summa cum laude"}
CERT = {"name": "Certified  Information Systems Security Professional", "issuer": "ISC2",
        "year": "2021", "credential_id": "123456"}
LIC = {"name": "Professional Engineer", "issuer": "State of Texas", "year": "2015"}
PROJ = {"name": "Inventory tool", "role": "Maintainer", "dates": "2022 - 2023",
        "description": "Tracks 4,000 assets & their owners <weekly>.", "link": "example.org/tool"}
HEADINGS = ("Education", "Certifications", "Licenses", "Projects")


def _strip_ids(entries: list[dict]) -> list[dict]:
    return [{k: v for k, v in e.items() if k != "id"} for e in entries]


def _shipped_layouts():
    from kestrel.docs.seed import _LAYOUTS

    return [NS(key=x["key"], section_order=x["section_order"],
               max_roles_shown=x["max_roles_shown"], description=x["description"])
            for x in _LAYOUTS]


def _catalog(*, sections: bool, look: dict | None = None, kind: str = "resume"):
    from kestrel.docs.catalog import MasterCatalog, SectionView, SlotView

    sec = NS(section_kind="role", org_label="Acme", org_label_redacted="A software firm",
             title="CIO", start_date="2019", end_date="2024", context_line=None)
    variant = NS(key="original", text=ORIGINAL, tags=[])
    master = NS(doc_kind=kind, track="employment", header={"name": "Pat Example"},
                summary_variants=[], skill_groups=[], look=look or {},
                education=[EDU] if sections else [], certifications=[CERT] if sections else [],
                licenses=[LIC] if sections else [], projects=[PROJ] if sections else None)
    return MasterCatalog(master=master, sections=[SectionView(sec, [
        SlotView("r1_cost", False, "original", [variant])])], layouts=_shipped_layouts())


def _html(layout: str | None, *, sections: bool = True, look: dict | None = None,
          **config) -> str:
    from kestrel.docs.catalog import resolve
    from kestrel.docs.render import render_html

    cfg = {"layout": layout, **config} if layout else config
    return render_html(resolve(_catalog(sections=sections, look=look), cfg))


def _headings(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    return [h.get_text(strip=True) for h in soup.select("h2.section-heading")]


ALL_LAYOUTS = ["executive", "condensed", "technical", "classic", "modern", "compact",
               "capability", "capability_short", None]


# ------------------------------------------------------------------- migration 0013 (D-DOC-3)
def test_migration_adds_the_columns_and_downgrades_cleanly(tmp_path):
    from alembic import command
    from sqlalchemy import create_engine

    from kestrel import paths
    from kestrel.db import migrate

    db = tmp_path / "m.db"
    migrate.run_migrations(db)

    def cols() -> set[str]:
        eng = create_engine(f"sqlite:///{db.as_posix()}")
        try:
            return {c["name"] for c in sa_inspect(eng).get_columns("doc_master")}
        finally:
            eng.dispose()

    new = {"education", "certifications", "licenses", "projects", "look"}
    assert new <= cols()
    assert migrate.current_revision(db) == migrate.head_revision()
    with paths.resource_path(paths.packaged_migrations()) as mig_dir:
        command.downgrade(migrate._config(str(mig_dir), db), "0012")
    assert not (new & cols()) and "skill_groups" in cols()
    migrate.run_migrations(db)
    assert new <= cols()


def test_migration_0013_downgrades_with_doc_section_child_rows(tmp_path):
    """doc_master is a parent table and migrations run with foreign_keys=ON: a batch table copy
    failed on DROP TABLE doc_master whenever a doc_section row existed (review finding 9)."""
    import sqlite3

    from alembic import command

    from kestrel import paths
    from kestrel.db import migrate

    db = tmp_path / "m.db"
    migrate.run_migrations(db)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO doc_master (id, track, name, doc_kind, is_active, education) "
                "VALUES (1, 'employment', 'r', 'resume', 1, '[]')")
    con.execute("INSERT INTO doc_section (id, doc_master_id, section_kind, org_label, sort_order)"
                " VALUES (1, 1, 'role', 'Acme', 0)")
    con.commit()
    con.close()
    with paths.resource_path(paths.packaged_migrations()) as mig_dir:
        command.downgrade(migrate._config(str(mig_dir), db), "0012")
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT org_label FROM doc_section").fetchall() == [("Acme",)]
        assert con.execute("SELECT name FROM doc_master").fetchall() == [("r",)]
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        con.close()
    assert migrate.current_revision(db) == "0012"


# ------------------------------------------------- optional sections in every layout (D-DOC-3)
@pytest.mark.parametrize("layout", ALL_LAYOUTS)
def test_empty_optional_sections_never_print(layout):
    heads = _headings(_html(layout, sections=False))
    for h in HEADINGS:
        assert h not in heads


@pytest.mark.parametrize("layout", ALL_LAYOUTS)
def test_present_optional_sections_print_verbatim_in_every_layout(layout):
    from kestrel.docs.render import bullets_in_html

    html = _html(layout)
    heads = _headings(html)
    for h in HEADINGS:
        assert heads.count(h) == 1, (layout, heads)
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text()
    # Typed text prints as typed (double space kept, entities decoded back to the source).
    for value in (*EDU.values(), *LIC.values(), CERT["name"], CERT["issuer"],
                  PROJ["description"], PROJ["link"]):
        assert value in text, (layout, value)
    assert "Credential ID 123456" in text
    # Entries are not model-selected bullets: only the variant reaches li[data-vk] (the
    # capability layouts print no resume roles at all).
    expected = [] if (layout or "").startswith("capability") else [("original", ORIGINAL)]
    assert bullets_in_html(html) == expected
    assert soup.find("table") is None and soup.find("img") is None


def test_the_order_follows_the_layout_and_unnamed_sections_go_last():
    assert _headings(_html("classic")) == ["Experience", *HEADINGS]
    assert _headings(_html("modern")) == ["Experience", "Projects", "Education",
                                          "Certifications", "Licenses"]
    # The capability layouts do not name them: they follow everything else, in the fixed order.
    cap = _headings(_html("capability"))
    assert cap[-4:] == ["Education", "Certifications", "Licenses", "Projects"]


def test_a_section_with_only_a_blank_or_malformed_entry_does_not_print():
    from kestrel.docs.catalog import resolve
    from kestrel.docs.render import render_html

    cat = _catalog(sections=False)
    cat.master.projects = [{"name": ""}, "not a dict", {"role": "no name"}]
    assert "Projects" not in _headings(render_html(resolve(cat, {"layout": "modern"})))


def test_redaction_still_swaps_the_employer_with_sections_present():
    html = _html("executive", redact_orgs=True)
    soup = BeautifulSoup(html, "html.parser")
    assert soup.select_one(".role .org").get_text() == "A software firm"
    assert "Acme" not in soup.get_text() and "State University" in soup.get_text()


def test_redaction_swaps_the_employer_inside_optional_section_text():
    """Confidential mode applies the same org_label -> org_label_redacted substitution to the
    optional sections (review finding 6): whole word, any case, never inside another word."""
    from kestrel.docs.catalog import resolve

    cat = _catalog(sections=True)
    cat.master.education = [{"institution": "ACME Leadership Academy",
                             "detail": "Sponsored by acme"}]
    cat.master.projects = [{"name": "Acmeville portal", "description": "Built at Acme, 2020"}]
    cat.master.certifications = []
    cat.master.licenses = []

    def texts(redact: bool) -> str:
        doc = resolve(cat, {"layout": "modern", "redact_orgs": redact})
        return " | ".join(
            f"{e.lead} {e.sub} {e.dates} {e.detail} {e.extra}"
            for b in doc.blocks for e in (b.entries or [])
        )

    plain, red = texts(False), texts(True)
    assert "ACME Leadership Academy" in plain and "Built at Acme" in plain
    # D-PRIV-1 matching: the name as written and in ALL CAPS, whole word. A single-word name
    # never matches in lowercase ("acme" could be ordinary prose).
    assert "Acme" not in red.replace("Acmeville", "") and "ACME" not in red
    assert "A software firm Leadership Academy" in red and "Built at A software firm" in red
    assert "Sponsored by acme" in red and "Acmeville portal" in red
    assert "Built at A software firm, 2020" in red and "Acmeville portal" in red


def test_the_p3_schema_inputs_do_not_include_optional_sections():
    from kestrel.docs.catalog import schema_inputs

    inputs = schema_inputs(_catalog(sections=True))
    assert set(inputs) == {"layouts", "summary_variants", "slots", "skill_groups"}
    dumped = json.dumps(inputs)
    assert "State University" not in dumped and "Inventory tool" not in dumped


def test_clean_entry_requires_the_lead_field_and_drops_unknown_fields():
    from kestrel.docs.sections import FIELD_MAX, clean_entry

    assert clean_entry("education", {"credential": "B.S."}) is None
    got = clean_entry("projects", {"name": "  X  ", "evil": "y", "link": "", "role": "R"})
    assert got == {"name": "X", "role": "R"}
    assert len(clean_entry("licenses", {"name": "n" * 999})["name"]) == FIELD_MAX


# --------------------------------------------------------------------- accent color (D-DOC-4)
@pytest.mark.parametrize("layout", ALL_LAYOUTS)
def test_every_layout_draws_its_accent_from_the_variable(layout):
    soup = BeautifulSoup(_html(layout, accent="#1f3a5f"), "html.parser")
    css = soup.style.get_text()
    assert soup.body["data-accent"] == "#1F3A5F"
    assert "--accent: #1F3A5F" in css and "var(--accent)" in css
    assert "#D4622A" not in css  # no hardcoded orange left in any style


def test_accent_precedence_and_validation():
    from kestrel.docs.styles import accent_from_params, valid_accent

    body = lambda html: BeautifulSoup(html, "html.parser").body  # noqa: E731
    assert body(_html("technical"))["data-accent"] == "#D4622A"  # the layout's own
    assert body(_html("technical", look={"accent": "#2F5D3A"}))["data-accent"] == "#2F5D3A"
    assert body(_html("technical", look={"accent": "#2F5D3A"},
                      accent="#7A1F2B"))["data-accent"] == "#7A1F2B"  # config wins
    hostile = _html("technical", accent="red;}</style><script>x</script>",
                    look={"accent": "url(x)"})
    assert body(hostile)["data-accent"] == "#D4622A" and "<script>x" not in hostile
    for bad in ("#12345", "#1234567", "123456", "#GGGGGG", "navy", " #12345g"):
        assert valid_accent(bad) is None
    assert accent_from_params({"accent": "custom", "accent_custom": "#abcdef"}) == (
        "#ABCDEF", None)
    assert accent_from_params({"accent": "custom", "accent_custom": "blue"})[1]
    assert accent_from_params({"accent": "#zzzzzz"})[1]
    assert accent_from_params({"accent": ""}) == (None, None)
    assert accent_from_params({"accent": "custom", "accent_custom": ""})[0] is None
    assert "Type a six-digit" in accent_from_params({"accent": "custom"})[1]


def test_render_html_rejects_a_hand_built_style_accent():
    from kestrel.docs.catalog import resolve
    from kestrel.docs.render import render_html

    doc = resolve(_catalog(sections=False), {"layout": "modern"})
    doc.style["accent"] = "red; background: url(evil)"
    assert BeautifulSoup(render_html(doc), "html.parser").body["data-accent"] == "#1F3A5F"


# ------------------------------------------------------------- new layouts (D-DOC-5)
def test_three_new_layouts_are_seeded_scoped_to_resumes_and_look_different():
    from kestrel.docs.catalog import _layout_kind
    from kestrel.docs.seed import _LAYOUTS

    keys = [x["key"] for x in _LAYOUTS]
    assert {"classic", "modern", "compact"} <= set(keys) and len(keys) == len(set(keys)) == 8
    for k in ("classic", "modern", "compact"):
        assert _layout_kind(NS(key=k)) == "resume"
    classes = {k: tuple(BeautifulSoup(_html(k), "html.parser").body["class"])
               for k in ("executive", "condensed", "technical", "classic", "modern", "compact")}
    assert len({c[0] for c in classes.values()}) == 6  # six distinct treatments
    compact = BeautifulSoup(_html("compact"), "html.parser")
    assert "(2019 - 2024)" in compact.select_one(".role .row").get_text()  # dates inline


def test_shipped_layout_copy_is_neutral():
    from kestrel.docs.seed import _LAYOUTS

    for x in _LAYOUTS:
        words = (x["label"] + " " + x["description"]).lower()
        for personal in ("cio", "cto", " it ", "jordan", "my ", " me "):
            assert personal not in f" {words} ", (x["key"], personal)
        assert "—" not in words


def test_new_layouts_are_inserted_at_startup_seeding(monkeypatch):
    from kestrel.db.engine import get_sessionmaker, reset_engine
    from kestrel.db.models import DocLayout
    from kestrel.docs.seed import _ensure_layouts

    _app(monkeypatch)

    async def go():
        await reset_engine()
        async with get_sessionmaker()() as s:
            await _ensure_layouts(s)
            await s.commit()
        await reset_engine()

    asyncio.run(go())
    keys = {r.key for r in _rows(DocLayout)}
    assert {"classic", "modern", "compact"} <= keys


# ---------------------------------------------------------------- the Documents page routes
def test_entry_add_edit_remove_round_trip(monkeypatch):
    client, mid = _app(monkeypatch)
    r = client.post(f"/documents/{mid}/sections/certifications", data=CERT)
    assert r.status_code == 200 and "Added to Certifications." in r.text
    assert 'data-kind="certifications" open' in r.text
    (m,) = _rows(DocMaster, id=mid)
    eid = m.certifications[0]["id"]
    assert len(eid) == 12 and _strip_ids(m.certifications) == [CERT]  # exactly as typed
    assert f'data-entry-id="{eid}"' in r.text
    r = client.post(f"/documents/{mid}/sections/certifications/{eid}",
                    data={**CERT, "year": "2022", "credential_id": ""})
    assert "Saved in Certifications." in r.text
    (m,) = _rows(DocMaster, id=mid)
    assert m.certifications == [{"name": CERT["name"], "issuer": "ISC2", "year": "2022",
                                 "id": eid}]
    r = client.post(f"/documents/{mid}/sections/certifications/{eid}/delete")
    assert "Removed from Certifications." in r.text
    (m,) = _rows(DocMaster, id=mid)
    assert m.certifications == []


def test_entry_routes_refuse_bad_input(monkeypatch):
    client, mid = _app(monkeypatch)
    r = client.post(f"/documents/{mid}/sections/education", data={"credential": "B.S."})
    assert "needs the institution" in r.text
    (m,) = _rows(DocMaster, id=mid)
    assert not m.education
    assert client.post(f"/documents/{mid}/sections/hobbies", data={"name": "x"}
                       ).status_code == 404
    r = client.post(f"/documents/{mid}/sections/projects/0", data=PROJ)
    assert "no longer in Projects" in r.text and "Nothing was saved" in r.text
    assert client.post("/documents/999/sections/projects", data=PROJ).status_code == 404
    client.post(f"/documents/{mid}/sections/projects", data=PROJ)
    (m,) = _rows(DocMaster, id=mid)
    eid = m.projects[0]["id"]
    r = client.post(f"/documents/{mid}/sections/projects/{eid}", data={"name": ""})
    assert "cannot be empty" in r.text
    (m,) = _rows(DocMaster, id=mid)
    assert _strip_ids(m.projects) == [PROJ]


def test_entries_are_addressed_by_id_so_a_stale_window_never_hits_the_wrong_entry(monkeypatch):
    """Two windows on one list (review finding 7): window B removes the first entry; window A
    then saves the second entry by its id, which still lands on that entry, and an edit to the
    removed one is refused by name instead of overwriting whatever moved into its position."""
    client, mid = _app(monkeypatch)
    client.post(f"/documents/{mid}/sections/licenses", data=LIC)
    client.post(f"/documents/{mid}/sections/licenses", data={**LIC, "name": "Second"})
    (m,) = _rows(DocMaster, id=mid)
    first, second = (e["id"] for e in m.licenses)
    assert first != second
    client.post(f"/documents/{mid}/sections/licenses/{first}/delete")  # window B
    r = client.post(f"/documents/{mid}/sections/licenses/{second}",
                    data={**LIC, "name": "Second", "year": "2030"})  # window A
    assert "Saved in Licenses." in r.text
    r = client.post(f"/documents/{mid}/sections/licenses/{first}",
                    data={**LIC, "name": "Stale"})  # window A, the entry B removed
    assert "no longer in Licenses" in r.text
    (m,) = _rows(DocMaster, id=mid)
    assert m.licenses == [{**LIC, "name": "Second", "year": "2030", "id": second}]


def test_entries_saved_before_ids_get_one_on_read_and_keep_it(monkeypatch):
    from kestrel.db.engine import get_sessionmaker, reset_engine

    client, mid = _app(monkeypatch)

    async def legacy():
        await reset_engine()
        async with get_sessionmaker()() as s:
            m = await s.get(DocMaster, mid)
            m.education = [EDU, "not an entry"]
            await s.commit()
        await reset_engine()

    asyncio.run(legacy())
    html = client.get(f"/documents?master_id={mid}").text
    (m,) = _rows(DocMaster, id=mid)
    eid = m.education[0]["id"]
    assert m.education[1] == "not an entry" and _strip_ids([m.education[0]]) == [EDU]
    assert f'data-entry-id="{eid}"' in html
    client.get(f"/documents?master_id={mid}")
    assert _rows(DocMaster, id=mid)[0].education[0]["id"] == eid  # stable across reads
    r = client.post(f"/documents/{mid}/sections/education/{eid}",
                    data={**EDU, "dates": "2009"})
    assert "Saved in Education." in r.text


def test_editor_hints_at_redaction_in_confidential_mode(monkeypatch):
    from kestrel.db.engine import get_sessionmaker, reset_engine
    from kestrel.db.models import Setting

    client, mid = _app(monkeypatch)
    assert 'data-testid="optional-confidential-hint"' not in client.get("/documents").text

    async def on():
        await reset_engine()
        async with get_sessionmaker()() as s:
            s.add(Setting(key="confidential_mode", value=True))
            await s.commit()
        await reset_engine()

    asyncio.run(on())
    html = client.get("/documents").text
    assert 'data-testid="optional-confidential-hint"' in html and "redacted label" in html
    assert "—" not in html.split('id="optional-sections"')[1].split("</details>")[0]


def test_the_page_lists_the_sections_and_the_preview_prints_them(monkeypatch):
    client, mid = _app(monkeypatch)
    html = client.get("/documents").text
    soup = BeautifulSoup(html, "html.parser")
    kinds = [d["data-kind"] for d in soup.select("#optional-sections details.optional-kind")]
    assert kinds == ["education", "certifications", "licenses", "projects"]
    assert "empty, not shown" in html

    def preview(**data) -> BeautifulSoup:
        r = client.post(f"/documents/{mid}/preview", data=data)
        return BeautifulSoup(BeautifulSoup(r.text, "html.parser").iframe["srcdoc"],
                             "html.parser")

    assert not set(HEADINGS) & set(_headings(str(preview(layout="modern"))))
    client.post(f"/documents/{mid}/sections/education", data=EDU)
    client.post(f"/documents/{mid}/sections/licenses", data=LIC)
    heads = _headings(str(preview(layout="modern")))
    assert "Education" in heads and "Licenses" in heads and "Projects" not in heads


def test_accent_is_saved_on_the_master_and_used_by_the_preview(monkeypatch):
    client, mid = _app(monkeypatch)
    html = client.get("/documents").text
    soup = BeautifulSoup(html, "html.parser")
    opts = [o["value"] for o in soup.select("select#accent option")]
    assert opts[0] == "" and "#1F3A5F" in opts and opts[-1] == "custom"
    assert soup.select_one("input#accent_custom") is not None

    r = client.post(f"/documents/{mid}/accent", data={"accent": "#1F3A5F"})
    assert "Saved #1F3A5F." in r.text
    (m,) = _rows(DocMaster, id=mid)
    assert m.look == {"accent": "#1F3A5F"}
    r = client.post(f"/documents/{mid}/preview", data={"layout": "technical"})
    inner = BeautifulSoup(BeautifulSoup(r.text, "html.parser").iframe["srcdoc"], "html.parser")
    assert inner.body["data-accent"] == "#1F3A5F"  # saved accent beats the layout's orange

    r = client.post(f"/documents/{mid}/accent",
                    data={"accent": "custom", "accent_custom": "#12ab9c"})
    assert "Saved #12AB9C." in r.text
    page = BeautifulSoup(client.get("/documents").text, "html.parser")
    assert page.select_one("select#accent option[selected]")["value"] == "custom"
    assert page.select_one("input#accent_custom")["value"] == "#12AB9C"

    bad = client.post(f"/documents/{mid}/accent",
                      data={"accent": "custom", "accent_custom": "#12ab9<"})
    assert "Nothing was saved" in bad.text and "<" not in bad.text.replace("<span", "").replace(
        "</span>", "")
    (m,) = _rows(DocMaster, id=mid)
    assert m.look == {"accent": "#12AB9C"}
    client.post(f"/documents/{mid}/accent", data={"accent": ""})
    (m,) = _rows(DocMaster, id=mid)
    assert m.look == {}


def test_the_review_carries_an_accent_and_the_test_render_passes_it(monkeypatch):
    from kestrel.db.models import DocRender

    seen: dict = {}

    async def fake_render(session, settings, master_id, *, config, opportunity_id=None, **kw):
        seen.update(config)
        r = DocRender(doc_master_id=master_id, config=config, rationale={})
        session.add(r)
        await session.commit()
        return NS(doc_render_id=r.id)

    monkeypatch.setattr("kestrel.docs.service.render_document", fake_render)
    client, mid = _app(monkeypatch)
    r = client.get(f"/renders/new?master_id={mid}&accent=custom&accent_custom=%232a5d3b",
                   follow_redirects=False)
    assert r.status_code == 303 and seen["accent"] == "#2A5D3B"
    rid = int(r.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/renders/{rid}").text
    assert 'id="look-accent"' in page and "Custom (#2A5D3B)" in page
    client.post(f"/renders/{rid}/look", data={"layout": "", "accent": "#7A1F2B"})
    (row,) = _rows(DocRender, id=rid)
    assert row.config["accent"] == "#7A1F2B"
    client.post(f"/renders/{rid}/look", data={"layout": "", "accent": "bogus"})
    (row,) = _rows(DocRender, id=rid)
    assert "accent" not in row.config  # refused, back to the document default


# ------------------------------------------------------------------ roles collapsed in the tree
def test_roles_render_collapsed_with_a_one_line_summary(monkeypatch):
    client, mid = _app(monkeypatch, two_slots=True)
    soup = BeautifulSoup(client.get("/documents").text, "html.parser")
    (role,) = soup.select("#tree details.section")
    assert not role.has_attr("open")
    head = role.select_one("summary").get_text(" ", strip=True)
    assert "Acme · CIO" in head and "2 bullets" in head
    # Editing still lives inside the collapsed role.
    assert role.select_one("form.add-bullet") and role.select_one("details.section-edit")


def test_the_role_just_edited_stays_open_after_the_tree_rerenders(monkeypatch):
    client, mid = _app(monkeypatch)
    (sec,) = _rows(DocSection, doc_master_id=mid)
    r = client.post(f"/documents/section/{sec.id}/bullets", data={"text": "Another one."})
    soup = BeautifulSoup(r.text, "html.parser")
    assert soup.select_one(f'details.section[data-section-id="{sec.id}"]').has_attr("open")
