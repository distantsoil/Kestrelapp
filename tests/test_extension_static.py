"""The browser extension's shipped files keep their promises (issue #186, D-EXT-740 to D-EXT-769).

Invariants, each enforced by the browser from the manifest rather than by reading the code:
- Least privilege: exactly activeTab, scripting, contextMenus and storage; host access to
  127.0.0.1 and localhost only; no optional hosts, no externally reachable files, no messaging
  from web pages. Any added permission or host fails here.
- It talks only to Kestrel on this computer: the extension pages' Content Security Policy allows
  connections to itself and loopback only, and no remote script, so even a bug in the code
  cannot send to another host or load code from one.
- No LinkedIn; the retired orange is gone; the JavaScript parses.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

EXT = Path("extension")
PERMISSIONS = ["activeTab", "scripting", "contextMenus", "storage"]
HOSTS = ["http://127.0.0.1/*", "http://localhost/*"]
# Keys a Manifest V3 extension may carry here. A key that widens what the extension can reach
# (optional_host_permissions, web_accessible_resources, externally_connectable, sandbox,
# declarative_net_request, chrome_url_overrides ...) is not on the list, so adding one fails.
ALLOWED_KEYS = {
    "manifest_version",
    "name",
    "version",
    "minimum_chrome_version",
    "description",
    "permissions",
    "host_permissions",
    "background",
    "action",
    "icons",
    "options_ui",
    "content_scripts",
    "content_security_policy",
    "browser_specific_settings",
}
CSP_ALLOWED = {
    "default-src": ["'self'"],
    "script-src": ["'self'"],
    "object-src": ["'none'"],
    "base-uri": ["'none'"],
    "form-action": ["'none'"],
    "frame-ancestors": ["'none'"],
    "connect-src": ["'self'", "http://127.0.0.1:*", "http://localhost:*"],
    "img-src": ["'self'", "blob:"],
    "style-src": ["'self'"],
    "font-src": ["'self'"],
}
BOARD_HOSTS = {
    "greenhouse.io",
    "boards.greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "indeed.com",
    "glassdoor.com",
    "glassdoor.co.uk",
    "glassdoor.ca",
    "glassdoor.com.au",
    "glassdoor.ie",
    "glassdoor.co.in",
    "glassdoor.de",
    "glassdoor.fr",
}


def _manifest() -> dict:
    return json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))


def _built() -> list[tuple[str, dict]]:
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_extension", "scripts/build_extension.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    base = _manifest()
    return [
        ("source", base),
        ("chrome", mod.chrome_manifest(base)),
        ("firefox", mod.firefox_manifest(base)),
    ]


def _csp(policy: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for part in policy.split(";"):
        words = part.split()
        if words:
            out[words[0]] = words[1:]
    return out


@pytest.mark.parametrize("target,manifest", _built())
def test_permissions_are_exactly_the_four_and_hosts_are_loopback_only(target, manifest):
    assert manifest["manifest_version"] == 3
    assert set(manifest) <= ALLOWED_KEYS, (target, sorted(set(manifest) - ALLOWED_KEYS))
    assert manifest["permissions"] == PERMISSIONS, target
    assert manifest["host_permissions"] == HOSTS, target
    assert "tabs" not in manifest["permissions"] and "downloads" not in manifest["permissions"]


@pytest.mark.parametrize("target,manifest", _built())
def test_pages_may_connect_only_to_themselves_and_this_computer(target, manifest):
    policy = manifest["content_security_policy"]
    assert set(policy) == {"extension_pages"}, target
    assert _csp(policy["extension_pages"]) == CSP_ALLOWED, target


@pytest.mark.parametrize("target,manifest", _built())
def test_content_scripts_run_only_on_the_named_boards_never_linkedin(target, manifest):
    matches = [m for cs in manifest["content_scripts"] for m in cs["matches"]]
    hosts = set()
    for m in matches:
        found = re.fullmatch(r"\*://(\*\.)?([a-z0-9.-]+)/\*", m)
        assert found, m
        hosts.add(found.group(2))
    assert hosts == BOARD_HOSTS, target
    assert not any("linkedin" in m.lower() for m in matches)
    assert all(cs["js"] == ["content.js"] and "css" not in cs for cs in manifest["content_scripts"])


def test_firefox_build_has_an_id_an_event_page_and_the_data_declaration():
    firefox = dict(_built())["firefox"]
    gecko = firefox["browser_specific_settings"]["gecko"]
    assert gecko["id"] == "capture@kestrelapp.co"
    assert gecko["data_collection_permissions"] == {
        "required": ["browsingActivity", "websiteContent"]
    }
    assert firefox["background"] == {"scripts": ["api.js", "background.js"]}
    assert "minimum_chrome_version" not in firefox
    chrome = dict(_built())["chrome"]
    assert chrome["background"] == {"service_worker": "background.js"}
    assert "browser_specific_settings" not in chrome


def test_every_shipped_file_exists_and_nothing_else_ships():
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_extension", "scripts/build_extension.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for rel in mod.SHIPPED:
        assert (EXT / rel).is_file(), rel
    referenced = {"popup.html", "options.html", "background.js", "content.js"}
    manifest = _manifest()
    referenced |= set(manifest["icons"].values())
    assert referenced <= set(mod.SHIPPED)
    assert not any(rel.endswith(".md") for rel in mod.SHIPPED)


def test_pages_load_scripts_and_styles_only_from_the_package():
    for page in ("popup.html", "options.html"):
        html = (EXT / page).read_text(encoding="utf-8")
        for src in re.findall(r"<script\b[^>]*\bsrc=\"([^\"]+)\"", html):
            assert (EXT / src).is_file(), (page, src)
        assert not re.search(r"<script\b(?![^>]*\bsrc=)", html), page  # no inline script
        tags = re.findall(r"<[a-zA-Z][^>]*>", html)
        assert not [t for t in tags if re.search(r"\s(style|on[a-z]+)=", t)], page  # CSP
        assert "javascript:" not in html.lower(), page
        for href in re.findall(r"<link\b[^>]*\bhref=\"([^\"]+)\"", html):
            assert (EXT / href).is_file(), (page, href)


def test_the_retired_orange_is_gone():
    suffixes = {".js", ".html", ".css", ".json", ".md", ".svg"}
    offenders = [
        path
        for path in EXT.rglob("*")
        if path.is_file()
        and path.suffix in suffixes
        and "d4622a" in path.read_text(encoding="utf-8").lower()
    ]
    assert not offenders, offenders


@pytest.mark.parametrize(
    "name", ["api.js", "background.js", "content.js", "popup.js", "options.js"]
)
def test_the_javascript_parses(name):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available to parse-check the extension")
    proc = subprocess.run(
        [node, "--check", str(EXT / name)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
