"""Render a resolved document to HTML and then to a PDF (SPEC-MODULES 1.4). The HTML is the single
source of truth for the ?preview route and the PDF — the preview is identical, not an approximation.

Playwright with print_background, prefer_css_page_size, and EXPLICITLY EMPTY header/footer so no
page number, URL, or date leaks. The browser is the Edge already on a Windows machine
(channel="msedge", nothing bundled: SPEC.md 2); where Edge is absent the launch ladder tries Chrome,
then Playwright's own Chromium (`playwright install chromium`, the macOS/Linux path), and
KESTREL_BROWSER_PATH names an explicit executable ahead of all three. After generation, pypdf strips
all document metadata so no generator string and no organization name can appear there (privacy,
SPEC.md 9), and counts pages for the page-count guard. The bullet text in the HTML is
doc_variant.text verbatim; nothing here rewrites it."""

from __future__ import annotations

import io
import os
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog
from jinja2 import Environment, FileSystemLoader, select_autoescape

from kestrel.docs.catalog import RenderDoc

log = structlog.get_logger()

BROWSER_PATH_ENV = "KESTREL_BROWSER_PATH"
INSTALL_HINT = "uv run playwright install chromium"


class BrowserUnavailable(RuntimeError):
    """No Chromium-based browser could be launched for PDF rendering. The message names every
    attempt and the remediation, so the UI's graceful error tells the operator what to do."""


def launch_plan() -> list[tuple[str, dict[str, Any]]]:
    """The ordered launch attempts as (label, chromium.launch kwargs). One place, so render_pdf
    and doctor's browser check can never disagree about what will be used."""
    plan: list[tuple[str, dict[str, Any]]] = []
    explicit = os.environ.get(BROWSER_PATH_ENV)
    if explicit:
        plan.append((f"{BROWSER_PATH_ENV} ({explicit})", {"executable_path": explicit}))
    plan += [
        ("Edge (msedge)", {"channel": "msedge"}),
        ("Chrome (chrome)", {"channel": "chrome"}),
        ("Playwright Chromium", {}),
    ]
    return plan


async def launch_browser(playwright: Any, *, headless: bool = True) -> Any:
    """Walk launch_plan() and return the first browser that starts. Raises BrowserUnavailable,
    naming each failure and the install command, when none does."""
    failures: list[str] = []
    for label, kwargs in launch_plan():
        try:
            browser = await playwright.chromium.launch(headless=headless, **kwargs)
        except Exception as exc:  # Playwright's Error for a missing channel; keep walking
            failures.append(f"{label}: {str(exc).splitlines()[0][:160]}")
            log.debug("browser_launch_attempt_failed", browser=label, error=str(exc)[:200])
            continue
        log.info("browser_launched", browser=label)
        return browser
    raise BrowserUnavailable(
        "No browser available for PDF rendering. Tried: " + "; ".join(failures)
        + f". Run `{INSTALL_HINT}` (macOS/Linux), install Microsoft Edge (Windows), or set "
        f"{BROWSER_PATH_ENV} to a Chromium-based browser executable."
    )


# ----------------------------------------------------------------- detection (doctor, no launch)
def _edge_candidates() -> list[Path]:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        return [
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
            Path(local) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        ]
    if sys.platform == "darwin":
        return [Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")]
    return [Path(p) for n in ("microsoft-edge", "microsoft-edge-stable")
            if (p := shutil.which(n))]


def _chrome_candidates() -> list[Path]:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        return [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe",
        ]
    if sys.platform == "darwin":
        return [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    return [Path(p) for n in ("google-chrome", "google-chrome-stable", "chromium",
                              "chromium-browser") if (p := shutil.which(n))]


def playwright_browsers_dir() -> Path:
    """Where `playwright install` puts its browsers (PLAYWRIGHT_BROWSERS_PATH, else the per-OS
    cache dir Playwright documents)."""
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override and override != "0":
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "ms-playwright"


def _expected_chromium_dirs() -> list[str]:
    """The directory names the INSTALLED Playwright will look for, from its own browsers.json.
    A Chromium from an older Playwright sits in the same cache under a different revision and is
    not launchable by this one, so a bare "any chromium-* dir" probe would be a false PASS."""
    try:
        import json

        import playwright

        meta = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
        entries = json.loads(meta.read_text(encoding="utf-8"))["browsers"]
    except Exception:  # unknown layout: fall back to the loose probe below
        return []
    names = {
        "chromium": "chromium-{rev}",
        "chromium-headless-shell": "chromium_headless_shell-{rev}",
    }
    return [names[b["name"]].format(rev=b["revision"]) for b in entries
            if b.get("name") in names and b.get("revision")]


def _bundled_chromium() -> Path | None:
    root = playwright_browsers_dir()
    expected = _expected_chromium_dirs()
    if expected:
        return next((root / d for d in expected if (root / d).is_dir()), None)
    try:
        hits = sorted(p for p in root.iterdir()
                      if p.is_dir() and p.name.startswith(("chromium-", "chromium_headless")))
    except OSError:
        return None
    return hits[-1] if hits else None


def browser_path_warning(exists: Callable[[Path], bool] = Path.is_file) -> str | None:
    """The warning for a KESTREL_BROWSER_PATH that names nothing on disk: the ladder falls
    through to Edge, Chrome and Playwright's Chromium exactly as launch_browser does, and doctor
    shows this text so a typo in the variable is not mistaken for a missing browser."""
    explicit = os.environ.get(BROWSER_PATH_ENV)
    if explicit and not exists(Path(explicit)):
        return (
            f"{BROWSER_PATH_ENV} is set to {explicit!r} but no file is there; it is ignored and "
            "the usual browsers are tried instead. Fix the path or unset the variable."
        )
    return None


def detect_browser(exists: Callable[[Path], bool] = Path.is_file) -> tuple[str, str] | None:
    """(label, path) of the browser the launch ladder is expected to use, from filesystem probes
    only (no launch, so doctor stays fast). None when nothing is found. The order mirrors
    launch_plan(); `exists` is injectable for tests. A KESTREL_BROWSER_PATH that points at
    nothing is logged (browser_path_warning) and the probe falls through, as the launch does."""
    explicit = os.environ.get(BROWSER_PATH_ENV)
    if explicit:
        if exists(Path(explicit)):
            return (BROWSER_PATH_ENV, explicit)
        log.warning("browser_path_env_missing", path=explicit)
    for label, candidates in (("Edge (msedge)", _edge_candidates()),
                              ("Chrome (chrome)", _chrome_candidates())):
        for p in candidates:
            if exists(p):
                return label, str(p)
    bundled = _bundled_chromium()
    if bundled is not None:
        return "Playwright Chromium", str(bundled)
    return None


def browser_remediation() -> str:
    """What to do when detect_browser() finds nothing, per platform."""
    if sys.platform == "win32":
        return (f"Install Microsoft Edge (ships with Windows) or run `{INSTALL_HINT}`; "
                f"or set {BROWSER_PATH_ENV} to a Chromium-based browser executable.")
    return (f"Run `{INSTALL_HINT}`, or set {BROWSER_PATH_ENV} to a Chromium-based browser "
            "executable (Edge or Chrome are used automatically when installed).")


@lru_cache(maxsize=1)
def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(_templates_dir()),
        autoescape=select_autoescape(["html"]),
    )


def _templates_dir() -> str:
    from importlib.resources import files

    return str(files("kestrel.docs") / "templates")


def render_html(doc: RenderDoc) -> str:
    """Semantic, ATS-safe HTML. Each bullet carries data-vk so the byte-for-byte guarantee is
    checkable: the element text equals the doc_variant.text it came from. The look (style class,
    font, header alignment, accent color) comes from doc.style, which only ever holds whitelisted
    values (kestrel.docs.styles), so the font stack and the #RRGGBB accent are safe to write into
    the stylesheet."""
    from kestrel.docs.styles import (
        ALIGNS,
        DEFAULT_ACCENT,
        FONTS,
        STYLES,
        layout_defaults,
        resolve_style,
        valid_accent,
    )

    style = dict(doc.style or resolve_style(None, None))
    # Re-derive from the whitelists whatever reaches the stylesheet or a class name, so nothing
    # outside them can be written there even if a caller built `style` by hand.
    style["style"] = style.get("style") if style.get("style") in STYLES else "classic"
    style["align"] = style.get("align") if style.get("align") in ALIGNS else "left"
    style["font"] = style.get("font") if style.get("font") in FONTS else "Georgia"
    style["font_css"] = FONTS[style["font"]]
    style["accent"] = (valid_accent(style.get("accent"))
                       or layout_defaults(style.get("layout")).get("accent", DEFAULT_ACCENT))
    return _env().get_template("document.html").render(
        header=doc.header, blocks=doc.blocks, style=style
    )


@dataclass
class RenderedPdf:
    pdf: bytes
    page_count: int


async def render_pdf(html: str) -> RenderedPdf:
    """HTML -> PDF via the first launchable browser (launch_plan), then scrub metadata and count
    pages. Runs on the caller loop."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await launch_browser(p)
        try:
            page = await browser.new_page()
            await page.set_content(html, wait_until="load")
            raw = await page.pdf(
                print_background=True,
                prefer_css_page_size=True,
                display_header_footer=False,  # no default header/footer
                header_template="",  # explicitly empty (belt and suspenders)
                footer_template="",
            )
        finally:
            await browser.close()
    scrubbed, pages = _scrub_and_count(raw)
    return RenderedPdf(pdf=scrubbed, page_count=pages)


def _scrub_and_count(pdf_bytes: bytes) -> tuple[bytes, int]:
    """Remove ALL document metadata (no generator, no org name can hide there) and return the page
    count. Uses pypdf; the content streams (the selectable text) are untouched."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    # A fresh writer does not copy Chromium's /Info, so its Producer/Creator/Title (which could
    # hold the org name) are already gone; explicitly blank the generator strings the writer sets
    # itself so no tool name leaks either (privacy, SPEC.md 9).
    writer.add_metadata({"/Producer": "", "/Creator": "", "/Author": "", "/Title": ""})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), len(reader.pages)


_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


def _slug(text: str | None, *, default: str) -> str:
    """Strip everything but letters and digits WITHOUT re-casing: the operator's surname is theirs
    ("McRae" must not become "Mcrae"), and an org's own casing is kept the same way."""
    s = _SLUG_RE.sub("", text or "")
    return s or default


def filename_for(header: dict, org: str | None, role: str | None) -> str:
    """{LastName}_{Org}_{RoleSlug}.pdf (SPEC-MODULES 1.4)."""
    name = (header.get("name") or "").strip()
    last = name.split()[-1] if name else "Document"
    return (
        f"{_slug(last, default='Document')}_{_slug(org, default='Org')}"
        f"_{_slug(role, default='Role')}.pdf"
    )


def bullets_in_html(html: str) -> list[tuple[str, str]]:
    """Extract (variant_key, text) for every rendered bullet, so a test can assert each equals a
    doc_variant.text row. Uses bs4 to decode entities back to the source text."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    return [(li.get("data-vk"), li.get_text()) for li in soup.select("li[data-vk]")]
