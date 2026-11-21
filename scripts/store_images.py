"""Make the store listing images for Kestrel Capture (issue #186, D-EXT-761).

    uv run --extra dev python scripts/store_images.py

Reads the screenshots the extension browser test saves (tests/test_extension_browser.py with
KESTREL_SHOTS=dist/extension-shots; dist/ is never committed) and writes, under
docs/handoff/extension-store/:

- screenshot-1280x800-1.png to -5.png and the same at 640x400 (Chrome: 1 to 5 at 1280x800 or
  640x400; Edge: up to 6 at 1280x800; AMO: 1280x800 recommended);
- promo-440x280.png (Chrome and Edge small promo tile);
- icon-128.png (Chrome store icon: 96x96 artwork inside 16 px of clear padding);
- edge-logo-300.png (Edge extension logo, 300x300).

Every screenshot shows invented data from the test's seed. Rendering uses the Playwright Chromium
the dev environment already has, and the bundled Manrope font, so nothing is fetched.
"""

from __future__ import annotations

import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "dist" / "extension-shots"  # D-EXT-776: screenshots are made, never committed
OUT = ROOT / "docs" / "handoff" / "extension-store"
FONT = ROOT / "extension" / "fonts" / "manrope-latin-wght-normal.woff2"
ICON = ROOT / "kestrel" / "web" / "static" / "icon.svg"

# (screenshot, headline, line). The headline says what the person gets; the line, how.
SLIDES = (
    (
        "known-role-light.png",
        "Everything you need to apply, by hand",
        "On a posting Kestrel knows: the score, why it fits, the gaps and your tailored documents.",
    ),
    (
        "popup-check-before-saving-light.png",
        "Fill in a role Kestrel couldn't read",
        "Open the posting yourself, click Read, check what will be saved, then Save.",
    ),
    (
        "board-badges-light.png",
        "See Kestrel's score on job boards",
        "A small badge next to postings Kestrel already knows. It never changes the page.",
    ),
    (
        "popup-not-a-job-posting-light.png",
        "Never past a check page",
        'A "verify you are human" page is refused. Finish the check yourself.',
    ),
    (
        "options-about-light.png",
        "Talks only to Kestrel on this computer",
        "Nothing goes to any other server. It never applies, fills or submits anything.",
    ),
)


def _data(path: Path, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _style() -> str:
    return (
        "@font-face{font-family:Manrope;font-weight:200 800;"
        f"src:url('{_data(FONT, 'font/woff2')}') format('woff2')}}"
        "*{box-sizing:border-box}body{margin:0;font-family:Manrope,sans-serif;color:#141A33}"
    )


def _slide(shot: Path, headline: str, line: str) -> str:
    return (
        f"<html><head><style>{_style()}"
        ".s{width:1280px;height:800px;display:flex;align-items:center;gap:64px;padding:0 80px;"
        "background:linear-gradient(180deg,#E1E8FF 0,#F5F7FC 520px)}"
        ".t{flex:1}.t img{width:56px;height:56px}.t h1{font-size:46px;line-height:1.12;"
        "margin:22px 0 16px;font-weight:800}.t p{font-size:22px;line-height:1.45;color:#454C69}"
        ".f{flex:none;max-height:680px;max-width:620px;border-radius:16px;"
        "box-shadow:0 24px 60px rgba(20,26,51,.18);border:1px solid #E3E7F1}"
        "</style></head><body><div class='s'><div class='t'>"
        f"<img src='{_data(ICON, 'image/svg+xml')}' alt=''><h1>{headline}</h1><p>{line}</p></div>"
        f"<img class='f' src='{_data(shot, 'image/png')}' alt=''></div></body></html>"
    )


def _promo() -> str:
    return (
        f"<html><head><style>{_style()}"
        ".p{width:440px;height:280px;display:flex;flex-direction:column;justify-content:center;"
        "gap:10px;padding:0 36px;background:linear-gradient(135deg,#4F5BD5,#3A44A8);color:#fff}"
        ".p img{width:64px;height:64px}.p b{font-size:30px;font-weight:800}"
        ".p span{font-size:16px;line-height:1.4;opacity:.92}"
        "</style></head><body><div class='p'>"
        f"<img src='{_data(ICON, 'image/svg+xml')}' alt=''><b>Kestrel Capture</b>"
        "<span>Send the posting you have open to Kestrel on your own computer.</span>"
        "</div></body></html>"
    )


def _icon(size: int, art: int) -> str:
    pad = (size - art) // 2
    return (
        f"<html><body style='margin:0;background:transparent'><div style='width:{size}px;"
        f"height:{size}px;padding:{pad}px'><img src='{_data(ICON, 'image/svg+xml')}' "
        f"width='{art}' height='{art}' alt=''></div></body></html>"
    )


def _browser(pw):
    try:
        return pw.chromium.launch()
    except Exception:  # noqa: BLE001 - the bundled build is missing; use the preinstalled one
        return pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")


def main() -> int:
    from playwright.sync_api import sync_playwright

    missing = [name for name, _h, _l in SLIDES if not (SHOTS / name).is_file()]
    if missing:
        raise SystemExit("run the extension browser test with "
                         f"KESTREL_SHOTS=dist/extension-shots first: {missing}")
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = _browser(pw)
        try:
            for n, (name, headline, line) in enumerate(SLIDES, start=1):
                for w, h, scale in ((1280, 800, 1), (640, 400, 0.5)):
                    page = browser.new_page(
                        viewport={"width": 1280, "height": 800}, device_scale_factor=scale
                    )
                    page.set_content(_slide(SHOTS / name, headline, line))
                    page.screenshot(path=str(OUT / f"screenshot-{w}x{h}-{n}.png"))
                    page.close()
            jobs = (
                ("promo-440x280.png", _promo(), 440, 280, False),
                ("icon-128.png", _icon(128, 96), 128, 128, True),
                ("edge-logo-300.png", _icon(300, 300), 300, 300, True),
            )
            for name, html, w, h, clear in jobs:
                page = browser.new_page(viewport={"width": w, "height": h})
                page.set_content(html)
                page.screenshot(path=str(OUT / name), omit_background=clear)
                page.close()
        finally:
            browser.close()
    for path in sorted(OUT.glob("*.png")):
        print(path.relative_to(ROOT))  # noqa: T201 - a script's own output
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
