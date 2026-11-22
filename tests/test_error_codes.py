"""The error code registry is a contract (SPEC-INTERFACES.md §2.7, D-ERR-1, D-ERR-2).

A code is never renumbered, reused or deleted; every entry's text is plain and safe to show; the
generated help table matches the registry. Each guard has its own test, so one broken rule turns
exactly its own test red.
"""

from __future__ import annotations

import dataclasses
import datetime
import importlib.util
import re
from pathlib import Path

import pytest

from kestrel import error_codes
from kestrel.error_codes import (
    _ALL,
    _RETIRED_ALL,
    AREAS,
    CODES,
    RETIRED,
    ErrorCode,
    RetiredCode,
    get,
    lookup,
    render_table,
)
from kestrel.errors import CODE_MARK, DETAIL_MARK, shown_code, shown_failure

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "docs" / "guide" / "error-codes.md"

# Every code ever issued, one block per area in AREAS order, each block in the order issued.
# Append only, at the end of the area's own block (D-ERR-4): a new code is added here in the same
# change that adds it to the registry, and nothing is ever removed or edited. A code no longer in
# use moves to error_codes._RETIRED_ALL; it stays in this list. scripts/error_code_base.py checks,
# area by area, that main's list is a prefix of this one.
ISSUED: tuple[str, ...] = (
    # ---- STA
    "STA-001",
    "STA-002",
    "STA-003",
    "STA-004",
    "STA-005",
    "STA-006",
    "STA-007",
    "STA-008",
    # ---- DAT
    # ---- MDL
    "MDL-001",
    "MDL-002",
    "MDL-003",
    "MDL-004",
    "MDL-005",
    "MDL-006",
    "MDL-007",
    # ---- ONL
    "ONL-001",
    "ONL-002",
    "ONL-003",
    "ONL-004",
    "ONL-005",
    "ONL-006",
    # ---- SRC
    "SRC-001",
    "SRC-002",
    "SRC-003",
    "SRC-004",
    "SRC-005",
    "SRC-006",
    "SRC-007",
    "SRC-008",
    "SRC-009",
    "SRC-010",
    "SRC-011",
    "SRC-012",
    "SRC-013",
    "SRC-014",
    # ---- RUN
    "RUN-001",
    "RUN-002",
    "RUN-003",
    # ---- IMP
    # ---- DOC
    # ---- NET
    # ---- MCP
    # ---- UPD
    # ---- CHK
    "CHK-001",
    "CHK-002",
    "CHK-003",
    "CHK-004",
    "CHK-005",
    "CHK-006",
    "CHK-007",
    "CHK-008",
    "CHK-009",
    "CHK-010",
    "CHK-011",
    "CHK-012",
    "CHK-013",
    "CHK-014",
    "CHK-015",
    "CHK-016",
    "CHK-017",
    "CHK-018",
    "CHK-019",
    "CHK-020",
    "CHK-021",
    "CHK-022",
    "CHK-023",
    "CHK-024",
    "CHK-025",
    "CHK-026",
    "CHK-027",
    "CHK-028",
    "CHK-029",
    "CHK-030",
    "CHK-031",
    # ---- APP
    "APP-001",
)

# Every code ever retired. Append only: a retired code never comes back to CODES with a new
# meaning, so it stays in RETIRED for good.
EVER_RETIRED: tuple[str, ...] = ()

# The title of every code ever issued, frozen. A live code keeps its title, so a code cannot be
# reused for a different problem. Append only, in the same change that issues the code.
FROZEN_TITLES: dict[str, str] = {
    # ---- STA
    "STA-001": "The last start is still closing",
    "STA-002": "Kestrel did not start twice in a row",
    "STA-003": "Your data is from a newer version",
    "STA-004": "Kestrel could not open your data",
    "STA-005": "Another app is using the connection",
    "STA-006": "The local server did not answer",
    "STA-007": "The last session is still closing",
    "STA-008": "The start was canceled",
    # ---- DAT
    # ---- MDL
    "MDL-001": "The AI request did not finish",
    "MDL-002": "The local AI stopped while loading",
    "MDL-003": "The AI took too long to answer",
    "MDL-004": "Kestrel could not reach the AI",
    "MDL-005": "The AI service asked Kestrel to slow down",
    "MDL-006": "A model download or build stalled",
    "MDL-007": "The local AI engine reported an error",
    # ---- ONL
    "ONL-001": "The online service did not accept the key",
    "ONL-002": "The online account cannot pay for this request",
    "ONL-003": "Nothing was sent: the send was not confirmed",
    "ONL-004": "Nothing was sent: the destination changed",
    "ONL-005": "Nothing was sent: confidential mode held it",
    "ONL-006": "Nothing was sent: Hosted mode is off",
    # ---- SRC
    "SRC-001": "This source's setup is not valid",
    "SRC-002": "The site asked Kestrel to slow down",
    "SRC-003": "The site had a server problem",
    "SRC-004": "The site took too long to answer",
    "SRC-005": "The site does not allow reading this page",
    "SRC-006": "This source needs a key of your own",
    "SRC-007": "The daily limit for this source is used",
    "SRC-008": "The page was not found",
    "SRC-009": "Part of the check did not finish",
    "SRC-010": "Kestrel could not read this source",
    "SRC-011": "A posting could not be saved",
    "SRC-012": "A source hit a problem Kestrel did not expect",
    "SRC-013": "A search has no job boards left",
    "SRC-014": "No posting could be saved",
    # ---- RUN
    "RUN-001": "The run did not run",
    "RUN-002": "Kestrel closed before the run ended",
    "RUN-003": "A search was cut off",
    # ---- IMP
    # ---- DOC
    # ---- NET
    # ---- MCP
    # ---- UPD
    # ---- CHK
    "CHK-001": "Python is the wrong version",
    "CHK-002": "The uv tool was not found",
    "CHK-003": "A settings or rules file is not valid",
    "CHK-004": "Your profile is missing or incomplete",
    "CHK-005": "The AI engine is not running",
    "CHK-006": "A model is not downloaded",
    "CHK-007": "A model has too little room to read",
    "CHK-008": "A model failed the room-to-read test",
    "CHK-009": "A model does not turn thinking off",
    "CHK-010": "A model does not answer in the right format",
    "CHK-011": "No browser was found for PDFs",
    "CHK-012": "The connector port is in use",
    "CHK-013": "An Ollama setting could be better",
    "CHK-014": "The AI address is not on this computer",
    "CHK-015": "Ollama listens beyond this computer",
    "CHK-016": "A cloud-hosted model is installed",
    "CHK-017": "Ollama made a connection you did not expect",
    "CHK-018": "A model is not one Kestrel tested",
    "CHK-019": "No contact is set for reading job sites",
    "CHK-020": "A company page did not look as expected",
    "CHK-021": "Your data could not be fully checked",
    "CHK-022": "An online model is not set up fully",
    "CHK-023": "The search model is not ready",
    "CHK-024": "Your saved settings could not be read",
    "CHK-025": "Model tests were skipped during a run",
    "CHK-026": "A model may be too large for this computer",
    "CHK-027": "The check could not run",
    "CHK-028": "A model has not been built",
    "CHK-029": "Your profile file could not be read",
    "CHK-030": "A browser path you set points at nothing",
    "CHK-031": "Your data needs updating",
    # ---- APP
    "APP-001": "Something went wrong on this page",
}

FROZEN_AREAS = {
    "STA": "Starting Kestrel",
    "DAT": "Your data, backup and restore",
    "MDL": "AI models",
    "ONL": "Online models and consent",
    "SRC": "Job sources and searches",
    "RUN": "Runs and scoring",
    "IMP": "Importing files",
    "DOC": "Documents and PDFs",
    "NET": "Other devices and access",
    "MCP": "The MCP server",
    "UPD": "Updates",
    "CHK": "Health checks",
    "APP": "Anything else",
}

CODE_RE = re.compile(r"^[A-Z]{3}-[0-9]{3}$", re.ASCII)  # ASCII only (D-ERR-11)

# What must never appear in a title, meaning or fix (D-SEC-4, D-UI-263, D-UI-268). The class name
# rule follows tests/test_no_class_name_in_error_text.py: the shown sentence is Kestrel's own words.
FORBIDDEN = {
    "em or en dash": re.compile(r"[\u2014\u2013]"),
    "class name": re.compile(r"\b[A-Z][A-Za-z0-9]*(?:Error|Exception|Warning)\b"),
    "dunder or type name": re.compile(r"__\w+__|\btype\("),
    "internal identifier": re.compile(r"\b[a-z0-9]+_[a-z0-9_]+\b"),
    "windows path": re.compile(r"\b[A-Za-z]:[\\/]|\\\\"),
    "posix path": re.compile(r"(?:^|[\s(])[~.]?/[\w.-]+"),
    "file name": re.compile(
        r"(?:\b[\w-]+|(?:^|(?<=\s)))\.(?:py|log|db|sqlite|yml|yaml|json|toml|txt|exe|pkl|env)\b"
    ),
    "markup": re.compile(r"[<>*_`\[\]#]"),
    "web address": re.compile(r"https?://|www\.|\b[\w-]+\.(?:co|com|org|net|io|ai|app)\b"),
    "email or ip address": re.compile(r"@|\b\d{1,3}(?:\.\d{1,3}){3}\b|localhost"),
    "British spelling": re.compile(
        r"(?i)\b\w*(?:colour|behaviour|favour|centre|licence|organis|recognis|authoris|"
        r"prioritis|customis|analys(?:e|ing)|cancell|modell|travell)\w*\b"
    ),
}


def _every_entry() -> dict[str, ErrorCode]:
    return {**CODES, **{code: item.entry for code, item in RETIRED.items()}}


def _texts() -> list[tuple[str, str, str]]:
    return [
        (code, field, getattr(entry, field))
        for code, entry in _every_entry().items()
        for field in ("title", "meaning", "fix")
    ]


def test_areas_are_exactly_the_fixed_thirteen():
    assert AREAS == FROZEN_AREAS
    assert list(AREAS) == list(FROZEN_AREAS)


def test_every_key_is_well_formed_in_a_known_area_and_matches_its_entry():
    bad = [
        key
        for key, entry in CODES.items()
        if not CODE_RE.match(key)
        or key[:3] not in AREAS
        or entry.code != key
        or entry.area != key[:3]
    ]
    assert bad == []


def test_retired_codes_are_well_formed_and_dated():
    bad = []
    for key, item in RETIRED.items():
        when = item.retired
        try:
            datetime.date.fromisoformat(when)
            dated = re.fullmatch(r"\d{4}-\d{2}-\d{2}", when) is not None
        except ValueError:
            dated = False
        if not CODE_RE.match(key) or key[:3] not in AREAS or not dated or item.entry.code != key:
            bad.append(key)
    assert bad == []


def test_no_code_is_both_live_and_retired():
    assert set(CODES) & set(RETIRED) == set()


def test_no_code_is_defined_twice():
    """CODES and RETIRED are built from tuples; a second entry with the same code would silently
    replace the first, so the counts must match."""
    assert len(_ALL) == len(CODES)
    assert len(_RETIRED_ALL) == len(RETIRED)


def test_every_issued_code_still_exists_live_or_retired():
    """A code removed or renumbered disappears from both; that is never allowed."""
    missing = [code for code in ISSUED if code not in CODES and code not in RETIRED]
    assert missing == []


def test_every_registered_code_was_issued():
    """A new code is added to ISSUED in the same change, so the frozen list stays complete."""
    unlisted = sorted((set(CODES) | set(RETIRED)) - set(ISSUED))
    assert unlisted == []


def test_issued_list_has_no_duplicates():
    assert len(ISSUED) == len(set(ISSUED))


def test_every_entry_has_a_version():
    bad = [c for c, e in CODES.items() if not re.fullmatch(r"\d+\.\d+\.\d+", e.since)]
    assert bad == []


def test_every_text_is_a_plain_non_empty_sentence():
    bad = []
    for code, field, text in _texts():
        if not text or text != text.strip() or "\n" in text or "|" in text:
            bad.append((code, field, "empty, padded, multi-line or has a pipe"))
        elif field == "title" and text.endswith("."):
            bad.append((code, field, "a title has no closing period"))
        elif field != "title" and not text.endswith("."):
            bad.append((code, field, "a sentence ends with a period"))
    assert bad == []


@pytest.mark.parametrize("rule", sorted(FORBIDDEN))
def test_no_text_holds_forbidden_content(rule):
    pattern = FORBIDDEN[rule]
    hits = [(code, field, text) for code, field, text in _texts() if pattern.search(text)]
    assert hits == []


@pytest.mark.parametrize(
    ("rule", "sample"),
    [
        ("em or en dash", "Kestrel stopped \u2014 try again."),
        ("class name", "A StartupError stopped the start."),
        ("dunder or type name", "Cause: type(exc).__name__."),
        ("internal identifier", "The health_never_up check failed."),
        ("windows path", "Open C:\\Users\\sample\\kestrel."),
        ("posix path", "Open /home/sample/kestrel."),
        ("file name", "Open kestrel.log for the cause."),
        ("file name", "Put the key in .env and try again."),
        ("markup", "Choose <b>Try again</b>."),
        ("British spelling", "The start was cancelled."),
        ("British spelling", "Authorise the provider."),
        ("web address", "Download it from example.co today."),
        ("email or ip address", "Write to help@example.test."),
        ("British spelling", "Change the colour."),
    ],
)
def test_each_forbidden_pattern_catches_its_sample(rule, sample):
    """The text guard is only as good as its patterns: each must catch a plain example."""
    assert FORBIDDEN[rule].search(sample) is not None


def test_code_shape_is_ascii_only():
    assert CODE_RE.match("STA-001")
    assert not CODE_RE.match("STA-00\uff11")  # a full-width digit one
    assert not CODE_RE.match("STA-00\u0661")  # an Arabic-Indic digit one


def test_get_returns_the_entry_and_refuses_an_unknown_code():
    assert get("STA-006") is CODES["STA-006"]
    with pytest.raises(KeyError):
        get("STA-999")
    with pytest.raises(KeyError):
        get("health_never_up")


def test_entries_are_frozen():
    entry = get("STA-001")
    assert isinstance(entry, ErrorCode)
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.title = "Changed"  # type: ignore[misc]


def test_every_retired_code_stays_retired():
    """A code once retired is never brought back to CODES, where it could mean something new."""
    revived = [code for code in EVER_RETIRED if code not in RETIRED]
    assert revived == []
    assert sorted(set(RETIRED) - set(EVER_RETIRED)) == []


def test_every_issued_code_has_a_frozen_title():
    assert sorted(FROZEN_TITLES) == sorted(ISSUED)


def test_every_code_keeps_its_frozen_title():
    """Reusing a code for a different problem changes its title; that is never allowed, live or
    retired."""
    changed = [c for c, e in _every_entry().items() if e.title != FROZEN_TITLES.get(c)]
    assert changed == []


def test_lookup_knows_live_and_retired_codes_and_nothing_else(monkeypatch):
    old = ErrorCode("APP-901", "APP", "Sample old title", "It happened.", "Do this.", "0.7.7")
    monkeypatch.setattr(error_codes, "RETIRED", {"APP-901": RetiredCode(old, "2026-11-13")})
    assert error_codes.lookup("STA-006") is CODES["STA-006"]
    assert error_codes.lookup("APP-901") is old
    assert error_codes.lookup("APP-999") is None
    assert error_codes.lookup("sta-006") is None
    assert error_codes.lookup("") is None
    with pytest.raises(KeyError):
        error_codes.get("APP-901")  # get is for codes in use; a stored row goes through lookup


def test_shown_failure_hides_the_code_line_and_the_detail():
    stored = f"Kestrel could not reach the sample service.{CODE_MARK}STA-006{DETAIL_MARK}X: y"
    assert shown_failure(stored) == "Kestrel could not reach the sample service."
    assert shown_code(stored) == "STA-006"


def test_an_old_row_with_no_code_line_shows_no_code():
    stored = f"Kestrel could not reach the sample service.{DETAIL_MARK}SampleProblem: boom"
    assert shown_failure(stored) == "Kestrel could not reach the sample service."
    assert shown_code(stored) is None
    assert shown_code(None) is None
    assert shown_failure(None) == ""


def test_a_code_line_inside_the_detail_never_counts():
    stored = f"Sample sentence.{DETAIL_MARK}SampleProblem: text{CODE_MARK}STA-001"
    assert shown_code(stored) is None
    assert shown_failure(stored) == "Sample sentence."


def test_an_unregistered_code_line_shows_no_code():
    stored = f"Sample sentence.{CODE_MARK}ZZZ-999{DETAIL_MARK}SampleProblem: text"
    assert shown_code(stored) is None
    assert shown_failure(stored) == "Sample sentence."


def test_a_retired_code_on_an_old_row_still_shows(monkeypatch):
    old = ErrorCode("APP-901", "APP", "Sample old title", "It happened.", "Do this.", "0.7.7")
    monkeypatch.setattr(error_codes, "RETIRED", {"APP-901": RetiredCode(old, "2026-11-13")})
    stored = f"Sample sentence.{CODE_MARK}APP-901{DETAIL_MARK}SampleProblem: text"
    assert shown_code(stored) == "APP-901"
    assert lookup("APP-901") is old


def test_table_has_one_anchor_per_code_under_its_area_heading():
    table = render_table()
    for code, entry in CODES.items():
        assert error_codes.anchor(code) == code.lower()
        assert table.count(f'<a id="{code.lower()}"></a>{code} |') == 1
        heading = f"## {AREAS[entry.area]} ({entry.area})"
        assert heading in table
        assert table.index(heading) < table.index(f'<a id="{code.lower()}"></a>')


def test_table_escapes_a_pipe_and_lists_retired_codes(monkeypatch):
    sample = ErrorCode("APP-900", "APP", "Sample title", "A | B happened.", "Try again.", "0.7.7")
    monkeypatch.setattr(error_codes, "CODES", {"APP-900": sample})
    old = ErrorCode("APP-901", "APP", "Sample old title", "It happened.", "Do this.", "0.7.7")
    monkeypatch.setattr(error_codes, "RETIRED", {"APP-901": RetiredCode(old, "2026-11-13")})
    table = error_codes.render_table()
    assert "A \\| B happened." in table
    assert "## Retired codes" in table
    assert (
        '| <a id="app-901"></a>APP-901 | **Sample old title.** It happened. | 2026-11-13 |' in table
    )
    assert "## Starting Kestrel" not in table


def test_committed_table_matches_the_registry():
    committed = TABLE.read_bytes()
    assert committed == render_table().encode("utf-8"), "run scripts/error_codes_table.py"


def test_table_script_writes_the_rendered_table(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "error_codes_table", ROOT / "scripts" / "error_codes_table.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = tmp_path / "table.md"
    assert module.main([str(out)]) == 0
    assert out.read_bytes() == render_table().encode("utf-8")
