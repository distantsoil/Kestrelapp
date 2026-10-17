"""Phase 13 web-UI gate: the filter preview returns correct counts, a slot swap on /renders writes a
selection_override, confidential mode redacts in a list view AND in the rendered document, and a
Playwright sweep loads every page with zero console errors (including a mobile viewport). The
Playwright tests skip cleanly where Edge is unavailable; the rest are offline."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.base import utcnow_iso
from kestrel.db.engine import make_async_engine, reset_engine
from kestrel.db.models import (
    DocMaster,
    DocRender,
    DocSection,
    DocSlot,
    DocVariant,
    Opportunity,
    Org,
    SelectionOverride,
    Setting,
    Source,
)
from tests.browser import launch_sync

_PAGES = ["/", "/opportunities", "/pursuits", "/documents", "/stories", "/offers", "/sources",
          "/settings", "/analytics", "/bench", "/about", "/calibrate", "/runs"]


def _wipe_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)


_last_engine = None


@pytest.fixture(autouse=True)
def _dispose_engines():
    """Dispose this module's ad-hoc engine and the app engine after every test, so the sqlite file
    is not left locked for the next web test on Windows."""
    yield
    global _last_engine
    if _last_engine is not None:
        asyncio.run(_last_engine.dispose())
        _last_engine = None
    asyncio.run(reset_engine())


async def _fresh_sm():
    # Dispose any ad-hoc engine from a prior test first: on Windows the sqlite file stays locked
    # until its engine is disposed, so _wipe_db would otherwise hit a PermissionError.
    global _last_engine
    if _last_engine is not None:
        await _last_engine.dispose()
    await reset_engine()
    _wipe_db()
    migrate.run_migrations()
    _last_engine = make_async_engine(paths.db_path())
    return async_sessionmaker(_last_engine, expire_on_commit=False)


def _emp_opp(org, src, i, **kw) -> Opportunity:
    base = dict(
        track="employment", opp_type="job_posting", org_id=org.id, source_id=src.id,
        title="VP of IT", org_name=org.name, url=f"https://x/{i}", stage="t2_done",
        first_seen_at=utcnow_iso(),
    )
    base.update(kw)
    return Opportunity(**base)


# ---------------------------------------------------------------- gate 2: filter preview counts
async def _run_filter_preview():
    from kestrel.config import get_settings
    from kestrel.web.pages.settings import _filter_preview

    sm = await _fresh_sm()
    async with sm() as s:
        org = Org(name="Acme", normalized_name="acme")
        src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
        s.add_all([org, src])
        await s.flush()
        # One posting requires an active clearance (trips the `clearance` hard blocker); one benign.
        s.add(_emp_opp(org, src, 1,
                      description_md="Requires active Top Secret clearance to start."))
        s.add(_emp_opp(org, src, 2, description_md="A normal IT leadership role, no clearance."))
        await s.commit()
        preview = await _filter_preview(s, get_settings())
    await reset_engine()
    return preview


def test_filter_preview_returns_correct_counts():
    preview = asyncio.run(_run_filter_preview())
    emp = {r["id"]: r for r in preview["employment"]}
    assert "clearance" in emp
    # Exactly one of the two seeded opportunities requires a clearance.
    assert emp["clearance"]["rejects"] == 1
    assert emp["clearance"]["of_n"] == 2
    # A benign rule fires on neither.
    assert emp["clearance"]["rejects"] <= emp["clearance"]["of_n"]


# ---------------------------------------------------- gate 4b: confidential redaction in a render
async def _seed_doc(sm, *, redact_label: str) -> int:
    async with sm() as s:
        master = DocMaster(track="employment", name="Resume", doc_kind="resume",
                           header={"name": "Jane Rivera"}, summary_variants=[], skill_groups=[],
                           is_active=True)
        s.add(master)
        await s.flush()
        sec = DocSection(doc_master_id=master.id, section_kind="role", org_label="Acme Corp",
                         org_label_redacted=redact_label, title="VP of IT", sort_order=0)
        s.add(sec)
        await s.flush()
        slot = DocSlot(doc_section_id=sec.id, key="s1", sort_order=0, optional=False,
                       default_variant_key="a")
        s.add(slot)
        await s.flush()
        s.add(DocVariant(doc_slot_id=slot.id, key="a", text="Led the IT function.", tags=[],
                         derived=False))
        s.add(DocVariant(doc_slot_id=slot.id, key="b", text="Owned technology strategy.", tags=[],
                         derived=False))
        await s.commit()
        return master.id


async def _run_confidential_render():
    from kestrel.docs.catalog import load_catalog, resolve
    from kestrel.docs.render import render_html

    sm = await _fresh_sm()
    master_id = await _seed_doc(sm, redact_label="[Confidential]")
    async with sm() as s:
        catalog = await load_catalog(s, master_id)
    plain = render_html(resolve(catalog, {"redact_orgs": False}))
    redacted = render_html(resolve(catalog, {"redact_orgs": True}))
    await reset_engine()
    return plain, redacted


def test_confidential_mode_redacts_in_a_rendered_document():
    plain, redacted = asyncio.run(_run_confidential_render())
    assert "Acme Corp" in plain  # normally the real employer is shown
    assert "Acme Corp" not in redacted  # confidential render swaps it
    assert "[Confidential]" in redacted


# ------------------------- gate 3 + 4a: selection_override written; confidential list redaction
def _server_seed():
    async def _seed():
        sm = await _fresh_sm()
        # A doc render with a chosen variant to swap, plus a confidential employer + a posting.
        master_id = await _seed_doc(sm, redact_label="[Confidential]")
        async with sm() as s:
            slot = (await s.execute(select(DocSlot))).scalars().first()
            render = DocRender(opportunity_id=None, doc_master_id=master_id,
                               config={"slots": {slot.key: "a"}}, rationale={slot.key: "best fit"},
                               template="classic", filename="r.pdf", page_count=1)
            s.add(render)
            org = Org(name="Acme Corp", normalized_name="acme corp")
            src = Source(track="employment", kind="greenhouse", name="b", enabled=True)
            s.add_all([org, src])
            await s.flush()
            opp = _emp_opp(org, src, 1, title="VP IT", org_name="Acme Corp")
            s.add(opp)
            s.add(Setting(key="confidential_mode", value=True))
            await s.commit()
            return render.id, slot.key, opp.id
    return asyncio.run(_seed())


def test_selection_override_and_confidential_list(monkeypatch):
    from starlette.testclient import TestClient

    from kestrel.web.app import build_app

    render_id, slot_key, opp_id = _server_seed()
    asyncio.run(reset_engine())  # let the app build its engine on the request loop
    client = TestClient(build_app(worker=False))

    # gate 3: swapping a slot writes a selection_override row.
    r = client.post(f"/renders/{render_id}/swap",
                    data={"slot_key": slot_key, "variant_key": "b"})
    assert r.status_code < 400

    async def _count_overrides():
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            n = (await s.execute(select(func.count()).select_from(SelectionOverride))).scalar_one()
            row = (await s.execute(select(SelectionOverride))).scalars().first()
        await engine.dispose()
        return n, row

    asyncio.run(reset_engine())
    n, row = asyncio.run(_count_overrides())
    assert n == 1
    assert row.slot_key == slot_key
    assert row.model_choice == "a" and row.operator_choice == "b"

    # gate 4a: confidential mode redacts the employer in the opportunities list AND on the detail
    # page (confidential is "everywhere", not just the list — reviewer finding 3).
    client = TestClient(build_app(worker=False))
    html = client.get("/opportunities").text
    assert "[Confidential]" in html
    assert "Acme Corp" not in html
    detail = client.get(f"/opportunities/{opp_id}").text
    assert "Acme Corp" not in detail


def test_swap_refuses_a_variant_key_the_slot_does_not_have():
    """The swap route wrote whatever key the form carried; resolve() then fell back to the slot
    default while the config claimed otherwise, and the review page showed the fallback silently.
    An unknown key is a 400 with nothing written: no override row, config unchanged (rule 2)."""
    from starlette.testclient import TestClient

    from kestrel.web.app import build_app

    render_id, slot_key, _opp_id = _server_seed()
    asyncio.run(reset_engine())
    client = TestClient(build_app(worker=False))

    r = client.post(f"/renders/{render_id}/swap",
                    data={"slot_key": slot_key, "variant_key": "MADE_UP"})
    assert r.status_code == 400
    assert "MADE_UP" in r.text and slot_key in r.text  # the refusal names the slot and the key
    # ("hide" is allowed on any part for the operator, optional or not: tests/test_stream3_*.)
    # An unknown slot is refused too.
    r = client.post(f"/renders/{render_id}/swap", data={"slot_key": "ghost", "variant_key": "a"})
    assert r.status_code == 400

    async def _state():
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            n = (await s.execute(select(func.count()).select_from(SelectionOverride))).scalar_one()
            render = await s.get(DocRender, render_id)
        await engine.dispose()
        return n, render.config

    asyncio.run(reset_engine())
    n, config = asyncio.run(_state())
    assert n == 0  # nothing written on a refusal
    assert config["slots"][slot_key] == "a"  # config untouched


def test_review_page_names_an_unresolved_key_instead_of_showing_the_fallback_silently():
    """A render whose stored config names a key the catalog no longer has (a variant deleted since,
    a hand-edited config): the preview falls back to the slot default, and the page must SAY so,
    naming the slot and the key, rather than show a different bullet than the config claims."""
    from starlette.testclient import TestClient

    from kestrel.web.app import build_app

    render_id, slot_key, _opp_id = _server_seed()

    async def _corrupt():
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            render = await s.get(DocRender, render_id)
            render.config = {"slots": {slot_key: "GONE"}, "summary": "NO_SUMMARY"}
            await s.commit()
        await engine.dispose()

    asyncio.run(_corrupt())
    asyncio.run(reset_engine())
    client = TestClient(build_app(worker=False))
    html = client.get(f"/renders/{render_id}").text
    assert 'data-testid="unresolved-keys"' in html
    assert "GONE" in html and slot_key in html  # the slot miss is named
    assert "NO_SUMMARY" in html  # the summary miss is named too
    assert "Led the IT function." in html  # the default is what the preview shows, visibly flagged

    # Swapping to a real key clears the slot warning.
    r = client.post(f"/renders/{render_id}/swap", data={"slot_key": slot_key, "variant_key": "b"})
    assert r.status_code == 200
    assert "GONE" not in r.text
    assert "NO_SUMMARY" in r.text  # the summary miss remains until the config is fixed


# ------------------------------------- gate 1 + 5: Playwright sweep (zero console errors) + mobile
def test_playwright_sweep_all_pages_no_console_errors(monkeypatch):
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    # /analytics calls honest_summary, which makes a REAL generation against local Ollama. That is
    # the only live model call in the default suite, and it made this test the suite's one source
    # of non-determinism: ~35s to render against Playwright's 30s networkidle budget when the model
    # is cold, passing only when it happens to be warm. The page's rendering is what is under test
    # here, not the model, so the call is stubbed. Note the route swallows exceptions and falls back
    # to metrics-only, so raising here still exercises the real fallback path the operator sees
    # whenever the model is unavailable.
    async def _no_model(*args, **kwargs):
        raise RuntimeError("model call stubbed out in the page-render sweep")

    monkeypatch.setattr("kestrel.analytics.summary.honest_summary", _no_model, raising=False)
    monkeypatch.setattr("kestrel.web.pages.analytics.honest_summary", _no_model, raising=False)

    asyncio.run(_fresh_sm_and_seed())
    asyncio.run(reset_engine())
    with running_server(worker=False) as base:
        with sync_playwright() as p:
            browser = launch_sync(p)
            try:
                for viewport, pages in ((None, _PAGES),
                                        ({"width": 390, "height": 844},
                                         ["/opportunities", "/pursuits"])):
                    page = browser.new_page(viewport=viewport) if viewport else browser.new_page()
                    errors: list[str] = []

                    def _console(m, sink=errors):
                        if m.type == "error":
                            sink.append(m.text)

                    page.on("console", _console)
                    page.on("pageerror", lambda e, sink=errors: sink.append(str(e)))
                    for path in pages:
                        page.goto(base + path, wait_until="networkidle")
                    assert not errors, f"console errors (viewport={viewport}): {errors}"
                    page.close()
            finally:
                browser.close()


async def _fresh_sm_and_seed():
    sm = await _fresh_sm()
    async with sm() as s:
        org = Org(name="Acme", normalized_name="acme")
        src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
        s.add_all([org, src])
        await s.flush()
        s.add(_emp_opp(org, src, 1))
        await s.commit()
    await reset_engine()
