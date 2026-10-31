"""Static files are versioned and cached for good within a build (D-UI-209). A fresh install's
new file dates made the window re-check every stylesheet and script with the server on each page
change, which lengthened every page change. Offline: no network, no model."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from kestrel import paths
from kestrel.web import deps
from tests.test_ui_shell import _client, _seed


def test_no_template_points_at_a_static_file_without_its_version():
    bare = []
    for f in Path(paths.web_templates_dir()).rglob("*.html"):
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"""(?:src|srcset|href)=["']/static/""", text):
            bare.append(f"{f.name}: {m.group(0)}")
    assert not bare, bare


def test_pages_ask_for_versioned_files_and_those_are_cached_for_good():
    asyncio.run(_seed())
    token = deps.static_token()
    assert re.fullmatch(r"[0-9a-f]{12}", token)
    with _client() as c:
        page = c.get("/about").text
        urls = re.findall(r'(?:src|href)="(/static/[^"]+)"', page)
        assert urls and all(u.endswith(f"?v={token}") for u in urls), urls
        versioned = c.get(urls[0])
        assert versioned.status_code == 200
        assert "immutable" in versioned.headers["cache-control"]
        plain = c.get(urls[0].split("?")[0])
        assert plain.status_code == 200
        assert "immutable" not in plain.headers.get("cache-control", "")
        # An older build's version (a LAN tab open across an upgrade) is not cached for good.
        old = c.get(urls[0].split("?")[0] + "?v=000000000000")
        assert "immutable" not in old.headers.get("cache-control", "")


def test_the_offline_cache_is_named_for_this_build():
    asyncio.run(_seed())
    token = deps.static_token()
    with _client() as c:
        sw = c.get("/sw.js").text
    assert f"'kestrel-{token}'" in sw
    assert f'"/static/css/kestrel.css?v={token}"' in sw
    assert "__TOKEN__" not in sw and "__ASSETS__" not in sw


def test_the_interface_font_is_declared_and_preloaded():
    """D-UI-127: tokens.css declares Manrope from the shipped files; base.html preloads it."""
    css = (Path(paths.web_static_dir()) / "css" / "tokens.css").read_text(encoding="utf-8")
    assert css.count("@font-face") == 2 and 'font-family: "Manrope"' in css
    assert "../fonts/manrope/manrope-latin-wght-normal.woff2" in css
    base = (Path(paths.web_templates_dir()) / "base.html").read_text(encoding="utf-8")
    assert "fonts/manrope/manrope-latin-wght-normal.woff2" in base and 'as="font"' in base
