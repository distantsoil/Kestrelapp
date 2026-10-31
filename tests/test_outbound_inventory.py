"""No hidden outbound call (D-REL-3, owner 2026-10-30): every web address in the shipped code is
listed, with its kind, in docs/OUTBOUND-CALLS.md, the source of the website's outbound-calls page,
and every host listed there is still in the code. A new address fails here until it is listed, so
the page cannot fall behind the code. Offline: reads files only."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs" / "OUTBOUND-CALLS.md"
KINDS = {"call", "link", "local", "reference", "build"}
SUFFIXES = {".py", ".js", ".html", ".css", ".yml", ".yaml", ".json", ".toml", ".txt", ".md",
            ".webmanifest", ".svg"}
# A host, where a {placeholder} (an f-string or template part) counts as one piece.
_URL = re.compile(r"(?i)\b(?:https?|wss?)://((?:\{[^}\n]*\}|[a-z0-9_.\-])+(?::\d+)?)")
_ENTRY = re.compile(r"^- `([^`]+)`: (\w+)\.", re.M)


def _normalize(host: str) -> str:
    return re.sub(r"\{[^}]*\}", "{*}", host.lower()).rstrip(".")


def _hosts_in_code() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted((ROOT / "kestrel").rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUFFIXES or "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for m in _URL.finditer(text):
            host = _normalize(m.group(1))
            if host:  # a bare "https://" prefix in a check has no host
                found.setdefault(host, []).append(str(path.relative_to(ROOT)))
    return found


def _inventory() -> dict[str, str]:
    text = INVENTORY.read_text(encoding="utf-8")
    index = text.split("## Host index", 1)[1]
    return {host: kind for host, kind in _ENTRY.findall(index)}


def test_every_address_in_the_code_is_in_the_outbound_inventory():
    listed = _inventory()
    missing = {h: files[:3] for h, files in _hosts_in_code().items() if h not in listed}
    assert not missing, (
        "New web addresses in kestrel/: add each to the host index in docs/OUTBOUND-CALLS.md "
        "with its kind (call, link, local, reference, build), and describe any call in the "
        f"tables above it: {missing}"
    )


def test_the_inventory_lists_nothing_the_code_no_longer_has_and_uses_known_kinds():
    in_code = _hosts_in_code()
    listed = _inventory()
    assert listed, "the host index is empty or its format changed"
    stale = sorted(h for h in listed if h not in in_code)
    assert not stale, f"listed in docs/OUTBOUND-CALLS.md but gone from the code: {stale}"
    odd = {h: k for h, k in listed.items() if k not in KINDS}
    assert not odd, f"unknown kinds in docs/OUTBOUND-CALLS.md: {odd}"


def test_the_guard_finds_a_new_address_and_reads_placeholders_as_one_part():
    """The guard itself: a literal URL is found, an f-string part reads as {*}."""
    sample = ('x = "https://new.example.org/path"; '
              'y = f"https://{slug}.jobs.personio.{tld or \'de\'}/xml"')
    hosts = {_normalize(m.group(1)) for m in _URL.finditer(sample)}
    assert hosts == {"new.example.org", "{*}.jobs.personio.{*}"}
