"""Plain-language pages (D-UI-47): Interview prep, My stories, Failed scoring, and the Tailor
resume review (intro, per-part Swap and Hide, a Back link that honors `from`). Offline: a
migrated SQLite file and the test client."""

from __future__ import annotations

import asyncio

from sqlalchemy import select
from starlette.testclient import TestClient

from kestrel import paths
from kestrel.db.engine import make_async_engine, reset_engine
from kestrel.db.models import DocRender, DocSection, DocSlot, DocVariant, SelectionOverride
from tests.test_web_phase13 import (  # noqa: F401  (the autouse fixture must be importable)
    _dispose_engines,
    _fresh_sm,
    _seed_doc,
)


def _seed_render() -> tuple[int, str]:
    async def _go():
        sm = await _fresh_sm()
        master_id = await _seed_doc(sm, redact_label="[Confidential]")
        async with sm() as s:
            sec = (await s.execute(select(DocSection))).scalars().first()
            opt = DocSlot(doc_section_id=sec.id, key="side_project", sort_order=1, optional=True,
                          default_variant_key="p1")
            s.add(opt)
            await s.flush()
            s.add(DocVariant(doc_slot_id=opt.id, key="p1", text="Built a side tool.", tags=[],
                             derived=False))
            slot = (await s.execute(select(DocSlot).where(DocSlot.key == "s1"))).scalars().one()
            render = DocRender(
                doc_master_id=master_id, opportunity_id=None, template="classic",
                config={"slots": {"s1": "a", "side_project": "p1"}}, rationale={},
                filename="r.pdf", page_count=1,
            )
            s.add(render)
            await s.commit()
            return render.id, slot.key

    return asyncio.run(_go())


def _client() -> TestClient:
    from kestrel.web.app import build_app

    asyncio.run(reset_engine())
    return TestClient(build_app(worker=False))


def test_tailor_page_shows_cards_with_the_other_wordings_and_no_free_editing():
    render_id, _ = _seed_render()
    html = _client().get(f"/renders/{render_id}").text
    # The proof strip says whose words these are; the other wordings are the operator's own.
    assert "Every line is your own text, word for word." in html
    assert "Other wordings (1)" in html and "Owned technology strategy." in html
    assert "Leave this line out" in html  # the optional slot only
    assert html.count("Leave this line out") == 1
    # Edits happen in Profile > Documents: no free editing, no Hide button, no AI-written summary.
    assert "Edit wording" not in html and ">Hide</button>" not in html
    assert "AI suggestion" not in html


def test_hide_this_part_goes_through_the_override_path():
    render_id, _ = _seed_render()
    c = _client()
    r = c.post(f"/renders/{render_id}/swap", data={"slot_key": "side_project",
                                                  "variant_key": "hide"})
    assert r.status_code == 200 and 'id="tl-card-side_project"' in r.text
    assert "Not on this resume." in r.text and "Left out" in r.text

    async def _rows():
        engine = make_async_engine(paths.db_path())
        try:
            from sqlalchemy.ext.asyncio import async_sessionmaker

            async with async_sessionmaker(engine)() as s:
                return (await s.execute(select(SelectionOverride))).scalars().all()
        finally:
            await engine.dispose()

    (row,) = asyncio.run(_rows())
    assert (row.slot_key, row.model_choice, row.operator_choice) == ("side_project", "p1", "hide")


def test_tailor_back_link_honors_from_and_ignores_anything_else():
    render_id, _ = _seed_render()
    c = _client()
    base = c.get(f"/renders/{render_id}").text
    assert 'href="/documents?master_id=' in base and "Back to Documents" in base
    role = c.get(f"/renders/{render_id}?from=/opportunities/12").text
    assert 'href="/opportunities/12" data-testid="render-back"' in role
    apps = c.get(f"/renders/{render_id}?from=/pursuits").text
    assert 'href="/pursuits" data-testid="render-back"' in apps and "Back to Applications" in apps
    for bad in ("//evil.example/x", "https://evil.example", "/settings"):
        assert "Back to Documents" in c.get(f"/renders/{render_id}", params={"from": bad}).text


def test_renamed_pages_use_the_plain_names():
    asyncio.run(_fresh_sm())
    c = _client()
    stories = " ".join(c.get("/stories").text.split())
    assert "<h1>Interview prep</h1>" in stories and "My stories" not in stories
    assert "A draft is yours once you save it; nothing rewrites it after that." in stories
    prep = c.get("/prep/999").text
    # D-UI-221: the tabs stay even for a role that is gone; the page says so under them.
    assert "<h1>Interview prep</h1>" in prep and 'data-testid="pp-tabs"' in prep
    quarantine = " ".join(c.get("/quarantine").text.split())
    assert "<h1>Needs another try</h1>" in quarantine
    assert "Some roles couldn’t be scored. Nothing was dropped; they wait here." in quarantine
    assert "schema" not in quarantine.split("<main")[-1].lower()
