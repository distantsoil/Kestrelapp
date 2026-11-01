"""Issue #73: delete a whole imported document (resume or capability statement), even the last
one. Children and unattached renders go in one transaction; a document an application used is
kept with a plain explanation; readiness returns to "Add your resume"."""

from __future__ import annotations

import asyncio

from bs4 import BeautifulSoup
from sqlalchemy import func, select

from kestrel import paths
from kestrel.db.models import (
    DocMaster,
    DocRender,
    DocSection,
    DocSlot,
    DocVariant,
    Opportunity,
    Pursuit,
    SelectionOverride,
)
from tests.test_documents_editor import _app


def _run(coro_fn):
    from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine

    async def go():
        await reset_engine()
        async with get_sessionmaker()() as s:
            out = await coro_fn(s)
        await reset_engine()
        return out

    r = asyncio.run(go())
    forget_engine()
    return r


def _count(model):
    async def f(s):
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()
    return _run(f)


def _add_render(master_id: int, *, pursuit: str | None = None) -> tuple[int, str]:
    docs = paths.data_dir() / "documents"
    docs.mkdir(parents=True, exist_ok=True)

    async def f(s):
        opp = Opportunity(track="employment", opp_type="job_posting", title="CIO",
                          org_name="Acme", url="https://example.test/x", stage="t2_done",
                          description_md="x")
        s.add(opp)
        await s.flush()
        r = DocRender(opportunity_id=opp.id, doc_master_id=master_id, config={})
        s.add(r)
        await s.flush()
        pdf = docs / f"{r.id}_x.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        r.pdf_path = str(pdf)
        s.add(SelectionOverride(doc_render_id=r.id, opportunity_id=opp.id, slot_key="k"))
        if pursuit:
            s.add(Pursuit(opportunity_id=opp.id, track="employment", status="applied",
                          **{pursuit: r.id}))
        await s.commit()
        return r.id, str(pdf)
    return _run(f)


def _add_choices(render_id: int, opp_id: int | None, n: int) -> None:
    async def f(s):
        for i in range(n):
            s.add(SelectionOverride(doc_render_id=render_id, opportunity_id=opp_id,
                                    slot_key=f"extra{i}", model_choice="a", operator_choice="b",
                                    model_choice_text="Model line", operator_choice_text="My line"))
        await s.commit()
    _run(f)


def _overrides_now():
    async def f(s):
        return [(o.doc_render_id, o.opportunity_id, o.slot_key, o.operator_choice_text)
                for o in (await s.execute(select(SelectionOverride)
                                          .order_by(SelectionOverride.id))).scalars()]
    return _run(f)


def _panel(client):
    soup = BeautifulSoup(client.get("/documents").text, "html.parser")
    return soup, soup.select_one('[data-testid="doc-delete-dialog"]')


def test_delete_opens_a_confirm_panel_that_names_what_goes_and_what_is_kept(monkeypatch):
    client, mid = _app(monkeypatch)
    rid, _pdf = _add_render(mid)
    _add_choices(rid, None, 2)  # 3 learned choices in all
    soup, panel = _panel(client)
    btn = soup.select_one('[data-testid="doc-delete-btn"]')
    assert btn is not None and btn["data-k-modal"] == "#doc-delete-dialog"
    assert "hx-confirm" not in btn.attrs and "hx-post" not in btn.attrs
    # D-UI-230: a small danger button in the header row beside "Import a resume", not bare text.
    assert btn.name == "button" and {"k-btn", "k-btn--sm", "k-btn--danger"} <= set(btn["class"])
    row = btn.find_parent(class_="pf-drow")
    assert row is not None and row.select_one('[data-testid="pf-import-jump"]') is not None
    assert btn.find_parent(class_="pf-dmain") is None
    # The shared modal pattern (focus trap, scrim, motion, reduced motion all come with it).
    assert panel is not None and "k-modal" in panel["class"] and panel["role"] == "dialog"
    assert panel["aria-modal"] == "true" and panel.select_one("[data-k-close]") is not None
    form = panel.select_one("form")
    assert form["hx-post"] == f"/documents/{mid}/delete"
    what = panel.select_one('[data-testid="doc-delete-what"]').get_text(" ", strip=True)
    assert "1 section, 1 wording and 1 saved render" in what
    assert "Proof points you already accepted into your profile stay." in what
    assert "\u2014" not in panel.get_text()
    box = panel.select_one('[data-testid="doc-forget-choices"]')
    assert box is not None and box["type"] == "checkbox" and box["name"] == "forget_choices"
    assert "checked" not in box.attrs  # unticked by default
    text = " ".join(panel.get_text().split())
    assert "Also forget the 3 choices Kestrel learned from this resume" in text
    assert ("Kestrel uses these choices to pick wordings for new roles. "
            "Forgetting them cannot be undone.") in text


def test_the_forget_option_is_hidden_when_there_is_nothing_learned(monkeypatch):
    client, mid = _app(monkeypatch)  # no renders, so no choices
    _soup, panel = _panel(client)
    assert panel is not None
    assert panel.select_one('[data-testid="doc-forget-choices"]') is None
    assert "Also forget" not in panel.get_text()
    assert "Kestrel uses these choices" not in panel.get_text()


def test_one_learned_choice_reads_in_the_singular(monkeypatch):
    client, mid = _app(monkeypatch)
    _add_render(mid)
    _soup, panel = _panel(client)
    assert "Also forget the 1 choice Kestrel learned from this resume" in " ".join(
        panel.get_text().split())


def test_delete_keeps_the_learned_choices_by_default(monkeypatch):
    client, mid = _app(monkeypatch, two_slots=True)
    rid, pdf = _add_render(mid)
    _add_choices(rid, None, 1)
    before = _overrides_now()
    assert len(before) == 2
    r = client.post(f"/documents/{mid}/delete")
    assert r.status_code == 200 and r.headers["HX-Redirect"] == "/documents?choices_kept=2"
    for model in (DocMaster, DocSection, DocSlot, DocVariant, DocRender):
        assert _count(model) == 0, model
    assert not __import__("pathlib").Path(pdf).exists()
    after = _overrides_now()
    assert [(a[1], a[2], a[3]) for a in after] == [(b[1], b[2], b[3]) for b in before]
    assert all(a[0] is None for a in after)  # detached from the render that went
    assert after[1][3] == "My line"  # the wording the choice stood for survives the resume
    assert "No documents yet" in client.get("/documents").text


def test_a_detached_choice_is_still_a_learning_signal(monkeypatch):
    client, mid = _app(monkeypatch)
    rid, _pdf = _add_render(mid)
    _add_choices(rid, None, 1)
    client.post(f"/documents/{mid}/delete")
    from kestrel.learning.capture import record_selection_override

    async def f(s):
        await record_selection_override(s, doc_render_id=None, slot_key="k", model_choice="a",
                                        operator_choice="b", opportunity_id=None,
                                        operator_choice_text="Own words")
        return (await s.execute(select(SelectionOverride.slot_key, SelectionOverride.doc_render_id,
                                       SelectionOverride.operator_choice_text)
                                .order_by(SelectionOverride.id))).all()
    rows = _run(f)
    assert [r[1] for r in rows] == [None, None, None] and rows[-1][2] == "Own words"


def test_ticking_forget_deletes_the_choices_too(monkeypatch):
    client, mid = _app(monkeypatch)
    rid, _pdf = _add_render(mid)
    _add_choices(rid, None, 2)
    r = client.post(f"/documents/{mid}/delete", data={"forget_choices": "1"})
    assert r.headers["HX-Redirect"] == "/documents?choices_forgot=3"
    assert _count(SelectionOverride) == 0 and _count(DocMaster) == 0


def test_the_result_states_the_number_actually_forgotten_not_the_one_the_panel_showed(
        monkeypatch):
    client, mid = _app(monkeypatch)
    rid, _pdf = _add_render(mid)
    _, panel = _panel(client)  # the panel was drawn with 1 learned choice
    assert "Also forget the 1 choice " in " ".join(panel.get_text().split())
    _add_choices(rid, None, 2)  # two more are learned before the person confirms
    r = client.post(f"/documents/{mid}/delete", data={"forget_choices": "1"})
    assert r.headers["HX-Redirect"] == "/documents?choices_forgot=3"
    page = BeautifulSoup(client.get(r.headers["HX-Redirect"]).text, "html.parser")
    assert page.select_one('[data-testid="doc-delete-result"]').get_text(strip=True) \
        == "Forgot 3 choices."


def test_the_kept_result_names_the_count_and_a_forged_one_shows_nothing(monkeypatch):
    client, mid = _app(monkeypatch)
    rid, _pdf = _add_render(mid)
    r = client.post(f"/documents/{mid}/delete")
    assert r.headers["HX-Redirect"] == "/documents?choices_kept=1"
    soup = BeautifulSoup(client.get("/documents?choices_kept=1").text, "html.parser")
    assert soup.select_one('[data-testid="doc-delete-result"]').get_text(strip=True) \
        == "Kept 1 choice Kestrel learned from."
    for bad in ("0", "x", "<b>", "99999999"):
        soup = BeautifulSoup(client.get(f"/documents?choices_forgot={bad}").text, "html.parser")
        assert soup.select_one('[data-testid="doc-delete-result"]') is None


async def _other_row(s):
    s.add(SelectionOverride(doc_render_id=None, slot_key="unrelated"))
    await s.commit()


def test_only_the_deleted_resumes_choices_go_when_forgetting(monkeypatch):
    client, mid = _app(monkeypatch)
    rid, _pdf = _add_render(mid)
    _add_choices(rid, None, 1)
    _run(_other_row)
    client.post(f"/documents/{mid}/delete", data={"forget_choices": "1"})
    assert [o[2] for o in _overrides_now()] == ["unrelated"]


def test_a_forged_forget_option_with_nothing_learned_is_harmless(monkeypatch):
    client, mid = _app(monkeypatch)
    r = client.post(f"/documents/{mid}/delete", data={"forget_choices": "1"})
    assert r.headers["HX-Redirect"] == "/documents"
    assert _count(DocMaster) == 0 and _count(SelectionOverride) == 0
    client2, mid2 = _app(monkeypatch)
    r = client2.post(f"/documents/{mid2}/delete", data={"forget_choices": "junk"})
    assert r.headers["HX-Redirect"] == "/documents"


def test_the_panel_rides_the_shared_modal_which_respects_reduced_motion():
    css = (__import__("pathlib").Path(__file__).parents[1]
           / "kestrel/web/static/css/components.css").read_text()
    reduce = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    assert 'transition: none !important' in reduce and "*, *::before" in reduce


def test_a_document_an_application_used_is_kept(monkeypatch):
    for col in ("document_id", "cover_letter_id"):
        client, mid = _app(monkeypatch)
        rid, pdf = _add_render(mid, pursuit=col)
        r = client.post(f"/documents/{mid}/delete")
        assert "HX-Redirect" not in r.headers
        assert "attached to 1 application" in r.text and "Nothing was changed" in r.text
        assert _count(DocMaster) == 1 and _count(DocRender) == 1 and _count(DocSection) == 1
        assert [o[0] for o in _overrides_now()] == [rid]  # still attached
        assert __import__("pathlib").Path(pdf).exists()
        html = client.get("/documents").text
        assert 'data-testid="doc-delete-btn"' not in html
        assert 'data-testid="doc-delete-dialog"' not in html
        assert 'data-testid="doc-delete-blocked"' in html


def test_forged_and_missing_ids_get_a_plain_message(monkeypatch):
    client, mid = _app(monkeypatch)
    for bad in ("9999", "abc", "-1", "1e3"):
        r = client.post(f"/documents/{bad}/delete")
        assert r.status_code == 200 and "already gone" in r.text, bad
        assert "HX-Redirect" not in r.headers
        assert "Traceback" not in r.text
    assert _count(DocMaster) == 1


def test_a_failure_shows_a_fixed_sentence_and_changes_nothing(monkeypatch):
    client, mid = _app(monkeypatch)
    _add_render(mid)

    async def boom(*a, **k):
        raise RuntimeError("secret /home/x/path")

    monkeypatch.setattr("kestrel.web.pages.documents._pursuits_using", boom)
    r = client.post(f"/documents/{mid}/delete")
    assert "secret" not in r.text and "/home" not in r.text
    assert "Nothing was deleted" in r.text
    assert _count(DocMaster) == 1


def test_readiness_returns_to_add_your_resume(monkeypatch):
    client, mid = _app(monkeypatch)
    from kestrel.services import profile_readiness as pr

    async def ready(s):
        return await pr.load_readiness(s, "employment")

    assert _run(ready).has_resume is True
    client.post(f"/documents/{mid}/delete")
    after = _run(ready)
    assert after.has_resume is False
    assert after.link_label == "Add your resume"
