"""D-PRIV-812: a bounded field of the diagnostics bundle is scrubbed whole, then cut.

INVARIANT: no part of a saved key of 14 or more characters reaches the diagnostics bundle,
wherever the key sits in the text. Every bounded field is scrubbed whole, then cut.

The bundle used to cut a quarantined reply to RAW_RESPONSE_CHARS characters and scrub what was
left, so a saved key lying across the cut reached the scrubber without its end, matched nothing
and kept its first characters in quarantine.json. Each case below saves a key, seeds a
quarantined reply that echoes it at a chosen distance from the cut, and builds the bundle through
the route the desktop app uses. Invented keys; a migrated SQLite file; no network."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import zipfile

import pytest
from fastapi.testclient import TestClient

from kestrel import paths
from kestrel.config import USE_SYSTEM_KEYS_ENV
from kestrel.services import diagnostics
from tests.test_diagnostics_key_leak import _prepare, downloads  # noqa: F401  (the fixture)

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

ENV_NAME = "INVENTED_GATEWAY_API_KEY"
CUT = diagnostics.RAW_RESPONSE_CHARS
# Invented; none belongs to an account.
HEX32 = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"
GATEWAY = "gw-" + "Zq7Xw2Vb9Nc4Md8Lk3Jh6Gf1Rt5Yu8Io"
OPENROUTER = "sk-or-v1-" + "0123456789abcdef" * 4
# case -> (key, how many of its characters lie before the cut). The first three are the
# reviewer's probes; then a key one character over the cut, one character before it, and the
# two keys that only touch the cut (wholly kept, wholly cut away).
CASES = {
    "hex32-24-kept": (HEX32, 24),
    "gateway-24-kept": (GATEWAY, 24),
    "openrouter-8-kept": (OPENROUTER, 8),
    "all-but-one-kept": (HEX32, len(HEX32) - 1),
    "one-kept": (GATEWAY, 1),
    "ends-at-the-cut": (HEX32, len(HEX32)),
    "starts-at-the-cut": (GATEWAY, 0),
}


def _reply(key: str, kept: int) -> str:
    """A reply that echoes `key` with exactly `kept` of its characters before the cut."""
    filler = ("x y " * CUT)[: CUT - kept - 1] + " "
    reply = filler + key + " and the reply went on"
    assert reply[:CUT].endswith(key[:kept]) and reply[CUT:].startswith(key[kept:])
    return reply


def _seed_quarantine(reply: str) -> None:
    with sqlite3.connect(paths.db_path()) as conn:
        conn.execute(
            "INSERT INTO quarantine (tier, model, raw_response, error, attempts, created_at) "
            "VALUES (2, 'invented-model', ?, 'schema: verdict missing', 1, "
            "'2026-01-01T00:00:00+00:00')",
            (reply,),
        )


def _bundle(downloads) -> dict[str, str]:  # noqa: F811
    from kestrel.web.app import build_app

    r = TestClient(build_app(worker=False)).post("/about/diagnostics/save")
    assert r.status_code == 200 and "Saved to" in r.text, r.text[:400]
    archives = sorted(downloads.glob("kestrel-diagnostics-*.zip"))
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as z:
        return {name: z.read(name).decode("utf-8", errors="replace") for name in z.namelist()}


@pytest.mark.parametrize("case", sorted(CASES))
def test_no_part_of_a_saved_key_is_left_at_the_cut(case, downloads, monkeypatch) -> None:  # noqa: F811
    key, kept = CASES[case]
    asyncio.run(_prepare())
    monkeypatch.setenv(USE_SYSTEM_KEYS_ENV, "1")
    monkeypatch.setenv(ENV_NAME, key)
    assert key in diagnostics.saved_secret_values(), "the key is not held, so nothing is proven"
    _seed_quarantine(_reply(key, kept))

    members = _bundle(downloads)
    rows = json.loads(members["quarantine.json"])["rows"]
    assert len(rows) == 1, "the seeded reply is not in the bundle, so nothing is proven"
    shown = rows[0]["raw_response"]
    assert shown.startswith("x y x y ") and len(shown) <= CUT
    assert rows[0]["raw_response_truncated"] is True
    # No leading part of the key is the last thing shown, down to one character.
    heads = [key[:n] for n in range(1, len(key) + 1) if shown.endswith(key[:n])]
    assert heads == [], f"{case}: the field ends with the key's first {len(heads[-1])} characters"
    # And no piece of the key of 6 or more characters is anywhere in any member.
    pieces = {key[i:i + 6] for i in range(len(key) - 5)}
    for name, text in members.items():
        assert sorted(p for p in pieces if p in text) == [], name


def test_a_reply_with_no_key_is_cut_as_before(downloads, monkeypatch) -> None:  # noqa: F811
    """The control: ordinary text keeps its first RAW_RESPONSE_CHARS characters unchanged."""
    asyncio.run(_prepare())
    monkeypatch.setenv(USE_SYSTEM_KEYS_ENV, "1")
    monkeypatch.setenv(ENV_NAME, HEX32)
    reply = "plain words about a role, " * 40
    _seed_quarantine(reply)

    rows = json.loads(_bundle(downloads)["quarantine.json"])["rows"]
    assert rows[0]["raw_response"] == reply[:CUT]
    assert rows[0]["raw_response_truncated"] is True


def test_the_two_bounded_fields_share_one_scrub_then_cut() -> None:
    """scrub_then_cut hands the scrubber the whole text and cuts what comes back."""
    seen: list[str] = []

    def scrub(text: str) -> str:
        seen.append(text)
        return text.replace("secret", "<redacted>")

    assert diagnostics.scrub_then_cut(scrub, "abc secret", 7) == "abc <re"
    assert seen == ["abc secret"]
