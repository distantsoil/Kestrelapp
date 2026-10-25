"""The Tailor resume screen (D-UI-120): every route and action, one test per state of the locked
mockup, and the rules the screen must keep (every printed line is the operator's own wording,
a failed pick is surfaced and never defaulted away, nothing goes online without the operator's
confirmation, Undo reverts only what it changed).

Cloud-safe: database and web layer only; the model and the PDF renderer are stand-ins and nothing
touches the network or a browser. The fictional data is the mockup's (tests/tailor_seed.py)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db.engine import make_async_engine
from kestrel.db.models import DocMaster, DocRender, Pursuit, SelectionOverride, Setting
from kestrel.web import tailor_view as tv
from kestrel.web.pages import renders as page_mod
from tests import tailor_seed

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

HX = {"HX-Request": "true"}
ROOT = Path(__file__).resolve().parent.parent / "kestrel" / "web"


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _sm() -> async_sessionmaker:
    return async_sessionmaker(make_async_engine(paths.db_path()), expire_on_commit=False)


async def _get(model, id_):
    async with _sm()() as s:
        return await s.get(model, id_)


async def _all(model):
    async with _sm()() as s:
        return list((await s.execute(select(model).order_by(model.id))).scalars())


async def _config(render_id: int) -> dict:
    return dict((await _get(DocRender, render_id)).config)


async def _set_config(render_id: int, **changes) -> None:
    async with _sm()() as s:
        r = await s.get(DocRender, render_id)
        cfg = dict(r.config or {})
        cfg.update(changes)
        r.config = cfg
        await s.commit()


async def _write_pdf(render_id: int, tmp_path: Path) -> Path:
    path = tmp_path / "saved.pdf"
    path.write_bytes(b"%PDF-1.4 saved")
    async with _sm()() as s:
        r = await s.get(DocRender, render_id)
        r.pdf_path = str(path)
        await s.commit()
    return path


def _soup(text: str) -> BeautifulSoup:
    return BeautifulSoup(text, "html.parser")


def _srcdoc(page: str) -> BeautifulSoup:
    return _soup(_soup(page).select_one("iframe#tl-frame")["srcdoc"])


def _src_of_update(text: str) -> BeautifulSoup:
    return _soup(_soup(text).select_one("textarea#tl-src").get_text())


def _card(soup: BeautifulSoup, key: str):
    return soup.select_one(f'[id="tl-card-{key}"]')


def _toast(r) -> dict:
    return json.loads(r.headers["HX-Trigger"])["tl-toast"]


def _words_of(el) -> str:
    return " ".join(el.get_text(" ").split())


# ============================================================================ the default state
async def test_the_page_header_the_proof_strip_and_the_columns():
    ids = await tailor_seed.seed_async()
    with _client() as c:
        r = c.get(f"/renders/{ids['render']}?from=/pursuits")
    t = r.text
    soup = _soup(t)
    assert r.status_code == 200
    assert soup.select_one("h1").get_text(strip=True) == "Tailor resume"
    # A converted screen: its own sheet and script, none of the legacy page styles.
    assert "/static/css/pages/tailor.css" in t and "/static/js/tailor.js" in t
    assert "/static/css/kestrel.css" not in t
    head = _words_of(soup.select_one('[data-testid="tl-head"]'))
    assert "Senior Operations Analyst" in head and "Larkspur Logistics" in head
    assert "From your Operations resume" in head
    assert "Austin, TX" in head and "$118,000 to $142,000" in head
    assert soup.select_one('[data-testid="tl-fit"]')["aria-label"] == "Fit 4.6 out of 5"
    assert soup.select_one('[data-testid="tl-use"]').get_text(strip=True) == (
        "Use for this application")
    dl = soup.select_one('[data-testid="tl-download"]')
    assert dl["href"] == f"/renders/{ids['render']}/download" and "Download PDF" in dl.get_text()
    assert 'data-testid="render-back"' in t and "Back to Applications" in t
    # Nothing picked yet: the proof strip says so and offers the pick; the badge says whose pick.
    proof = soup.select_one('[data-testid="tl-proof"]')
    assert proof["data-pick"] == "none"
    assert "Every line is your own text, word for word." in proof.get_text()
    assert "has not picked for this role yet" in proof.get_text()
    assert soup.select_one('[data-testid="tl-pick"]').get_text(strip=True) == "Pick for this role"
    assert len(soup.select('[data-testid="tl-card"]')) == 6  # five lines and the summary
    assert soup.select_one('[data-testid="tl-card-skills"]') is not None
    assert "Your first wording" in _words_of(_card(soup, "m_throughput"))
    # The help column: four help cards, and the page's "How it works" opens a flyout.
    assert len(soup.select(".k-hcard")) == 4
    assert soup.select_one('[data-testid="tl-how"]')["data-k-flyout"] == "#tl-help-fly"
    # Page guides are gone (D-UI-146): no intro paragraph, just the strip.
    assert "tailor-intro" not in t


async def test_a_picked_render_names_the_model_behind_show_details_and_gives_each_reason():
    ids = await tailor_seed.seed_async(
        config={"pick_meta": {"model": "qwen3:14b", "online": False, "seconds": 38,
                              "at": "2026-10-24T10:00:00+00:00"}},
        rationale={"m_throughput": "Matches process improvement across sites word for word.",
                   "summary": "Leads with reporting across sites."},
    )
    with _client() as c:
        soup = _soup(c.get(f"/renders/{ids['render']}").text)
    proof = soup.select_one('[data-testid="tl-proof"]')
    assert proof["data-pick"] == "picked"
    assert "Kestrel picked which of your wordings fits this role." in proof.get_text()
    assert "It did not write or change any of them." in proof.get_text()
    by = soup.select_one('[data-testid="tl-picked-by"]').get_text(" ", strip=True)
    assert by.startswith("Picked on this computer · 38 seconds")
    assert soup.select_one("#tl-model").has_attr("hidden") and "qwen3:14b" in (
        soup.select_one("#tl-model").get_text())
    assert soup.select_one('[data-testid="tl-pick"]').get_text(strip=True) == "Pick again"
    card = _card(soup, "m_throughput")
    assert "Picked" in _words_of(card.select_one(".tl-sl"))
    assert "Matches process improvement across sites word for word." in _words_of(card)
    assert "Leads with reporting across sites." in _words_of(_card(soup, "summary"))


async def test_every_printed_line_is_the_operators_own_text_and_links_to_its_card():
    ids = await tailor_seed.seed_async()
    with _client() as c:
        page = c.get(f"/renders/{ids['render']}").text
    doc = _srcdoc(page)
    own = {t for _s, _o, _d, vs in tailor_seed.SLOTS for _k, t, _g in vs}
    lines = doc.select("li[data-vk]")
    assert lines and all(li.get_text() in own for li in lines)
    assert all(li["data-slot"] for li in lines)
    assert doc.select_one("p.summary")["data-slot"] == "summary"
    assert doc.select_one(".skills")["data-slot"] == "skills"
    # The frame is one true-size page: the preview sheet gives it its margin and highlight.
    assert "body{padding:.5in" in _soup(page).select_one("iframe#tl-frame")["srcdoc"]


async def test_other_wordings_are_radio_rows_and_only_an_optional_line_can_be_left_out():
    ids = await tailor_seed.seed_async()
    with _client() as c:
        soup = _soup(c.get(f"/renders/{ids['render']}").text)
    card = _card(soup, "m_throughput")
    radios = card.select('input[type="radio"]')
    assert [r["value"] for r in radios] == ["t1", "t2"]
    assert radios[0].has_attr("checked") and not radios[1].has_attr("checked")
    assert "Other wordings (1)" in _words_of(card)
    assert "Leave this line out" not in _words_of(card)
    opt = _card(soup, "r_o2c")
    assert [r["value"] for r in opt.select('input[type="radio"]')] == ["o1", "hide"]
    assert "Leave this line out" in _words_of(opt)
    one = _card(soup, "m_forecast")
    assert not one.select('input[type="radio"]')
    assert "Only one wording." in _words_of(one) and "Profile" in _words_of(one)
    # No free editing and no keyword chip that adds text.
    assert not soup.select("textarea:not(#tl-src)") and "Edit wording" not in soup.get_text()


async def test_a_missing_render_says_so_and_a_page_without_a_role_has_no_posting():
    ids = await tailor_seed.seed_async(opportunity=False)
    with _client() as c:
        r = c.get("/renders/9999")
    assert r.status_code == 200 and "No version found for id 9999." in r.text
    with _client() as c:
        soup = _soup(c.get(f"/renders/{ids['render']}").text)
    assert soup.select_one('[data-testid="tl-use"]') is None  # nothing to attach it to
    assert soup.select_one('[data-testid="tl-view-posting"]') is None
    assert "not for a role" in soup.get_text()


# ============================================================================== swap and Undo
async def test_a_swap_writes_the_override_first_and_answers_with_the_changed_parts():
    ids = await tailor_seed.seed_async()
    with _client() as c:
        r = c.post(f"/renders/{ids['render']}/swap",
                   data={"slot_key": "m_throughput", "variant_key": "t2"}, headers=HX)
    assert r.status_code == 200 and "<html" not in r.text
    soup = _soup(r.text)
    card = soup.select_one('#tl-card-m_throughput[hx-swap-oob]')
    assert card is not None and "Your choice" in _words_of(card)
    assert "You chose this. Kestrel learns from your swaps." in _words_of(card)
    assert card.select_one('input[value="t2"]').has_attr("checked")
    # The preview source rides along; the frame is patched, never reloaded.
    src = soup.select_one("textarea#tl-src")
    assert src["data-flash"] == "m_throughput"
    assert "Raised throughput 22% across two" in _src_of_update(r.text).get_text()
    for part in ("tl-head", "tl-warn", "tl-guard", "tl-chg", "tl-reqs", "tl-words"):
        assert soup.select_one(f'[id="{part}"][hx-swap-oob]'), part
    (row,) = await _all(SelectionOverride)
    assert (row.slot_key, row.model_choice, row.operator_choice) == ("m_throughput", "t1", "t2")
    cfg = await _config(ids["render"])
    assert cfg["slots"]["m_throughput"] == "t2" and cfg["stale"] is True
    toast = _toast(r)
    assert toast["message"].startswith("Swapped.") and "/swap/undo/" in toast["undoUrl"]


async def test_undo_reverts_only_that_swap_and_says_so_when_it_can_no_longer_be_undone():
    ids = await tailor_seed.seed_async()
    rid = ids["render"]
    with _client() as c:
        first = c.post(f"/renders/{rid}/swap", data={"slot_key": "m_review", "variant_key": "r2"})
        undo_first = _toast(first)["undoUrl"]
        second = c.post(f"/renders/{rid}/swap", data={"slot_key": "m_review", "variant_key": "r1"})
        undo_second = _toast(second)["undoUrl"]
        # A later change to the same part: the first Undo must not touch it.
        stale = c.post(undo_first)
        assert 'data-ok="0"' in stale.text and "This can no longer be undone." in stale.text
        assert (await _config(rid))["slots"]["m_review"] == "r1"
        ok = c.post(undo_second)
        assert 'data-ok="1"' in ok.text
        # The second swap's Undo put the part back to what it was just before: wording r2.
        assert (await _config(rid))["slots"]["m_review"] == "r2"
        assert c.post(undo_second).text.count('data-ok="0"') == 1  # used up
    rows = await _all(SelectionOverride)
    assert [(o.operator_choice) for o in rows] == ["r2"]  # the undone swap's audit row went too


async def test_leaving_an_optional_line_out_shows_left_out_and_undo_brings_it_back():
    ids = await tailor_seed.seed_async()
    rid = ids["render"]
    with _client() as c:
        r = c.post(f"/renders/{rid}/swap", data={"slot_key": "r_o2c", "variant_key": "hide"})
        card = _soup(r.text).select_one("#tl-card-r_o2c")
        # The operator's own choice reads "Your choice" (as in the mockup); a line the pick left
        # out reads "Left out".
        assert "Your choice" in _words_of(card.select_one(".tl-sl"))
        assert "Not on this resume." in card.get_text()
        assert "Mapped order-to-cash" not in _src_of_update(r.text).get_text()
        assert c.post(_toast(r)["undoUrl"]).text.count('data-ok="1"') == 1
    assert (await _config(rid))["slots"]["r_o2c"] == "o1"


async def test_a_wording_the_part_does_not_have_is_refused_and_nothing_is_written():
    ids = await tailor_seed.seed_async()
    with _client() as c:
        bad = c.post(f"/renders/{ids['render']}/swap",
                     data={"slot_key": "m_throughput", "variant_key": "alt_3"})
        ghost = c.post(f"/renders/{ids['render']}/swap",
                       data={"slot_key": "ghost", "variant_key": "t1"})
    assert bad.status_code == 400 and ghost.status_code == 400
    assert await _all(SelectionOverride) == []
    assert (await _config(ids["render"]))["slots"]["m_throughput"] == "t1"


async def test_a_skills_group_is_a_switch_with_the_same_override_path():
    ids = await tailor_seed.seed_async()
    with _client() as c:
        page = _soup(c.get(f"/renders/{ids['render']}").text)
        card = _card(page, "skills")
        boxes = card.select('input[type="checkbox"]')
        assert [b.has_attr("checked") for b in boxes] == [True, True, False]
        r = c.post(f"/renders/{ids['render']}/skills", data={"group": "languages", "on": "1"})
    assert r.status_code == 200
    assert "Spanish (conversational)" in _src_of_update(r.text).get_text()
    (row,) = await _all(SelectionOverride)
    assert (row.slot_key, row.model_choice, row.operator_choice) == (
        "skills:languages", "hide", "show")
    assert (await _config(ids["render"]))["skill_groups"]["languages"] is True


# ============================================================================== a failed pick
async def test_a_pick_that_failed_its_check_is_marked_and_the_default_shows_until_chosen():
    ids = await tailor_seed.seed_async(config={"needs_pick": {"m_review": "alt_3"}})
    rid = ids["render"]
    with _client() as c:
        page = c.get(f"/renders/{rid}").text
        soup = _soup(page)
        strip = soup.select_one('[data-testid="unresolved-keys"]')
        assert "couldn't pick a wording for 1 line" in strip.get_text()
        assert "alt_3" in strip.get_text()
        card = _card(soup, "m_review")
        assert "is-err" in card["class"]
        assert "Needs your pick" in _words_of(card.select_one(".tl-sl"))
        assert 'The AI asked for a wording called "alt_3".' in card.get_text()
        assert "so nothing was invented" in card.get_text()
        # The standard wording is what shows (the operator's own), and nothing is pre-selected.
        assert "Ran the weekly operations review for the VP of Operations." in card.get_text()
        assert not card.select('input[type="radio"][checked]')
        assert soup.select_one('[data-testid="tl-needs-note"]').get_text(strip=True) == (
            "1 line needs your pick")
        assert soup.select_one('[data-testid="tl-download"]')["data-needs"] == "1"
        assert soup.select_one('[data-testid="tl-use"]')["data-needs"] == "1"
        # Choosing clears it, says so, and the download gate opens.
        r = c.post(f"/renders/{rid}/swap", data={"slot_key": "m_review", "variant_key": "r2"})
        assert _toast(r)["message"].startswith("Thanks. That line now uses the wording you chose.")
        after = _soup(r.text)
        assert not after.select('[data-testid="unresolved-keys"]')
        assert after.select_one('[id="tl-head"]')["data-needs"] == "0"
    assert "needs_pick" not in await _config(rid)


async def test_a_saved_key_the_catalog_no_longer_has_needs_a_pick_too():
    ids = await tailor_seed.seed_async()
    await _set_config(ids["render"], slots={**tailor_tail(), "m_review": "GONE"},
                      summary="NO_SUMMARY")
    with _client() as c:
        soup = _soup(c.get(f"/renders/{ids['render']}").text)
    strip = soup.select_one('[data-testid="unresolved-keys"]')
    assert "2 lines" in strip.get_text() and "GONE" in strip.get_text()
    assert "NO_SUMMARY" in strip.get_text()
    assert 'The saved wording "GONE" no longer exists.' in _card(soup, "m_review").get_text()


def tailor_tail() -> dict:
    return {k: d for k, _o, d, _v in tailor_seed.SLOTS}


# ============================================================================= download and use
async def test_download_and_use_ask_first_while_a_line_needs_a_pick(tmp_path):
    ids = await tailor_seed.seed_async(config={"needs_pick": {"m_review": "alt_3"}})
    rid = ids["render"]
    await _write_pdf(rid, tmp_path)
    with _client() as c:
        no = c.get(f"/renders/{rid}/download")
        assert no.status_code == 409 and "1 line still needs your pick." in no.text
        att = c.post(f"/renders/{rid}/attach")
        assert att.status_code == 409 and "1 line still needs your pick." in att.text
        assert (await _get(Pursuit, ids["pursuit"])).document_id is None
        yes = c.get(f"/renders/{rid}/download?anyway=1")
        assert yes.status_code == 200 and yes.content == b"%PDF-1.4 saved"
        assert yes.headers["content-type"] == "application/pdf"
        ok = c.post(f"/renders/{rid}/attach", data={"anyway": "1"})
        assert ok.status_code == 200
    # The choice is recorded on the render, so the document never leaves silently.
    assert (await _config(rid))["left_unresolved"] == ["m_review"]
    assert (await _get(Pursuit, ids["pursuit"])).document_id == rid


async def test_download_makes_the_pdf_again_from_the_current_picks_when_they_changed(
        tmp_path, monkeypatch):
    ids = await tailor_seed.seed_async()
    rid = ids["render"]
    old = await _write_pdf(rid, tmp_path)
    calls: list[int] = []

    async def fake_refresh(session, settings, render):
        calls.append(render.id)
        fresh = tmp_path / "fresh.pdf"
        fresh.write_bytes(b"%PDF-1.4 fresh")
        render.pdf_path = str(fresh)
        cfg = dict(render.config or {})
        cfg.pop("stale", None)
        render.config = cfg
        await session.commit()
        return render

    monkeypatch.setattr("kestrel.docs.service.refresh_pdf", fake_refresh)
    with _client() as c:
        same = c.get(f"/renders/{rid}/download")
        assert same.content == old.read_bytes() and calls == []  # nothing changed: the saved file
        c.post(f"/renders/{rid}/swap", data={"slot_key": "m_throughput", "variant_key": "t2"})
        again = c.get(f"/renders/{rid}/download")
        assert again.content == b"%PDF-1.4 fresh" and calls == [rid]
        assert c.get(f"/renders/{rid}/download").content == b"%PDF-1.4 fresh" and calls == [rid]


async def test_download_says_why_when_the_pdf_cannot_be_made(tmp_path, monkeypatch):
    ids = await tailor_seed.seed_async(config={"stale": True})

    async def boom(session, settings, render):
        raise RuntimeError("no browser here")

    monkeypatch.setattr("kestrel.docs.service.refresh_pdf", boom)
    with _client() as c:
        r = c.get(f"/renders/{ids['render']}/download")
    assert r.status_code == 503 and "Could not make the PDF" in r.text
    assert "no browser here" not in r.text  # never the text of an arbitrary exception


async def test_refresh_pdf_writes_the_file_counts_the_pages_and_clears_stale(monkeypatch):
    from kestrel.config import get_settings
    from kestrel.docs import service
    from kestrel.docs.render import RenderedPdf

    ids = await tailor_seed.seed_async(config={"stale": True, "margins": "narrow"})
    seen: dict = {}

    async def fake_pdf(html: str):
        seen["html"] = html
        return RenderedPdf(pdf=b"%PDF-1.4 new", page_count=2)

    monkeypatch.setattr(service, "render_pdf", fake_pdf)
    async with _sm()() as s:
        render = await s.get(DocRender, ids["render"])
        out = await service.refresh_pdf(s, get_settings(), render)
    assert out.page_count == 2 and Path(out.pdf_path).read_bytes() == b"%PDF-1.4 new"
    assert "stale" not in out.config
    assert "margin: 0.35in" in seen["html"]  # the look reached the file
    assert "Led process improvement across two distribution centers" in seen["html"]


# ============================================================================== attach and Undo
async def test_use_for_this_application_attaches_then_detaches_and_undo_restores_exactly():
    ids = await tailor_seed.seed_async()
    rid = ids["render"]
    with _client() as c:
        on = c.post(f"/renders/{rid}/attach")
        assert "Attached to this application." in on.text
        assert _toast(on)["message"] == (
            "Attached to your Larkspur Logistics application. Kestrel never sends it.")
        assert (await _get(Pursuit, ids["pursuit"])).document_id == rid
        assert 'aria-pressed="true"' in on.text and "Attached" in _soup(on.text).select_one(
            "#tl-use").get_text()
        off = c.post(f"/renders/{rid}/attach")
        assert "Removed from this application." in off.text
        assert (await _get(Pursuit, ids["pursuit"])).document_id is None
        # Undo of the detach re-attaches; undo of the attach (now stale) refuses.
        assert c.post(_toast(on)["undoUrl"]).text.count('data-ok="0"') == 1
        assert c.post(_toast(off)["undoUrl"]).text.count('data-ok="1"') == 1
        assert (await _get(Pursuit, ids["pursuit"])).document_id == rid


async def test_attach_says_what_is_missing_when_there_is_no_role_or_no_list_entry():
    ids = await tailor_seed.seed_async(pursuit=False)
    with _client() as c:
        r = c.post(f"/renders/{ids['render']}/attach")
    assert "Add this role to your list first" in r.text and "application" in r.text


# ============================================================ coverage, words, privacy, posting
async def test_what_they_ask_for_is_covered_partly_or_not_and_links_to_the_line():
    ids = await tailor_seed.seed_async()
    with _client() as c:
        soup = _soup(c.get(f"/renders/{ids['render']}").text)
    rows = {r.select_one("div").get_text(" ", strip=True): r for r in soup.select(".tl-req")}
    assert len(rows) == 4
    proc = rows["Process improvement across sites Covered · hover to see the line"]
    assert "tl-req--m" in proc["class"] and proc["data-req"] == "m_throughput"
    kpi = next(r for k, r in rows.items() if k.startswith("KPI dashboards"))
    assert "tl-req--p" in kpi["class"] and kpi["data-req"] == "r_dash"
    gap = next(r for k, r in rows.items() if k.startswith("Six Sigma"))
    assert "tl-req--n" in gap["class"] and not gap.has_attr("data-req")
    assert "Not in your documents" in gap.get_text()


async def test_leaving_a_line_out_never_claims_the_requirement_is_still_covered():
    ids = await tailor_seed.seed_async(config={"slots": {**tailor_tail(), "r_dash": "hide"}})
    with _client() as c:
        soup = _soup(c.get(f"/renders/{ids['render']}").text)
    kpi = next(r for r in soup.select(".tl-req") if r.get_text().strip().startswith("KPI"))
    assert "tl-req--n" in kpi["class"] and "Left out of this version" in kpi.get_text()


async def test_a_role_that_was_never_scored_has_nothing_to_compare_and_no_fit_ring():
    ids = await tailor_seed.seed_async(evaluation=False)
    with _client() as c:
        soup = _soup(c.get(f"/renders/{ids['render']}").text)
    assert not soup.select(".tl-req")
    assert "has not been scored yet" in soup.select_one("#tl-reqs").get_text()
    assert soup.select_one(".tl-ring--none") is not None


async def test_words_from_the_posting_are_green_only_when_the_operators_text_holds_them():
    ids = await tailor_seed.seed_async()
    with _client() as c:
        soup = _soup(c.get(f"/renders/{ids['render']}").text)
    kws = {k.get_text(strip=True): "is-y" in k["class"]
           for k in soup.select('[data-testid="tl-kw"]')}
    assert kws["SQL"] is True and kws["KPI"] is True
    assert kws["WMS"] is False and kws["ERP"] is False and kws["Six Sigma Green Belt"] is False
    words_card = soup.select_one("#tl-words").get_text()
    assert "Kestrel will not add a word you did not write." in words_card
    # Nothing here writes text: the chips are not buttons and carry no form.
    assert not soup.select_one("#tl-words").select("button, form, input")


async def test_the_posting_slide_over_marks_requirements_and_never_applies_for_you():
    ids = await tailor_seed.seed_async()
    with _client() as c:
        soup = _soup(c.get(f"/renders/{ids['render']}").text)
    over = soup.select_one("#tl-post")
    assert over["role"] == "dialog"
    assert soup.select_one('[data-testid="tl-view-posting"]')["data-k-slideover"] == "#tl-post"
    link = over.select_one('[data-testid="tl-open-posting"]')
    assert link["href"] == "https://jobs.example.test/larkspur/1"
    assert link["target"] == "_blank" and "noopener" in link["rel"]
    assert "Kestrel never applies for you." in over.get_text()
    marks = over.select(".tl-pq")
    assert marks and {m["data-req"] for m in marks if m.has_attr("data-req")} >= {"m_throughput"}
    assert any("tl-pq--n" in m["class"] for m in marks)  # the Six Sigma line, not in the documents
    assert "Austin, TX" in over.get_text() and "$118,000 to $142,000" in over.get_text()


async def test_the_employer_switch_hides_the_current_employer_in_the_preview():
    ids = await tailor_seed.seed_async()
    rid = ids["render"]
    with _client() as c:
        soup = _soup(c.get(f"/renders/{rid}").text)
        sw = soup.select_one('[data-testid="tl-conf"]')
        assert not sw.has_attr("checked") and not sw.has_attr("disabled")
        priv = soup.select_one("#tl-priv").get_text()
        assert 'Shows "A regional food distributor" instead.' in priv
        assert "Meadowbrook Foods" in _srcdoc(c.get(f"/renders/{rid}").text).get_text()
        r = c.post(f"/renders/{rid}/privacy", data={"on": "1"})
        txt = _src_of_update(r.text).get_text()
        assert "A regional food distributor" in txt and "Meadowbrook Foods" not in txt
        assert (await _config(rid))["redact_orgs"] is True
    assert "carries no company names in its file details" in soup.get_text()


async def test_confidential_mode_keeps_the_employer_hidden_and_the_switch_locked():
    ids = await tailor_seed.seed_async()
    async with _sm()() as s:
        s.add(Setting(key="confidential_mode", value=True))
        await s.commit()
    with _client() as c:
        page = c.get(f"/renders/{ids['render']}").text
    soup = _soup(page)
    sw = soup.select_one('[data-testid="tl-conf"]')
    assert sw.has_attr("checked") and sw.has_attr("disabled")
    assert "Confidential mode is on in Settings" in soup.select_one("#tl-priv").get_text()
    assert "Meadowbrook Foods" not in _srcdoc(page).get_text()
    # The cards are the operator's editor: real wording, redacted only in the preview (D-PRIV-1).


async def test_with_no_employer_marked_the_switch_says_where_to_mark_it():
    ids = await tailor_seed.seed_async(redact_label=None)
    with _client() as c:
        soup = _soup(c.get(f"/renders/{ids['render']}").text)
    assert soup.select_one('[data-testid="tl-conf"]').has_attr("disabled")
    assert "No current employer is marked." in soup.select_one("#tl-priv").get_text()


# ================================================================================ page guard
async def test_a_line_the_pick_left_out_reads_left_out():
    ids = await tailor_seed.seed_async(config={"slots": {**tailor_tail(), "r_o2c": "hide"}})
    with _client() as c:
        card = _card(_soup(c.get(f"/renders/{ids['render']}").text), "r_o2c")
    assert "Left out" in _words_of(card.select_one(".tl-sl"))
    assert [r["value"] for r in card.select('input[type="radio"]')] == ["o1", "hide"]
    assert card.select_one('input[value="hide"]').has_attr("checked")


async def test_the_page_guard_fits_or_lists_every_optional_line_to_leave_out():
    ids = await tailor_seed.seed_async(pdf_pages=1)
    with _client() as c:
        ok = _soup(c.get(f"/renders/{ids['render']}").text).select_one("#tl-guard")
    assert "is-ok" in ok["class"] and "Fits on 1 page." in ok.get_text()
    assert ok.select_one(".tl-pgw__over").has_attr("hidden")
    over_ids = await tailor_seed.seed_async(pdf_pages=3)
    with _client() as c:
        g = _soup(c.get(f"/renders/{over_ids['render']}").text).select_one("#tl-guard")
    assert "is-over" in g["class"] and "3 pages, your limit is 2." in g.get_text()
    buttons = g.select("button")
    assert [b["hx-post"] for b in buttons] == [f"/renders/{over_ids['render']}/swap"]
    assert json.loads(buttons[0]["hx-vals"]) == {"slot_key": "r_o2c", "variant_key": "hide"}
    assert "Line 2" in buttons[0].get_text() and "Mapped order-to-cash" in buttons[0].get_text()


async def test_after_a_change_the_saved_count_is_stale_and_the_page_re_estimates_it():
    ids = await tailor_seed.seed_async(pdf_pages=3)
    with _client() as c:
        r = c.post(f"/renders/{ids['render']}/swap",
                   data={"slot_key": "r_o2c", "variant_key": "hide"})
    g = _soup(r.text).select_one("#tl-guard")
    assert g["data-stale"] == "1" and "is-over" not in g["class"]
    assert "Fits on" in g.get_text()


# ===================================================================================== the look
async def test_the_look_control_changes_this_application_only_and_can_become_the_default():
    ids = await tailor_seed.seed_async()
    rid = ids["render"]
    with _client() as c:
        soup = _soup(c.get(f"/renders/{rid}").text)
        look = soup.select_one('[data-testid="tl-look"]')
        labels = {b.get_text(strip=True) for b in look.select("button")}
        assert {"Classic", "Modern", "Smaller", "Default", "Larger", "Narrow", "Normal"} <= labels
        assert "Same as your Profile look (Classic)." in look.select_one(".tl-note").get_text()
        r = c.post(f"/renders/{rid}/look",
                   data={"layout": "modern", "size": "larger", "margins": "narrow"})
        note = _soup(r.text).select_one('[data-testid="tl-look-note"]').get_text(" ", strip=True)
        assert "Only for this application. Your Profile look stays Classic." in note
        assert "Make this my default" in note
        body = _src_of_update(r.text).select_one("body")
        assert body["data-margins"] == "narrow"
        cfg = await _config(rid)
        assert (cfg["layout"], cfg["size"], cfg["margins"]) == ("modern", "larger", "narrow")
        assert not (await _get(DocMaster, ids["master"])).look  # the Profile look is untouched
        made = c.post(f"/renders/{rid}/look/default")
        assert _toast(made)["message"] == "Saved as your Profile look."
        look_saved = (await _get(DocMaster, ids["master"])).look
        assert look_saved["layout"] == "modern" and look_saved["size"] == "larger"
        assert look_saved["margins"] == "narrow"
        assert "Same as your Profile look (Modern)." in _soup(made.text).select_one(
            '[data-testid="tl-look-note"]').get_text()
        # Undo puts back exactly what it replaced; used twice it says it cannot.
        assert c.post(_toast(made)["undoUrl"]).text.count('data-ok="1"') == 1
        assert not (await _get(DocMaster, ids["master"])).look
        assert c.post(_toast(made)["undoUrl"]).text.count('data-ok="0"') == 1


async def test_a_look_undo_refuses_when_the_profile_look_changed_since():
    ids = await tailor_seed.seed_async()
    rid = ids["render"]
    with _client() as c:
        c.post(f"/renders/{rid}/look", data={"layout": "modern"})
        made = c.post(f"/renders/{rid}/look/default")
        async with _sm()() as s:
            m = await s.get(DocMaster, ids["master"])
            m.look = {**m.look, "layout": "classic"}
            await s.commit()
        assert c.post(_toast(made)["undoUrl"]).text.count('data-ok="0"') == 1
    assert (await _get(DocMaster, ids["master"])).look["layout"] == "classic"


async def test_an_unknown_layout_size_or_margin_is_refused_or_ignored():
    ids = await tailor_seed.seed_async()
    rid = ids["render"]
    with _client() as c:
        bad = c.post(f"/renders/{rid}/look", data={"layout": "<b>nope</b>"})
        assert "nothing was changed" in bad.text and "<b>" not in bad.text
        c.post(f"/renders/{rid}/look", data={"layout": "modern", "size": "huge",
                                            "margins": "wide"})
    cfg = await _config(rid)
    assert cfg["layout"] == "modern" and "size" not in cfg and "margins" not in cfg


def test_the_document_template_honors_size_and_margins_and_only_from_a_whitelist():
    from kestrel.docs.catalog import RenderDoc
    from kestrel.docs.render import render_html
    from kestrel.docs.styles import resolve_style

    def html(**cfg) -> str:
        return render_html(RenderDoc(header={"name": "A"}, blocks=[],
                                     style=resolve_style("classic", cfg)))

    base = html()
    assert "margin: 0.5in" in base and "font-size: 10.5pt" in base
    narrow = html(size="larger", margins="narrow")
    assert "margin: 0.35in" in narrow and "font-size: 11.34pt" in narrow
    junk = html(size="huge; color:red", margins="1in")
    assert "margin: 0.5in" in junk and "color:red" not in junk


# ===================================================================================== Pick again
def _selection(**kw):
    from kestrel.docs.select import Selection

    cfg = {
        "layout": "classic", "summary": "sum_b",
        "slots": {"m_throughput": "t2", "m_forecast": "f1", "m_review": "r1",
                  "r_dash": "d1", "r_o2c": "hide"},
        "skill_groups": {"analysis": True, "tools": False, "languages": False},
        "redact_orgs": False,
    }
    return Selection(config=cfg, rationale={"m_throughput": "Best match."},
                     defaults_used=False, **kw)


async def test_pick_again_keeps_the_operators_swaps_marks_a_failed_slot_and_records_the_pick(
        monkeypatch):
    ids = await tailor_seed.seed_async()
    rid = ids["render"]
    calls: list[int] = []

    async def fake_select(session, settings, catalog, opportunity_id):
        calls.append(opportunity_id)
        return _selection(needs_pick={"r_dash": "alt_3"})

    monkeypatch.setattr(page_mod, "_run_selection", fake_select)
    with _client() as c:
        c.post(f"/renders/{rid}/swap", data={"slot_key": "m_review", "variant_key": "r2"})
        r = c.post(f"/renders/{rid}/pick", headers=HX)
    assert r.status_code == 200 and calls == [ids["opp"]]
    cfg = await _config(rid)
    assert cfg["slots"]["m_review"] == "r2"  # the operator's swap stays
    assert cfg["slots"]["m_throughput"] == "t2" and cfg["slots"]["r_o2c"] == "hide"
    assert cfg["summary"] == "sum_b" and cfg["needs_pick"] == {"r_dash": "alt_3"}
    assert cfg["pick_meta"]["online"] is False and cfg["pick_meta"]["model"]
    assert cfg["model_picks"]["m_review"] == "r1"
    assert cfg["model_picks"]["r_dash"] == "alt_3"  # the key the model named, not our default
    rr = await _get(DocRender, rid)
    assert rr.rationale == {"m_throughput": "Best match."} and rr.defaults_used is False
    soup = _soup(r.text)
    assert soup.select_one('[data-testid="tl-proof"]')["data-pick"] == "picked"
    assert "Picked on this computer" in soup.select_one('[data-testid="tl-picked-by"]').get_text()
    assert "alt_3" in soup.select_one('[data-testid="unresolved-keys"]').get_text()
    assert "Your choice" in _words_of(_card(soup, "m_review").select_one(".tl-sl"))
    # A later swap reports the MODEL's pick (r1), not the operator's earlier one, to the loop.
    with _client() as c:
        c.post(f"/renders/{rid}/swap", data={"slot_key": "m_review", "variant_key": "r1"})
    assert [(o.slot_key, o.model_choice, o.operator_choice)
            for o in await _all(SelectionOverride)][-1] == ("m_review", "r1", "r1")


async def test_pick_again_that_fails_changes_nothing_and_says_why(monkeypatch):
    ids = await tailor_seed.seed_async()
    rid = ids["render"]
    before = await _config(rid)

    async def boom(session, settings, catalog, opportunity_id):
        raise ConnectionError("localhost:11434 refused")

    monkeypatch.setattr(page_mod, "_run_selection", boom)
    with _client() as c:
        r = c.post(f"/renders/{rid}/pick", headers=HX)
        msg = _soup(r.text).select_one('[data-testid="tl-pick-error"]').get_text()
        assert "Kestrel could not pick this time" in msg and "Your picks are unchanged." in msg
        assert "11434" not in r.text  # never the text of an arbitrary exception

        async def defaults(session, settings, catalog, opportunity_id):
            return NS(defaults_used=True, error="doc-select validation: layout", config={})

        monkeypatch.setattr(page_mod, "_run_selection", defaults)
        r2 = c.post(f"/renders/{rid}/pick", headers=HX)
        assert "could not make a valid pick this time" in r2.text
    assert await _config(rid) == before


async def test_pick_again_needs_a_role_to_pick_for(monkeypatch):
    ids = await tailor_seed.seed_async(opportunity=False)
    with _client() as c:
        soup = _soup(c.post(f"/renders/{ids['render']}/pick", headers=HX).text)
    assert "nothing to pick for" in soup.select_one('[data-testid="tl-pick-error"]').get_text()
    assert soup.select_one('[data-testid="tl-pick"]') is None  # no button without a role


def _online(monkeypatch, employer="hidden"):
    async def route(session, settings, master_id=None):
        return {"online": True, "model": "vendor/model-x", "dest": "b|https://x.example/v1|vendor/model-x",
                "provider": "OpenRouter",
                "sends": "your resume variant texts and the opportunity summary",
                "pick_sends": page_mod._PICK_SENDS, "employer": employer}

    monkeypatch.setattr(page_mod, "_pick_route", route)


async def test_an_online_pick_asks_first_and_the_server_checks_the_ask_again(monkeypatch):
    ids = await tailor_seed.seed_async()
    rid = ids["render"]
    _online(monkeypatch)
    sent: list[int] = []

    async def fake_select(session, settings, catalog, opportunity_id):
        sent.append(opportunity_id)
        return _selection()

    monkeypatch.setattr(page_mod, "_run_selection", fake_select)
    with _client() as c:
        # The page offers the dialog, not a direct send.
        btn = _soup(c.get(f"/renders/{rid}").text).select_one('[data-testid="tl-pick"]')
        assert btn["hx-get"] == f"/renders/{rid}/pick" and "hx-post" not in btn.attrs
        dlg = _soup(c.get(f"/renders/{rid}/pick").text)
        t = dlg.select_one('[data-testid="tl-ask"]').get_text(" ", strip=True)
        assert "OpenRouter" in t and "vendor/model-x" in t
        assert dlg.select_one('[data-testid="tl-sends"]').get_text() == (
            "Sends your resume variant texts and the opportunity summary.")
        assert "Not sent: your name, contact details and your resume file." in t
        assert "your current employer is hidden before anything is sent" in t
        assert dlg.select_one('[data-testid="tl-confirm"]').has_attr("required")
        # Nothing is sent without the confirmation, for the configured model.
        for data in ({}, {"confirmed": "1"}, {"confirmed": "1", "model": "other/model"},
                     {"model": "vendor/model-x"},
                     {"confirmed": "1", "model": "vendor/model-x"},  # no destination
                     {"confirmed": "1", "model": "vendor/model-x",
                      "dest": "b|https://other.example/v1|vendor/model-x"}):
            r = c.post(f"/renders/{rid}/pick", data=data, headers=HX)
            assert "Nothing was sent." in r.text, data
        assert sent == []
        ok = c.post(f"/renders/{rid}/pick", headers=HX,
                    data={"confirmed": "1", "model": "vendor/model-x",
                          "dest": "b|https://x.example/v1|vendor/model-x"})
    assert sent == [ids["opp"]]
    assert (await _config(rid))["pick_meta"]["online"] is True
    assert "Picked by an online model" in _soup(ok.text).select_one(
        '[data-testid="tl-picked-by"]').get_text()


async def test_an_online_pick_is_held_when_confidential_mode_has_no_employer(monkeypatch):
    ids = await tailor_seed.seed_async()
    _online(monkeypatch, employer="held")
    sent: list[int] = []

    async def fake_select(session, settings, catalog, opportunity_id):
        sent.append(1)
        return _selection()

    monkeypatch.setattr(page_mod, "_run_selection", fake_select)
    with _client() as c:
        dlg = _soup(c.get(f"/renders/{ids['render']}/pick").text)
        held = dlg.select_one('[data-testid="tl-employer-held"]').get_text()
        assert "This send will be held." in held
        assert dlg.select_one('[data-testid="tl-send"]') is None
        r = c.post(f"/renders/{ids['render']}/pick", headers=HX,
                   data={"confirmed": "1", "model": "vendor/model-x",
                         "dest": "b|https://x.example/v1|vendor/model-x"})
    assert "no current employer is chosen" in r.text and sent == []


async def test_a_local_pick_never_claims_to_leave_the_computer_and_posts_directly():
    ids = await tailor_seed.seed_async()
    with _client() as c:
        soup = _soup(c.get(f"/renders/{ids['render']}").text)
        btn = soup.select_one('[data-testid="tl-pick"]')
        assert btn["hx-post"] == f"/renders/{ids['render']}/pick"
        assert "on this computer" in btn["data-busy-text"]
        dlg = c.get(f"/renders/{ids['render']}/pick").text
    assert "Nothing leaves it." in dlg


# ======================================================================== selection, partially
async def test_select_config_keeps_valid_picks_and_marks_only_the_failed_slot(monkeypatch):
    from kestrel.docs import select as select_mod
    from kestrel.docs.catalog import load_catalog
    from kestrel.llm.guard import GuardResult
    from kestrel.llm.schemas import P3DocSelect

    ids = await tailor_seed.seed_async()
    value = P3DocSelect(
        layout="classic", summary="sum_b",
        slots={"m_throughput": "t2", "m_forecast": "f1", "m_review": "alt_3", "r_dash": "d1",
               "r_o2c": "hide"},
        skill_groups={"analysis": True, "tools": True, "languages": False},
        rationale={"m_throughput": "Best.", "m_review": "Made up."}, coherence_check="ok",
    )

    async def fake(client, **kw):
        return GuardResult(ok=False, value=value, error="doc-select validation: slot 'm_review'")

    monkeypatch.setattr(select_mod, "run_doc_select", fake)
    async with _sm()() as s:
        catalog = await load_catalog(s, ids["master"])
        sel = await select_mod.select_config(s, NS(settings=None), catalog,
                                             opportunity_id=ids["opp"])
    assert sel.defaults_used is False and sel.needs_pick == {"m_review": "alt_3"}
    assert sel.config["slots"]["m_throughput"] == "t2" and sel.config["slots"]["r_o2c"] == "hide"
    assert sel.config["slots"]["m_review"] == "r1"  # the operator's standard wording, not alt_3
    assert "m_review" not in sel.rationale and sel.rationale["m_throughput"] == "Best."
    assert "alt_3" not in json.dumps(sel.config)


async def test_select_config_still_falls_back_whole_when_the_summary_is_not_real(monkeypatch):
    from kestrel.docs import select as select_mod
    from kestrel.docs.catalog import load_catalog
    from kestrel.llm.guard import GuardResult
    from kestrel.llm.schemas import P3DocSelect

    ids = await tailor_seed.seed_async()
    value = P3DocSelect(layout="classic", summary="made_up", slots={}, skill_groups={},
                        rationale={}, coherence_check="ok")

    async def fake(client, **kw):
        return GuardResult(ok=False, value=value, error="doc-select validation: summary")

    monkeypatch.setattr(select_mod, "run_doc_select", fake)
    async with _sm()() as s:
        catalog = await load_catalog(s, ids["master"])
        sel = await select_mod.select_config(s, NS(settings=None), catalog,
                                             opportunity_id=ids["opp"])
    assert sel.defaults_used is True and not sel.needs_pick


# ================================================================================== consulting
async def test_the_consulting_track_says_capability_statement_and_lead():
    ids = await tailor_seed.seed_async(track="consulting")
    async with _sm()() as s:
        m = await s.get(DocMaster, ids["master"])
        m.doc_kind = "capability_statement"
        await s.commit()
    with _client() as c:
        soup = _soup(c.get(f"/renders/{ids['render']}").text)
    assert soup.select_one("h1").get_text(strip=True) == "Tailor capability statement"
    assert soup.select_one('[data-testid="tl-use"]').get_text(strip=True) == "Use for this lead"
    assert "What I do" in _words_of(_card(soup, "summary"))
    assert "How I work" in _words_of(_card(soup, "skills"))
    assert "capability statement, full size" in soup.get_text()


# ====================================================================================== static
def test_no_hex_or_rgb_color_in_the_page_sheet_script_or_templates():
    files = [ROOT / "static/css/pages/tailor.css", ROOT / "static/js/tailor.js",
             ROOT / "templates/renders.html",
             *(ROOT / "templates/partials").glob("tailor_*.html"),
             ROOT / "templates/partials/render_review.html"]
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"#[0-9A-Fa-f]{3,8}\b", re.sub(r"&#\d+;", "", text)), f.name
        assert not re.search(r"\b(?:rgb|rgba|hsl|hsla)\(", text), f.name
    assert not re.search(r"—", "".join(f.read_text(encoding="utf-8") for f in files))


def test_the_page_never_sends_anything_on_the_operators_behalf():
    text = "".join(f.read_text(encoding="utf-8") for f in [
        ROOT / "templates/renders.html", ROOT / "templates/partials/render_review.html",
        ROOT / "templates/partials/tailor_macros.html", ROOT / "static/js/tailor.js"])
    assert "mailto:" not in text and "/send" not in text and "/apply" not in text
    # The one outbound link is the employer's own posting, in a new tab, with no referrer power.
    assert text.count('target="_blank"') == 1 and "noopener noreferrer" in text


# ================================================================================ view helpers
def test_tokens_drop_stop_words_and_plural_endings():
    assert tv.tokens("Experience with SQL and Forecasting dashboards") == {
        "sql", "forecasting", "dashboard"}


def test_a_requirement_links_to_the_line_that_shares_its_words_or_to_none():
    lines = [tv.Line("a", "Led process improvement across two distribution centers"),
             tv.Line("b", "Built KPI dashboards in SQL and Tableau", ("kpi",))]
    assert tv.link("Process improvement across sites", lines) == "a"
    assert tv.link("KPI dashboards", lines) == "b"
    assert tv.link("Six Sigma Green Belt", lines) is None
    assert tv.link("improvement", lines) == "a"  # one word is enough when that is all it has
    assert tv.link("Across the board of nothing", lines) is None  # one stray word is not enough


def test_posting_words_hold_only_what_the_posting_uses_and_mark_the_unheld_ones():
    words = tv.posting_words(
        "We use SQL, a WMS and ERP. A Six Sigma belt helps. Tableau is a plus.",
        vocabulary=["SQL", "Python", "Tableau"], shown_text="Built reports in SQL.",
        gap_phrases=["Six Sigma Green Belt (preferred)"],
    )
    held = {w["term"]: w["held"] for w in words}
    assert held["SQL"] is True and held["Tableau"] is False
    assert "Python" not in held  # the posting never uses it
    assert held["WMS"] is False and held["ERP"] is False and held["Six Sigma"] is False
    assert words[0]["held"] is True  # held words first
    assert tv.posting_words("", vocabulary=["SQL"], shown_text="SQL") == []


def test_posting_blocks_mark_the_best_matching_line_once_per_requirement():
    rows = tv.posting_blocks(
        "## Duties\n- Lead process improvement across sites.\n- Build KPI dashboards.\nPlain text.",
        [{"text": "Process improvement across sites", "status": "m", "slot": "a"},
         {"text": "Quantum telepathy", "status": "n", "slot": ""}],
    )
    assert [r["kind"] for r in rows] == ["h", "li", "li", "p"]
    assert rows[1]["req"] == "a" and rows[1]["status"] == "m" and rows[1]["name"] == "Covered"
    assert rows[2]["req"] is None and all(r["req"] is None for r in (rows[0], rows[3]))


# ============================================================================ review fixes
async def test_undo_of_a_swap_on_a_failed_pick_makes_it_a_failed_pick_again():
    ids = await tailor_seed.seed_async(config={"needs_pick": {"m_review": "alt_3"}})
    rid = ids["render"]
    with _client() as c:
        r = c.post(f"/renders/{rid}/swap", data={"slot_key": "m_review", "variant_key": "r2"})
        assert "needs_pick" not in await _config(rid)
        assert c.post(_toast(r)["undoUrl"]).text.count('data-ok="1"') == 1
        assert (await _config(rid))["needs_pick"] == {"m_review": "alt_3"}
        soup = _soup(c.get(f"/renders/{rid}").text)
    assert soup.select_one('[data-testid="tl-download"]')["data-needs"] == "1"


async def test_undo_of_a_summary_swap_restores_the_wording_as_well_as_the_hide():
    ids = await tailor_seed.seed_async(config={"hide_summary": True, "summary": "sum_a"})
    rid = ids["render"]
    with _client() as c:
        r = c.post(f"/renders/{rid}/swap", data={"slot_key": "summary", "variant_key": "sum_b"})
        assert c.post(_toast(r)["undoUrl"]).text.count('data-ok="1"') == 1
    cfg = await _config(rid)
    assert cfg["hide_summary"] is True and cfg["summary"] == "sum_a"


async def test_undo_of_an_attach_anyway_forgets_the_recorded_left_unresolved(tmp_path):
    ids = await tailor_seed.seed_async(config={"needs_pick": {"m_review": "alt_3"}})
    rid = ids["render"]
    with _client() as c:
        r = c.post(f"/renders/{rid}/attach", data={"anyway": "1"})
        assert (await _config(rid))["left_unresolved"] == ["m_review"]
        assert c.post(_toast(r)["undoUrl"]).text.count('data-ok="1"') == 1
    assert "left_unresolved" not in await _config(rid)


async def test_the_posting_link_only_ever_points_at_http_or_https():
    ids = await tailor_seed.seed_async()
    async with _sm()() as s:
        from kestrel.db.models import Opportunity

        o = await s.get(Opportunity, ids["opp"])
        o.url = "javascript:alert(1)"
        await s.commit()
    with _client() as c:
        page = c.get(f"/renders/{ids['render']}").text
    assert "javascript:" not in page
    assert _soup(page).select_one('[data-testid="tl-open-posting"]') is None


async def test_the_needs_list_names_lines_the_way_the_cards_do():
    ids = await tailor_seed.seed_async(config={"needs_pick": {"m_review": "alt_3"}})
    with _client() as c:
        soup = _soup(c.get(f"/renders/{ids['render']}").text)
    item = soup.select_one('[data-need="m_review"]').get_text(" ", strip=True)
    assert item.startswith("Line 3") and "m review" not in item


async def test_the_dialog_reads_the_employer_state_of_this_document(monkeypatch):
    ids = await tailor_seed.seed_async()
    async with _sm()() as s:
        render = await s.get(DocRender, ids["render"])
        from kestrel.config import get_settings

        st = get_settings().model_copy(deep=True)
        assert await page_mod._employer_for(s, st, render.doc_master_id) == "off"
        st.confidential_mode = True
        assert await page_mod._employer_for(s, st, render.doc_master_id) == "hidden"


async def test_attach_undo_preserves_preexisting_unresolved_record():
    ids = await tailor_seed.seed_async(config={"left_unresolved": ["earlier"]})
    rid = ids["render"]
    with _client() as c:
        r = c.post(f"/renders/{rid}/attach")
        assert 'data-ok="1"' in c.post(_toast(r)["undoUrl"]).text
    assert (await _config(rid))["left_unresolved"] == ["earlier"]
