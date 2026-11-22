"""Read a page's code the way it reads in the browser (D-INST-8).

The Content-Security-Policy moved every inline script and style into files under /static. Tests
that check what a page's code does read those files through here: each `<script src>` and
stylesheet link to a /static file (rendered as /static/...?v=..., or in a raw template as
{{ static_url('...') }}) is replaced by the file's text inside <script> or <style>, and each print
stylesheet link (/frame-css/<sha256>.css) by the stylesheet text, so a test's assertions about
the code are unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "kestrel" / "web" / "static"

_SCRIPT = re.compile(
    r"""<script src="(?:/static/([^"?]+)(?:\?v=[^"]*)?|\{\{ static_url\('([^']+)'\) \}\})"[^>]*>"""
    r"</script>")
_SHEET = re.compile(
    r"""<link rel="stylesheet"(?: id="[^"]*")? href="(?:/static/([^"?]+)(?:\?v=[^"]*)?|"""
    r"""\{\{ static_url\('([^']+)'\) \}\})">""")
_INLINED = ("js/k-", "js/partials/", "css/toast.css", "css/csp.css")
_FRAME = re.compile(r'<link rel="stylesheet" href="/frame-css/([0-9a-f]{64})\.css">')


def _text(rel: str) -> str:
    return (STATIC / rel).read_text(encoding="utf-8")


def inline_static(html: str) -> str:
    """`html` with the app's moved scripts and sheets inlined (vendored libraries and page
    scripts that were always files are left as links)."""

    def script(m: re.Match[str]) -> str:
        rel = m.group(1) or m.group(2)
        return f"<script>{_text(rel)}</script>" if rel.startswith(_INLINED) else m.group(0)

    def sheet(m: re.Match[str]) -> str:
        rel = m.group(1) or m.group(2)
        return f"<style>{_text(rel)}</style>" if rel.startswith(_INLINED) else m.group(0)

    def frame(m: re.Match[str]) -> str:
        # A print stylesheet this process served as /frame-css/<sha256>.css (D-SEC-59).
        from kestrel.web.csp import _FRAME_CSS

        css = _FRAME_CSS.get(m.group(1))
        return m.group(0) if css is None else f"<style>{css}</style>"

    return _FRAME.sub(frame, _SHEET.sub(sheet, _SCRIPT.sub(script, html)))


_U_CLASS = re.compile(r'(\sclass="[^"]*?)((?:\s+u-[a-z0-9-]+)+)(")')


def as_written(html: str) -> str:
    """inline_static, and each class attribute without the u-* classes that replaced a style
    attribute (static/css/csp.css), so `class="warnbox"` reads as it did. For tests that anchor
    on a class string; the classes themselves are tested with the policy (tests/test_csp*.py)."""
    text = inline_static(html)
    text = _U_CLASS.sub(lambda m: m.group(1) + m.group(3), text)
    return re.sub(r'\sclass="u-[a-z0-9-]+(?:\s+u-[a-z0-9-]+)*\s*"', "", text)
