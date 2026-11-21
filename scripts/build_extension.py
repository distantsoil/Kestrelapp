"""Build the Kestrel Capture browser extension for each store (issue #186, D-EXT-750).

    uv run --extra dev python scripts/build_extension.py            # folders and zips
    uv run --extra dev python scripts/build_extension.py --icons    # also redraw the PNG icons

Writes dist/extension/chrome/ (Chrome, Edge, Brave, Arc) and dist/extension/firefox/, each with
its own manifest, plus kestrel-capture-<version>-<browser>.zip beside them. The source in
extension/ is copied as is: no bundler, no minifier, so the store reviewers read the same code
that is in the repository and Firefox needs no separate source upload. Only the files in SHIPPED
go into a package; docs and store notes stay out.

--icons redraws extension/icons/*.png from kestrel/web/static/icon.svg with the Playwright
Chromium the dev environment already has. The PNGs are committed, so a normal build needs no
browser.
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "extension"
DIST = ROOT / "dist" / "extension"
ICON_SIZES = (16, 32, 48, 128)
SHIPPED = (
    "manifest.json",
    "api.js",
    "background.js",
    "content.js",
    "popup.html",
    "popup.js",
    "popup.css",
    "options.html",
    "options.js",
    "options.css",
    "theme.css",
    "icon.svg",
    "fonts/manrope-latin-wght-normal.woff2",
    "fonts/OFL.txt",
    *(f"icons/icon-{n}.png" for n in ICON_SIZES),
)
GECKO_ID = "capture@kestrelapp.co"
FIREFOX_MIN = "140.0"  # the first desktop release with the built-in data consent (D-EXT-752)


def chrome_manifest(base: dict) -> dict:
    return copy.deepcopy(base)


def firefox_manifest(base: dict) -> dict:
    """Firefox runs MV3 backgrounds as event pages (background.scripts), not service workers,
    and AMO needs an add-on ID and the data collection declaration."""
    m = copy.deepcopy(base)
    m.pop("minimum_chrome_version", None)
    m["background"] = {"scripts": ["api.js", "background.js"]}
    m["browser_specific_settings"] = {
        "gecko": {
            "id": GECKO_ID,
            "strict_min_version": FIREFOX_MIN,
            # Page text goes to the Kestrel app on this computer when the person clicks. The
            # owner confirms this answer on submission (docs/handoff/extension-store-submission.md).
            # browsingActivity: opening the popup sends the open tab's address (D-EXT-773).
            "data_collection_permissions": {"required": ["browsingActivity", "websiteContent"]},
        }
    }
    return m


def draw_icons() -> None:
    from playwright.sync_api import sync_playwright

    svg = (ROOT / "kestrel" / "web" / "static" / "icon.svg").read_text(encoding="utf-8")
    out = SRC / "icons"
    out.mkdir(exist_ok=True)
    with sync_playwright() as pw:
        fallback = Path("/opt/pw-browsers/chromium")  # a cloud container's preinstalled build
        try:
            browser = pw.chromium.launch()
        except Exception:  # noqa: BLE001 - the bundled build is missing; try the fallback once
            if not fallback.exists():
                raise
            browser = pw.chromium.launch(executable_path=str(fallback))
        try:
            for n in ICON_SIZES:
                page = browser.new_page(viewport={"width": n, "height": n}, device_scale_factor=1)
                page.set_content(
                    "<html><body style='margin:0;background:transparent'>"
                    f"<div style='width:{n}px;height:{n}px'>{svg}</div></body></html>"
                )
                page.eval_on_selector(
                    "svg",
                    f"s => {{ s.setAttribute('width','{n}'); s.setAttribute('height','{n}'); }}",
                )
                page.locator("svg").screenshot(
                    path=str(out / f"icon-{n}.png"), omit_background=True
                )
                page.close()
        finally:
            browser.close()


def build(target: str, manifest: dict) -> Path:
    folder = DIST / target
    if folder.exists():
        shutil.rmtree(folder)
    for rel in SHIPPED:
        if rel == "manifest.json":
            continue
        src = SRC / rel
        if not src.is_file():
            raise SystemExit(f"missing {src.relative_to(ROOT)}; run with --icons for the PNGs")
        dest = folder / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    archive = DIST / f"kestrel-capture-{manifest['version']}-{target}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(folder).as_posix())
    return archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--icons", action="store_true", help="redraw the PNG icons first")
    args = parser.parse_args(argv)
    if args.icons:
        draw_icons()
    base = json.loads((SRC / "manifest.json").read_text(encoding="utf-8"))
    DIST.mkdir(parents=True, exist_ok=True)
    for target, manifest in (
        ("chrome", chrome_manifest(base)),
        ("firefox", firefox_manifest(base)),
    ):
        archive = build(target, manifest)
        sys.stdout.write(f"{target}: {archive.relative_to(ROOT)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
