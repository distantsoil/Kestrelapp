"""Health checks carry a stable CHK code on every warning and failure (#174 task E): the doctor
(structurally, by walking its source), the `kestrel doctor` text, and the Models health card."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kestrel import doctor, error_codes
from kestrel.services import setup_state as ss
from kestrel.web import models_view as mv

ROOT = Path(doctor.__file__).resolve().parent  # the kestrel package
CODE_RE = re.compile(r"^CHK-\d{3}$")
_STATUSES = "PASS = 'PASS'\n"  # a snippet is judged as part of a module that names statuses
_STATUS_WORD = re.compile(r"\b_?(PASS|INFO|WARN|FAIL)\b")
_PLAIN = ("PASS", "INFO", "_PASS", "_INFO")


def _status_of(call: ast.Call) -> ast.expr | None:
    if len(call.args) >= 2 and not any(isinstance(a, ast.Starred) for a in call.args[:2]):
        return call.args[1]
    return next((k.value for k in call.keywords if k.arg == "status"), None)


def _forwards_everything(call: ast.Call) -> bool:
    """`CheckResult(*a, **k)`: the one wrapper (DoctorReport.add) that passes its caller's
    arguments through. The callers of that wrapper are the calls that are checked."""
    return (len(call.args) == 1 and isinstance(call.args[0], ast.Starred)
            and len(call.keywords) == 1 and call.keywords[0].arg is None)


def _result_calls(tree: ast.AST) -> list[ast.Call]:
    """Every call that can make a health check row: `CheckResult(...)`, or `.add(...)` on a
    report (`rep`, `report`) or whose status names PASS, INFO, WARN or FAIL. A call whose status
    cannot be read (starred arguments, no status at all) is returned too, and then fails."""
    found = []
    # A module that makes health check rows names the statuses as strings; another module's
    # two-argument `.add` (a report of a different kind) is not a health check.
    names_statuses = any(isinstance(n, ast.Constant) and n.value in ("PASS", "WARN", "FAIL")
                         for n in ast.walk(tree))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "attr", None) or getattr(fn, "id", None)
        if name == "CheckResult":
            if not _forwards_everything(node):
                found.append(node)
        elif name == "add":
            status = _status_of(node)
            # any receiver: a set or a session adds one thing, so two or more arguments, or a
            # status keyword, is a health check row whatever the report is called
            recv = ast.unparse(fn.value) if isinstance(fn, ast.Attribute) else ""
            has_status_kw = any(k.arg == "status" for k in node.keywords)
            names_a_status = status is not None and _STATUS_WORD.search(ast.unparse(status))
            if (recv in ("rep", "report") or (names_statuses and len(node.args) >= 2)
                    or has_status_kw or names_a_status):
                found.append(node)
    return found


def _missing_code(tree: ast.AST) -> list[str]:
    """Calls whose status is anything but a plain PASS or INFO (a variable, a conditional, an
    unreadable status all count as possibly WARN or FAIL) with no `code=` that is a string
    literal naming a CHK code."""
    bad = []
    for call in _result_calls(tree):
        status = _status_of(call)
        if status is not None and ast.unparse(status) in _PLAIN:
            continue
        code = next((k.value for k in call.keywords if k.arg == "code"), None)
        if not (isinstance(code, ast.Constant) and isinstance(code.value, str)
                and CODE_RE.match(code.value)):
            what = ast.unparse(status) if status is not None else "unreadable"
            bad.append(f"line {call.lineno}: {ast.unparse(call.args[0]) if call.args else '?'} "
                       f"({what})")
    return bad


def _modules_with_checks() -> dict[str, ast.AST]:
    """Every module under kestrel/ that makes a health check row, found by walking the package."""
    out = {}
    for path in sorted(ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _result_calls(tree):
            out[path.relative_to(ROOT).as_posix()] = tree
    return out


def test_the_walk_finds_the_doctor_and_the_model_fit_check():
    assert {"doctor.py", "llm/profiles.py"} <= set(_modules_with_checks())


def test_every_warn_or_fail_check_in_every_module_carries_a_code():
    problems = {m: _missing_code(t) for m, t in _modules_with_checks().items()}
    assert {m: p for m, p in problems.items() if p} == {}


def test_the_walk_sees_the_checks_it_is_meant_to_guard():
    calls = _result_calls(_modules_with_checks()["doctor.py"])
    risky = [c for c in calls if ast.unparse(_status_of(c)) not in _PLAIN]
    assert len(calls) > 80 and len(risky) > 50  # not vacuous: the doctor has ~100 rows


@pytest.mark.parametrize("snippet", [
    "rep.add('x', FAIL, 'd')",
    "rep.add('x', WARN, 'd', 'fix')",
    "rep.add('x', _WARN, 'd')",
    "rep.add('x', PASS if ok else WARN, 'd')",
    "rep.add('x', status, 'd')",
    "rep.add('x', status=WARN, detail='d')",
    "rep.add(*args)",
    "self.add('x', status, 'd')",
    "checks.add('x', flag, 'd')",
    "self.report.add('x', ok_or_not)",
    "rep.add('x')",
    "CheckResult('x', FAIL, 'd', blocking=True)",
    "CheckResult(name='x', status=FAIL)",
    "rep.add('x', FAIL, 'd', code=some_variable)",
    "rep.add('x', FAIL, 'd', code='MDL-001')",
    "rep.add('x', FAIL, 'd', code='')",
])
def test_the_walk_catches_a_missing_or_wrong_code(snippet):
    assert len(_missing_code(ast.parse(_STATUSES + snippet))) == 1


def test_the_walk_accepts_a_coded_row_and_a_plain_pass():
    ok = ("rep.add('x', FAIL, 'd', code='CHK-003')\nrep.add('y', PASS, 'fine')\n"
          "rep.add('z', _INFO, 'n')\nrep.add('w', status=WARN, detail='d', code='CHK-004')\n"
          "seen.add(5)")
    assert _missing_code(ast.parse(_STATUSES + ok)) == []


def _used_codes() -> set[str]:
    return {m for p in ROOT.rglob("*.py")
            for m in re.findall(r'code="(CHK-\d{3})"', p.read_text(encoding="utf-8"))}


def test_every_code_used_by_a_check_is_a_registered_check_code():
    used = _used_codes()
    assert len(used) >= 20
    assert all(error_codes.get(c).area == "CHK" for c in used)


def test_every_registered_check_code_is_used_by_a_check():
    registered = {c for c in error_codes.CODES if c.startswith("CHK-")}
    assert registered == _used_codes()


def test_a_check_result_has_no_code_unless_it_is_given_one():
    assert doctor.CheckResult("x", doctor.PASS).code == ""
    assert doctor.CheckResult("x", doctor.FAIL, code="CHK-005").code == "CHK-005"


def test_the_doctor_text_shows_the_code_under_a_warning_and_a_failure_only():
    rep = doctor.DoctorReport()
    rep.add("Ollama reachable", doctor.FAIL, "not reachable", "Start it.", code="CHK-005")
    rep.add("uv present", doctor.WARN, "not found", code="CHK-002")
    rep.add("Database", doctor.PASS, "migrated", code="CHK-021")
    rep.add("Ingest contact", doctor.INFO, "skipped", code="CHK-019")
    text = doctor.format_report(rep)
    assert "Code CHK-005" in text and "Code CHK-002" in text
    assert "CHK-021" not in text and "CHK-019" not in text


def test_the_doctor_text_of_a_row_with_no_code_has_no_code_line():
    rep = doctor.DoctorReport()
    rep.add("Ollama reachable", doctor.FAIL, "not reachable", "Start it.")
    assert "Code " not in doctor.format_report(rep)


# ------------------------------------------------------------------ the Models health card
def _report(*rows: dict) -> dict:
    return {"results": list(rows)}


def _row(name, status, code=None, detail="d", remediation="Fix it."):
    row = {"name": name, "status": status, "detail": detail, "remediation": remediation}
    if code is not None:
        row["code"] = code
    return row


def _by_key(rows):
    return {r["key"]: r for r in rows}


def test_a_failed_health_check_shows_its_code():
    rows = _by_key(mv.health_rows(_report(_row("Ollama reachable", "FAIL", "CHK-005"))))
    assert (rows["engine"]["state"], rows["engine"]["code"]) == ("fail", "CHK-005")


def test_a_warned_health_check_shows_its_code():
    rows = _by_key(mv.health_rows(_report(_row("needle kestrel-tier2", "WARN", "CHK-008"))))
    assert (rows["room"]["state"], rows["room"]["code"]) == ("warn", "CHK-008")


def test_a_passing_health_check_shows_no_code_even_if_one_is_stored():
    rows = _by_key(mv.health_rows(_report(_row("Ollama reachable", "PASS", "CHK-005"))))
    assert (rows["engine"]["state"], rows["engine"]["code"]) == ("ok", "")


def test_an_old_report_with_no_code_shows_none():
    rows = _by_key(mv.health_rows(_report(_row("Ollama reachable", "FAIL"))))
    assert (rows["engine"]["state"], rows["engine"]["code"]) == ("fail", "")


def test_an_unregistered_or_foreign_code_in_a_report_shows_none():
    for bad in ("CHK-999", "<b>x</b>", "not a code", "MDL-001x"):
        rows = _by_key(mv.health_rows(_report(_row("Ollama reachable", "FAIL", bad))))
        assert rows["engine"]["code"] == "", bad


def test_the_card_takes_the_code_of_the_first_failure_not_a_later_warning():
    rows = _by_key(mv.health_rows(_report(
        _row("num_ctx tier1", "FAIL", "CHK-007"), _row("needle kestrel-tier1", "WARN", "CHK-008"),
    )))
    assert rows["room"]["code"] == "CHK-007"


@pytest.mark.parametrize(("name", "code"), [
    ("Local AI: backend address", "CHK-014"),
    ("Local AI: Ollama bind", "CHK-015"),
    ("Local AI: cloud model tags", "CHK-016"),
    ("Local AI: egress", "CHK-017"),
    ("Local AI: model digests", "CHK-018"),
])
def test_each_local_ai_row_shows_its_code_when_it_warns(name, code):
    rows = mv.local_ai_rows(_report(_row(name, "WARN", code, detail="Different address")))
    shown = [r for r in rows if r["code"]]
    assert [r["code"] for r in shown] == [code] and shown[0]["state"] == "warn"


def test_local_ai_rows_that_pass_or_were_never_checked_show_no_code():
    ok = mv.local_ai_rows(_report(_row("Local AI: egress", "PASS", "CHK-017", detail="none")))
    assert all(r["code"] == "" for r in ok)
    assert all(r["code"] == "" for r in mv.local_ai_rows(None))


def test_an_unverified_local_ai_row_from_a_warning_keeps_its_code():
    rows = mv.local_ai_rows(_report(_row(
        "Local AI: egress", "WARN", "CHK-017", detail="Unverified: could not list connections")))
    egress = _by_key(rows)["egress"]
    assert (egress["state"], egress["code"]) == ("unverified", "CHK-017")


@pytest.fixture
def client(_isolate_data_home, monkeypatch):
    from kestrel.db import migrate
    from kestrel.web.app import build_app

    # The card shows the latest check's log from a process-wide registry; an earlier test's
    # check (with its own codes) must not reach this test's card.
    monkeypatch.setattr(ss, "_registry", {})
    migrate.run_migrations()

    with TestClient(build_app(worker=False)) as c:
        yield c


def _save(*results: tuple) -> None:
    rep = doctor.DoctorReport()
    for name, status, detail, code in results:
        rep.add(name, status, detail, "Do the thing.", code=code)
    ss.save_report(rep)


def test_the_card_renders_the_code_under_the_sentence_and_again_in_details(client):
    _save(("Ollama reachable", "FAIL", "not reachable", "CHK-005"),
          ("Local AI: backend address", "WARN", "another address", "CHK-014"))
    resp = client.get("/models/health")
    assert resp.status_code == 200
    html = resp.text
    assert re.search(r'data-testid="mo-hc-code-engine">Code CHK-005<', html)
    assert re.search(r'data-testid="mo-la-code-backend">Code CHK-014<', html)
    details = html.split("<summary>Show details</summary>")[1]
    assert "Code CHK-005: Ollama reachable" in details
    assert "Code CHK-014: Local AI: backend address" in details


def test_the_card_of_an_old_report_renders_no_code(client):
    _save(("Ollama reachable", "FAIL", "not reachable", ""))
    resp = client.get("/models/health")
    assert resp.status_code == 200
    html = resp.text
    assert "Ollama reachable" in html or "AI engine running" in html
    assert "mo-hc-code" not in html and "Code CHK" not in html


def test_a_registered_code_of_another_area_on_a_health_row_shows_none():
    for other in ("SRC-005", "RUN-001", "STA-001"):
        rows = _by_key(mv.health_rows(_report(_row("Ollama reachable", "FAIL", other))))
        assert (rows["engine"]["state"], rows["engine"]["code"]) == ("fail", ""), other
        la = _by_key(mv.local_ai_rows(_report(_row("Local AI: egress", "WARN", other))))
        assert (la["egress"]["state"], la["egress"]["code"]) == ("warn", ""), other


# ---------------------------------------------------- a code and its row text are one decision
EXPECTED_CODES = {  # function -> the codes of its rows, in source order; a new row is added here
    "_check_python": ["CHK-001"],
    "_check_uv": ["CHK-002"],
    "_check_config": ["CHK-003", "CHK-003", "CHK-003"],
    "_check_profile": ["CHK-029", "CHK-004"],
    "_check_ollama": ["CHK-005", "CHK-005"],
    "_check_models_present": ["CHK-027", "CHK-006"],
    "_check_num_ctx": ["CHK-028", "CHK-027", "CHK-007", "CHK-007"],
    "_check_needle": ["CHK-028", "CHK-027", "CHK-027", "CHK-008", "CHK-027", "CHK-027",
                      "CHK-027", "CHK-027"],
    "_check_thinking": ["CHK-009", "CHK-027", "CHK-027"],
    "_check_schema": ["CHK-027", "CHK-010"],
    "_check_browser": ["CHK-027", "CHK-030", "CHK-011"],
    "_check_mcp_port": ["CHK-012"],
    "_check_ollama_env": ["CHK-013"],  # one WARN branch, one row per variable (#181)
    "_check_local_ai_settings": ["CHK-014", "CHK-015"],
    "_check_cloud_model_tags": ["CHK-016"],
    "_check_local_ai_egress": ["CHK-027", "CHK-027", "CHK-017", "CHK-027"],
    "_check_model_digests": ["CHK-018"],
    "_check_ingest_contact": ["CHK-019"],
    "_check_ats_shapes": ["CHK-027", "CHK-020", "CHK-027", "CHK-027"],
    "_check_database": ["CHK-031", "CHK-021", "CHK-031"],
    "_check_hosted": ["CHK-022", "CHK-022", "CHK-023", "CHK-022", "CHK-022"],
    "_skip_local_checks": ["CHK-023"],
    "_check_inprocess_embed": ["CHK-023", "CHK-023"],
    "_settings_for_doctor": ["CHK-024", "CHK-024"],
    "_run_probes": ["CHK-025"],
    "check_model_fit": ["CHK-026"],
}
# A specific code may sit only on a branch whose own text proves its cause.
PROVES = {
    "CHK-028": r"not built", "CHK-007": r"num_ctx", "CHK-008": r"LOST the code",
    "CHK-009": r"thinks before answering", "CHK-010": r"did not match the schema",
    "CHK-011": r"no Edge, Chrome", "CHK-030": r"bad_path", "CHK-029": r"could not be read",
    "CHK-006": r"missing: ", "CHK-031": r"not initialized|revision mismatch",
    "CHK-005": r"Ollama reachable",
    "CHK-019": r"placeholder contact", "CHK-013": r"OLLAMA_",
}
# Text that says a check could not run. Such a branch gets CHK-027, unless the thing it could
# not do IS the failure the code names (an invalid file, an unreachable engine, unread settings).
COULD_NOT = re.compile(r"could not|unverified|not verified|probe failed|timed out|answered \{|"
                       r"_why\(exc\)|context not verified")
CAUSE_IS_THE_FAILURE = {("_check_config", "CHK-003"), ("_check_ollama", "CHK-005"),
                        ("_settings_for_doctor", "CHK-024"), ("_check_database", "CHK-021"),
                        ("_check_profile", "CHK-029")}


def _coded_rows():
    """(function, code, source of the call) for every coded health check row, in source order."""
    rows = []
    for rel in ("doctor.py", "llm/profiles.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        for fn in ast.walk(ast.parse(src)):
            if not isinstance(fn, ast.FunctionDef):
                continue
            calls = sorted((n for n in ast.walk(fn) if isinstance(n, ast.Call)
                            and getattr(n.func, "attr", "") == "add"),
                           key=lambda n: (n.lineno, n.col_offset))
            for call in calls:
                code = next((k.value.value for k in call.keywords if k.arg == "code"), None)
                if code:
                    # the source plus its string pieces joined, so a sentence split over two
                    # literals still reads as one
                    pieces = "".join(c.value for c in ast.walk(call)
                                     if isinstance(c, ast.Constant) and isinstance(c.value, str))
                    rows.append((fn.name, code, ast.get_source_segment(src, call) + " " + pieces))
    return rows


def test_every_coded_row_is_in_the_frozen_table_and_nothing_else():
    got: dict[str, list[str]] = {}
    for fn, code, _src in _coded_rows():
        got.setdefault(fn, []).append(code)
    assert got == EXPECTED_CODES


# Rows whose words are built in a variable: the static reading cannot see them, so each branch
# is driven at run time in tests/test_doctor_code_agreement.py (its RUNTIME_PROVEN must match).
RUNTIME_PROVEN = frozenset({
    "_check_python", "_check_uv", "_check_ollama_env", "_check_local_ai_settings",
    "_check_cloud_model_tags", "_check_local_ai_egress", "_check_model_digests",
    "_check_ats_shapes",
})


def _static_rows():
    return [r for r in _coded_rows() if r[0] not in RUNTIME_PROVEN]


def test_the_runtime_proven_rows_are_the_ones_the_runtime_tests_drive():
    from tests import test_doctor_code_agreement as runtime

    assert RUNTIME_PROVEN == runtime.RUNTIME_PROVEN
    assert RUNTIME_PROVEN <= set(EXPECTED_CODES)


def test_a_specific_code_sits_only_on_a_branch_that_proves_its_cause():
    for fn, code, src in _static_rows():
        proof = PROVES.get(code)
        assert proof is None or re.search(proof, src), (fn, code, src[:120])


def test_a_branch_that_says_the_check_could_not_run_has_the_general_code():
    rows = _static_rows()
    general = [r for r in rows if r[1] == "CHK-027"]
    specific = [r for r in rows if r[1] != "CHK-027"]
    # both groups are real, not empty (the runtime-proven rows are counted in their own tests)
    assert len(general) >= 10 and len(specific) >= 25
    assert all(COULD_NOT.search(src) for fn, _code, src in general)
    assert all(not COULD_NOT.search(src) or (fn, code) in CAUSE_IS_THE_FAILURE
               for fn, code, src in specific)


def test_the_general_code_says_nothing_about_a_cause():
    entry = error_codes.get("CHK-027")
    assert "could not finish" in entry.meaning and "does not say" in entry.meaning
