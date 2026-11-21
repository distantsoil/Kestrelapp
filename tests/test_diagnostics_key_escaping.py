"""D-PRIV-797: a saved key is masked in a diagnostics bundle whatever its characters and however
many times the line around it was JSON-escaped.

INVARIANT: a saved key never appears in plaintext in a diagnostics bundle or a log, whatever its
characters and however many times the line around it is JSON-escaped.

The first scrubber matched the raw value and its once-escaped form. A key holding a backslash or a
quote, on a line that carries JSON inside JSON, is escaped twice and matched neither; a non-ASCII
key written as `\\uXXXX` matched neither as well. Each shape below is saved, an older log is seeded
with it bare, in a Bearer header, nested once and nested three times, and the bundle is built
through the route the desktop app uses. Invented keys; a migrated SQLite file; no network."""

from __future__ import annotations

import asyncio
import html
import json
import re
import urllib.parse
import zipfile

import pytest
from fastapi.testclient import TestClient

from kestrel import paths
from kestrel.config import USE_SYSTEM_KEYS_ENV
from kestrel.services import diagnostics
from tests.test_diagnostics_key_leak import _prepare, downloads  # noqa: F401  (the fixture)

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

REDACTED = "<redacted>"
# The random middle every invented key below shares: it must be nowhere in the bundle.
MIDDLE = "Zq7Xw2Vb9Nc4Md8Lk3Jh6Gf1Rt5Yu8Io"
SHAPES = {
    "backslash": "corp\\gateway\\" + MIDDLE,
    "quote": 'gw"' + MIDDLE + '1Pa4"x',
    "non-ascii": "clé-" + MIDDLE + "-ü\U0001f511",
    "plain": "gw-" + MIDDLE,
}
ENV_NAME = "INVENTED_GATEWAY_API_KEY"
MARKERS = ("provider_read", "Authorization: Bearer", '"dump"', '"nested3"', '"escaped_ascii"')


def _nest(key: str, depth: int, *, ensure_ascii: bool = False) -> str:
    """The key inside `depth` layers of JSON, each layer a string value of the next."""
    text = key
    for _ in range(depth):
        text = json.dumps({"headers": {"x": text}}, ensure_ascii=ensure_ascii)
    return text


def _lines(key: str) -> list[str]:
    return [
        json.dumps({"event": "provider_read", "key": key}, ensure_ascii=False),
        f"GET https://example.invalid/v1/key Authorization: Bearer {key}",
        json.dumps({"event": "dump", "raw": _nest(key, 1)}, ensure_ascii=False),
        json.dumps({"event": "nested3", "raw": _nest(key, 3)}, ensure_ascii=False),
        json.dumps({"event": "escaped_ascii", "raw": _nest(key, 2, ensure_ascii=True)}),
    ]


def _spellings(key: str) -> set[str]:
    """The key as each layer of escaping writes it, down to four layers, both JSON styles."""
    out = {key, MIDDLE}
    for ensure_ascii in (False, True):
        text = key
        for _ in range(4):
            text = json.dumps(text, ensure_ascii=ensure_ascii)[1:-1]
            out.add(text)
    return out


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_a_key_with_escapable_characters_is_masked_at_every_depth(
    shape, downloads, monkeypatch  # noqa: F811
) -> None:
    from kestrel.web.app import build_app

    key = SHAPES[shape]
    asyncio.run(_prepare())
    # A key the Save form refuses (non-ASCII) can still be held: the system environment, when
    # the person opted in to it, is not shape-checked. All four shapes are saved that way.
    monkeypatch.setenv(USE_SYSTEM_KEYS_ENV, "1")
    monkeypatch.setenv(ENV_NAME, key)
    assert key in diagnostics.saved_secret_values(), "the key is not held, so nothing is proven"

    logs = paths.logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    seeded = _lines(key)
    assert all(MIDDLE in line for line in seeded)
    (logs / "kestrel.log").write_text("\n".join(seeded) + "\n", encoding="utf-8")

    r = TestClient(build_app(worker=False)).post("/about/diagnostics/save")
    assert r.status_code == 200 and "Saved to" in r.text, r.text[:400]

    archives = sorted(downloads.glob("kestrel-diagnostics-*.zip"))
    assert len(archives) == 1
    bundled = ""
    with zipfile.ZipFile(archives[0]) as z:
        for name in z.namelist():
            body = z.read(name).decode("utf-8", errors="replace")
            found = sorted(s for s in _spellings(key) if s in body)
            assert found == [], (name, found)
            bundled += body
    # Every seeded line reached the bundle with the key masked: a scrub that drops the log, or
    # blanks the line, fails here instead of passing on an empty bundle.
    for marker in MARKERS:
        lines = [ln for ln in bundled.splitlines() if marker in ln]
        assert lines, f"the seeded log line with {marker!r} is not in the bundle"
        assert all(REDACTED in ln for ln in lines), (marker, lines)


@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("depth", [0, 1, 2, 3, 6])
@pytest.mark.parametrize("ensure_ascii", [False, True])
def test_the_scrubber_masks_a_key_nested_to_any_depth(shape, depth, ensure_ascii) -> None:
    key = SHAPES[shape]
    scrub = diagnostics.make_scrubber(None, [key])
    line = json.dumps({"event": "dump", "raw": _nest(key, depth, ensure_ascii=ensure_ascii)},
                      ensure_ascii=ensure_ascii)
    assert MIDDLE in line
    out = scrub(line)
    assert MIDDLE not in out and REDACTED in out
    assert out.startswith('{"event": "dump"'), "the line around the key is kept"


@pytest.mark.parametrize("shape", ["backslash", "quote", "non-ascii"])
def test_an_encoding_nobody_listed_still_loses_the_run_around_the_middle(shape) -> None:
    """The allowlist side (D-SEC-20): URL-encoding is not one of the escaped spellings, so only
    the fragment rule can catch it. The run holding the key's plain middle is masked whole."""
    key = SHAPES[shape]
    scrub = diagnostics.make_scrubber(None, [key])
    encoded = urllib.parse.quote(key, safe="")
    assert encoded != key and MIDDLE in encoded
    out = scrub(f"GET /v1/models?key={encoded}&page=2 failed")
    assert MIDDLE not in out and REDACTED in out
    assert out.startswith("GET /v1/models?") and out.endswith("&page=2 failed")


def test_a_short_plain_run_is_not_used_as_a_fragment() -> None:
    """The fragment rule needs a run long enough not to occur by chance; ordinary words in a
    log are left alone when the key has none."""
    key = "ab\\cd\\ef\\gh\\ij"
    scrub = diagnostics.make_scrubber(None, [key])
    assert scrub("the cd player and the ef clef") == "the cd player and the ef clef"
    assert scrub(json.dumps({"k": json.dumps({"k": key})})).count(REDACTED) == 1


# ---------------------------------------------- round 2: encoders that are not Python's json
# The first fix listed escapes, so it missed the ones it did not list (the adversarial review of
# d6da5b23): Go and Jinja JSON write `&` as &, .NET JSON writes `+` and `"` as + and
# ", URL-encoding and HTML have their own spellings. A key is now known by its plain runs,
# whatever stands between them. Ten key shapes times thirteen encoders, each on a line shaped
# like a kept provider reply.
KEY_SHAPES = {
    "amp": MIDDLE[:16] + "&" + MIDDLE[16:],
    "plus-eq": MIDDLE[:16] + "+" + MIDDLE[16:] + "=",
    "angle": "<gw>" + MIDDLE,
    "short-quote": 'Zq7Xw2Vb9N"c4Md8Lk3Jh"6Gf1Rt5Yu8I"o',
    "short-backslash": "Zq7Xw2Vb9N\\c4Md8Lk3Jh\\6Gf1Rt5Yu8I",
    "long-backslash": SHAPES["backslash"],
    "long-quote": SHAPES["quote"],
    "non-ascii": "clé-" + MIDDLE + "-ü",
    "mixed": "a+b/" + MIDDLE[:10] + "=&" + MIDDLE[10:20] + "<" + MIDDLE[20:] + ">",
    "plain": SHAPES["plain"],
}


def _go_json(key: str) -> str:
    text = json.dumps(key, ensure_ascii=False)[1:-1]
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _dotnet_json(key: str) -> str:
    out = ""
    for ch in key:
        if ch in "\"+&<>'`" or ord(ch) > 126:
            out += f"\\u{ord(ch):04X}"
        else:
            out += "\\\\" if ch == "\\" else ch
    return out


def _html_numeric(key: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else f"&#{ord(c)};" for c in key)


ENCODERS = {
    "python-json": lambda k: json.dumps(k, ensure_ascii=False)[1:-1],
    "python-json-ascii": lambda k: json.dumps(k)[1:-1],
    "go-json": _go_json,
    "jinja-tojson": lambda k: _go_json(k).replace("'", "\\u0027"),
    "dotnet-json": _dotnet_json,
    "url": lambda k: urllib.parse.quote(k, safe=""),
    "url-plus": urllib.parse.quote_plus,
    "url-twice": lambda k: urllib.parse.quote(urllib.parse.quote(k, safe=""), safe=""),
    "html": html.escape,
    "html-numeric": _html_numeric,
    "python-repr": lambda k: repr(k)[1:-1],
    "bytes-repr": lambda k: repr(k.encode())[2:-1],
    "json-in-html": lambda k: html.escape(json.dumps(k)),
}


def _runs(key: str) -> list[str]:
    """The key's plain runs worth finding: what an encoder leaves as it was."""
    return [r for r in re.findall(r"[A-Za-z0-9_-]+", key) if len(r) >= 6]


@pytest.mark.parametrize("shape", sorted(KEY_SHAPES))
@pytest.mark.parametrize("encoder", sorted(ENCODERS))
def test_no_saved_key_survives_any_encoder(shape, encoder) -> None:
    key = KEY_SHAPES[shape]
    scrub = diagnostics.make_scrubber(None, [key])
    encoded = ENCODERS[encoder](key)
    line = json.dumps({"event": "dump", "raw": json.dumps({"headers": {"x": encoded}})})
    assert all(run in line for run in _runs(key)), "the line does not hold the key"
    out = scrub(line)
    assert [run for run in _runs(key) if run in out] == []
    assert REDACTED in out and out.startswith('{"event": "dump"')


@pytest.mark.parametrize(
    ("shape", "encoder"),
    [("amp", "go-json"), ("amp", "url"), ("plus-eq", "dotnet-json"), ("plus-eq", "url"),
     ("short-quote", "dotnet-json"), ("short-backslash", "url"), ("angle", "html")],
)
def test_a_provider_reply_that_echoes_the_key_is_masked_in_the_bundle(
    shape, encoder, downloads, monkeypatch  # noqa: F811
) -> None:
    """Text a provider or gateway wrote and Kestrel kept: an error body that echoes the
    Authorization header in its own encoder's spelling, logged as Kestrel logs (Python JSON)."""
    from kestrel.web.app import build_app

    key = KEY_SHAPES[shape]
    asyncio.run(_prepare())
    monkeypatch.setenv(USE_SYSTEM_KEYS_ENV, "1")
    monkeypatch.setenv(ENV_NAME, key)
    assert key in diagnostics.saved_secret_values(), "the key is not held, so nothing is proven"
    body = '{"error": {"headers": {"authorization": "Bearer ' + ENCODERS[encoder](key) + '"}}}'
    seeded = json.dumps({"event": "provider_error_body", "status": 401, "body": body})
    assert all(run in seeded for run in _runs(key))
    logs = paths.logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "kestrel.log").write_text(seeded + "\n", encoding="utf-8")

    r = TestClient(build_app(worker=False)).post("/about/diagnostics/save")
    assert r.status_code == 200 and "Saved to" in r.text, r.text[:400]
    archives = sorted(downloads.glob("kestrel-diagnostics-*.zip"))
    assert len(archives) == 1
    bundled = ""
    with zipfile.ZipFile(archives[0]) as z:
        for name in z.namelist():
            body_text = z.read(name).decode("utf-8", errors="replace")
            assert [run for run in _runs(key) if run in body_text] == [], name
            bundled += body_text
    lines = [ln for ln in bundled.splitlines() if "provider_error_body" in ln]
    assert lines and all(REDACTED in ln for ln in lines), lines


def test_ordinary_text_is_left_alone_by_the_run_rules() -> None:
    """The rules need the key's own runs, in the key's order and close together: its pieces out
    of order, or far apart, mask nothing."""
    scrub = diagnostics.make_scrubber(None, ['ab12"cd34"ef56', "x+y"])
    plain = (
        "cd34 came first and ab12 after it; a good deal later in this long sentence, "
        "well past the gap, ef56 turns up; x and y too"
    )
    assert scrub(plain) == plain
    assert scrub("value=ab12%22cd34%22ef56 end") == "<redacted> end"
