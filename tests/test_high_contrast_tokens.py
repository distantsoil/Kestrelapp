"""High contrast text and focus tokens, including legacy cascade and OS preference."""
from __future__ import annotations

import pytest

from tests.test_ui_tokens import PAIRS, _effective, contrast


def test_legacy_ui_rules_use_theme_colors():
    import re

    from tests.test_ui_tokens import _CSS_DIR

    css = (_CSS_DIR / "kestrel.css").read_text(encoding="utf-8")
    for selector in (".saved", ".palette", ".step .ic"):
        rule = re.search(re.escape(selector) + r"\s*\{([^}]+)\}", css).group(1)
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", rule), (selector, rule)


@pytest.mark.parametrize("theme,os_dark", [("light", False), ("light", True),
                                         ("dark", False), ("dark", True),
                                         (None, False), (None, True)])
def test_high_contrast_text_and_focus_meet_aaa(theme, os_dark):
    t = _effective(theme, "contrast", os_dark)
    pairs = PAIRS + [("focus-ring", ("paper", "bg", "soft")),
                     ("brand-ink", ("brand-2",)),
                     ("accent-deep", ("warm", "accent-tint")),
                     ("action-fg", ("action",)), ("keep-fg", ("keep-tint",))]
    for fg, grounds in pairs:
        for bg in grounds:
            assert contrast(t[fg], t[bg]) >= 7, (theme, os_dark, fg, bg)
    assert t["line"] == t["ink"]
    assert t["soft"] == t["paper"]
    assert t["main-bg"] == t["bg"]
    assert t["paper"] == ("#000000" if theme == "dark" or (theme is None and os_dark)
                          else "#FFFFFF")


def _avatar_pair(variant: str, t: dict[str, str]) -> tuple[str, str]:
    """(foreground, background) the avatar `variant` ends up with under high contrast, cascading
    components.css the way the browser does (specificity, then source order)."""
    import re

    from tests.test_ui_tokens import _CSS_DIR

    css = re.sub(r"/\*.*?\*/", "", (_CSS_DIR / "components.css").read_text(encoding="utf-8"),
                 flags=re.S)
    found: list[tuple[int, int, str, str]] = []
    for order, m in enumerate(re.finditer(r"([^{}@]+)\{([^{}]*)\}", css)):
        for sel in m.group(1).split(","):
            sel = sel.strip()
            contrast_only = 'data-scheme="contrast"' in sel
            base = re.sub(r"^(:root)+\[data-scheme=\"contrast\"\]\s*", "", sel)
            if base not in (".k-avatar", f".k-avatar--{variant}"):
                continue
            weight = 1 + sel.count(":root") + sel.count("[") if contrast_only else 1
            weight += 1 if base != ".k-avatar" else 0
            for prop, val in re.findall(r"(?<![-\w])(background|color)\s*:\s*([^;]+)", m.group(2)):
                found.append((weight, order, prop, val.strip()))
    out: dict[str, str] = {}
    for _w, _o, prop, val in sorted(found):
        var = re.fullmatch(r"var\(--([\w-]+)\)", val)
        if var:
            out[prop] = t[var.group(1)]
    return out["color"], out["background"]


@pytest.mark.parametrize("theme,os_dark", [("light", False), ("dark", False), (None, True)])
@pytest.mark.parametrize(
    "variant", ["initial", "bird", "leaf", "star", "sun", "mountain", "compass"]
)
def test_high_contrast_avatar_glyph_is_legible_in_every_variant(variant, theme, os_dark):
    t = _effective(theme, "contrast", os_dark)
    fg, bg = _avatar_pair(variant, t)
    assert contrast(fg, bg) >= 4.5, (variant, theme, fg, bg)
