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
        order = [page.index(f'/static/{p}?v=') for p in (
            "css/kestrel.css", "css/tokens.css", "css/components.css",
            "js/scenes/scenes-base.js", "js/scenes/seasons-b.js", "js/scenes/scene-band.js",
            "js/kestrel-ui.js")]
        assert order[:3] == sorted(order[:3]) and order[3:] == sorted(order[3:])
        assert 'id="k-toasts"' in page
        # Top bar: search/jump with Ctrl K, bell slot, theme menu, avatar; palette lists settings.
        assert 'class="k-avatar k-avatar--' in page and "Search or jump to" in page
        assert "e.ctrlKey || e.metaKey" in page and 'hx-get="/notifications/bell"' in page
        assert "/static/js/palette.js" in page and 'role="listbox"' in page
        # One source for appearance writes: the chrome watchers call KUI, not localStorage.
        assert "KUI.setTheme(v)" in page and "localStorage.setItem('kestrel-theme'" not in page
        # The command palette (static/js/palette.js) lists Model check among its settings.
        assert "Model check" in c.get("/static/js/palette.js").text
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
    # The offsets fall back to the window values; kestrel-ui.js anchor() sets them only when an
    # ancestor is the overlay's containing block (D-UI-216).
    assert "position: fixed" in fly and "right: calc(var(--k-ar, 0px) + 12px)" in fly
    assert "top: var(--k-ay, 50%)" in fly
    assert "translate(30px, -50%)" in fly  # starts off to the right, already centered
    assert "translate(0, -50%)" in _rule(css, ".k-flyout.is-open")
    # Slide-over overlays content and is never in the page flow; modal is centered.
    so = _rule(css, ".k-slideover")
    assert "position: fixed" in so and "right: var(--k-ar, 0px)" in so
    assert "top: var(--k-ay, 50%)" in so
    assert "--k-slideover-top" not in css  # never pinned to a fixed top
    assert "translate(0, -50%)" in _rule(css, ".k-slideover.is-open")
    modal = _rule(css, ".k-modal")
    assert "left: var(--k-ax, 50%)" in modal and "top: var(--k-ay, 50%)" in modal
    # The page scrolls on the document with no visible bar; the scroll cues mark more (D-UI-203).
    assert "scrollbar-width: none" in _rule(css, "html")


def test_reduced_motion_rule_and_reflow_breakpoints_are_present():
    css = (_STATIC / "css" / "components.css").read_text(encoding="utf-8")
    assert "@media (prefers-reduced-motion: reduce)" in css
    reduced = css.split("@media (prefers-reduced-motion: reduce)")[1]
    assert "animation: none !important" in reduced and "transition: none !important" in reduced
    # Window steps measure the named window container, as the mockups' .win does.
    assert "container: kwin / inline-size" in css
    layout_1300 = css.split("@container kwin (max-width: 1300px)")[1].split("}\n@container")[0]
    assert ".k-layout" in layout_1300 and ".k-help-col" in layout_1300
    assert ".k-help-col" in css.split("@container kwin (max-width: 1000px)")[1]


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


# The page lifecycle (D-UI-210, docs/handoff/D-UI-210-plan.md "The page lifecycle contract"):
# KUI.page and the context each mount gets, under node (an EventTarget stands in for the page).
_PAGE_JS = """
  global.window = global;
  const K = require(%s);
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const out = {};
  const root = { getAttribute: () => 'demo' };
  const target = new EventTarget();
  const log = [];
  let ctxSeen = null;
  K.page.define('demo', {
    mount(el, ctx) {
      ctxSeen = ctx;
      log.push('mount:' + (el === root));
      const fn = () => log.push('click');
      ctx.on(target, 'click', fn);
      ctx.on(target, 'click', fn);              // the browser ignores this one: counted once
      ctx.on(target, 'click', fn, true);        // capture is a registration of its own
      ctx.on(target, 'ping', () => log.push('ping'), { once: true });
      ctx.on(null, 'click', fn);                // no target: nothing
      ctx.every(5, () => log.push('tick'));
      ctx.after(1, () => log.push('after'));
      ctx.after(60000, () => log.push('late'));
      ctx.observe({ disconnect() { log.push('disconnect'); } });
      ctx.onUnmount(() => log.push('cleanup1'));
      ctx.onUnmount(() => log.push('cleanup2'));
    },
    unmount() { log.push('unmount'); },
  });
"""


def _page_node(body: str) -> dict:
    script = _PAGE_JS % json.dumps(str(_STATIC / "js" / "kestrel-ui.js"))
    return json.loads(_node(script + "(async () => {" + body +
                            "\nconsole.log(JSON.stringify(out)); })();"))


def test_page_define_mount_and_unmount_own_everything_the_page_starts():
    out = _page_node("""
      out.before = [K.page.current(), K.page._stats()];
      out.mounted = K.page.mount('demo', root);
      out.first = K.page._stats();
      out.gotRoot = log.includes('mount:true');
      target.dispatchEvent(new Event('click'));
      target.dispatchEvent(new Event('ping'));
      target.dispatchEvent(new Event('ping'));   // once: the second never reaches the page
      out.pings = log.filter(x => x === 'ping').length;
      out.afterPing = K.page._stats().listeners;
      await sleep(30);
      out.timersRunning = K.page._stats().timers;  // the 1 ms one fired and was let go
      out.ran = ['tick', 'after'].map(x => log.includes(x));
      out.current = K.page.current();
      const signal = ctxSeen.signal;
      out.unmounted = K.page.unmount();
      out.aborted = signal.aborted;
      out.after = [K.page.current(), K.page._stats()];
      const n = log.length;
      target.dispatchEvent(new Event('click'));
      await sleep(30);
      out.quiet = log.slice(n);                  // no listener and no timer after unmount
      out.order = log.filter(x => /unmount|cleanup|disconnect/.test(x));
      out.unmountAgain = K.page.unmount();
      out.remounted = K.page.mount(root);         // the name comes from the root
      out.second = K.page._stats();
      K.page.unmount();
      out.clicks = log.filter(x => x === 'click').length;
    """)
    empty = {"name": None, "listeners": 0, "timers": 0, "observers": 0}
    assert out["before"] == [None, empty]
    assert out["mounted"] is True and out["gotRoot"] is True and out["current"] == "demo"
    assert out["first"] == {"name": "demo", "listeners": 3, "timers": 3, "observers": 1}
    assert out["pings"] == 1 and out["afterPing"] == 2  # the once listener ran and was let go
    assert out["timersRunning"] == 2 and out["ran"] == [True, True]
    assert out["unmounted"] is True and out["aborted"] is True
    assert out["after"] == [None, empty]
    assert out["quiet"] == []
    # The page's own unmount first, then the observer, then the cleanups last to first.
    assert out["order"] == ["unmount", "disconnect", "cleanup2", "cleanup1"]
    assert out["unmountAgain"] is False
    assert out["remounted"] is True and out["second"] == out["first"]
    assert out["clicks"] == 2  # the one click before unmount, heard by bubble and capture


def test_page_context_stops_early_and_does_nothing_once_unmounted():
    out = _page_node("""
      K.page.mount('demo', root);
      const ctx = ctxSeen;
      const id = ctx.every(5, () => log.push('stray'));
      out.withExtra = K.page._stats().timers;
      ctx.clear(id);
      const t = ctx.after(5, () => log.push('stray'));
      ctx.clear(t);
      out.cleared = K.page._stats().timers;
      const off = ctx.on(target, 'extra', () => log.push('stray'));
      out.withListener = K.page._stats().listeners;
      off();
      off();
      target.dispatchEvent(new Event('extra'));
      out.listeners = K.page._stats().listeners;
      let gone = 0;
      const obs = ctx.observe({ disconnect() { gone += 1; } });
      out.sameObserver = ctx.observe(obs) === obs && K.page._stats().observers === 2;
      obs.disconnect();                            // done early: the context lets it go
      out.observers = K.page._stats().observers;
      K.page.unmount();
      out.goneOnce = gone === 1;
      await sleep(20);
      out.stray = log.filter(x => x === 'stray').length;
      // A context whose page was unmounted starts nothing more.
      out.lateEvery = ctx.every(5, () => log.push('stray'));
      out.lateAfter = ctx.after(5, () => log.push('stray'));
      ctx.on(target, 'extra', () => log.push('stray'));
      let lateDisconnect = 0;
      ctx.observe({ disconnect() { lateDisconnect += 1; } });
      let lateCleanup = 0;
      ctx.onUnmount(() => { lateCleanup += 1; });
      target.dispatchEvent(new Event('extra'));
      await sleep(20);
      out.late = [log.filter(x => x === 'stray').length, lateDisconnect, lateCleanup];
      out.statsAfter = K.page._stats();
    """)
    assert out["withExtra"] == 4 and out["cleared"] == 3
    assert out["withListener"] == 4 and out["listeners"] == 3
    assert out["sameObserver"] is True and out["observers"] == 1
    assert out["goneOnce"] is True and out["stray"] == 0
    assert out["lateEvery"] == 0 and out["lateAfter"] == 0
    assert out["late"] == [0, 1, 1]  # an observer or a cleanup handed over late ends at once
    assert out["statsAfter"] == {"name": None, "listeners": 0, "timers": 0, "observers": 0}


def test_page_mount_replaces_the_mounted_page_and_skips_an_unknown_one():
    out = _page_node("""
      const other = [];
      K.page.define('other', { mount(el, ctx) { other.push('mount'); ctx.every(5, () => {}); },
                               unmount() { other.push('unmount'); } });
      K.page.mount('demo', root);
      out.switched = K.page.mount('other', root);
      out.current = K.page.current();
      out.demoGone = log.includes('unmount') && log.includes('cleanup1');
      out.unknown = K.page.mount('nowhere', root);  // nothing defined: nothing mounted
      out.afterUnknown = [K.page.current(), other];
      out.start = K.page.start();                   // no document here: nothing to mount
      try { K.page.define('broken', {}); out.badDefine = 'accepted'; }
      catch (e) { out.badDefine = e.name; }
    """)
    assert out["switched"] is True and out["current"] == "other" and out["demoGone"] is True
    assert out["unknown"] is False and out["afterUnknown"] == [None, ["mount", "unmount"]]
    assert out["start"] is False
    assert out["badDefine"] == "TypeError"


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
