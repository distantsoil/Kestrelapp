"""Confidential mode's matcher (D-PRIV-1): operator-controlled, exact, no heuristics.

Each current employer carries an explicit list of NAMES TO HIDE that the operator controls (the
role editor's chips). The default list when a role is marked is the label as written, the label
with corporate suffixes stripped, and the ALL-CAPS form of each. Nothing else is inferred: no
lowercase variants, no CamelCase, no domain guessing. An entry matches exactly as typed,
case-sensitive, on whole-word boundaries (any non-word character, so "@", "." and "/" bound an
entry like "acme.com"), with any run of whitespace between its words.

Before an employer's names are substituted, every OTHER employer label in the operator's
documents (and its suffix-stripped form) is protected: the longest alternative is tried first and
a protected label maps to itself, so "Joined Acme Labs" survives when "Acme" is current.

Two replacements:

* OUTPUTS (rendered documents and previews, list views, the JSON API, MCP): the operator's
  "Show it as" text (`EmployerMap.redact`).
* ONLINE SENDS: a random code per hidden text (each spelling that occurred), eight uppercase
  letters and digits, fresh for every call (`EmployerMap.codebook()`). A reply is restored by
  replacing that exact code with exactly the text it replaced; a reply that still carries a
  code, or any five-character piece of one, is a failed attempt (one repair, then
  quarantine with the raw reply; hard rule 3). Save boundaries
  refuse text carrying a live code (`contains_live_code`)."""

from __future__ import annotations

import json
import re
import secrets
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

_SUFFIXES = (
    r"Inc\.?|LLC|L\.L\.C\.|LLP|L\.L\.P\.|LP|L\.P\.|Ltd\.?|Limited|Corp\.?|Corporation|Co\.?|"
    r"Company|plc|PLC|GmbH|AG|SA|S\.A\.|Pty\.?(?:\s+Ltd\.?)?|NV|N\.V\.|BV|B\.V\.|SE|Group|"
    r"Holdings"
)
_SUFFIX_RE = re.compile(rf"(?:,?\s+(?:{_SUFFIXES}))+\s*$")
# Everyday words an employer name may also be. Listing one hides it everywhere it appears with
# that exact capitalization, so the editor warns.
COMMON_WORDS = frozenset({
    "square", "box", "target", "ford", "apple", "gap", "shell", "visa", "oracle", "amazon",
    "meta", "block", "delta", "chase", "best", "general", "national", "united", "global",
    "first", "bank", "capital", "energy", "health", "systems", "group", "solutions", "services",
    "partners", "media", "labs", "digital", "union", "state", "city", "home", "market", "office",
    "word", "windows", "slack", "notion", "zoom", "uber", "lyft", "snap", "twitter", "x",
})
COMMON_WORD_WARNING = ("This is also an everyday word; it will be hidden everywhere it appears "
                       "with this exact capitalization.")
_CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LEN = 8
_PIECE = 5
_NAME_MAX = 120


def strip_suffix(name: str) -> str:
    return _SUFFIX_RE.sub("", name).strip().rstrip(",").strip()


def _dedupe(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    for i in items:
        i = " ".join((i or "").split())[:_NAME_MAX]
        if i and i not in out:
            out.append(i)
    return out


def default_names(label: str | None) -> list[str]:
    """The default names to hide for a marked role: the label as written, the label with
    corporate suffixes stripped, and the ALL-CAPS form of each."""
    base = " ".join((label or "").split())
    if not base:
        return []
    forms = [base]
    stripped = strip_suffix(base)
    if stripped and stripped != base:
        forms.append(stripped)
    return _dedupe([*forms, *(f.upper() for f in forms)])


def clean_names(names: Iterable[str]) -> list[str]:
    return _dedupe(names)


def is_common_word(name: str) -> bool:
    return " ".join(name.split()).casefold() in COMMON_WORDS


def _rx(form: str) -> str:
    body = r"\s+".join(re.escape(w) for w in form.split())
    return rf"(?<!\w){body}(?!\w)"


@dataclass(frozen=True)
class Employer:
    name: str
    shown_as: str
    names: tuple[str, ...] = ()
    inferred: bool = False
    section_id: int | None = None
    master_id: int | None = None

    def hide(self) -> list[str]:
        return list(self.names) or default_names(self.name)


@dataclass
class EmployerMap:
    """The current employer(s) and the other labels to protect. Falsy when nothing is hidden."""

    employers: list[Employer] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        alts: list[tuple[str, int | None]] = []  # (form, employer index or None = protected)
        mine: set[str] = set()
        for i, e in enumerate(self.employers):
            for form in e.hide():
                if form not in mine:
                    mine.add(form)
                    alts.append((form, i))
        for label in self.protected:
            for form in _dedupe([label, strip_suffix(label)]):
                if form and form not in mine:
                    alts.append((form, None))
        alts.sort(key=lambda a: len(a[0]), reverse=True)
        self._alts = alts
        self._pattern = (re.compile("|".join(f"({_rx(f)})" for f, _i in alts))
                         if self.employers and alts else None)

    def __bool__(self) -> bool:
        return bool(self.employers)

    def __len__(self) -> int:
        return len(self.employers)

    def names(self) -> list[str]:
        return [e.name for e in self.employers]

    def _sub(self, text: str | None, fn) -> str | None:
        """Each listed-name span becomes fn(the exact text matched, employer index)."""
        if not text or self._pattern is None:
            return text

        def repl(m: re.Match) -> str:
            _form, i = self._alts[m.lastindex - 1]
            return m.group(0) if i is None else fn(m.group(0), i)

        return self._pattern.sub(repl, text)

    def redact(self, text: str | None) -> str | None:
        """Outputs: each listed name becomes its employer's "Show it as" text."""
        return self._sub(text, lambda _got, i: self.employers[i].shown_as)

    def redact_name(self, name: str | None) -> str | None:
        """A whole org-name field (a list view's org column, a role header): the employer under
        any spelling ingestion treats as the same company, else the name-list substitution."""
        if not name or not self.employers:
            return name
        from kestrel.ingest.orgs import normalized_name

        key = normalized_name(name)
        for e in self.employers:
            if key and key == normalized_name(e.name):
                return e.shown_as
        return self.redact(name)

    def redact_json(self, value: Any) -> Any:
        return walk_strings(value, self.redact)

    def codebook(self, avoid: Iterable[str] = ()) -> CodeBook | None:
        """Fresh random codes for one call (None when nothing is hidden). `avoid` is the text
        about to be sent, so a code never collides with something already in it."""
        if not self.employers:
            return None
        return CodeBook(self, avoid)


# Codes issued in this process, so a save boundary can refuse text that still carries one.
_LIVE: deque[str] = deque(maxlen=4096)


def remember_codes(codes: Iterable[str]) -> None:
    for c in codes:
        if c not in _LIVE:
            _LIVE.append(c)


def contains_live_code(text: str | None) -> bool:
    return bool(text) and any(c in text for c in _LIVE)


def _new_code(taken: set[str], blob: str) -> str:
    while True:
        code = "".join(secrets.choice(_CODE_CHARS) for _ in range(_CODE_LEN))
        if code not in taken and code not in blob:
            return code


class CodeBook:
    """One call's codes: one per hidden TEXT (each spelling that actually occurred, "Acme Inc.",
    "ACME" and "acme.com" each its own), so a reply's code is restored to exactly the text it
    replaced. Codes are drawn as the text is encoded and never collide with it."""

    def __init__(self, emap: EmployerMap, avoid: Iterable[str] = (), *,
                 codes: dict[str, str] | None = None) -> None:
        self.emap = emap
        self._blob = "\n".join(avoid)
        self.by_text: dict[str, str] = dict(codes or {})  # hidden text -> code
        remember_codes(self.by_text.values())

    @property
    def codes(self) -> list[str]:
        return list(self.by_text.values())

    def _code(self, text: str) -> str:
        code = self.by_text.get(text)
        if code is None:
            code = _new_code(set(self.by_text.values()), self._blob)
            self.by_text[text] = code
            remember_codes([code])
        return code

    def to_json(self) -> dict:
        return {"codes": self.by_text, "names": self.emap.names()}

    @classmethod
    def from_json(cls, emap: EmployerMap, data: dict) -> CodeBook | None:
        """The stored codebook when it still matches this map's employers, else None."""
        if not data or data.get("names") != emap.names() or not isinstance(
                data.get("codes"), dict):
            return None
        return cls(emap, codes=dict(data["codes"]))

    def encode(self, text: str | None) -> str | None:
        return self.emap._sub(text, lambda got, _i: self._code(got))

    def encode_messages(self, messages: list[dict]) -> list[dict]:
        return [{**m, "content": self.encode(m.get("content") or "") or ""} for m in messages]

    def encode_json(self, value: Any, *, keys: bool = False) -> Any:
        return walk_strings(value, self.encode, keys=keys)

    def decode(self, text: str | None) -> str | None:
        """Exact codes back to exactly the text each replaced; nothing else is rewritten."""
        if not text:
            return text
        out = text
        for hidden, code in self.by_text.items():
            out = out.replace(code, hidden)
        return out

    def leftover(self, text: str) -> str | None:
        """A code, or any five-character piece of one, still in text (after decoding)."""
        for code in self.by_text.values():
            for k in range(_PIECE, len(code) + 1):
                for j in range(len(code) - k + 1):
                    if code[j:j + k] in text:
                        return code[j:j + k]
        return None

    # The guard's reply filter protocol (llm.guard.run_guarded).
    def restore_raw(self, content: str) -> tuple[str | None, str | None]:
        out = content or ""
        for hidden, code in self.by_text.items():
            out = out.replace(code, json.dumps(hidden, ensure_ascii=False)[1:-1])
        hit = self.leftover(out)
        return (None, _leftover_error(hit)) if hit else (out, None)

    def finish(self, value: Any) -> tuple[Any, str | None]:
        dump = getattr(value, "model_dump", None)
        data = dump() if dump is not None else value
        left: list[str] = []

        def fix(text: str) -> str:
            out = self.decode(text) or ""
            hit = self.leftover(out)
            if hit:
                left.append(hit)
            return out

        fixed = walk_strings(data, fix)
        if left:
            return None, _leftover_error(left[0])
        if dump is None:
            return fixed, None
        return (value if fixed == data else type(value).model_validate(fixed)), None


def _leftover_error(found: str) -> str:
    return (f"the reply contains part of a stand-in code ({found!r}); copy stand-in codes "
            "exactly or leave them out")


def walk_strings(value: Any, fn, *, keys: bool = False) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, dict):
        return {(fn(k) if keys and isinstance(k, str) else k): walk_strings(v, fn, keys=keys)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [walk_strings(v, fn, keys=keys) for v in value]
    return value


def from_pairs(pairs: Iterable[tuple], protected: Iterable[str] = ()) -> EmployerMap:
    """A map from (name, shown as[, names]) tuples: for tests and hand-built catalogs."""
    out = []
    for p in pairs:
        name, shown = p[0], p[1]
        names = tuple(p[2]) if len(p) > 2 else ()
        if name and shown:
            out.append(Employer(name, shown, names))
    return EmployerMap(out, list(protected))
