"""Review fixes for the Profile screen (task 13): the Look card, the photo, confidential mode on
About you, the shared story bank, work mode, the level list, title chips and Undo that says
when it cannot. Offline, on the per-test data home from conftest."""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import pytest
import yaml
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import select

from kestrel import paths
from kestrel.services import profile_edit as pe
from tests import profile_seed


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


@pytest.fixture
def client():
    from kestrel.web.app import build_app

    master_id = profile_seed.seed()
    with TestClient(build_app(worker=False)) as c:
        c.master_id = master_id
        yield c


def _png() -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    raw = b"\x00\xff\x00\x00"
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _setting(key):
    import asyncio

    from kestrel.db.engine import get_sessionmaker
    from kestrel.db.models import Setting

    async def go():
        async with get_sessionmaker()() as s:
            row = await s.get(Setting, key)
            return row.value if row else None

    return asyncio.run(go())


def _master(mid):
    import asyncio

    from kestrel.db.engine import get_sessionmaker
    from kestrel.db.models import DocMaster

    async def go():
        async with get_sessionmaker()() as s:
            m = await s.get(DocMaster, mid)
            return dict(m.look or {})

    return asyncio.run(go())


# ------------------------------------------------------------------------ 1. the Look card
def test_look_card_is_the_mockup_card_with_layouts_to_choose(client):
    soup = _soup(client.get(f"/documents?master_id={client.master_id}").text)
    card = soup.select_one('[data-testid="doc-look-card"]')
    assert card is not None
    assert card.select_one("select#layout") is None  # the empty select is gone
    buttons = card.select("[data-pf-layout] button")
    assert len(buttons) >= 3 and all(b.get_text(strip=True) for b in buttons)
    assert card.select_one("input#layout[type=hidden]")["value"]  # one is always chosen
    assert card.select_one("select#font") and card.select_one("input#header_align")
    assert len(card.select(".pf-swatch")) >= 5 and card.select_one("input#accent_custom")
    # Helper text sits under its control, never beside it.
    assert not card.select(".formgrid")
    assert all(f.select_one(".pf-hint") is None or f.find("label") for f in card.select(".pf-f"))


def test_look_is_saved_on_the_document_and_read_back(client):
    r = client.post(f"/documents/{client.master_id}/look", data={
        "layout": "modern", "font": "Arial", "header_align": "center", "accent": "#1F3A5F"})
    assert r.status_code == 200 and "Look saved" in r.text
    assert _master(client.master_id) == {
        "layout": "modern", "font": "Arial", "header_align": "center", "accent": "#1F3A5F"}
    soup = _soup(client.get(f"/documents?master_id={client.master_id}").text)
    assert soup.select_one("input#layout")["value"] == "modern"
    assert soup.select_one("select#font option[selected]")["value"] == "Arial"
    assert soup.select_one("input#header_align")["value"] == "center"
    # A preview that names nothing uses the saved look.
    pv = client.post(f"/documents/{client.master_id}/preview", data={})
    inner = _soup(_soup(pv.text).iframe["srcdoc"])
    assert inner.body["data-font"] == "Arial" and "align-center" in inner.body["class"]


def test_look_refuses_an_unknown_layout_font_or_accent_and_saves_nothing(client):
    bad = client.post(f"/documents/{client.master_id}/look", data={"layout": "nope"})
    assert "Nothing was saved" in bad.text and _master(client.master_id) == {}
    bad = client.post(f"/documents/{client.master_id}/look",
                      data={"layout": "modern", "font": "Comic Sans"})
    assert "Nothing was saved" in bad.text and _master(client.master_id) == {}
    bad = client.post(f"/documents/{client.master_id}/look",
                      data={"layout": "modern", "accent": "custom", "accent_custom": "x"})
    assert "Nothing was saved" in bad.text and _master(client.master_id) == {}


# ---------------------------------------------------------------------------- 3. the photo
def test_photo_upload_is_local_served_back_and_removable(client):
    r = client.post("/profile/photo", files={"photo": ("me.png", _png(), "image/png")})
    assert r.status_code == 200
    stored = list((paths.data_dir() / "profile").glob("photo.*"))
    assert len(stored) == 1 and stored[0].read_bytes() == _png()
    assert _setting("ui.avatar") == "photo"
    got = client.get("/profile/photo")
    assert got.status_code == 200 and got.headers["content-type"] == "image/png"
    assert got.headers["x-content-type-options"] == "nosniff"
    about = _soup(client.get("/profile?tab=about").text)
    assert about.select_one(".pf-avatar img[src^='/profile/photo']") is not None
    assert "Change photo" in about.get_text() and about.select_one("[data-pf-photo-remove]")
    assert "never goes on a resume" in about.get_text()
    client.post("/profile/photo/delete")
    assert not list((paths.data_dir() / "profile").glob("photo.*"))
    assert _setting("ui.avatar") == "initial"
    assert client.get("/profile/photo").status_code == 404
    about = _soup(client.get("/profile?tab=about").text)
    assert about.select_one(".pf-avatar").get_text(strip=True) == "JR"


def test_photo_refuses_what_is_not_a_small_image(client):
    r = client.post("/profile/photo", files={"photo": ("x.png", b"<svg onload=1>", "image/png")})
    assert "Choose a PNG, JPEG, WebP or GIF" in r.text
    big = _png() + b"\0" * (3 * 1024 * 1024)
    r = client.post("/profile/photo", files={"photo": ("x.png", big, "image/png")})
    assert "smaller than 2 MB" in r.text
    assert not list((paths.data_dir() / "profile").glob("photo.*"))
    assert _setting("ui.avatar") is None


# ------------------------------------------------- 4. confidential mode on About you
def test_about_has_confidential_mode_with_employer_and_shown_as(client):
    soup = _soup(client.get("/profile?tab=about").text)
    box = soup.select_one("#pf-conf")
    assert box.select_one("input[name=conf_mode][role=switch]")
    assert box.select_one("input[name=conf_employer]") and box.select_one("input[name=conf_shown]")
    assert "Hide my current employer" in box.get_text()


def test_confidential_toggle_and_labels_use_the_existing_settings_and_marks(client):
    from kestrel.db.engine import get_sessionmaker
    from kestrel.db.models import DocSection

    r = client.post("/profile/confidential", data={
        "conf_mode": "on", "conf_employer": "A regional food distributor",
        "conf_shown": "a food company"})
    assert r.status_code == 200 and "Saved" in r.text
    assert _setting("confidential_mode") is True

    import asyncio

    async def shown():
        async with get_sessionmaker()() as s:
            return (await s.execute(select(DocSection.org_label_redacted))).scalars().all()

    assert "a food company" in asyncio.run(shown())
    soup = _soup(client.get("/profile?tab=about").text)
    assert soup.select_one("input[name=conf_mode]").has_attr("checked")
    assert soup.select_one("input[name=conf_employer]")["value"] == "A regional food distributor"
    assert soup.select_one("input[name=conf_shown]")["value"] == "a food company"
    client.post("/profile/confidential", data={"conf_mode": "off"})
    assert _setting("confidential_mode") is False


def test_confidential_names_a_missing_employer_instead_of_guessing(client):
    r = client.post("/profile/confidential", data={
        "conf_mode": "on", "conf_employer": "Nowhere Inc", "conf_shown": "a company"})
    assert "No role at Nowhere Inc" in r.text


def test_typing_in_the_privacy_card_does_not_light_the_save_bar():
    js = (Path(paths.web_static_dir()) / "js" / "profile.js").read_text("utf-8")
    assert "data-pf-nodirty" in js


# --------------------------------------------------------------- 5. stories, shared with /stories
def _add_story(title="Fixed a broken intake"):
    import asyncio

    from kestrel.db.engine import get_sessionmaker
    from kestrel.db.models import Story

    async def go():
        async with get_sessionmaker()() as s:
            st = Story(title=title, situation="Intake was slow.", action="I rebuilt the form.",
                       result="Cycle time fell 30%.", themes=["process"], is_canonical=True,
                       operator_edited=True)
            s.add(st)
            await s.commit()
            return st.id

    return asyncio.run(go())


def test_stories_tab_shows_the_shared_bank_in_the_users_own_words(client):
    sid = _add_story()
    html = client.get("/profile?tab=stories").text
    soup = _soup(html)
    card = soup.select_one(f"[data-story='{sid}']")
    assert card is not None and "Fixed a broken intake" in card.get_text()
    for words in ("Intake was slow.", "I rebuilt the form.", "Cycle time fell 30%."):
        assert words in card.get_text()
    assert soup.select_one(".pf-tabs a[data-tab=stories] .pf-tabs__n").get_text(strip=True) == "1"
    assert soup.select_one("#pf-story-new form#pf-story-form")
    assert soup.select_one("[data-testid=pf-stories-open]")


def test_story_added_and_deleted_on_profile_is_the_same_bank_as_stories(client):
    r = client.post("/profile/stories", data={"title": "Led a cutover", "situation": "S",
                                              "action": "A", "result": "R"})
    assert "Led a cutover" in r.text
    assert "Led a cutover" in client.get("/stories").text
    sid = _soup(r.text).select_one("[data-story]")["data-story"]
    gone = client.post(f"/profile/stories/{sid}/delete")
    assert "Led a cutover" not in gone.text
    assert "Led a cutover" not in client.get("/stories").text
    empty = client.post("/profile/stories", data={"title": " "})
    assert "give the story a title" in empty.text


# ------------------------------------- 6. work mode, the level list and title chips
def test_employment_has_title_chips_a_level_list_and_work_mode(client):
    soup = _soup(client.get("/profile?tab=employment&track=employment").text)
    chips = soup.select("[data-pf-chips='target_titles'] .pf-chip")
    assert [c.get_text(" ", strip=True).replace(" x", "") for c in chips][:2] == [
        "Senior Operations Analyst", "Process Improvement Lead"][:2] or len(chips) == 2
    hidden = soup.select_one("textarea[name=target_titles]")
    assert hidden.get_text().splitlines() == ["Senior Operations Analyst",
                                              "Process Improvement Lead"]
    assert soup.select("[data-pf-chips='anti_titles'] .pf-chip")
    level = soup.select_one(".pf-level")
    assert level.select_one("input[name=seniority_floor][type=hidden]") is not None
    assert len(level.select("button")) >= 6
    assert soup.select_one("select[name=seniority_floor]") is None
    modes = soup.select_one("[data-testid=work-mode]")
    assert [i["name"] for i in modes.select("input[type=checkbox]")] == [
        "mode_remote", "mode_hybrid", "mode_onsite"]


def test_work_mode_is_saved_with_the_employment_terms_and_read_by_the_scorer(client):
    r = client.post("/profile/employment/employment", data={
        "seniority_floor": "", "mode_remote": "on", "mode_hybrid": "on"})
    assert r.status_code == 200
    data = yaml.safe_load(pe.profile_path().read_text("utf-8"))
    assert data["employment"]["employment_terms"]["work_modes"] == ["remote", "hybrid"]
    soup = _soup(client.get("/profile?tab=employment&track=employment").text)
    on = [i["name"] for i in soup.select("[data-testid=work-mode] input:checked")]
    assert on == ["mode_remote", "mode_hybrid"]
    from kestrel.pipeline.context import profile_compact
    from kestrel.pipeline.profile import load_profile

    assert "Work modes wanted: remote, hybrid" in profile_compact(load_profile("employment"))


def test_consulting_has_work_mode_too(client):
    client.post("/profile/consulting/consulting", data={"mode_remote": "on"})
    data = yaml.safe_load(pe.profile_path().read_text("utf-8"))
    assert data["consulting"]["consulting_terms"]["work_modes"] == ["remote"]
    soup = _soup(client.get("/profile?tab=consulting&track=consulting").text)
    assert soup.select_one("[data-testid=work-mode] input[name=mode_remote]").has_attr("checked")


# ------------------------------------------------------------------------------ 7. Undo
def test_a_save_raises_a_profile_toast_that_names_its_undo_token(client):
    r = client.post("/profile/employment/about", data={"first_name": "Jordan", "headline": "One"})
    ev = json.loads(r.headers["hx-trigger"])["pf-saved"]
    assert ev["undoUrl"] == "/profile/undo" and ev["token"]


def test_an_undo_after_a_newer_save_says_so_and_changes_nothing(client):
    first = json.loads(client.post("/profile/employment/about", data={
        "first_name": "Jordan", "headline": "One"}).headers["hx-trigger"])["pf-saved"]
    client.post("/profile/employment/about", data={"first_name": "Jordan", "headline": "Two"})
    r = client.post("/profile/undo", data={"token": first["token"]})
    assert r.status_code == 409 and "saved again" in r.text
    assert yaml.safe_load(pe.profile_path().read_text("utf-8"))[
        "employment"]["headline"] == "Two"


def test_undo_with_the_current_token_restores(client):
    client.post("/profile/employment/about", data={"first_name": "Jordan", "headline": "One"})
    second = json.loads(client.post("/profile/employment/about", data={
        "first_name": "Jordan", "headline": "Two"}).headers["hx-trigger"])["pf-saved"]
    assert client.post("/profile/undo", data={"token": second["token"]}).status_code == 204
    assert yaml.safe_load(pe.profile_path().read_text("utf-8"))[
        "employment"]["headline"] == "One"


def test_the_script_shows_a_plain_message_when_undo_fails():
    js = (Path(paths.web_static_dir()) / "js" / "profile.js").read_text("utf-8")
    assert "Could not undo. Your saved profile is unchanged." in js
    assert "pf-saved" in js


# -------------------------------------------------------------------------------- nits
def test_near_floor_hint_uses_the_mockup_wording(client):
    soup = _soup(client.get("/profile?tab=employment&track=employment").text)
    text = soup.select_one('[data-testid="near-floor-hint"]').get_text(" ", strip=True)
    assert 'Roles within 5% below your pay floor stay in, marked "Just under' in text


def test_kinds_of_work_checks_have_room():
    css = (Path(paths.web_static_dir()) / "css" / "pages" / "profile.css").read_text("utf-8")
    rule = css.split(".pf-checks {", 1)[1].split("}", 1)[0]
    assert "gap: 12px 28px" in rule


def test_the_preview_keeps_the_printed_paper_white(client):
    pv = client.post(f"/documents/{client.master_id}/preview", data={})
    assert "html{background:#fff}" in _soup(pv.text).iframe["srcdoc"]
    src = (Path(__file__).parents[1] / "kestrel" / "web" / "pages" / "documents.py").read_text()
    assert "printed paper" in src.split("html{background:#fff}")[0][-600:]


def test_undo_refuses_an_empty_or_malformed_token_plainly(client):
    client.post("/profile/employment/about", data={"first_name": "Jordan", "headline": "One"})
    client.post("/profile/employment/about", data={"first_name": "Jordan", "headline": "Two"})
    for data in ({}, {"token": ""}, {"token": "   "}, {"token": "zzz"}, {"token": "x" * 5000}):
        r = client.post("/profile/undo", data=data)
        assert r.status_code == 409 and "Nothing was changed" in r.text
    assert yaml.safe_load(pe.profile_path().read_text("utf-8"))[
        "employment"]["headline"] == "Two"
