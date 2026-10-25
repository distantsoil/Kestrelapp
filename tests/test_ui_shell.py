"""The web shell (docs/UI_PROPOSAL.html, Screen 1): the sidebar groups and links, the active
state, the server-rendered color scheme, the status rail, the Appearance route, the page sweep,
and a stylesheet coverage check so no template class ever silently loses its rule. Offline:
the Ollama probe is stubbed, nothing calls a model or a network."""

from __future__ import annotations

import asyncio
import html
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import Evaluation, Opportunity, Org, Setting, Source
from kestrel.web import deps
from kestrel.web.deps import BOTTOM_BAR, NAV_GROUPS, nav_active, nav_title

_PAGES = ["/", "/opportunities", "/pursuits", "/documents", "/profile", "/stories", "/offers",
          "/sources", "/settings", "/analytics", "/bench", "/about", "/calibrate", "/runs",
          "/models", "/help", "/quarantine", "/backup"]

_TEMPLATES = Path(paths.web_templates_dir())
_CSS = Path(paths.web_static_dir()) / "css" / "kestrel.css"
_COMPONENTS_CSS = Path(paths.web_static_dir()) / "css" / "components.css"

# Classes that templates set but that are state hooks for HTMX or Alpine, or that a page sets
# only for a test selector, never styling targets in their own right.
_CSS_SKIP = {"htmx-indicator", "htmx-request", "htmx-added", "htmx-settling", "htmx-swapping"}


def _fresh_engine():
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    return make_async_engine(paths.db_path())


async def _seed(rows: list | None = None) -> None:
    await reset_engine()
    engine = _fresh_engine()
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        for row in rows or []:
            s.add(row)
        await s.commit()
    await engine.dispose()
    forget_engine()
    deps.set_ui_scheme_cache(None)  # the file was replaced under the process-level cache


async def _setting(key: str):
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        row = (await s.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
        value = row.value if row else None
    await engine.dispose()
    return value


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


@pytest.fixture(autouse=True)
def _no_ollama(monkeypatch):
    """The rail asks Ollama what is loaded; the suite never touches a live daemon."""
    from kestrel.web import routes_control

    async def _fake(native_url: str):
        return {"up": True, "loaded": ["kestrel-tier2:latest"]}

    monkeypatch.setattr(routes_control, "_probe_ollama", _fake)
    deps.set_ui_scheme_cache(None)
    yield
    deps.set_ui_scheme_cache(None)


def _links(html: str) -> dict[str, str]:
    """href -> the <a> tag's class attribute, for the sidebar's links only."""
    side = html.split('<aside class="side"')[1].split("</aside>")[0]
    return {
        m.group(1): m.group(2)
        for m in re.finditer(r'<a href="([^"]+)" class="([^"]*)"', side)
    }


# ------------------------------------------------------------------------------- the sidebar
def test_base_renders_the_three_groups_and_every_link():
    # The track switch shows only when both tracks are on (a new install is employment-only).
    asyncio.run(_seed([Setting(key="tracks.enabled", value=["employment", "consulting"])]))
    with _client() as c:
        html = c.get("/").text
    for group in ("Work", "Learn", "Setup"):
        assert f'<div class="lbl">{group}</div>' in html
    links = _links(html)
    for _group, items in NAV_GROUPS:
        for item in items:
            assert item.href in links, item.label
            assert f'title="{item.label}"' in html
    # The labels the proposal names, and the ones it renames.
    for label in ("Today", "Pipeline", "Applications", "Profile", "Interview prep", "Offers",
                  "Calibrate", "Analytics", "Sources", "Models", "Settings", "Help", "Runs",
                  "About"):
        assert f">{label}</a>" in html, label
    # Bench left the sidebar (D-DRIFT-11): it is Model check, reached from Models.
    assert ">Bench</a>" not in html and 'href="/bench"' not in _links(html)
    assert ">Dashboard</a>" not in html and ">Stories</a>" not in html
    assert ">My stories</a>" not in html
    # Brand mark: the icon plus the word, not split.
    assert '<img class="mark" src="/static/icon.svg"' in html
    assert '<span class="word">Kestrel</span>' in html
    assert "Kes<b>trel</b>" not in html
    # The three bottom-bar pages and More.
    assert '<nav class="bottombar"' in html
    for href in BOTTOM_BAR:
        assert f'<a href="{href}"' in html.split('<nav class="bottombar"')[1]
    assert ">More</span>" in html
    # Shell plumbing: status rail, count badges, track switch, palette, service worker.
    assert 'hx-get="/status/rail"' in html and 'every 10s' in html
    assert 'hx-get="/status/counts"' in html and 'every 30s' in html
    # Both tracks are on (seeded above), so the switch shows. The choice is now loaded on every
    # startup, worker or not (release round review, finding 7); before, a worker=False app read
    # the employment-only default and this line wrongly passed with the switch absent.
    assert 'class="track-switch"' in html
    assert "serviceWorker" in html and "kestrel-track" in html
    assert "⌘K" in html and 'hx-get="/notifications/bell"' in html


@pytest.mark.parametrize(
    ("path", "active_href"),
    [("/", "/"), ("/opportunities", "/opportunities"), ("/quarantine", "/runs"),
     ("/pursuits", "/pursuits"), ("/stories", "/stories"), ("/settings", "/settings"),
     ("/about", "/about"), ("/runs", "/runs"),
     # Model check (/bench) highlights Models, where it now lives (D-DRIFT-11).
     ("/bench", "/models")],
)
def test_active_class_follows_the_current_path(path, active_href):
    asyncio.run(_seed())
    with _client() as c:
        html = c.get(path).text
    links = _links(html)
    active = [href for href, cls in links.items() if "active" in cls.split()]
    assert active == [active_href], (path, active)
    assert 'aria-current="page"' in html


def test_nav_helpers_match_exactly_one_item_per_known_path():
    for path in _PAGES:
        hits = [it for _g, items in NAV_GROUPS for it in items if nav_active(it, path)]
        assert len(hits) == 1, (path, [h.label for h in hits])
    assert nav_title("/opportunities/42") == "Pipeline"
    assert nav_title("/nowhere") == ""


# ----------------------------------------------------------------------------- color scheme
def test_html_carries_the_stored_scheme_for_the_first_paint():
    asyncio.run(_seed())
    with _client() as c:
        assert 'data-scheme="indigo"' in c.get("/").text
    asyncio.run(_seed([Setting(key="ui.scheme", value="teal")]))
    with _client() as c:
        html = c.get("/").text
    assert html.startswith('<!doctype html>\n<html lang="en" data-scheme="teal"')
    # The pre-paint script is in <head>, ahead of the stylesheet, so there is no flash.
    head = html.split("</head>")[0]
    assert head.index("kestrel-scheme") < head.index('rel="stylesheet"')


def test_a_bad_stored_scheme_falls_back_to_the_default():
    asyncio.run(_seed([Setting(key="ui.scheme", value="mauve")]))
    assert deps.ui_scheme() == "indigo"


# -------------------------------------------------------------------------------- status rail
def test_status_rail_returns_the_four_rows():
    asyncio.run(_seed())
    with _client() as c:
        r = c.get("/status/rail")
    assert r.status_code == 200
    rows = re.findall(r'data-rail="([a-z]+)"', r.text)
    assert rows == ["worker", "model", "scan", "hosted"]
    assert r.text.count('class="dot ') == 4
    assert "Not scoring anything right now" in r.text
    assert "Full review loaded" in r.text
    assert "No scan yet" in r.text
    assert "Online deeper-read key: not set" in r.text
    for state in re.findall(r'class="dot ([a-z]+)"', r.text):
        assert state in {"good", "idle", "warn", "bad"}


def test_api_status_carries_the_rail_fields_and_counts():
    asyncio.run(_seed())
    with _client() as c:
        body = c.get("/api/status").json()
    for key in ("version", "paused", "activity", "mode", "pending_mode", "worker", "model",
                "loaded_model", "scan", "hosted", "counts"):
        assert key in body, key
    assert body["loaded_model"] == "kestrel-tier2:latest"
    assert body["counts"] == {"shortlist": 0, "unrated": 0, "calibrate": 0}


def test_status_counts_are_out_of_band_badges_that_hide_at_zero():
    org = Org(name="Acme", normalized_name="acme")
    src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
    asyncio.run(_seed([org, src]))

    async def _more():
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            o = (await s.execute(select(Org))).scalar_one()
            sr = (await s.execute(select(Source))).scalar_one()
            opp = Opportunity(track="employment", opp_type="job_posting", org_id=o.id,
                              source_id=sr.id, title="VP IT", org_name="Acme",
                              url="https://x/1", stage="t2_done",
                              description_md="Own IT strategy.")
            s.add(opp)
            await s.flush()
            s.add(Evaluation(opportunity_id=opp.id, tier=2, track="employment",
                             verdict="apply", score=4.4, weighted_score=3.0, needs_review=True))
            await s.commit()
        await engine.dispose()

    asyncio.run(_more())
    forget_engine()
    with _client() as c:
        r = c.get("/status/counts")
    assert r.status_code == 200
    assert 'id="nav-count-shortlist" class="n" hx-swap-oob="true">1<' in r.text
    assert 'id="nav-count-needs_review"' not in r.text  # finding 12: no badge, no span
    assert 'id="nav-count-calibrate" class="n" hx-swap-oob="true">1<' in r.text


# ------------------------------------------------------------------------------- appearance
def test_appearance_route_writes_the_row_and_refuses_an_unknown_scheme():
    asyncio.run(_seed())
    with _client() as c:
        r = c.post("/settings/appearance", data={"scheme": "teal"})
        assert r.status_code == 200 and "Saved" in r.text
        assert 'data-scheme="teal"' in c.get("/opportunities").text  # no restart, no wait
        bad = c.post("/settings/appearance", data={"scheme": "mauve"})
        assert bad.status_code == 400 and "mauve" in bad.text
        empty = c.post("/settings/appearance", data={})
        assert empty.status_code == 400
    forget_engine()
    assert asyncio.run(_setting("ui.scheme")) == "teal"  # the refusals wrote nothing


def test_settings_page_has_the_appearance_section_with_the_shown_schemes():
    asyncio.run(_seed([Setting(key="ui.scheme", value="teal")]))
    with _client() as c:
        html = c.get("/settings").text
        pane = c.get("/settings/section/preferences").text
    assert 'data-scheme="teal"' in html
    assert "Paper and Indigo" in pane and "Paper and Teal" in pane
    from kestrel.config import UI_SCHEMES

    assert pane.count("data-st-scheme=") == len(UI_SCHEMES)
    for scheme in UI_SCHEMES:
        assert f'data-st-scheme="{scheme}"' in pane
    assert 'aria-checked="true" data-st-scheme="teal"' in pane
    for mode in ("light", "dark", "auto"):
        assert f'value="{mode}"' in pane


# ----------------------------------------------------------------------------------- sweep
def test_every_page_still_renders(monkeypatch):
    async def _no_model(*args, **kwargs):
        raise RuntimeError("model call stubbed out in the page sweep")

    monkeypatch.setattr("kestrel.web.pages.analytics.honest_summary", _no_model, raising=False)
    asyncio.run(_seed())
    with _client() as c:
        for path in _PAGES:
            r = c.get(path)
            assert r.status_code == 200, (path, r.status_code)
            body = r.text
            assert '<aside class="side"' in body, path
            assert '<main class="container">' in body, path
            # No em dash reaches the screen anywhere (D-R3-2): entities are decoded first.
            assert "\u2014" not in html.unescape(body), path


# ------------------------------------------------------------------------------- stylesheet
def _template_classes() -> dict[str, set[str]]:
    """Every static class token set in a template, with the templates that set it. Jinja tags
    inside the attribute are replaced by a space so `{% if x %}a{% else %}b{% endif %}` yields a
    and b, and `legit-{{ status }}` yields the prefix (skipped as a dynamic token)."""
    seen: dict[str, set[str]] = {}
    for tmpl in _TEMPLATES.rglob("*.html"):
        text = tmpl.read_text(encoding="utf-8")
        # A plain class attribute only: `:class="{...}"` is an Alpine expression, not a class list.
        for attr in re.findall(r'(?<![:\w-])class="([^"]*)"', text):
            cleaned = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", " ", attr)
            for token in cleaned.split():
                if token.endswith("-") or not re.fullmatch(r"[A-Za-z][\w-]*", token):
                    continue  # a dynamic prefix such as `legit-`
                seen.setdefault(token, set()).add(tmpl.name)
    return seen


def test_stylesheet_has_a_rule_for_every_template_class():
    # The k- classes of the shared macros (templates/ui) live in components.css; a converted
    # screen keeps its own sheet beside them (retry.css, pages/*.css), loaded through
    # base.html's head_extra.
    css = "".join(p.read_text(encoding="utf-8") for p in sorted(_CSS.parent.rglob("*.css")))
    missing = {
        token: sorted(files)
        for token, files in _template_classes().items()
        if token not in _CSS_SKIP and not re.search(rf"\.{re.escape(token)}(?![\w-])", css)
    }
    assert not missing, f"classes used in templates with no stylesheet rule: {missing}"


def test_stylesheet_defines_both_schemes_in_both_modes_and_constant_verdict_tokens():
    css = _CSS.read_text(encoding="utf-8")
    assert css.index(":root {") < css.index('@media (prefers-color-scheme: dark)')
    assert ':root:not([data-theme="light"])' in css and ':root[data-theme="dark"]' in css
    assert ':root[data-scheme="teal"] {' in css
    assert ':root[data-scheme="teal"]:not([data-theme="light"])' in css
    assert ':root[data-scheme="teal"][data-theme="dark"]' in css
    for token in ("--accent", "--accent-deep", "--accent-tint", "--action", "--action-fg",
                  "--paper", "--warm", "--line", "--ink", "--muted", "--good", "--good-tint",
                  "--keep", "--keep-tint", "--keep-fg", "--warn", "--warn-tint", "--bad",
                  "--bad-tint", "--sans", "--mono"):
        assert f"{token}:" in css, token
    # The teal blocks only move the ground and the accent, never a verdict color.
    teal = css.split(':root[data-scheme="teal"] {')[1].split("/* ---")[0]
    for token in ("--good", "--keep", "--warn", "--bad"):
        assert f"{token}:" not in teal, token
    assert "--keep: #D4622A" in css  # keep is the Kestrel orange in light mode
    assert "IBM Plex" not in css  # not loadable offline; the system stack is used
    for component in (".card", ".card > h3", ".list > .it", ".stripe", ".stripe.apply", ".step",
                      ".meter", ".pill.keep", ".pill.review", ".pill.kill", ".dot", ".rail",
                      ".navgroup", ".bottombar", ".sheet", ".stat", ".chip"):
        assert component in css, component
    assert "@media (max-width: 1000px)" in css and "@media (max-width: 700px)" in css


def test_rail_says_scanning_while_searches_run_and_names_failing_searches():
    """D-UI-33: a running scan is not idle, and a failing search shows on the rail."""
    from kestrel.db.models import Task

    asyncio.run(_seed([
        Source(track="employment", kind="jobspy", name="ok", enabled=True),
        Source(track="employment", kind="jobspy", name="broken", enabled=True,
               consecutive_failures=2),
        # Switched off by the runner after three failures: still the operator's to hear about.
        Source(track="employment", kind="jobspy", name="off", enabled=False,
               consecutive_failures=3),
        # Retired, or switched off by the operator before three: not raised again.
        Source(track="employment", kind="jobspy", name="old", enabled=False,
               consecutive_failures=4, origin="retired"),
        Source(track="employment", kind="jobspy", name="paused", enabled=False,
               consecutive_failures=1),
        Task(type="scrape", status="pending"),
        Task(type="scrape", status="leased"),
        Task(type="t1", status="pending"),
    ]))
    with _client() as c:
        html = c.get("/status/rail").text
    # The step in words (D-UI-58): the first of two searches is under way, nothing found yet.
    assert "Searching 1 of 2 searches" in html
    assert "2 searches failing · see Sources" in html
    assert "Not scoring anything right now" not in html


def test_qr_ground_and_ink_tokens_are_fixed_light_in_every_scheme_block():
    """#28: the QR is always dark on light, so --qr-bg and --qr-ink are defined in every block
    (light, both dark selectors, teal and its dark selectors) with the same values."""
    css = _CSS.read_text(encoding="utf-8")
    selectors = (
        ":root {",
        ':root:not([data-theme="light"]) {',
        ':root[data-theme="dark"] {',
        ':root[data-scheme="teal"] {',
        ':root[data-scheme="teal"]:not([data-theme="light"]) {',
        ':root[data-scheme="teal"][data-theme="dark"] {',
    )
    values = set()
    for sel in selectors:
        block = css.split(sel, 1)[1].split("}", 1)[0]
        bg = re.search(r"--qr-bg:\s*(#[0-9A-Fa-f]{6})", block)
        ink = re.search(r"--qr-ink:\s*(#[0-9A-Fa-f]{6})", block)
        assert bg and ink, f"{sel} lacks --qr-bg / --qr-ink"
        values.add((bg.group(1).lower(), ink.group(1).lower()))
    assert len(values) == 1, values  # identical everywhere: never theme-dependent
    (bg, ink), = values

    def lum(h: str) -> float:
        return sum(int(h[i : i + 2], 16) for i in (1, 3, 5)) / 3

    assert lum(bg) > 235 and lum(ink) < 40
    assert re.search(r"\.qr\s*\{[^}]*background:\s*var\(--qr-bg\)", css)
    assert re.search(r"\.qr svg[^{]*\{[^}]*var\(--qr-ink\)", css)


def test_board_cards_stay_unselectable_so_dragging_still_works():
    css = _CSS.read_text(encoding="utf-8")
    assert re.search(r"\.pcard\s*\{[^}]*user-select:\s*none", css)


def test_no_page_in_the_nav_renders_a_guide_strip():
    """D-UI-146: the per-page guide strips are gone; each page's How it works button is its help."""
    asyncio.run(_seed())
    paths_in_nav = {item.href for _g, items in deps.NAV_GROUPS for item in items}
    with _client() as c:
        for path in sorted(set(_PAGES) | paths_in_nav):
            r = c.get(path, follow_redirects=True)
            assert r.status_code == 200, path
            for gone in ('data-testid="guide-strip"', "data-guide=", 'class="guide-strip"',
                         "/guides/"):
                assert gone not in r.text, (path, gone)
        assert c.post("/guides/today/dismiss").status_code in (404, 405)
