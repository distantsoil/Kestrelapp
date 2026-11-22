"""Setting rows survive the restore's flatten and regroup unchanged (D-SEC-16, D-INST-9).

A restore flattens every local-only row to dotted leaves, picks each leaf from this computer or
the backup, then regroups the leaves into rows. Regrouping must be the exact inverse of the
flatten for every row shape, including the root row (key ""), whose leaves carry no prefix.
All values are invented."""

from __future__ import annotations

import json
import sqlite3

import pytest

from kestrel.services import backup


def _rows(raw: dict[str, object]) -> dict[str, tuple[str, str]]:
    return {k: (json.dumps(v), "2026-01-01T00:00:00+00:00") for k, v in raw.items()}


def _decoded(rows: dict[str, tuple[str, str]]) -> dict[str, object]:
    return {k: json.loads(v) for k, (v, _u) in rows.items()}


def _effective(rows: dict[str, tuple[str, str]]) -> dict[str, object]:
    """The settings a row set means, as leaves (what the app reads)."""
    out: dict[str, object] = {}
    for key in sorted(rows, key=lambda k: (k.count(".") + (k != ""), k)):
        out.update(backup._flat(json.loads(rows[key][0]), key))
    return out


@pytest.mark.parametrize("raw", [
    {"": {"llm": {"mode": "local"}, "ui": {"theme": "dark"}}},
    {"": {"llm": {"mode": "local", "backends": {"lab": {"base_url": "http://127.0.0.1:9"}}}}},
    {"llm": {"mode": "local", "backends": {"lab": {"base_url": "http://127.0.0.1:9"}}}},
    {"llm": {"mode": "local"}, "mcp": {"enabled": False}},
    {"llm.mode": "local"},
], ids=["root", "root-deep", "nested", "two-sections", "leaf"])
def test_regroup_is_the_inverse_of_flatten(raw):
    rows = _rows(raw)
    assert _decoded(backup._regroup(backup._leaf_rows(rows))) == raw


def test_a_root_row_beside_a_section_row_keeps_every_letter():
    rows = _rows({"": {"llm": {"mode": "local"}, "ui": {"theme": "dark"}},
                  "llm": {"tiers": {"t1": {"model": "m"}}}})
    out = backup._regroup(backup._leaf_rows(rows))
    assert _effective(out) == _effective(rows)
    assert all(not k or k.split(".")[0] in {"llm", "ui"} for k in out), sorted(out)
    # No row overlaps another: the root row holds every key, so beside a section row its leaves
    # are written one per row, as for any other overlap.
    assert not any(a != b and backup._under(b, a) for a in out for b in out), sorted(out)


def _db(path, rows: dict[str, tuple[str, str]]) -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE setting (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    con.executemany("INSERT INTO setting VALUES (?, ?, ?)",
                    [(k, v, u) for k, (v, u) in rows.items()])
    con.commit()
    con.close()


def _read(path) -> dict[str, tuple[str, str]]:
    con = sqlite3.connect(str(path))
    try:
        return {k: (v, u) for k, v, u in con.execute("SELECT key, value, updated_at FROM setting")}
    finally:
        con.close()


def test_a_restore_keeps_this_computers_root_row(tmp_path):
    """The reproduced loss: a root row on this computer came back as {"lm": .., "i": ..}."""
    mine = _rows({"": {"llm": {"mode": "local"}, "ui": {"theme": "dark"}}})
    theirs = _rows({"": {"llm": {"mode": "hosted"}, "ui": {"theme": "light"}}})
    live, staged = tmp_path / "live.db", tmp_path / "staged.db"
    _db(live, mine)
    _db(staged, theirs)
    backup._keep_local_settings(staged, live)
    got = _effective(_read(staged))
    assert got["llm.mode"] == "local"  # local-only: this computer's
    assert got["ui.theme"] == "light"  # not local-only: the backup's
    assert not any(k.startswith(("lm", "i.")) for k in got), sorted(got)


def test_flatten_reaches_any_depth_that_parsed():
    """A row nested past the recursion limit (it still parses as JSON) flattens like any other,
    so no restore filter is skipped by depth (D-INST-12)."""
    deep: dict = {"leaf": 1}
    for _ in range(5000):
        deep = {"a": deep}
    out = backup._flat({"install": {"id": "x"}, "deep": deep}, "")
    assert out["install.id"] == "x"
    assert out["deep." + "a." * 5000 + "leaf"] == 1
