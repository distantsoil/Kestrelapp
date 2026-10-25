"""Shared UI contract (UI overhaul, Phase 1): the new sidebar, the base template's assets, the
Jinja macros, the overlay-centering rule, and the browser runtime. The runtime and the scene
files are exercised under node when it is installed; nothing here opens a browser or a network
connection."""

from __future__ import annotations

import asyncio
import html
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from kestrel import paths
from kestrel.web import deps
from kestrel.web.deps import NAV_GROUPS

_STATIC = Path(paths.web_static_dir())
_TEMPLATES = Path(paths.web_templates_dir())
_NODE = shutil.which("node")


# ---------------------------------------------------------------------------------- sidebar
def test_nav_groups_match_the_locked_sidebar_and_have_no_bench():
    """D-DRIFT-11: Bench is Model check and lives under Models, not in the sidebar."""
    labels = [(g, [i.label for i in items]) for g, items in NAV_GROUPS]
    assert labels == [
        ("Work", ["Today", "Pipeline", "Applications", "Profile", "Interview prep", "Offers"]),
        ("Learn", ["Calibrate", "Analytics"]),
        ("Setup", ["Sources", "Models", "Runs", "Settings"]),
        ("", ["Help", "About"]),
    ]
    assert all("/bench" != i.href for _g, items in NAV_GROUPS for i in items)
    models = next(i for _g, items in NAV_GROUPS for i in items if i.label == "Models")
    assert "/bench" in models.prefixes


def test_rendered_sidebar_lists_the_new_items_in_order_and_loads_the_design_system():
    from test_ui_shell import _client, _seed

    asyncio.run(_seed())
    with _client() as c:
        # A screen still on the legacy sheet (Today, About and Help no longer load it).
        page = c.get("/backup").text
        side = page.split('<aside class="side"')[1].split("</aside>")[0]
        names = re.findall(r'<a href="[^"]+" class="[^"]*" title="([^"]+)"', side)
        assert names == ["Today", "Pipeline", "Applications", "Profile", "Interview prep",
                         "Offers", "Calibrate", "Analytics", "Sources", "Models", "Runs",
                         "Settings", "Help", "About"]
        assert "Bench" not in side
        # The design system loads after the legacy sheet, scenes before the runtime.
        order = [page.index(f'/static/{p}"') for p in (
            "css/kestrel.css", "css/tokens.css", "css/components.css",
            "js/scenes/scenes-base.js", "js/scenes/seasons-b.js", "js/scenes/scene-band.js",
            "js/kestrel-ui.js")]
        assert order[:3] == sorted(order[:3]) and order[3:] == sorted(order[3:])
        assert 'id="k-toasts"' in page
        # Top bar: search/jump with Ctrl K, bell slot, theme menu, avatar; palette lists settings.
        assert 'class="k-avatar k-avatar--' in page and "Search or jump to" in page
        assert "e.ctrlKey || e.metaKey" in page and 'hx-get="/notifications/bell"' in page
        assert "label: 'Settings: Appearance'" in page and 'role="listbox"' in page
        # One source for appearance writes: the chrome watchers call KUI, not localStorage.
        assert "KUI.setTheme(v)" in page and "localStorage.setItem('kestrel-theme'" not in page
        assert "label: 'Model check', href: '/bench'" in page  # the command palette lists it
        for src in re.findall(r'(?:href|src)="(/static/(?:css|js)/[^"]+)"', page):
            assert c.get(src).status_code == 200, src


# ------------------------------------------------------------------------------------ macros
def _render(body: str, **ctx: object) -> str:
    env = deps.templates().env
    return env.from_string('{% import "ui/macros.html" as m %}' + body).render(**ctx)


CASES = {
    "icon": ('{{ m.icon("shield") }}', ['<svg class="k-ic', 'aria-hidden="true"', "<path"]),
    "icon_unknown_falls_back": ('{{ m.icon("nope") }}', ["<svg class=\"k-ic"]),
    "button": ('{{ m.button("Save", "primary", small=True) }}',
               ["k-btn k-btn--primary k-btn--sm", ">Save<"]),
    "button_link": ('{{ m.button("Open", href="/x") }}', ['<a class="k-btn" href="/x"']),
    "pill": ('{{ m.pill("Healthy", "good", "check") }}', ["k-pill k-pill--good", "Healthy"]),
    "chip": ('{{ m.chip("New", 4, True) }}',
             ["k-chip is-on", 'aria-pressed="true"', '<span class="k-chip__n">4</span>']),
    "section_card": ('{% call m.section_card("Models", "models", "good", "On this computer") %}'
                     'body{% endcall %}',
                     ['<section class="k-card">', "k-sec-head", "k-badge k-badge--good",
                      "<h3>Models</h3>", "On this computer", "body"]),
    "help_card": ('{{ m.help_card("What a run does", ["One", "Two"], "warn", "clock", "fly1") }}',
                  ["k-hcard k-hcard--warn", "<li>One</li>", 'data-k-flyout="#fly1"',
                   "More about this"]),
    "help_card_without_flyout": ('{{ m.help_card("T", ["a"]) }}', ["k-hcard--info"]),
    "help_column": ('{% call m.help_column() %}X{% endcall %}', ['<aside class="k-help-col"']),
    "flyout": ('{% call m.flyout("fly1", "How it works", help_href="/help#runs") %}Hi'
               '{% endcall %}',
               ['class="k-flyout"', 'id="fly1"', 'role="dialog"', "data-k-close",
                'href="/help#runs"', "More about this"]),
    "slide_over": ('{% call m.slide_over("so1", "Details", "Source") %}B{% endcall %}',
                   ['class="k-slideover"', 'id="so1"', "k-slideover__body", "data-k-close",
                    'aria-modal="true"']),
    "modal": ('{% call m.modal("md1", "Send to an online model?", '
              '[{"label": "Cancel", "close": True}, {"label": "Send", "kind": "primary"}], '
              'static=True) %}Body{% endcall %}',
              ['class="k-modal"', 'aria-modal="true"', "data-k-static", "k-btn--primary",
               "k-modal__foot"]),
    "sheet": ('{% call m.modal("sh1", "Sheet", sheet=True) %}B{% endcall %}',
              ["k-modal k-modal--sheet"]),
    "toast_region": ('{{ m.toast_region() }}', ['id="k-toasts"', 'role="status"']),
    "switch": ('{{ m.switch("still", "Keep art still", True) }}',
               ['role="switch"', 'name="still"', " checked", "k-switch__track"]),
    "segmented": ('{{ m.segmented("mode", [("local", "Local"), ("online", "Online")], "online") }}',
                  ['class="k-seg"', 'value="online"', 'aria-pressed="true"', "is-on",
                   '<input type="hidden" name="mode" value="online">']),
    "stepper": ('{{ m.stepper("count", 3, 1, 9) }}',
                ["k-stepper", 'value="3"', 'data-k-step="-1"', 'data-k-step="1"']),
    "steps": ('{{ m.steps(["Find", "Rate", "Tailor", "Apply"], 2) }}',
              ["k-steps", "--k-steps: 4", "is-done", 'is-now', 'aria-current="step"']),
    "danger_zone": ('{% call m.danger_zone("Reset") %}<div class="k-dz__row">x</div>'
                    '{% endcall %}',
                    ['class="k-dz"', "k-dz__head", "Reset", 'aria-labelledby="k-dz-t"']),
    "scene_band": ('{{ m.scene_band("Today", "Friday", "coast", "lantern", False) }}',
                   ['class="k-scene"', 'data-scene="coast"', 'data-touch="lantern"',
                    'data-animate="0"', "<h1>Today</h1>"]),
    "scene_band_actions": ('{% call m.scene_band("Pipeline") %}<button>Go</button>'
                           '{% endcall %}',
                           ["k-scene__title", "<button>Go</button>", 'data-animate="1"']),
    "empty_state": ('{{ m.empty_state("Nothing yet", "Run a search.", "bird", "Run now", '
                    '"/runs") }}',
                    ["k-empty", "Nothing yet", "Run a search.", 'href="/runs"']),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_every_macro_renders(name):
    body, needles = CASES[name]
    out = _render(body)
    for needle in needles:
        assert needle in out, (name, needle, out)
    assert "{{" not in out and "{%" not in out
    assert "—" not in html.unescape(out)


def test_macros_escape_what_callers_pass_in():
    out = _render('{{ m.pill("<script>alert(1)</script>", "bad") }}'
                  '{% call m.flyout("f", "<b>t</b>") %}ok{% endcall %}')
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "<b>t</b>" not in out


def test_macros_template_has_no_color_literals_or_inline_colors():
    text = (_TEMPLATES / "ui" / "macros.html").read_text(encoding="utf-8")
    body = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
    assert not re.search(r"#[0-9A-Fa-f]{6}\b", body.replace("&#", ""))
    assert not re.search(r'style="[^"]*(?:color|background)\s*:', body)


# --------------------------------------------------------------------- overlay-centering rule
def _rule(css: str, selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, selector
    return " ".join(m.group(1).split())


def test_flyout_is_right_anchored_and_vertically_centered_in_css():
    css = (_STATIC / "css" / "components.css").read_text(encoding="utf-8")
    fly = _rule(css, ".k-flyout")
    assert "position: fixed" in fly and "right: 12px" in fly and "top: 50%" in fly
    assert "translate(30px, -50%)" in fly  # starts off to the right, already centered
    assert "translate(0, -50%)" in _rule(css, ".k-flyout.is-open")
    # Slide-over overlays content and is never in the page flow; modal is centered.
    so = _rule(css, ".k-slideover")
    assert "position: fixed" in so and "right: 0" in so and "top: 50%" in so
    assert "--k-slideover-top" not in css  # never pinned to a fixed top
    assert "translate(0, -50%)" in _rule(css, ".k-slideover.is-open")
    assert "left: 50%" in _rule(css, ".k-modal") and "top: 50%" in _rule(css, ".k-modal")
    assert "scrollbar-gutter: stable" in _rule(css, ".k-main")


def test_reduced_motion_rule_and_reflow_breakpoints_are_present():
    css = (_STATIC / "css" / "components.css").read_text(encoding="utf-8")
    assert "@media (prefers-reduced-motion: reduce)" in css
    reduced = css.split("@media (prefers-reduced-motion: reduce)")[1]
    assert "animation: none !important" in reduced and "transition: none !important" in reduced
    layout_1300 = css.split("@container (max-width: 1300px)")[1].split("}\n@container")[0]
    assert ".k-layout" in layout_1300 and ".k-help-col" in layout_1300
    assert ".k-help-col" in css.split("@container (max-width: 1000px)")[1]


# ----------------------------------------------------------------------------- JS under node
def _node(script: str) -> str:
    if _NODE is None:
        pytest.skip("node is not installed")
    done = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, timeout=60,
                          check=False)
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
@pytest.mark.parametrize(
    "rel", ["js/kestrel-ui.js", "js/scenes/scene-band.js", "js/scenes/scenes-base.js",
            "js/scenes/scenes-a.js", "js/scenes/scenes-b.js", "js/scenes/seasons-a.js",
            "js/scenes/seasons-b.js"],
)
def test_script_parses(rel):
    done = subprocess.run([_NODE, "--check", str(_STATIC / rel)], capture_output=True,
                          text=True, check=False)
    assert done.returncode == 0, done.stderr


def test_centering_math_matches_the_rule():
    out = _node(f"""
      global.window = global;
      const K = require({json.dumps(str(_STATIC / "js" / "kestrel-ui.js"))});
      console.log(JSON.stringify([K.centerTop(1000, 400), K.centerTop(1000, 990),
        K.centerTop(800, 800), K.centerTop(900, 301)]));
    """)
    assert json.loads(out) == [300, 12, 12, 300]  # centered; too tall pins to the margin


def test_every_scene_and_touch_draws_markup_with_prefixed_ids():
    if _NODE is None:
        pytest.skip("node is not installed")
    done = subprocess.run(
        [_NODE, str(Path(__file__).parent / "js" / "scene_smoke.js"),
         str(_STATIC / "js" / "scenes")],
        capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    res = json.loads(done.stdout)
    assert len(res["scenes"]) == 14 and len(res["seasons"]) == 16
    for sid, length, stray, _ids in res["scenes"] + res["seasons"]:
        assert length > 200, sid
        assert stray == 0, f"{sid} has an id outside its sc-/ss- prefix"
    assert res["uniq"] == [True, True, True]
    assert res["band"] == [2, True, True]  # scene plus touch, stacked
    assert res["skipped"] is True  # a matching data-rendered band is not redrawn
    assert res["none"] == ""
