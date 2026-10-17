"""Second 0.3.0 feedback round (D-UI-19, D-UI-20, D-SRC-1): the Analytics page never pays for
a model call on load and writes the narrative on request; the shipped sources are seeded at
startup; long actions show an inline status instead of nothing; the Models window picker, the
pipeline picker spacing and the Pursuits board fit the window. Offline: no model, no network."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kestrel import paths
from kestrel.analytics.summary import honest_summary
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
from kestrel.db.models import Source

_TEMPLATES = Path(paths.web_templates_dir())
_CSS = (Path(paths.web_static_dir()) / "css" / "kestrel.css").read_text(encoding="utf-8")
_BASE = (_TEMPLATES / "base.html").read_text(encoding="utf-8")


class _Explode:
    """A client that must never be asked anything."""

    async def complete(self, *args, **kwargs):
        raise AssertionError("the model was called")


def _fresh_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


@pytest.fixture(autouse=True)
def _no_ollama(monkeypatch):
    from kestrel.web import routes_control

    async def _down(*a, **k):
        return None

    monkeypatch.setattr(routes_control, "_probe_ollama", _down, raising=False)


# ------------------------------------------------------------------- analytics (D-UI-19)
async def test_honest_summary_skips_the_model_when_there_is_nothing_to_say(sm):
    async with sm() as s:
        summary = await honest_summary(s, _Explode(), "employment")
    assert summary.insufficient_data is True
    assert summary.narrative_pending is False  # nothing to write: the verdict is the story
    assert "0 recorded outcomes" in summary.summary_md


async def test_honest_summary_with_no_client_offers_the_narrative_only_when_data_allows(sm):
    async with sm() as s:
        summary = await honest_summary(s, None, "employment")
    assert summary.narrative_pending is False
    assert summary.summary_md


def test_analytics_page_never_calls_the_model_and_the_narrative_route_does(monkeypatch):
    seen: list[object] = []

    async def _spy(session, client, track):
        seen.append(client)
        return await honest_summary(session, None, track)

    monkeypatch.setattr("kestrel.web.pages.analytics.honest_summary", _spy)
    asyncio.run(reset_engine())
    _fresh_db()
    forget_engine()
    with _client() as c:
        r = c.get("/analytics")
        assert r.status_code == 200
        assert seen == [None]
        # With no outcomes there is no narrative to offer either.
        assert 'data-testid="write-narrative"' not in r.text
        r = c.post("/analytics/narrative?track=employment")
        assert r.status_code == 200
        assert seen[-1] is not None  # the one place the page asks the model
        assert 'id="honest-summary"' in r.text
    asyncio.run(reset_engine())


def test_narrative_form_shows_its_own_status_and_skips_the_busy_cursor():
    html = (_TEMPLATES / "partials" / "analytics_summary.html").read_text(encoding="utf-8")
    form = html.split('hx-post="/analytics/narrative')[1].split("</form>")[0]
    assert "data-no-busy" in form and 'class="import-status"' in form and 'class="spin"' in form


# ------------------------------------------------------------------- sources (D-SRC-1)
def test_shipped_sources_are_seeded_at_startup_disabled_and_only_once():
    from kestrel.ingest.sources import load_source_specs
    from kestrel.web.app import _seed_shipped_sources

    async def _run():
        await reset_engine()
        _fresh_db()
        forget_engine()
        await _seed_shipped_sources()
        await _seed_shipped_sources()  # a second start creates nothing
        async with get_sessionmaker()() as s:
            rows = (await s.execute(select(Source))).scalars().all()
        await reset_engine()
        return rows

    rows = asyncio.run(_run())
    specs = load_source_specs()
    # Since D-SRC-3 only SAM.gov ships; job searches are generated from the profile.
    assert [sp["kind"] for sp in specs] == ["sam_gov"]
    assert sorted(r.name for r in rows) == sorted(sp["name"] for sp in specs)
    assert not any(r.enabled for r in rows)  # nothing scrapes until the operator turns one on


def test_a_broken_sources_file_does_not_stop_startup(monkeypatch):
    from kestrel.web import app as app_module

    def _bad():
        raise ValueError("sources.yml is not valid")

    monkeypatch.setattr("kestrel.ingest.sources.load_source_specs", _bad)
    asyncio.run(reset_engine())
    _fresh_db()
    forget_engine()
    asyncio.run(app_module._seed_shipped_sources())  # logs and returns
    asyncio.run(reset_engine())


def test_sources_page_lists_the_shipped_sources_after_seeding():
    from kestrel.web.app import _seed_shipped_sources

    async def _run():
        await reset_engine()
        _fresh_db()
        forget_engine()
        await _seed_shipped_sources()
        await reset_engine()
        # SAM.gov is a consulting source; a new install is employment-only, so turn it on.
        from kestrel.db.engine import get_sessionmaker
        from kestrel.services.tracks import BOTH, save_enabled

        async with get_sessionmaker()() as s:
            await save_enabled(s, BOTH)
            await s.commit()
        await reset_engine()

    asyncio.run(_run())
    with _client() as c:
        html = c.get("/sources").text
    assert "SAM.gov federal IT contracts" in html and 'hx-post="/sources/1/toggle"' in html
    asyncio.run(reset_engine())


# ----------------------------------------------------------- inline progress (D-UI-20)
_BUSY_EXPECTED = {
    "bench.html": ["/bench/run"],
    "runs.html": ["/runs/now"],
    "partials/opp_decision.html": ["/escalate", "/research"],
    "partials/mock_body.html": ["/start"],
    "partials/mock_active.html": ["/answer", "/end"],
    "partials/prep_pack.html": ["/build", "/pdf"],
    "partials/offers_list.html": ["/scripts"],
    "partials/render_review.html": ["/swap"],
    "partials/run_now_dialog.html": ["/run-now"],
    "partials/opp_actions.html": ["/research", "/contacts"],
}


def _tag_with_post(text: str, route: str) -> str:
    m = re.search(r"<(?:form|button)\b[^>]*hx-post=\"[^\"]*" + re.escape(route) + r"\"[^>]*>", text)
    assert m, route
    return m.group(0)


@pytest.mark.parametrize("tmpl,routes", sorted(_BUSY_EXPECTED.items()))
def test_every_slow_action_says_what_it_is_doing(tmpl, routes):
    text = (_TEMPLATES / tmpl).read_text(encoding="utf-8")
    for route in routes:
        tag = _tag_with_post(text, route)
        assert 'data-busy-text="' in tag, (tmpl, route)
        label = re.search(r'data-busy-text="([^"]+)"', tag).group(1)
        assert label.strip() and "—" not in label


def test_base_shows_the_busy_text_inline_and_not_as_a_cursor():
    assert "data-busy-text" in _BASE
    assert "import-status busy-status" in _BASE and "className = 'spin'" in _BASE
    # A request from such an element never counts toward the busy cursor.
    assert "closest('[data-no-busy], [data-busy-text]')" in _BASE
    assert ".busy-status" in _CSS


def test_bench_run_disables_its_button_while_running():
    tag = _tag_with_post((_TEMPLATES / "bench.html").read_text(encoding="utf-8"), "/bench/run")
    assert 'hx-disabled-elt="find button"' in tag


# ----------------------------------------------------------------- visual fixes (D-UI-20)
def test_a_selected_ghost_button_keeps_its_primary_background():
    """Models page window picker: `btn ghost primary` rendered as a dark square with no text
    because .btn.ghost (declared later) made the background transparent under the primary
    text color."""
    ghost = _CSS.index(".btn.ghost, .btn.quiet")
    fix = _CSS.index(".btn.ghost.primary")
    assert fix > ghost
    rule = _CSS[fix:].split("}")[0]
    assert "background: var(--action)" in rule and "color: var(--action-fg)" in rule


def test_models_page_marks_the_selected_window():
    asyncio.run(reset_engine())
    _fresh_db()
    forget_engine()
    with _client() as c:
        html = c.get("/models?since=30").text
    assert 'class="btn ghost primary"' in html
    assert html.count('class="btn ghost primary"') == 1
    asyncio.run(reset_engine())


def test_pipeline_picker_has_room_between_the_fields_and_the_chips():
    rule = _CSS[_CSS.index("#pipeline-form {"):].split("}")[0]
    assert "gap:" in rule and "margin-bottom:" in rule
    assert "#pipeline-form + .chips { margin-bottom:" in _CSS


def test_pursuits_board_fits_the_window_instead_of_scrolling_sideways():
    rule = _CSS[_CSS.index(".kanban {"):].split("}")[0]
    assert "overflow-x" not in rule
    assert "repeat(auto-fit" in rule


# ------------------------------------------------------------- keep awake (D-RUNS-2)
def test_keep_awake_holds_system_sleep_but_not_the_display(monkeypatch):
    from kestrel.services import keep_awake as ka

    calls: list[int] = []
    monkeypatch.setattr(ka.sys, "platform", "win32")
    monkeypatch.setattr(ka, "_set_state", lambda flags: calls.append(flags) or 1)
    assert ka.apply(True) is None
    assert ka.apply(False) is None
    assert calls == [ka.ES_CONTINUOUS | ka.ES_SYSTEM_REQUIRED, ka.ES_CONTINUOUS]
    assert not any(f & 0x2 for f in calls)  # ES_DISPLAY_REQUIRED is never set


def test_keep_awake_reports_a_refusal_and_other_platforms_without_raising(monkeypatch):
    from kestrel.services import keep_awake as ka

    monkeypatch.setattr(ka.sys, "platform", "linux")
    assert ka.apply(True) == ka.NOT_WINDOWS
    monkeypatch.setattr(ka.sys, "platform", "win32")
    monkeypatch.setattr(ka, "_set_state", lambda flags: 0)
    assert "refused" in ka.apply(True)

    def _boom(flags):
        raise OSError("no kernel32")

    monkeypatch.setattr(ka, "_set_state", _boom)
    assert "failed" in ka.apply(True)


def test_runs_page_toggle_stores_the_choice_and_startup_applies_it(monkeypatch):
    from kestrel.services import keep_awake as ka
    from kestrel.web import app as app_module

    applied: list[bool] = []
    monkeypatch.setattr(ka, "apply", lambda on: applied.append(on) or None)
    asyncio.run(reset_engine())
    _fresh_db()
    forget_engine()
    with _client() as c:
        html = c.get("/runs").text
        assert 'data-testid="keep-awake"' in html and "screen still turns off" in html
        assert 'name="on" value="on" checked' not in html
        r = c.post("/runs/keep-awake", data={"on": "on"})
        assert r.status_code == 200
        assert 'name="on" value="on" checked' in r.text
        assert "sleep is held off" in r.text
        assert applied == [True]
        assert 'name="on" value="on" checked' in c.get("/runs").text
    # A restart honors the stored choice from the loop thread.
    asyncio.run(reset_engine())
    asyncio.run(app_module._apply_keep_awake())
    assert applied == [True, True]
    asyncio.run(reset_engine())
    with _client() as c:
        r = c.post("/runs/keep-awake", data={})
        assert "Windows sleeps on its own" in r.text
        assert applied[-1] is False
    asyncio.run(reset_engine())


def test_keep_awake_problem_is_shown_not_hidden(monkeypatch):
    from kestrel.services import keep_awake as ka

    monkeypatch.setattr(ka, "apply", lambda on: ka.NOT_WINDOWS)
    asyncio.run(reset_engine())
    _fresh_db()
    forget_engine()
    with _client() as c:
        r = c.post("/runs/keep-awake", data={"on": "on"})
    assert "Saved, but has no effect on this platform" in r.text
    asyncio.run(reset_engine())


def test_applied_import_help_is_collapsed_by_default():
    html = (_TEMPLATES / "partials" / "applied_import.html").read_text(encoding="utf-8")
    tag = html[html.index("<details"):html.index("How to format the file")]
    assert " open" not in tag and "{% if" not in tag


def test_loading_is_visible_without_the_mouse():
    """The busy cursor shows only with the pointer over the window; a page bar always does."""
    rule = _CSS[_CSS.index("body.busy::before {"):].split("}")[0]
    assert "position: fixed" in rule and "animation: busybar" in rule
    assert "@keyframes busybar" in _CSS


def test_filters_tab_points_at_the_profile_not_the_yaml_and_lan_says_why():
    settings = (_TEMPLATES / "settings.html").read_text(encoding="utf-8")
    assert "come from your profile and the Core tab, not from a file" in settings
    assert "<b>What is this?</b>" in settings and 'href="/help#lan"' in settings
    help_ = (_TEMPLATES / "help.html").read_text(encoding="utf-8")
    assert 'id="lan"' in help_ and "same home network" in help_


def test_the_page_fills_the_window_width():
    rule = _CSS[_CSS.index(".container {"):].split("}")[0]
    assert "max-width" not in rule and "width: 100%" in rule


def test_with_enough_data_and_no_client_the_narrative_is_offered(monkeypatch):
    from kestrel.analytics import summary as summary_module
    from kestrel.analytics.summary import AnalyticsSummary

    async def _metrics(session, track):
        return {"score_vs_outcome": {"n_outcomes": 30, "discriminating": True, "headline": "x",
                                     "by_score_band": []}, "track": track}

    monkeypatch.setattr(summary_module, "collect_metrics", _metrics)

    async def _run():
        return await summary_module.honest_summary(None, None, "employment")

    s = asyncio.run(_run())
    assert s.narrative_pending is True and s.insufficient_data is False

    from kestrel.analytics.metrics import collect_metrics as real_metrics

    async def _pending(session, client, track):
        return AnalyticsSummary(track=track, metrics=await real_metrics(session, track),
                                discriminating=True, insufficient_data=False,
                                summary_md="verdict", headline_finding="verdict",
                                narrative_pending=client is None)

    monkeypatch.setattr("kestrel.web.pages.analytics.honest_summary", _pending)
    asyncio.run(reset_engine())
    _fresh_db()
    forget_engine()
    with _client() as c:
        page = c.get("/analytics").text
        assert 'data-testid="write-narrative"' in page
        after = c.post("/analytics/narrative?track=employment").text
        assert 'data-testid="write-narrative"' not in after
    asyncio.run(reset_engine())


def test_shipped_layouts_are_seeded_at_startup_and_the_preview_is_a_frame():
    from sqlalchemy import select as _select

    from kestrel.db.models import DocLayout
    from kestrel.web.app import _seed_shipped_layouts

    async def _run():
        await reset_engine()
        _fresh_db()
        forget_engine()
        await _seed_shipped_layouts()
        await _seed_shipped_layouts()
        async with get_sessionmaker()() as s:
            keys = [r.key for r in (await s.execute(_select(DocLayout))).scalars().all()]
        await reset_engine()
        return keys

    keys = asyncio.run(_run())
    assert "executive" in keys and len(keys) == len(set(keys)) >= 5
    body = (_TEMPLATES / "partials" / "documents_body.html").read_text(encoding="utf-8")
    # The preview and the test render both carry the layout, font, alignment (D-DOC-1) and the
    # accent color (D-DOC-4).
    assert 'hx-include="#layout, #font, #header_align, #accent, #accent_custom"' in body
    assert "['layout', 'font', 'header_align', 'accent', 'accent_custom']" in body
    assert "'&' + id + '='" in body
    side = _CSS[_CSS.index(".doc-side {"):].split("}")[0]
    assert ".preview-doc" in _CSS and "overflow-y: auto" in side
