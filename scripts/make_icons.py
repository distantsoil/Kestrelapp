"""Generate every raster icon Kestrel ships, from the two SVG marks in docs/images/.

Run: `.venv/Scripts/python.exe scripts/make_icons.py`

The marks are the source of truth and are NEVER redrawn here. Playwright renders them in Edge (the
same engine the app's own PDF pipeline uses, and already a dependency) and the PNGs are assembled
with Pillow. Redrawing the bird in PIL would be a redesign that silently drifts from the SVG the
operator actually approved, which is why this rasterizes instead.

Outputs:
  packaging/kestrel.ico            the installer and exe icon (256/128/64/48/32/16)
  kestrel/resources/icons/kestrel.ico  the same bytes, shipped as package data: the Windows
                                   notification IconUri (shell.register_notification_icon)
  packaging/kestrel.icns           the macOS app bundle icon (16 through 512, with @2x variants)
  kestrel/resources/icons/tray-*.png   the three tray states
  kestrel/web/static/icon-512.png  PWA install icon, and the AppImage's desktop icon
  kestrel/web/static/favicon.ico   browser tab (32/16)

Idempotent: re-running produces the same bytes for the same SVG input.

`scripts/make_icons.py --icns` regenerates only packaging/kestrel.icns from the already-rendered
kestrel/web/static/icon-512.png. It needs Pillow only (no Edge), so a Linux or macOS release
runner can rebuild it; on macOS it prefers Apple's own `iconutil` from an .iconset of PNGs.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARK = ROOT / "docs" / "images" / "kestrel-mark.svg"
MARK_LIGHT = ROOT / "docs" / "images" / "kestrel-mark-light.svg"

ICO_SIZES = (256, 128, 64, 48, 32, 16)
# Apple's iconset naming: icon_<n>x<n>.png and icon_<n>x<n>@2x.png. The largest source we render is
# 512, so icon_512x512@2x (1024) is an upscale; macOS only reads it for 1024-point previews.
ICNS_SIZES = (16, 32, 128, 256, 512)
TRAY_SIZE = 64
ATTENTION_RED = "#B3261E"  # matches the existing working_att accent in desktop/shell.py


def write_icns(png_path: Path, out: Path) -> str:
    """Write a macOS .icns from one square PNG (512 or larger). Returns which tool produced it.

    On macOS, `iconutil` is authoritative: it is what Xcode uses and it never produces an icon
    Finder refuses. Everywhere else, Pillow writes the ICNS container itself (supported since
    Pillow 8.1; verified with Pillow 12 on Linux), which is what the Linux CI job and a developer
    without a Mac use. Both paths take the same input and produce an icon macOS accepts."""
    from PIL import Image

    src = Image.open(png_path).convert("RGBA")
    if src.size[0] != src.size[1]:
        raise ValueError(f"{png_path} is not square: {src.size}")
    out.parent.mkdir(parents=True, exist_ok=True)

    if sys.platform == "darwin":
        with tempfile.TemporaryDirectory() as tmp:
            iconset = Path(tmp) / "kestrel.iconset"
            iconset.mkdir()
            for n in ICNS_SIZES:
                src.resize((n, n), Image.LANCZOS).save(iconset / f"icon_{n}x{n}.png", "PNG")
                src.resize((2 * n, 2 * n), Image.LANCZOS).save(
                    iconset / f"icon_{n}x{n}@2x.png", "PNG"
                )
            subprocess.run(
                ["iconutil", "--convert", "icns", "--output", str(out), str(iconset)],
                check=True,
            )
        return "iconutil"

    # Pillow picks the embedded sizes from the source: give it the largest square we have and it
    # writes 16 through 512 plus the @2x variants in one container.
    big = src if src.size[0] >= 1024 else src.resize((1024, 1024), Image.LANCZOS)
    big.save(out, format="ICNS")
    return "pillow"


def _render_svg(svg_path: Path, size: int) -> bytes:
    """Rasterize one SVG at `size` square, transparent background, via Edge."""
    from playwright.sync_api import sync_playwright

    svg = svg_path.read_text(encoding="utf-8")
    html = (
        "<!doctype html><html><head><style>"
        "html,body{margin:0;padding:0;background:transparent}"
        f"svg{{display:block;width:{size}px;height:{size}px}}"
        "</style></head><body>" + svg + "</body></html>"
    )
    with sync_playwright() as p:
        browser = _launch(p)
        try:
            page = browser.new_page(viewport={"width": size, "height": size})
            page.set_content(html)
            return page.screenshot(omit_background=True, type="png")
        finally:
            browser.close()



def _launch(p):
    """Edge when present (the engine the app's own PDF pipeline drives), else Chrome, else the
    Chromium Playwright ships: the same rendering engine, so a release runner or a cloud session
    without Edge produces the same bytes as the operator's machine."""
    explicit = os.environ.get("KESTREL_BROWSER_PATH")  # the PDF pipeline's override, same name
    if explicit:
        return p.chromium.launch(executable_path=explicit, headless=True)
    for channel in ("msedge", "chrome"):
        try:
            return p.chromium.launch(channel=channel, headless=True)
        except Exception:  # that channel is not installed here; try the next
            continue
    return p.chromium.launch(headless=True)

def _render_all(svg_path: Path, sizes: tuple[int, ...]) -> dict[int, bytes]:
    """One browser launch for all sizes: launching Edge per size is the slow part."""
    from playwright.sync_api import sync_playwright

    svg = svg_path.read_text(encoding="utf-8")
    out: dict[int, bytes] = {}
    with sync_playwright() as p:
        browser = _launch(p)
        try:
            for size in sizes:
                html = (
                    "<!doctype html><html><head><style>"
                    "html,body{margin:0;padding:0;background:transparent}"
                    f"svg{{display:block;width:{size}px;height:{size}px}}"
                    "</style></head><body>" + svg + "</body></html>"
                )
                page = browser.new_page(viewport={"width": size, "height": size})
                page.set_content(html)
                out[size] = page.screenshot(omit_background=True, type="png")
                page.close()
        finally:
            browser.close()
    return out


def _image(png_bytes: bytes):
    import io

    from PIL import Image

    return Image.open(io.BytesIO(png_bytes)).convert("RGBA")


def build() -> int:
    from PIL import ImageDraw

    if not MARK.exists() or not MARK_LIGHT.exists():
        print(f"missing marks: expected {MARK} and {MARK_LIGHT}", file=sys.stderr)
        return 1

    print("rendering the mark ...")
    dark = _render_all(MARK, ICO_SIZES + (512, TRAY_SIZE))
    light = _render_all(MARK_LIGHT, (TRAY_SIZE,))

    # --- the Windows app icon -------------------------------------------------------------
    ico_path = ROOT / "packaging" / "kestrel.ico"
    ico_path.parent.mkdir(parents=True, exist_ok=True)
    base = _image(dark[max(ICO_SIZES)])
    base.save(ico_path, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    print(f"  {ico_path.relative_to(ROOT)}  {', '.join(str(s) for s in ICO_SIZES)}")
    shipped_ico = ROOT / "kestrel" / "resources" / "icons" / "kestrel.ico"
    shipped_ico.parent.mkdir(parents=True, exist_ok=True)
    shipped_ico.write_bytes(ico_path.read_bytes())
    print(f"  {shipped_ico.relative_to(ROOT)}  (copy)")

    # --- the macOS app icon --------------------------------------------------------------
    icns_path = ROOT / "packaging" / "kestrel.icns"
    with tempfile.TemporaryDirectory() as tmp:
        src512 = Path(tmp) / "icon-512.png"
        src512.write_bytes(dark[512])
        tool = write_icns(src512, icns_path)
    print(f"  {icns_path.relative_to(ROOT)}  ({tool})")

    # --- tray states ----------------------------------------------------------------------
    # idle = the outline mark, working = the solid mark, working_att = solid plus a red dot.
    # The dot is the only drawn element, and it is an overlay rather than a redraw of the bird.
    icons_dir = ROOT / "kestrel" / "resources" / "icons"
    icons_dir.mkdir(parents=True, exist_ok=True)

    idle = _image(light[TRAY_SIZE])
    working = _image(dark[TRAY_SIZE])
    att = working.copy()
    d = ImageDraw.Draw(att)
    r = TRAY_SIZE // 4
    d.ellipse((TRAY_SIZE - r - 2, 2, TRAY_SIZE - 2, r + 2), fill=ATTENTION_RED)

    for name, img in (("idle", idle), ("working", working), ("working_att", att)):
        p = icons_dir / f"tray-{name}.png"
        img.save(p, format="PNG")
        print(f"  {p.relative_to(ROOT)}")

    # --- web -------------------------------------------------------------------------------
    static = ROOT / "kestrel" / "web" / "static"
    static.mkdir(parents=True, exist_ok=True)
    pwa = static / "icon-512.png"
    _image(dark[512]).save(pwa, format="PNG")
    print(f"  {pwa.relative_to(ROOT)}")

    favicon = static / "favicon.ico"
    _image(dark[32]).save(favicon, format="ICO", sizes=[(32, 32), (16, 16)])
    print(f"  {favicon.relative_to(ROOT)}")

    # The PWA manifest wants a maskable PNG alongside the SVG; the mark already has its own
    # rounded tile with generous padding, so the same raster serves both purposes.
    # Overwrite unconditionally. manifest.webmanifest lists this FIRST with sizes "any", which a
    # PWA installer prefers over any fixed raster, so a stale placeholder here would beat every
    # other icon this script generates.
    svg_copy = static / "icon.svg"
    svg_copy.write_text(MARK.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"  {svg_copy.relative_to(ROOT)}")

    print("done.")
    return 0


def build_icns_only() -> int:
    """`--icns`: rebuild packaging/kestrel.icns from the committed icon-512.png, no browser."""
    src = ROOT / "kestrel" / "web" / "static" / "icon-512.png"
    if not src.exists():
        print(f"missing {src}; run the full build first", file=sys.stderr)
        return 1
    out = ROOT / "packaging" / "kestrel.icns"
    tool = write_icns(src, out)
    print(f"  {out.relative_to(ROOT)}  ({tool})")
    return 0


if __name__ == "__main__":
    raise SystemExit(build_icns_only() if "--icns" in sys.argv[1:] else build())
