"""Render packaging/macos/dmg-background.svg to the 1x and @2x PNGs the disk image uses (#198).

    uv run python scripts/make_dmg_background.py

A developer tool: run it after editing the SVG and commit the two PNGs. The macOS build never
runs it (the PNGs are committed), so the release needs no browser.
"""

from __future__ import annotations

import base64
import os
import re
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MACOS = ROOT / "packaging" / "macos"
SVG = MACOS / "dmg-background.svg"
MARK = ROOT / "docs" / "images" / "kestrel-mark.svg"
TOKENS = ROOT / "kestrel" / "web" / "static" / "css" / "tokens.css"
WIDTH, HEIGHT = 660, 400


def light_tokens(css: str) -> dict[str, str]:
    """The `--name:#RRGGBB` colors of the first (light theme) `:root` block of tokens.css."""
    start = css.index(":root:root:root {")
    block = css[start : css.index("}", start)]
    return {
        name: value.upper()
        for name, value in re.findall(r"--([a-z0-9-]+):(#[0-9A-Fa-f]{6})\b", block)
    }


def with_tokens(svg: str, tokens: dict[str, str]) -> str:
    """The SVG with every var(--name) replaced by its light-theme value; an unknown name fails."""

    def value(match: re.Match[str]) -> str:
        name = match[1]
        if name not in tokens:
            raise KeyError(f"dmg-background.svg uses --{name}, which tokens.css does not define")
        return tokens[name]

    return re.sub(r"var\(--([a-z0-9-]+)\)", value, svg)


def main() -> int:
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        # An SVG drawn as an image cannot load another file, so the mark goes in as a data URI.
        mark = "data:image/svg+xml;base64," + base64.b64encode(MARK.read_bytes()).decode("ascii")
        tokens = light_tokens(TOKENS.read_text(encoding="utf-8"))
        text = with_tokens(SVG.read_text(encoding="utf-8"), tokens)
        text = text.replace('"kestrel-mark.svg"', f'"{mark}"')
        (work / SVG.name).write_text(text, encoding="utf-8")
        page_html = work / "page.html"
        page_html.write_text(
            '<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" '
            'href="page.css"></head><body><img src="dmg-background.svg" alt=""></body></html>',
            encoding="utf-8",
        )
        (work / "page.css").write_text(
            f"html,body{{margin:0;padding:0}}img{{display:block;width:{WIDTH}px;"
            f"height:{HEIGHT}px}}",
            encoding="utf-8",
        )
        with sync_playwright() as pw:
            # PLAYWRIGHT_CHROMIUM names a browser binary when the bundled one is not installed.
            chromium = os.environ.get("PLAYWRIGHT_CHROMIUM") or None
            browser = pw.chromium.launch(executable_path=chromium)
            for scale, name in ((1, "dmg-background.png"), (2, "dmg-background@2x.png")):
                page = browser.new_page(
                    viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=scale
                )
                page.goto(page_html.as_uri())
                page.wait_for_load_state("networkidle")
                page.screenshot(
                    path=str(MACOS / name),
                    clip={
                        "x": 0,
                        "y": 0,
                        "width": WIDTH,
                        "height": HEIGHT,
                    },
                )
                page.close()
            browser.close()
    print("wrote packaging/macos/dmg-background.png and dmg-background@2x.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
