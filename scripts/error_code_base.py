"""Error codes stay permanent across branches (D-ERR-6, SPEC-INTERFACES.md §2.7).

`tests/test_error_codes.py` compares the registry with lists on the same branch, so a branch that
edits both could still renumber, reuse or delete a code. This check compares the working tree with
the registry at the branch's merge base with main instead: every code issued there must still be
here with the same title (live or retired), a code retired there stays retired, and, area by area,
the base's issued list is a prefix of this branch's. `scripts/check.py` runs it with the static
checks on every tier; it needs the same base the other diff guards need and never skips.

Both files are read with `ast`, never imported, so the base's code never runs. This branch's
registry must also match its reviewed form outside the entries (CANONICAL, D-ERR-9).
"""

from __future__ import annotations

import ast
import io
import re
import subprocess
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = "kestrel/error_codes.py"
ISSUED_FILE = "tests/test_error_codes.py"
# The 0.7.7 integration branch: task A merges there first, so B to G are checked against it.
INTEGRATION_REF = "origin/claude/inspiring-allen-cllv1c"
# ASCII only (D-ERR-11): a full-width digit or letter is not a code. No \d, which matches any digit.
CODE_SHAPE = re.compile(r"[A-Z]{3}-[0-9]{3}", re.ASCII)


@dataclass
class Registry:
    titles: dict[str, str] = field(default_factory=dict)  # every code defined, live or retired
    retired: dict[str, str] = field(default_factory=dict)  # retired code to its date
    duplicates: list[str] = field(default_factory=list)
    bad_shape: list[str] = field(default_factory=list)  # codes not AREA-NNN in ASCII, known area
    live: list[str] = field(default_factory=list)  # the codes of _ALL, in order


# The registry is checked by equality, not by a list of forbidden constructs (D-ERR-9): with the
# elements of `_ALL` and `_RETIRED_ALL` emptied and docstrings dropped, the file must match
# CANONICAL exactly (compared as `ast.dump`, so formatting and comments do not matter). Any other
# change (a decorator, an import, a signature, a base class, a nested definition, an annotation)
# changes the dump and fails. The entries themselves must be literal `_code(...)` calls with plain
# strings. Changing the registry's code means changing CANONICAL here too, in a commit that names
# this file and a decision ID (scripts/error_code_base.py is a protected path).
CANONICAL = r"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorCode:
    code: str
    area: str
    title: str
    meaning: str
    fix: str
    since: str


AREAS: dict[str, str] = {
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


@dataclass(frozen=True)
class RetiredCode:
    entry: ErrorCode
    retired: str


def _code(code: str, title: str, meaning: str, fix: str, since: str = "0.7.7") -> ErrorCode:
    return ErrorCode(
        code=code, area=code[:3], title=title, meaning=meaning, fix=fix, since=since
    )


def _retired(entry: ErrorCode, retired: str) -> RetiredCode:
    return RetiredCode(entry=entry, retired=retired)


_ALL: tuple[ErrorCode, ...] = ()
CODES: dict[str, ErrorCode] = {entry.code: entry for entry in _ALL}
_RETIRED_ALL: tuple[RetiredCode, ...] = ()
RETIRED: dict[str, RetiredCode] = {item.entry.code: item for item in _RETIRED_ALL}


def get(code: str) -> ErrorCode:
    try:
        return CODES[code]
    except KeyError:
        raise KeyError(f"unregistered error code: {code!r}") from None


def lookup(code: str) -> ErrorCode | None:
    if code in CODES:
        return CODES[code]
    if code in RETIRED:
        return RETIRED[code].entry
    return None


def anchor(code: str) -> str:
    return code.lower()


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_table() -> str:
    lines = [
        "# Error codes",
        "",
        "Each code below names one problem Kestrel can report, such as `STA-006`. Find the code "
        "to see what happened and what you can do. A code never changes meaning and is never "
        "reused.",
        "",
        "This page is generated from Kestrel's list of codes by `scripts/error_codes_table.py`. "
        "Do not edit it by hand.",
    ]
    for area, name in AREAS.items():
        entries = sorted((e for e in CODES.values() if e.area == area), key=lambda e: e.code)
        if not entries:
            continue
        lines += [
            "",
            f"## {name} ({area})",
            "",
            "| Code | What happened | What to do |",
            "|---|---|---|",
        ]
        for e in entries:
            lines.append(
                f'| <a id="{anchor(e.code)}"></a>{e.code} | **{_cell(e.title)}.** '
                f"{_cell(e.meaning)} | {_cell(e.fix)} |"
            )
    if RETIRED:
        lines += [
            "",
            "## Retired codes",
            "",
            "Kestrel no longer shows these codes, but an older message may still carry one.",
            "",
            "| Code | What happened | Retired |",
            "|---|---|---|",
        ]
        for code in sorted(RETIRED):
            e = RETIRED[code].entry
            lines.append(
                f'| <a id="{anchor(code)}"></a>{code} | **{_cell(e.title)}.** {_cell(e.meaning)} '
                f"| {RETIRED[code].retired} |"
            )
    return "\n".join(lines) + "\n"
"""
CODE_FIELDS = ("code", "title", "meaning", "fix", "since")


def _bad(node: ast.AST, what: str) -> ValueError:
    return ValueError(f"{REGISTRY} line {getattr(node, 'lineno', '?')}: {what}")


def _code_call(node: ast.AST) -> tuple[str, str]:
    """(code, title) of a literal `_code("AREA-NNN", "title", "meaning", "fix"[, "since"])`."""
    if not (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_code"
    ):
        raise _bad(node, "an entry that is not a literal _code(...) call")
    if len(node.args) > len(CODE_FIELDS):
        raise _bad(node, "a _code(...) call with too many arguments")
    values: dict[str, ast.expr] = dict(zip(CODE_FIELDS, node.args, strict=False))
    for kw in node.keywords:
        if kw.arg not in CODE_FIELDS or kw.arg in values:
            raise _bad(node, "a _code(...) call with an unknown or repeated argument")
        values[kw.arg] = kw.value
    out: dict[str, str] = {}
    for name, arg in values.items():
        if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
            raise _bad(node, f"a _code(...) {name} that is not a plain string")
        out[name] = arg.value
    if "code" not in out or "title" not in out:
        raise _bad(node, "a _code(...) call without a code and a title")
    return out["code"], out["title"]


def _tuple(tree: ast.Module, name: str) -> ast.Tuple:
    """The one top-level tuple literal bound to `name`."""
    found = [
        n.value
        for n in tree.body
        if isinstance(n, ast.AnnAssign | ast.Assign)
        and [t.id for t in (n.targets if isinstance(n, ast.Assign) else [n.target])
             if isinstance(t, ast.Name)] == [name]
    ]
    if len(found) != 1 or not isinstance(found[0], ast.Tuple):
        raise ValueError(f"{REGISTRY}: {name} must be one top-level tuple literal")
    return found[0]


def _drop_docstrings(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]  # type: ignore[attr-defined]


def skeleton(source: str) -> str:
    """The registry's code with every entry and docstring removed, as `ast.dump`."""
    tree = ast.parse(source)
    _drop_docstrings(tree)
    for name in ("_ALL", "_RETIRED_ALL"):
        _tuple(tree, name).elts = []
    return ast.dump(tree)


def _areas() -> set[str]:
    for node in ast.parse(CANONICAL).body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "AREAS":
            return set(ast.literal_eval(node.value))  # type: ignore[arg-type]
    raise ValueError("CANONICAL has no AREAS")


def well_formed(code: str) -> bool:
    return CODE_SHAPE.fullmatch(code) is not None and code[:3] in _areas()


def _entries(tree: ast.Module) -> Registry:
    reg = Registry()

    def add(code: str, title: str) -> None:
        if not well_formed(code):
            reg.bad_shape.append(code)
        if code in reg.titles:
            reg.duplicates.append(code)
        reg.titles[code] = title

    for elt in _tuple(tree, "_ALL").elts:
        code, title = _code_call(elt)
        reg.live.append(code)
        add(code, title)
    for elt in _tuple(tree, "_RETIRED_ALL").elts:
        if not (
            isinstance(elt, ast.Call)
            and isinstance(elt.func, ast.Name)
            and elt.func.id == "_retired"
            and len(elt.args) == 2
            and not elt.keywords
            and isinstance(elt.args[1], ast.Constant)
            and isinstance(elt.args[1].value, str)
        ):
            raise _bad(elt, 'a retired entry that is not _retired(_code(...), "YYYY-MM-DD")')
        code, title = _code_call(elt.args[0])
        reg.retired[code] = elt.args[1].value
        if code not in reg.live:  # a code both live and retired is reported as that
            add(code, title)
    return reg


def parse_registry(source: str, *, canonical: bool = True) -> Registry:
    """Every entry of `_ALL` and `_RETIRED_ALL`. With `canonical` (this branch's file), the rest
    of the file must match CANONICAL; the base's file is only read for its entries. Raises
    ValueError with a sentence when the source cannot be vouched for."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"{REGISTRY} does not parse (line {exc.lineno})") from None
    reg = _entries(tree)
    if canonical and skeleton(source) != skeleton(CANONICAL):
        raise ValueError(
            f"{REGISTRY}: its code outside the _ALL and _RETIRED_ALL entries differs from the "
            "reviewed form (CANONICAL in scripts/error_code_base.py). Only entries may change on "
            "a branch; a change to the code itself needs a decision"
        )
    return reg


@dataclass
class TestLists:
    issued: list[str] = field(default_factory=list)
    frozen_titles: dict[str, str] = field(default_factory=dict)
    ever_retired: list[str] = field(default_factory=list)


_INDIRECT = {"globals", "locals", "vars", "exec", "eval"}


def _bound_once(tree: ast.Module, name: str) -> ast.expr:
    """The one top-level value bound to `name`, never rebound, never reached indirectly."""
    stores = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Name) and n.id == name and not isinstance(n.ctx, ast.Load)
    ]
    scoped = [
        n for n in ast.walk(tree) if isinstance(n, ast.Global | ast.Nonlocal) and name in n.names
    ]
    if len(stores) != 1 or scoped:
        raise ValueError(f"{ISSUED_FILE}: {name} must be bound exactly once")
    indirect = [
        n
        for n in ast.walk(tree)
        if (isinstance(n, ast.Constant) and n.value == name)
        or (isinstance(n, ast.Attribute) and n.attr == name)
        or (isinstance(n, ast.Name) and n.id in _INDIRECT)
    ]
    if indirect:
        raise ValueError(
            f"{ISSUED_FILE} line {indirect[0].lineno}: {name} reached indirectly "
            "(by name as a string, as an attribute, or through globals)"
        )
    for node in tree.body:
        targets = [node.target] if isinstance(node, ast.AnnAssign) else getattr(node, "targets", [])
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return node.value  # type: ignore[attr-defined,return-value]
    raise ValueError(f"{ISSUED_FILE}: {name} must be bound at the top level")


def _strings(value: ast.expr, name: str) -> list[str]:
    if isinstance(value, ast.Tuple) and all(
        isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts
    ):
        return [e.value for e in value.elts]  # type: ignore[attr-defined]
    raise ValueError(f"{ISSUED_FILE}: {name} must be a tuple of plain strings")


def parse_test_lists(source: str) -> TestLists:
    """ISSUED, FROZEN_TITLES and EVER_RETIRED from the guard test. Raises ValueError when one is
    missing, bound more than once, reached indirectly, or not a plain literal."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"{ISSUED_FILE} does not parse (line {exc.lineno})") from None
    lists = TestLists(
        issued=_strings(_bound_once(tree, "ISSUED"), "ISSUED"),
        ever_retired=_strings(_bound_once(tree, "EVER_RETIRED"), "EVER_RETIRED"),
    )
    titles = _bound_once(tree, "FROZEN_TITLES")
    if not (
        isinstance(titles, ast.Dict)
        and all(
            isinstance(x, ast.Constant) and isinstance(x.value, str)
            for x in [*titles.keys, *titles.values]
        )
    ):
        raise ValueError(f"{ISSUED_FILE}: FROZEN_TITLES must be a dict of plain strings")
    lists.frozen_titles = {
        k.value: v.value  # type: ignore[union-attr]
        for k, v in zip(titles.keys, titles.values, strict=True)
    }
    return lists


def parse_issued(source: str) -> list[str]:
    """The `ISSUED` tuple of the guard test (see parse_test_lists)."""
    return parse_test_lists(source).issued


def _by_area(codes: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for code in codes:
        out.setdefault(code[:3], []).append(code)
    return out


def compare(
    base_reg: Registry,
    base_issued: list[str],
    reg: Registry,
    issued: list[str],
    base_lists: TestLists | None = None,
    lists: TestLists | None = None,
) -> list[str]:
    """Problems with this branch's registry against the base's; an empty list when it is fine."""
    problems = [
        f"RULE error codes: {code!r} is not a code: a code is AREA-NNN in plain ASCII, with an "
        "area from AREAS. Fix: retype it."
        for code in sorted(set(reg.bad_shape))
    ]
    problems += [
        f"RULE error codes: {code} is defined more than once in {REGISTRY}. Fix: give the new "
        "entry the next free number in its area."
        for code in sorted(set(reg.duplicates))
    ]
    for code, title in sorted(base_reg.titles.items()):
        if code not in reg.titles:
            problems.append(
                f'RULE error codes: {code} ("{title}") was issued on main and is gone from '
                f"{REGISTRY}. A code is never deleted or renumbered. Fix: put it back; to stop "
                'using it, move it whole to _RETIRED_ALL with _retired(_code(...), "YYYY-MM-DD").'
            )
        elif reg.titles[code] != title:
            problems.append(
                f'RULE error codes: {code}\'s title changed from "{title}" to '
                f'"{reg.titles[code]}". A code is never reused for a different problem. Fix: '
                "restore the title and issue a new code for the new problem."
            )
    for code, date in sorted(base_reg.retired.items()):
        if reg.retired.get(code) != date:
            problems.append(
                f"RULE error codes: {code} was retired on main ({date}) and is no longer retired "
                f"with that date. A retired code never comes back. Fix: restore its _retired entry."
            )
    problems += [
        f"RULE error codes: {code} is both live and retired. Fix: keep it only in _RETIRED_ALL."
        for code in sorted(set(reg.live) & set(reg.retired))
    ]
    problems += [
        f"RULE error codes: {code!r} in ISSUED is not a code (AREA-NNN in plain ASCII). Fix: "
        "retype it."
        for code in issued
        if not well_formed(code)
    ]
    if base_lists is not None and lists is not None:
        for code, title in sorted(base_lists.frozen_titles.items()):
            if lists.frozen_titles.get(code) != title:
                problems.append(
                    f'RULE error codes: FROZEN_TITLES in {ISSUED_FILE} no longer holds {code} as '
                    f'"{title}". It is append only. Fix: restore the base\'s entry.'
                )
        problems += [
            f"RULE error codes: EVER_RETIRED in {ISSUED_FILE} lost {code}. It is append only. "
            "Fix: restore it."
            for code in base_lists.ever_retired
            if code not in lists.ever_retired
        ]
    mine = _by_area(issued)
    for area, codes in sorted(_by_area(base_issued).items()):
        if mine.get(area, [])[: len(codes)] != codes:
            problems.append(
                f"RULE error codes: ISSUED in {ISSUED_FILE} no longer starts, for area {area}, "
                f"with main's list {', '.join(codes)}. ISSUED is append only. Fix: restore main's "
                "entries and add new codes after them."
            )
    return problems


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def decode(data: bytes, path: str) -> str:
    """The text of a Python file, refusing any encoding but plain UTF-8 (D-ERR-11). `ast.parse` on
    a str ignores a coding cookie that the import system honors, so a cookie such as
    raw_unicode_escape could make the app run text this check never saw."""
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    except SyntaxError as exc:
        raise ValueError(f"{path} has an unreadable encoding declaration ({exc})") from None
    if encoding != "utf-8":
        raise ValueError(f"{path} declares the encoding {encoding}; only plain UTF-8 is allowed")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{path} is not valid UTF-8") from None


def _at_base(base: str, path: str) -> str | None:
    """A file's text at the base commit, or None when the file did not exist there."""
    if _git("cat-file", "-e", f"{base}:{path}").returncode != 0:
        return None
    shown = subprocess.run(
        ["git", "show", f"{base}:{path}"], cwd=ROOT, capture_output=True, check=False
    )
    if shown.returncode != 0:
        raise ValueError(f"git could not read {path} at {base[:10]}")
    return decode(shown.stdout, path)


def resolve_base(fallback: str) -> str:
    """The merge base with the integration branch when that ref exists, else `fallback` (the
    merge base with main that check.py resolved)."""
    found = _git("merge-base", "HEAD", INTEGRATION_REF)
    sha = found.stdout.strip()
    return sha if found.returncode == 0 and sha else fallback


def shadow_problems() -> list[str]:
    """Anything that would be imported instead of kestrel/error_codes.py (D-ERR-11): a package
    named error_codes, or a compiled or stub file beside it."""
    folder = ROOT / "kestrel"
    found = sorted(
        p.relative_to(ROOT).as_posix()
        for p in (folder.iterdir() if folder.is_dir() else [])
        if (p.name == "error_codes" or p.name.startswith("error_codes."))
        and p.name != "error_codes.py"
    )
    return [
        f"RULE error codes: {path} would shadow {REGISTRY}. Fix: remove it; the registry is "
        f"{REGISTRY} only."
        for path in found
    ]


def problems(base: str) -> list[str]:
    """Run the comparison against `base` (the merge base with main that check.py resolved). A base
    git cannot read is a problem, never "no registry there"."""
    base = resolve_base(base)
    if _git("cat-file", "-e", f"{base}^{{commit}}").returncode != 0:
        return [
            f"RULE error codes: the permanence check cannot read the base commit {base[:10]}. "
            "Fix: run `git fetch origin main` (a full fetch, not a shallow one), then this again."
        ]
    try:
        base_source = _at_base(base, REGISTRY)
        base_tests = _at_base(base, ISSUED_FILE)
        base_reg = (
            parse_registry(base_source, canonical=False) if base_source is not None else Registry()
        )
        base_lists = parse_test_lists(base_tests) if base_tests is not None else TestLists()
        reg = parse_registry(decode((ROOT / REGISTRY).read_bytes(), REGISTRY))
        lists = parse_test_lists(decode((ROOT / ISSUED_FILE).read_bytes(), ISSUED_FILE))
    except (ValueError, OSError) as exc:
        return [
            f"RULE error codes: the permanence check cannot read the registry: {exc}. Fix: "
            "keep the registry in the _code(...) form and ISSUED as a tuple of strings."
        ]
    return shadow_problems() + compare(
        base_reg, base_lists.issued, reg, lists.issued, base_lists, lists
    )
