"""Optional resume sections (D-DOC-3): education, certifications, licenses and projects.

Each is a JSON list on doc_master of entries the operator typed. They are the operator's own text,
printed exactly as typed (hard rule 2 holds by construction: no model writes or selects them). A
section with no entries prints nothing, not even its heading. They are not part of the P3
document-selection schema: when present they are always included.

One place defines each kind's fields, which field is required, and how an entry becomes the
lead / sub / dates / detail lines the document template prints, so the editor, the routes and the
renderer cannot disagree."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

# kind -> (heading, [(field, label, required, placeholder)])
FIELDS: dict[str, tuple[str, list[tuple[str, str, bool, str]]]] = {
    "education": ("Education", [
        ("institution", "Institution", True, "State University"),
        ("credential", "Degree or credential", False, "B.S."),
        ("field", "Field of study", False, "Computer Science"),
        ("dates", "Year or dates", False, "2008"),
        ("detail", "Detail line", False, "Optional: honors, thesis, activities"),
    ]),
    "certifications": ("Certifications", [
        ("name", "Certification", True, "Certified Information Systems Security Professional"),
        ("issuer", "Issuer", False, "ISC2"),
        ("year", "Year", False, "2021"),
        ("credential_id", "Credential ID", False, "Optional"),
    ]),
    "licenses": ("Licenses", [
        ("name", "License", True, "Professional Engineer"),
        ("issuer", "Issuer or jurisdiction", False, "The issuing state or board"),
        ("year", "Year", False, "2015"),
    ]),
    "projects": ("Projects", [
        ("name", "Project", True, "Open-source inventory tool"),
        ("role", "Your role", False, "Optional: maintainer"),
        ("dates", "Dates", False, "Optional: 2022 - 2023"),
        ("description", "Description", False, "One line on what it is and what it did"),
        ("link", "Link text", False, "Optional: github.com/example/tool"),
    ]),
}

# The order the sections print in when a layout's section_order does not name them.
KINDS: tuple[str, ...] = ("education", "certifications", "licenses", "projects")
FIELD_MAX = 300
ENTRIES_MAX = 40


def field_names(kind: str) -> list[str]:
    return [f for f, *_ in FIELDS[kind][1]]


def required_field(kind: str) -> str:
    return next(f for f, _label, req, _ph in FIELDS[kind][1] if req)


def clean_entry(kind: str, data) -> dict[str, str] | None:
    """An entry from a form (or any mapping): the kind's known fields, stripped and capped,
    blanks dropped. None when the required field is empty, so an entry never prints headless.
    Text inside a field is kept exactly as typed apart from the outer whitespace."""
    out: dict[str, str] = {}
    for name in field_names(kind):
        value = str(data.get(name) or "").strip()[:FIELD_MAX]
        if value:
            out[name] = value
    return out if out.get(required_field(kind)) else None


def new_entry_id() -> str:
    """A stable id for one stored entry, so an edit or a remove addresses that entry rather than
    a list position another window may have shifted."""
    return uuid.uuid4().hex[:12]


def with_ids(entries: list) -> tuple[list, bool]:
    """The stored list with an `id` on every dict entry lacking one (an entry saved before ids
    existed), and whether anything was assigned. Other items pass through untouched."""
    out: list = []
    changed = False
    for e in entries:
        if isinstance(e, dict) and not isinstance(e.get("id"), str):
            e = {**e, "id": new_entry_id()}
            changed = True
        out.append(e)
    return out, changed


def entries_of(master, kind: str) -> list[dict]:
    """The stored entries of one kind, tolerant of a master built before the column existed
    (None) or a hand-edited row (non-dict items are skipped)."""
    raw = getattr(master, kind, None) or []
    return [e for e in raw if isinstance(e, dict) and e.get(required_field(kind))]


@dataclass
class Entry:
    lead: str
    sub: str = ""
    dates: str = ""
    detail: str = ""
    extra: str = ""


def _join(*parts: str | None) -> str:
    return ", ".join(p for p in parts if p)


def to_entry(kind: str, e: dict) -> Entry:
    """How one stored entry prints: a bold lead, an italic sub line, right-hand dates, a detail
    line under it, and an extra line (credential ID, link). Field text is unchanged."""
    g = e.get
    if kind == "education":
        return Entry(lead=g("institution", ""), sub=_join(g("credential"), g("field")),
                     dates=g("dates", ""), detail=g("detail", ""))
    if kind == "certifications":
        cid = g("credential_id")
        return Entry(lead=g("name", ""), sub=g("issuer", ""), dates=g("year", ""),
                     extra=f"Credential ID {cid}" if cid else "")
    if kind == "licenses":
        return Entry(lead=g("name", ""), sub=g("issuer", ""), dates=g("year", ""))
    return Entry(lead=g("name", ""), sub=g("role", ""), dates=g("dates", ""),
                 detail=g("description", ""), extra=g("link", ""))


def heading(kind: str) -> str:
    return FIELDS[kind][0]
