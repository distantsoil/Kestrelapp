"""Profile screen overhaul (task 13, D-UI-137): the page on the shared design system, the first and
last name fields (D-UI-117), the near-floor margin control for pay and the day rate (D-RULES-2),
the Save bar and Undo (D-UI-119), the Stories tab, and the document preview as one scaled true
page that carries only the operator's own text (hard rule 2). Offline, on the per-test data home
from conftest; the page in a real browser is tests/test_profile_browser.py."""

from __future__ import annotations

import json
import re

import pytest
import yaml
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from kestrel.services import profile_edit as pe
from tests import profile_seed


@pytest.fixture
def client():
    from kestrel.web.app import build_app

    master_id = profile_seed.seed()
    with TestClient(build_app(worker=False)) as c:
        c.master_id = master_id
        yield c


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _stops(form: BeautifulSoup) -> tuple[list[str], str]:
    seg = form.select_one(".pf-margin .k-seg")
    labels = [b.get_text(strip=True) for b in seg.select("button")]
    return labels, seg.select_one("input[type=hidden]")["value"]


# ------------------------------------------------------------------------------- the page
def test_the_page_is_on_the_design_system_with_a_help_column_and_no_legacy_sheet(client):
    html = client.get("/profile?tab=about").text
    assert '/static/css/kestrel.css' not in html
    assert "/static/css/pages/profile.css" in html and "/static/js/profile.js" in html
    soup = _soup(html)
    assert soup.select_one(".k-scene h1").get_text(strip=True) == "Profile"
    cards = soup.select(".k-help-col .k-hcard")
    assert 3 <= len(cards) <= 4
    # Flyouts are outside the layout, where container containment cannot anchor them.
    assert soup.select_one(".k-layout #pf-fly-about-1") is None
    assert soup.select_one("#pf-fly-about-1.k-flyout") is not None
    assert not re.search(r"style=\"[^\"]*(#[0-9a-fA-F]{3,6}|rgba?\()", html.split("<main")[1])


def test_every_tab_names_its_help_and_stays_in_the_mockup_order(client):
    soup = _soup(client.get("/profile").text)
    assert [a["data-tab"] for a in soup.select(".pf-tabs a")] == [
        "about", "documents", "employment", "consulting", "location", "stories"]
    for tab in ("about", "employment", "consulting", "location", "stories"):
        track = tab if tab in ("employment", "consulting") else "employment"
        page = _soup(client.get(f"/profile?tab={tab}&track={track}").text)
        assert page.select_one(".k-help-col .k-hcard"), tab


def test_the_stories_tab_points_at_the_one_story_bank(client):
    html = client.get("/profile?tab=stories").text
    assert 'data-testid="pf-stories"' in html
    assert 'href="/stories"' in html and "never rewrites" in html
    assert 'data-tab="stories"' in html


def test_consulting_off_hides_its_tab_but_the_route_still_answers(client):
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel import paths
    from kestrel.db.engine import make_async_engine
    from kestrel.services.tracks import save_enabled

    async def off():
        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            await save_enabled(s, ("employment",))
            await s.commit()
        await engine.dispose()

    asyncio.run(off())
    html = client.get("/profile").text
    assert 'data-tab="consulting"' not in html
    assert client.get("/profile?tab=consulting&track=consulting").status_code == 200


# --------------------------------------------------------------------- first and last name
def test_about_you_has_a_required_first_name_and_an_optional_last_name(client):
    form = _soup(client.get("/profile?tab=about").text).select_one('[data-testid="profile-about"]')
    first, last = form.select_one("#pa-first"), form.select_one("#pa-last")
    assert first["name"] == "first_name" and first.has_attr("required")
    assert first["value"] == "Jordan" and last["name"] == "last_name" and last["value"] == "Rivera"
    assert not last.has_attr("required")
    assert form.select_one('[name="full_name"]') is None


def test_a_save_without_a_first_name_is_refused_and_a_one_word_name_is_kept(client):
    bad = client.post("/profile/employment/about", data={"first_name": " ", "last_name": "Rivera"})
    assert "Add your first name." in bad.text and 'data-testid="profile-problem"' in bad.text
    assert "k-toast" not in bad.headers.get("hx-trigger", "")
    ok = client.post("/profile/employment/about", data={"first_name": "Cher"})
    assert 'data-testid="profile-problem"' not in ok.text
    assert json.loads(ok.headers["hx-trigger"])["pf-saved"]["message"].startswith("Saved.")
    saved = yaml.safe_load(pe.profile_path().read_text(encoding="utf-8"))["employment"]
    assert (saved["first_name"], saved["last_name"], saved["full_name"]) == ("Cher", "", "Cher")


# ------------------------------------------------------------------------ near-floor margin
def test_employment_and_consulting_offer_the_switch_and_four_stops_defaulting_to_five(client):
    """The mockup's switch (D-UI-200): off is Exact, on shows 2, 5, 10 and 15 percent."""
    for tab, field in (("employment", "comp_floor_margin_pct"),
                       ("consulting", "day_rate_floor_margin_pct")):
        form = _soup(client.get(f"/profile?tab={tab}&track={tab}").text).select_one(
            f'[data-testid="profile-{tab}"]')
        labels, value = _stops(form)
        assert labels == ["2%", "5%", "10%", "15%"], tab
        assert value == "5", tab
        assert form.select_one(".pf-margin [data-pf-margin-on]").has_attr("checked"), tab
        assert form.select_one(f'.pf-margin input[name="{field}"]') is not None
        assert "% below" in form.select_one('[data-testid="near-floor-hint"]').get_text()
        assert "always kept" in form.select_one('[data-testid="near-floor-hint"]').get_text()


@pytest.mark.parametrize("stop,stored", [("0", 0), ("2", 2), ("5", 5), ("10", 10), ("15", 15)])
def test_each_stop_is_stored_and_read_back_on_both_tabs(client, stop, stored):
    r = client.post("/profile/employment/employment",
                    data={"seniority_floor": "", "comp_floor_margin_pct": stop,
                          "comp_floor_usd": "110000"})
    assert "pf-saved" in r.headers["hx-trigger"]
    assert _stops(_soup(r.text))[1] == stop
    r = client.post("/profile/consulting/consulting",
                    data={"day_rate_floor_margin_pct": stop, "day_rate_floor": "1800"})
    assert "pf-saved" in r.headers["hx-trigger"] and _stops(_soup(r.text))[1] == stop
    saved = yaml.safe_load(pe.profile_path().read_text(encoding="utf-8"))
    assert saved["employment"]["employment_terms"]["comp_floor_margin_pct"] == stored
    assert saved["consulting"]["consulting_terms"]["day_rate_floor_margin_pct"] == stored
    exact = _soup(client.get("/profile?tab=employment&track=employment").text)
    hint = exact.select_one('[data-testid="near-floor-hint"]').get_text(" ", strip=True)
    assert ("Off:" in hint) == (stored == 0)  # the switch is off at Exact (D-UI-200)


def test_a_stop_that_is_not_offered_is_refused_and_nothing_is_written(client):
    before = pe.profile_path().read_text(encoding="utf-8")
    r = client.post("/profile/employment/employment",
                    data={"seniority_floor": "", "comp_floor_margin_pct": "20"})
    assert "Pick a near-floor margin from the list." in r.text
    assert pe.profile_path().read_text(encoding="utf-8") == before


def test_the_margin_lives_only_on_profile_and_settings_links_to_it(client):
    settings = client.get("/settings").text
    assert "comp_floor_margin_pct" not in settings
    assert "day_rate_floor_margin_pct" not in settings


# ----------------------------------------------------------------------- the Save bar, Undo
def test_the_save_bar_is_in_the_page_and_a_save_offers_undo_in_a_toast(client):
    soup = _soup(client.get("/profile?tab=location&track=employment").text)
    form = soup.select_one("form[data-pf-form]")
    bar = form.select_one("[data-pf-savebar]")
    assert bar is not None and not bar.has_attr("data-shown")  # hidden until a field changes
    assert bar.select_one("[data-pf-discard]") and bar.select_one("button[type=submit]")
    r = client.post("/profile/employment/location", data={"remote_ok": "on"})
    toast = json.loads(r.headers["hx-trigger"])["pf-saved"]
    assert toast["message"].startswith("Saved.") and toast["undoUrl"] == "/profile/undo"
    assert _soup(r.text).select_one("[data-pf-savebar][data-shown]") is not None


def test_an_employment_save_also_refreshes_the_title_variants(client):
    r = client.post("/profile/employment/employment", data={"seniority_floor": ""})
    events = json.loads(r.headers["hx-trigger"])
    assert events["titleVariantsChanged"] is True and "pf-saved" in events


def test_undo_restores_the_previous_file_and_refuses_when_there_is_none(client):
    assert client.post("/profile/undo", data={"token": pe.undo_token()}).status_code in (204, 409)
    pe.profile_path().with_name(pe.PROFILE_BAK).unlink(missing_ok=True)
    assert client.post("/profile/undo", data={"token": pe.undo_token()}).status_code == 409
    client.post("/profile/employment/about", data={"first_name": "Jordan", "headline": "One"})
    client.post("/profile/employment/about", data={"first_name": "Jordan", "headline": "Two"})
    assert yaml.safe_load(pe.profile_path().read_text(encoding="utf-8"))[
        "employment"]["headline"] == "Two"
    assert client.post("/profile/undo", data={"token": pe.undo_token()}).status_code == 204
    assert yaml.safe_load(pe.profile_path().read_text(encoding="utf-8"))[
        "employment"]["headline"] == "One"


def test_undo_keeps_the_current_file_when_the_backup_cannot_be_read(client):
    client.post("/profile/employment/about", data={"first_name": "Jordan"})
    current = pe.profile_path().read_text(encoding="utf-8")
    pe.profile_path().with_name(pe.PROFILE_BAK).write_text("employment: [unclosed\n", "utf-8")
    assert client.post("/profile/undo", data={"token": pe.undo_token()}).status_code == 409
    assert pe.profile_path().read_text(encoding="utf-8") == current


def test_the_tab_switch_nudge_and_save_bar_are_wired_in_the_script():
    from pathlib import Path

    from kestrel import paths

    js = (Path(paths.web_static_dir()) / "js" / "profile.js").read_text(encoding="utf-8")
    assert "Save or discard your changes before you switch tabs." in js
    assert "data-pf-discard" in js and "/profile/undo" in js and "pf-saved" in js


# ------------------------------------------------------------------------------ the preview
def test_the_preview_is_one_scaled_true_page_of_the_operators_own_text(client):
    r = client.post(f"/documents/{client.master_id}/preview", data={})
    soup = _soup(r.text)
    page = soup.select_one("[data-pf-page] [data-pf-scale] iframe.preview-doc")
    assert page is not None
    doc = _soup(page["srcdoc"]).get_text(" ")
    for bullet in profile_seed.BULLETS:
        assert bullet in doc  # byte for byte from doc_variant.text (hard rule 2)
    assert "padding:.5in" in page["srcdoc"] and "min-height:11in" in page["srcdoc"]


def test_the_documents_tab_renders_the_preview_on_load_and_enlarge_outside_the_layout(client):
    soup = _soup(client.get(f"/documents?master_id={client.master_id}").text)
    pv = soup.select_one("#preview")
    assert "load" in pv["hx-trigger"] and "pf-refresh" in pv["hx-trigger"]
    assert pv["hx-post"] == f"/documents/{client.master_id}/preview"
    assert soup.select_one("[data-pf-enlarge]") is not None
    assert soup.select_one(".k-layout #pf-enlarge") is None and soup.select_one("#pf-enlarge")
    assert soup.select_one('[data-testid="doc-cards"] .pf-doccard.is-on b').get_text() \
        == "Operations resume"
    # Slot and variant keys are tooltips, never visible text (D-UI-29).
    assert "r1_b1" not in soup.select_one(".pf-docs").get_text()
    # The keys the Look controls post stay put for the preview and the test render.
    for ident in ("layout", "font", "header_align", "accent", "accent_custom"):
        assert soup.select_one(f"#{ident}") is not None


def test_documents_still_open_as_a_tab_of_profile(client):
    html = client.get("/documents").text
    assert 'data-tab="documents"' in html and 'aria-current="page"' in html
    assert 'id="import-form"' in html
