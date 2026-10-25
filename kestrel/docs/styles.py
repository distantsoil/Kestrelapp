"""How each layout LOOKS, separate from what it contains (D-DOC-1). A doc_layout row says which
sections appear and in what order; this module says the visual treatment the template applies for
that layout, the default font, and whether the name and headline are centered.

Every font here exists on a stock Windows install (or ships with Office, with a Windows fallback
behind it in the stack) and renders in Edge, so the PDF looks the same on the operator's machine
as in the preview. The font name is chosen from this list and never from free text: the CSS stack
is written into the document's stylesheet, so an arbitrary string must never reach it.

ATS safety is unchanged by any style: single column, real text, no images for text, no tables
for layout (SPEC-MODULES 1.4)."""

from __future__ import annotations

import re

# Display name -> CSS font-family stack. Order is the order the selects offer them.
FONTS: dict[str, str] = {
    "Georgia": '"Georgia", "Times New Roman", serif',
    "Cambria": '"Cambria", "Georgia", serif',
    "Garamond": '"Garamond", "EB Garamond", "Georgia", serif',
    "Calibri": '"Calibri", "Carlito", "Arial", sans-serif',
    "Arial": '"Arial", "Helvetica", "Liberation Sans", sans-serif',
    "Segoe UI": '"Segoe UI", "Arial", sans-serif',
}

ALIGNS = ("left", "center")

# Per-application size and margins (the Tailor page's Look control). Presentation only, chosen
# from these lists and never from free text, because the values reach the stylesheet.
SIZES: dict[str, float] = {"smaller": 0.92, "default": 1.0, "larger": 1.08}
MARGINS: dict[str, str] = {"normal": "0.5in", "narrow": "0.35in"}

# The visual treatments the document template knows (CSS classes on <body>).
STYLES = ("classic", "executive", "condensed", "modern", "traditional", "clean", "compact")

# Accent color (D-DOC-4): headings, rules and markers in every style use the CSS variable
# --accent. The presets are professional, print-safe colors; a custom value must be #rrggbb and
# nothing else, because it is written into the document's stylesheet.
ACCENT_PRESETS: dict[str, str] = {
    "Navy": "#1F3A5F",
    "Slate": "#475569",
    "Forest": "#2F5D3A",
    "Burgundy": "#7A1F2B",
    "Charcoal": "#333333",
    "Teal": "#1F6F78",
    "Orange": "#D4622A",
}
_HEX = re.compile(r"#[0-9A-Fa-f]{6}")

# Shipped layout key -> its look. A layout the operator adds without an entry here reads classic.
LAYOUT_STYLES: dict[str, dict[str, str]] = {
    "executive": {"style": "executive", "font": "Cambria", "align": "center",
                  "accent": "#222222"},
    "condensed": {"style": "condensed", "font": "Calibri", "align": "left",
                  "accent": "#111111"},
    "technical": {"style": "modern", "font": "Segoe UI", "align": "left", "accent": "#D4622A"},
    "classic": {"style": "traditional", "font": "Garamond", "align": "center",
                "accent": "#222222"},
    "modern": {"style": "clean", "font": "Calibri", "align": "left", "accent": "#1F3A5F"},
    "compact": {"style": "compact", "font": "Arial", "align": "left", "accent": "#333333"},
    "capability": {"style": "classic", "font": "Georgia", "align": "left", "accent": "#222222"},
    "capability_short": {"style": "condensed", "font": "Arial", "align": "left",
                         "accent": "#111111"},
}
_FALLBACK = {"style": "classic", "font": "Georgia", "align": "left", "accent": "#222222"}
DEFAULT_ACCENT = _FALLBACK["accent"]


def valid_accent(value: object) -> str | None:
    """The accent as an uppercase #RRGGBB, or None for anything that is not exactly that."""
    if isinstance(value, str) and _HEX.fullmatch(value.strip()):
        return value.strip().upper()
    return None


def accent_from_params(params) -> tuple[str | None, str | None]:
    """(accent, error) from a form or query string with `accent` (a preset hex, "custom", or
    empty for the default) and `accent_custom` (the typed #rrggbb). Empty is (None, None): the
    default applies. "custom" with nothing typed, or a value that is not #rrggbb, is refused with
    a message and no accent, so nothing invalid is ever written."""
    choice = str(params.get("accent") or "").strip()
    if not choice:
        return None, None
    if choice == "custom":
        typed = str(params.get("accent_custom") or "").strip()
        if not typed:
            return None, "Type a six-digit hex color like #1F3A5F in the box beside it."
        if valid_accent(typed) is None:
            return None, "Use a six-digit hex color like #1F3A5F."
        return valid_accent(typed), None
    if valid_accent(choice) is None:
        return None, "Use a six-digit hex color like #1F3A5F."
    return valid_accent(choice), None


def layout_defaults(layout_key: str | None) -> dict[str, str]:
    """The look a layout has when the config names no font or alignment."""
    return dict(LAYOUT_STYLES.get(layout_key or "", _FALLBACK))


def resolve_style(
    layout_key: str | None, config: dict | None, saved: dict | None = None
) -> dict[str, str]:
    """The effective look for a render config: the layout's defaults, with the config's `font`
    and `header_align` applied when they name a known value. An unknown value keeps the layout
    default (a font is a presentation choice; it never changes which text is printed). The
    accent is the config's, else the master's saved one (`saved`, doc_master.look), else the
    layout's; only a valid #rrggbb is ever kept."""
    config = config or {}
    out = layout_defaults(layout_key)
    accent = valid_accent(config.get("accent")) or valid_accent((saved or {}).get("accent"))
    if accent:
        out["accent"] = accent
    # The render's own choice wins; else the Look saved on the document (Profile > Documents).
    font = config.get("font") or (saved or {}).get("font")
    if isinstance(font, str) and font in FONTS:
        out["font"] = font
    align = config.get("header_align") or (saved or {}).get("header_align")
    if isinstance(align, str) and align in ALIGNS:
        out["align"] = align
    out["font_css"] = FONTS[out["font"]]
    out["layout"] = layout_key or ""
    size = config.get("size") or (saved or {}).get("size")
    out["size"] = size if isinstance(size, str) and size in SIZES else "default"
    margins = config.get("margins") or (saved or {}).get("margins")
    out["margins"] = margins if isinstance(margins, str) and margins in MARGINS else "normal"
    return out
