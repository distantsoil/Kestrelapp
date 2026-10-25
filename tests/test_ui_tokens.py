"""Design tokens and components (UI overhaul, Phase 1): contrast, token-only color, scene
registrations, no em dash. Pure file reads; no browser, no network."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kestrel import paths

_STATIC = Path(paths.web_static_dir())
_TOKENS = _STATIC / "css" / "tokens.css"
_COMPONENTS = _STATIC / "css" / "components.css"
SCHEMES = ("indigo", "teal", "plum", "graphite")
THEMES = ("light", "dark")


# ---------------------------------------------------------------------------- CSS parsing
_CSS_DIR = _STATIC / "css"
_BASE_HTML = Path(paths.web_templates_dir()) / "base.html"


def _rules(css: str) -> list[tuple[str, str, dict[str, str]]]:
    """(media, selector, declarations) for every :root rule that sets custom properties, in
    source order. media is "dark" inside @media (prefers-color-scheme: dark), "other" inside any
    other @media, "" outside."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out: list[tuple[str, str, dict[str, str]]] = []
    media = ""
    for m in re.finditer(r"(@media[^{]*)\{|([^{}]+)\{([^{}]*)\}|\}", css):
        if m.group(1):
            media = "dark" if "prefers-color-scheme: dark" in m.group(1) else "other"
        elif m.group(2) is None:
            media = ""
        else:
            decls = dict(re.findall(r"--([\w-]+)\s*:\s*([^;]+);", m.group(3)))
            for sel in m.group(2).split(","):
                sel = " ".join(sel.split())
                if sel.startswith(":root") and decls:
                    out.append((media, sel, {k: v.strip() for k, v in decls.items()}))
    return out


def _specificity(sel: str) -> int:
    """Class-level weight of a :root selector: each :root and each attribute test counts one."""
    return sel.count(":root") + sel.count("[")


def _matches(sel: str, media: str, theme: str | None, os_dark: bool, scheme: str | None) -> bool:
    if media == "other" or (media == "dark" and not os_dark):
        return False
    rest = sel
    for neg in re.findall(r':not\(\[data-theme="(\w+)"\]\)', sel):
        if theme == neg:
            return False
        rest = rest.replace(f':not([data-theme="{neg}"])', "")
    for attr, val in re.findall(r'\[data-(theme|scheme)="(\w+)"\]', rest):
        if (theme if attr == "theme" else scheme) != val:
            return False
    return True


def _sheet_order() -> list[str]:
    """The stylesheets base.html loads, in load order."""
    return re.findall(r"/static/css/([\w.-]+\.css)", _BASE_HTML.read_text(encoding="utf-8"))


def _effective(theme: str | None, scheme: str | None, os_dark: bool = False) -> dict[str, str]:
    """The custom properties :root ends up with when base.html's stylesheets are loaded in
    order: specificity first, then source order, the way the browser cascades. var() chains
    (the legacy --bg: var(--paper)) are resolved. theme None is "follow the OS"."""
    rules: list[tuple[int, int, dict[str, str]]] = []
    order = 0
    for sheet in _sheet_order():
        for media, sel, decls in _rules((_CSS_DIR / sheet).read_text(encoding="utf-8")):
            order += 1
            if _matches(sel, media, theme, os_dark, scheme):
                rules.append((_specificity(sel), order, decls))
    raw: dict[str, str] = {}
    for _spec, _ord, decls in sorted(rules, key=lambda r: (r[0], r[1])):
        raw.update(decls)

    def resolve(value: str, depth: int = 0) -> str:
        m = re.fullmatch(r"var\(--([\w-]+)\)", value)
        return resolve(raw[m.group(1)], depth + 1) if m and depth < 8 else value

    return {k: resolve(v) for k, v in raw.items()}


def _tokens(theme: str, scheme: str) -> dict[str, str]:
    """Effective tokens for a pinned theme and a scheme (indigo is the no-override default)."""
    return _effective(theme, None if scheme == "indigo" else scheme)


def _lum(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(fg: str, bg: str) -> float:
    a, b = sorted((_lum(fg), _lum(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def _token_values(theme: str, scheme: str) -> dict[str, str]:
    """tokens.css alone, no legacy sheet: what the design system declares."""
    rules = _rules(_TOKENS.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for _spec, _i, media, sel, decls in sorted(
        ((_specificity(s), i, m, s, d) for i, (m, s, d) in enumerate(rules)),
        key=lambda r: (r[0], r[1]),
    ):
        if _matches(sel, media, theme, False, None if scheme == "indigo" else scheme):
            out.update(decls)
    return out


GROUNDS = ("bg", "paper", "soft", "tint-info", "tint-warn")
# (foreground token, background tokens it is used on). Text only; borders and fills are not
# held to the text ratio.
PAIRS: list[tuple[str, tuple[str, ...]]] = [
    ("ink", GROUNDS + ("bad-bg", "good-bg", "warn-bg")),
    ("muted", GROUNDS),
    ("brand", ("paper", "bg", "soft", "tint-info")),
    ("link-ink", GROUNDS + ("bad-bg", "warn-bg", "chip-i-bg")),
    ("brand-ink", ("brand",)),
    ("good-ink", ("paper", "bg", "good-bg")),
    ("warn-ink", ("paper", "bg", "warn-bg", "tint-warn")),
    ("bad-ink", ("paper", "bg", "bad-bg")),
    ("chip-i-ink", ("chip-i-bg",)),
    ("tg0i", ("tg0",)),
    ("tg1i", ("tg1",)),
    ("tg2i", ("tg2",)),
    ("tg3i", ("tg3",)),
    ("tgdi", ("tgd",)),
    ("toast-ink", ("toast-bg",)),
    ("toast-action", ("toast-bg",)),
]


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("theme", THEMES)
def test_text_tokens_meet_wcag_aa_on_every_ground(theme, scheme):
    t = _tokens(theme, scheme)
    failures = []
    for fg, bgs in PAIRS:
        for bg in bgs:
            ratio = contrast(t[fg], t[bg])
            if ratio < 4.5:
                failures.append(f"{fg} {t[fg]} on {bg} {t[bg]} = {ratio:.2f}")
    assert not failures, f"{theme}/{scheme}: " + "; ".join(failures)


def test_media_dark_blocks_match_the_pinned_dark_blocks():
    """The OS-preference fallback and data-theme=dark must carry the same values."""
    rules = _rules(_TOKENS.read_text(encoding="utf-8"))
    by_sel = {(m, s): d for m, s, d in rules}
    root = ":root:root:root"
    assert by_sel[("dark", f'{root}:not([data-theme="light"])')] == by_sel[
        ("", f'{root}[data-theme="dark"]')
    ]
    for s in ("teal", "plum", "graphite"):
        assert by_sel[("dark", f'{root}:not([data-theme="light"])[data-scheme="{s}"]')] == by_sel[
            ("", f'{root}[data-theme="dark"][data-scheme="{s}"]')
        ], s
    # OS dark with no pinned theme resolves to the same effective tokens as pinned dark.
    for scheme in (None, "teal", "plum", "graphite"):
        assert _effective(None, scheme, os_dark=True) == _effective("dark", scheme), scheme


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("theme", THEMES)
def test_new_tokens_win_over_the_legacy_sheet(theme, scheme):
    """Both sheets load on every legacy page; the design system's value must be the effective
    one for every name tokens.css declares, in each theme and scheme."""
    declared = _token_values(theme, scheme)
    effective = _tokens(theme, scheme)
    losers = {k: (v, effective.get(k)) for k, v in declared.items() if effective.get(k) != v}
    assert not losers, f"{theme}/{scheme}: legacy overrides {losers}"
    # The legacy alias chain resolves through the new --paper, not the old one.
    assert effective["bg"] == declared["bg"] and effective["panel"] == declared["paper"]


def test_base_template_loads_legacy_css_before_the_design_system():
    order = _sheet_order()
    assert order.index("kestrel.css") < order.index("tokens.css") < order.index("components.css")


def test_tokens_define_every_scheme_and_scene_token():
    css = _TOKENS.read_text(encoding="utf-8")
    for s in ("teal", "plum", "graphite"):
        assert f':root:root:root[data-scheme="{s}"]' in css
    light = _token_values("light", "indigo")
    dark = _token_values("dark", "indigo")
    scene = ("sky-top sky-bot m1 m1s m2 m2s m3 m3s snow tree tree2 tree3 orb glow mist bird "
             "water water2 lite sand sand2 leaf aur1 aur2 z1 z2 z3 paper bad").split()
    for name in scene:
        assert name in light and name in dark, name
    assert light["brand"].upper() == "#4F5BD5" and dark["brand"].upper() == "#7B86F0"
    # Dark is genuinely different from light for the ground and the scene sky.
    assert light["bg"] != dark["bg"] and light["sky-bot"] != dark["sky-bot"]


# --------------------------------------------------------------------- components: tokens only
def test_components_css_has_no_color_literals():
    css = re.sub(r"/\*.*?\*/", "", _COMPONENTS.read_text(encoding="utf-8"), flags=re.S)
    assert not re.search(r"#[0-9A-Fa-f]{3,8}\b", css), "hex color in components.css"
    assert not re.search(r"\b(?:rgb|rgba|hsl|hsla|hwb|lab|lch|oklch|oklab)\(", css)
    # Named colors other than the structural keywords.
    assert not re.search(
        r":\s*(?:white|black|red|green|blue|gray|grey|orange|purple|yellow)\b", css
    )


def _page_sheets() -> list[Path]:
    css_dir = _STATIC / "css"
    sheets = sorted((css_dir / "pages").glob("*.css"))
    sheets += [css_dir / n for n in ("shell.css", "models.css", "sources.css", "calibrate.css",
                                      "retry.css")]
    return sheets


def test_every_page_stylesheet_has_no_color_literals():
    sheets = _page_sheets()
    assert len(sheets) >= 15  # the glob found the page sheets, not an empty directory
    for sheet in sheets:
        css = re.sub(r"/\*.*?\*/", "", sheet.read_text(encoding="utf-8"), flags=re.S)
        assert not re.search(r"#[0-9A-Fa-f]{3,8}\b", css), f"hex color in {sheet.name}"
        assert not re.search(r"\b(?:rgb|rgba|hsl|hsla|hwb|lab|lch|oklch|oklab)\(", css), sheet.name
        assert not re.search(
            r":\s*(?:white|black|red|green|blue|gray|grey|orange|purple|yellow)\b", css
        ), f"named color in {sheet.name}"


def test_components_css_covers_the_shared_components():
    css = _COMPONENTS.read_text(encoding="utf-8")
    for sel in (".k-btn", ".k-btn--primary", ".k-btn--quiet", ".k-btn--danger", ".k-btn--sm",
                ".k-chip", ".k-pill", ".k-card", ".k-sec-head", ".k-badge", ".k-help-col",
                ".k-hcard", ".k-morelink", ".k-flyout", ".k-slideover", ".k-modal", ".k-scrim",
                ".k-toast", ".k-toasts", ".k-switch", ".k-seg", ".k-stepper", ".k-input",
                ".k-select", ".k-steps", ".k-table", ".k-dz", ".k-scene", ".k-empty", ".k-win",
                ".k-side", ".k-layout", ".sc-night", ".sc-day", ":focus-visible",
                "prefers-reduced-motion", "scrollbar-width", "::-webkit-scrollbar"):
        assert sel in css, sel
    assert "@container (max-width: 1300px)" in css and "@container (max-width: 1000px)" in css


# ------------------------------------------------------------------------------------ scenes
_SCENE_DIR = _STATIC / "js" / "scenes"


def _count(name: str) -> int:
    return len(re.findall(r"\.push\(\{id:", (_SCENE_DIR / name).read_text(encoding="utf-8")))


def test_scene_files_register_fourteen_scenes_and_sixteen_touches():
    scenes = sum(_count(f) for f in ("scenes-base.js", "scenes-a.js", "scenes-b.js"))
    seasons = sum(_count(f) for f in ("seasons-a.js", "seasons-b.js"))
    assert scenes == 14, scenes
    assert seasons == 16, seasons
    for f in ("scene-band.js",):
        assert (_SCENE_DIR / f).is_file(), f


def test_scene_art_uses_tokens_not_brand_hex():
    """The art may use #fff in a mask; a brand or palette hex belongs in tokens.css."""
    for f in _SCENE_DIR.glob("*.js"):
        text = f.read_text(encoding="utf-8")
        hexes = set(re.findall(r"#[0-9A-Fa-f]{6}\b", text))
        assert not hexes, (f.name, sorted(hexes))


# --------------------------------------------------------------------------------- no em dash
def test_no_em_dash_in_templates_or_static_js_and_css():
    roots = [Path(paths.web_templates_dir()), _STATIC / "js", _STATIC / "css"]
    bad = [
        str(p.relative_to(_STATIC.parent))
        for root in roots
        for p in root.rglob("*")
        if p.is_file() and p.suffix in {".html", ".js", ".css"}
        and "—" in p.read_text(encoding="utf-8")
    ]
    assert not bad, f"em dash in {bad}"
