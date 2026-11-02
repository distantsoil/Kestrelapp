"""Resume / capability import (SPEC-MODULES 1.5, PROMPTS.md P7). The operator uploads or pastes a
resume; P7 maps it BY LINE REFERENCE (D-IMPORT-3) and this module copies the referenced lines into
a proposal of sections, slots, and variants. It writes NOTHING until the operator accepts:

  - every `derived: false` variant's text must appear VERBATIM in the input (else flagged);
  - at most two `derived: true` variants per slot (else flagged); import makes none, they are
    suggested per slot on demand (suggest_variants) and added one at a time;
  - proof points are extracted alongside and written to the profile on accept.

flag_proposal is pure (no model, no DB) so the checks are trivially verifiable; propose_import runs
P7 through the guard; accept_import is the only writer."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import DocMaster, DocSection, DocSlot, DocVariant
from kestrel.errors import UserFacingError
from kestrel.llm.guard import run_prompt
from kestrel.llm.prompts import load_prompt
from kestrel.services.profile_readiness import changed_at, mark_proof_points_changed

_WS = re.compile(r"\s+")
log = structlog.get_logger()
_MAX_DERIVED_PER_SLOT = 2


@dataclass
class ImportFlags:
    non_verbatim: list[str] = field(default_factory=list)  # "role/slot/variant" keys
    too_many_derived: list[str] = field(default_factory=list)  # "role/slot" keys
    # Bullet lines inside a role that the model did not place in any slot. Shown, not blocking:
    # the operator may mean to leave one out, but it is never dropped silently.
    unplaced: list[str] = field(default_factory=list)
    # Role company/title/dates the model copied that do not appear in the role's header lines.
    unverified: list[str] = field(default_factory=list)
    # Header-shaped lines (a separator and a date range) outside every role the outline mapped,
    # still there after one re-ask: roles Kestrel may have missed (#102). Never silent.
    missed_roles: list[str] = field(default_factory=list)
    # "role 2 start date: Sep 2019 (the model said Jan 2021)": a date the model gave that was not
    # the role's own header's, replaced by the header's.
    dates_taken: list[str] = field(default_factory=list)
    reasked: bool = False  # the outline was asked for a second time (#102); a count for the log
    # Roles Kestrel split on their own header lines after the model merged them into one range
    # (and a second ask did not fix it): shown as a note in the review.
    split_roles: int = 0

    @property
    def clean(self) -> bool:
        return not self.non_verbatim and not self.too_many_derived


@dataclass
class ImportProposal:
    value: object | None  # P7ResumeImport
    flags: ImportFlags
    error: str | None = None
    # What the markdown was converted from ("md", "txt", "docx", "pdf", "doc"), when known. Recorded
    # for the operator's review only; it changes nothing about validation.
    source_format: str | None = None


_EMPHASIS_RE = re.compile(r"\*\*|__")


def _verbatim(text: str, source: str) -> bool:
    """The variant text must appear in the input. Exact first; then whitespace-normalized so a
    reflowed paste still counts, then with bold markers (**, __) ignored on both sides, since a
    .docx bold run arrives as **text** and import copies the words without the markers
    (D-IMPORT-3). Wording and numbers must be unchanged."""
    t = (text or "").strip()
    if not t:
        return False
    if t in source:
        return True
    if _WS.sub(" ", t) in _WS.sub(" ", source):
        return True
    bare = _WS.sub(" ", _EMPHASIS_RE.sub("", t))
    return bare in _WS.sub(" ", _EMPHASIS_RE.sub("", source))


def flag_proposal(value, source_markdown: str) -> ImportFlags:
    flags = ImportFlags()
    for role in getattr(value, "roles", []) or []:
        rkey = getattr(role, "company_label", "?")
        for slot in getattr(role, "slots", []) or []:
            skey = getattr(slot, "key", "?")
            derived_count = 0
            for variant in getattr(slot, "variants", []) or []:
                if getattr(variant, "derived", False):
                    derived_count += 1
                elif not _verbatim(getattr(variant, "text", ""), source_markdown):
                    flags.non_verbatim.append(f"{rkey}/{skey}/{getattr(variant, 'key', '?')}")
            if derived_count > _MAX_DERIVED_PER_SLOT:
                flags.too_many_derived.append(f"{rkey}/{skey}")
    # Summary variants are variants too (they carry a `derived` flag): a derived:false summary whose
    # text is not verbatim in the source is invented prose that would reach the PDF (hard rule 2).
    for sv in getattr(value, "summary_variants", []) or []:
        text = getattr(sv, "text", "")
        if not getattr(sv, "derived", False) and not _verbatim(text, source_markdown):
            flags.non_verbatim.append(f"summary/{getattr(sv, 'key', '?')}")
    return flags


# ------------------------------------------------------------------ P7 v2: import by reference
# D-IMPORT-3. The model maps the resume by LINE NUMBER and the app copies the lines, so no resume
# text is ever generated: output stays a few hundred tokens per call whatever the resume length,
# and a model cannot reword what it never writes. An outline call finds the roles; one call per
# role maps its bullets. Rewordings are no longer made at import; they are suggested per slot,
# on demand (suggest_variants), and reviewed one at a time.
# A year that starts a line ("2019. Promoted") is text, not a list number: one or two digits only.
_BULLET_RE = re.compile(r"^\s*(?:[-*+\u2022\u25aa\u25e6\u2023\u2013]|\d{1,2}[.)])\s+")
_KEY_RE = re.compile(r"[^a-z0-9]+")
_LINE_LIST_FIELDS = frozenset({"lines", "header_lines", "context_lines"})
_LINE_FIELDS = frozenset({"first_line", "last_line"})
LAYOUT_WIDTH = 90  # the outline sees each BULLET line cut to this; it needs the shape
HEADER_WIDTH = 240  # other lines (headers with company, title, dates) are kept nearly whole
ROLE_CONCURRENCY = 4
_LABEL_MAX = 160


def number_lines(markdown: str) -> dict[int, str]:
    """1-based numbers for the non-blank lines of the source, in order. The numbers are the only
    vocabulary the model may answer in."""
    out: dict[int, str] = {}
    for raw in markdown.splitlines():
        if raw.strip():
            out[len(out) + 1] = raw.rstrip()
    return out


def _view(lines: dict[int, str], numbers, width: int | None = None) -> str:
    """Numbered lines for a prompt. With `width`, a bullet line is cut to it (the outline needs
    the shape, not the words); any other line keeps up to HEADER_WIDTH characters, so a long
    header's dates are never cut off before the model copies them."""
    rows = []
    for n in numbers:
        t = lines[n]
        if width is not None:
            cap = width if _BULLET_RE.match(t) else max(width, HEADER_WIDTH)
            if len(t) > cap:
                t = t[:cap].rstrip() + " ..."
        rows.append(f"[{n}] {t}")
    return "\n".join(rows)


def line_ref_schema(model_cls, valid: list[int]) -> dict:
    """The model's JSON schema with every line field restricted to the line numbers that exist,
    so on a constrained backend an invented reference cannot be produced at all (the P3 pattern,
    D-IMPORT-3). Validation in _check_refs still catches it on an unconstrained one."""
    from kestrel.llm.client import json_schema_for

    schema = json_schema_for(model_cls)
    enum = sorted(valid)

    def walk(node) -> None:
        if isinstance(node, dict):
            for name, prop in (node.get("properties") or {}).items():
                if not isinstance(prop, dict):
                    continue
                if name in _LINE_LIST_FIELDS and prop.get("type") == "array":
                    prop["items"] = {"type": "integer", "enum": enum}
                elif name in _LINE_FIELDS:
                    prop["enum"] = enum
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    return schema


_MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_SEASON = r"(?:spring|summer|fall|autumn|winter)"
# "Mar 2019", "2019", "03/2019", "Summer 2019", "Mar '19".
_DATE = (rf"(?:(?:{_MON}|{_SEASON})\s+(?:19|20)\d{{2}}|{_MON}\s+['\u2019]\d{{2}}"
         rf"|\d{{1,2}}/(?:19|20)\d{{2}}|(?:19|20)\d{{2}})")
_RANGE = rf"{_DATE}\s*(?:-|–|—|to)\s*(?:{_DATE}|present|current|now)"
_DATE_RANGE_RE = re.compile(rf"{_RANGE}\b", re.IGNORECASE)
_RANGE_ENDS_RE = re.compile(rf"{_RANGE}\W*$", re.IGNORECASE)
_RANGE_STARTS_RE = re.compile(rf"^\W*{_RANGE}", re.IGNORECASE)
_RANGE_GROUPS_RE = re.compile(
    rf"({_DATE})\s*(?:-|–|—|to)\s*({_DATE}|present|current|now)\b", re.IGNORECASE)
# A company separator: a pipe or a middle dot.
_COMPANY_SEP_RE = re.compile(r"[|\u00b7]")
# Sections whose dated lines are not jobs, and the headings that put a resume back in its jobs.
_OTHER_SECTIONS = frozenset({
    "education", "education and training", "skills", "technical skills", "key skills",
    "core competencies", "certifications", "certificates", "licenses and certifications",
    "training", "courses", "awards", "honors", "publications", "languages", "interests",
    "volunteer", "volunteering", "references", "patents", "affiliations", "projects"})
_SUMMARY_SECTIONS = frozenset({
    "summary", "profile", "professional summary", "career summary", "professional profile",
    "objective", "about", "about me", "overview"})
_ROLE_SECTIONS = frozenset({
    "experience", "work experience", "professional experience", "employment",
    "employment history", "work history", "career history", "relevant experience",
    "professional background", "consulting experience"})


_SENTENCE_WORDS = frozenset({"the", "for", "to", "by", "with", "that", "was", "were", "our",
                             "their", "its", "across", "through"})
HEADER_MAX_WORDS = 14


def _dated(text: str) -> bool:
    """A line that reads as a job header: a date range at its end or its start ("Mar 2019 -
    Present", "2015 to 2018"), short (about 14 words), with no bullet and no sentence before the
    date. A bullet never is, and neither is a sentence that only mentions a span of years
    ("Managed the ERP rollout for finance, 2021 - 2022")."""
    t = text.strip()
    if not t or len(t) > 240 or _BULLET_RE.match(t):
        return False
    if not (_RANGE_ENDS_RE.search(t) or _RANGE_STARTS_RE.search(t)):
        return False
    if len(t.split()) > HEADER_MAX_WORDS:
        return False
    before = _DATE_RANGE_RE.sub(" ", t)
    words = before.split()
    if ". " in before or before.rstrip(" ,;").endswith((".", "!", "?")) and len(words) > 3:
        return False
    # A sentence: lowercase function words in a longer line ("Managed the ERP rollout for
    # finance"), or a past-tense verb first with any of them. A title with "of" or "and" is not.
    hits = sum(w in _SENTENCE_WORDS for w in words)
    verb_first = bool(words) and words[0].lower().endswith("ed") and hits >= 1
    return not (len(words) >= 5 and (hits >= 2 or verb_first))


def _is_section_heading(text: str) -> bool:
    """A section heading in any case: a # heading, an ALL-CAPS or colon heading, or a short line
    (up to four words, no date, no sentence punctuation) that names a section ("Education",
    "Skills", "Certifications", "Projects", "Summary" ...)."""
    t = text.strip()
    if not t:
        return False
    if t.startswith("#") or _is_heading(t):
        return True
    words = t.split()
    key = t.rstrip(":").strip().lower()
    if len(words) > 4 or _dated(t) or t[-1] in ".!?;":
        return False
    return key in _OTHER_SECTIONS or key in _ROLE_SECTIONS or key in _SUMMARY_SECTIONS


def _header_shaped(text: str) -> bool:
    """A line that reads as a role header: a date range, with or without a separator (#102)."""
    return _dated(text)


def _non_role_lines(lines: dict[int, str]) -> set[int]:
    """Lines under an Education, Skills, Certifications (and similar) heading, up to the next
    heading: a date range there is a degree or a course, not a job."""
    out: set[int] = set()
    inside = False
    for n in sorted(lines):
        raw = lines[n].strip()
        key = raw.lstrip("#").strip().rstrip(":").strip().lower()
        if raw.startswith("#") or _is_section_heading(raw):
            inside = key in _OTHER_SECTIONS or key in _SUMMARY_SECTIONS
            if inside:
                out.add(n)
            continue
        if inside:
            out.add(n)
    return out


def _outside_roles(lines: dict[int, str], ranges, grouped: set[int]) -> list[int]:
    """Non-blank lines in no role range and in no summary or skills group."""
    return [n for n in sorted(lines)
            if lines[n].strip() and n not in grouped and not any(a <= n <= b for a, b in ranges)]


def _missed_headers(lines: dict[int, str], ranges, grouped: set[int]) -> list[int]:
    """Dated header lines in no role: a role the outline skipped. Lines under Education, Skills
    and similar headings are not jobs and are never named."""
    skip = _non_role_lines(lines)
    return [n for n in _outside_roles(lines, ranges, grouped)
            if n not in skip and _header_shaped(lines[n])]


def _company_header(text: str) -> bool:
    """A line that names a company (a separator) and a date range."""
    t = text.strip()
    return _dated(t) and bool(_COMPANY_SEP_RE.search(t))


_LOCATION_ONLY_RE = re.compile(
    r"^(?:remote|[A-Z][A-Za-z.'\- ]{1,30},\s*(?:[A-Z]{2}|[A-Z][a-z]+(?: [A-Z][a-z]+)?))$")


def _label_before_separator(text: str) -> str:
    """The part of a header before its separator (used to compare employers), or, with none, the
    line without its date range: copied as written, never composed."""
    t = text.strip()
    if _COMPANY_SEP_RE.search(t):
        return _COMPANY_SEP_RE.split(t, 1)[0].strip()
    return _DATE_RANGE_RE.sub("", t).strip(" ,;|-–—")


def _company_from_header(text: str) -> str:
    """The company of a split role's header, only where it is unambiguous: "Company | City, ST
    dates" (the part after the separator is a place). A header with no separator, or with
    a title-first order ("Title | Company, City"), gives "" and the review's note asks the
    person; a title or a sentence is never taken as a company."""
    t = text.strip()
    if not _COMPANY_SEP_RE.search(t):
        return ""
    rest = _DATE_RANGE_RE.sub(" ", t).strip(" ,;|-–—")
    parts = _COMPANY_SEP_RE.split(rest, 1)
    if len(parts) < 2:
        return ""
    left, right = parts[0].strip(), parts[1].strip()
    return left if _LOCATION_ONLY_RE.match(right) else ""


def _merged_headers(outline, lines: dict[int, str]) -> list[int]:
    """Header lines that sit INSIDE a role's range without starting a role: the model merged
    several roles into one (#102). A promotion's later title line (it follows the header block
    and names no company of its own), and a header that names the role's own employer, are not
    one."""
    skip = _non_role_lines(lines)
    out: list[int] = []
    for role in outline.roles:
        own = (role.company_label or "").strip().lower()
        block = set(role.header_lines)
        n = max(block) + 1 if block else 0
        while n in lines and _dated(lines[n]) and not _COMPANY_SEP_RE.search(lines[n]):
            block.add(n)
            n += 1
        for n in sorted(lines):
            if not role.first_line < n <= role.last_line or n in skip or not _dated(lines[n]):
                continue
            has_company = bool(_COMPANY_SEP_RE.search(lines[n]))
            if n in block and not has_company:
                continue
            company = _label_before_separator(lines[n]).lower()
            if has_company and own and company and (
                    company == own or company in own or own in company):
                continue
            out.append(n)
    return out


def _split_merged(outline, lines: dict[int, str], heads: list[int]):
    """Split each role at the company-header lines inside it. The first piece keeps the model's
    labels; each later piece takes its labels from its own header line, copied as written (a
    label that cannot be read from the header stays empty), so nothing is invented (hard rule 2)."""
    from kestrel.llm.schemas import P7OutlineRole

    roles = []
    for role in outline.roles:
        cuts = [n for n in heads if role.first_line < n <= role.last_line]
        if not cuts:
            roles.append(role)
            continue
        first_end = cuts[0] - 1
        roles.append(role.model_copy(update={
            "last_line": first_end,
            "header_lines": [n for n in role.header_lines if n <= first_end]
            or [role.first_line],
        }))
        for i, h in enumerate(cuts):
            end = cuts[i + 1] - 1 if i + 1 < len(cuts) else role.last_line
            text = lines[h].strip()
            company = _company_from_header(text)
            m = _RANGE_GROUPS_RE.search(text)
            head_lines = [h]
            if h + 1 <= end and h + 1 in lines and _header_shaped(lines[h + 1]) \
                    and not _COMPANY_SEP_RE.search(lines[h + 1]):
                head_lines.append(h + 1)  # a promotion's second title line
            roles.append(P7OutlineRole(
                header_lines=head_lines, first_line=h, last_line=end, company_label=company,
                title="", start_date=m.group(1) if m else "", end_date=m.group(2) if m else "",
            ))
    return outline.model_copy(update={"roles": roles})


_MONTH_NUM = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}


def _ym(text: str) -> tuple[int, int]:
    """A date as (year, month) for ordering; "Present", "Current" and "Now" come last."""
    low = text.lower()
    if any(w in low for w in ("present", "current", "now")):
        return (9999, 0)
    year = re.search(r"(?:19|20)\d{2}", low)
    short = re.search(r"['\u2019](\d{2})\b", low)
    y = int(year.group()) if year else (2000 + int(short.group(1)) if short else 0)
    numeric = re.match(r"\s*(\d{1,2})/", low)
    month = re.search(r"[a-z]{3}", low)
    m = int(numeric.group(1)) if numeric else _MONTH_NUM.get(month.group() if month else "", 0)
    return (y, m)


def _header_dates(lines: dict[int, str], header_lines) -> tuple[str, str] | None:
    """The role's own dates as written in its header line(s): the earliest start and the latest
    end of the date ranges found there (a promotion has one per title), or None when the header
    holds no range. Deterministic: no model is asked."""
    found = [m.groups() for n in header_lines if n in lines
             for m in _RANGE_GROUPS_RE.finditer(lines[n])]
    if not found:
        return None
    start = min((a for a, _b in found), key=_ym)
    end = max((b for _a, b in found), key=_ym)
    return _WS.sub(" ", start).strip(), _WS.sub(" ", end).strip()


def _same_date(a: str, b: str) -> bool:
    return _WS.sub(" ", (a or "")).strip().lower().rstrip(".") == \
        _WS.sub(" ", (b or "")).strip().lower().rstrip(".")


def _is_heading(text: str) -> bool:
    """A heading-only line: a few words in capitals ("PROFESSIONAL SUMMARY") or ending in a colon
    ("Tools:"). Never a bullet or a dated header."""
    t = text.strip()
    words = t.split()
    if not t or len(words) > 6 or _BULLET_RE.match(t) or _dated(t):
        return False
    return t.endswith(":") or (t.isupper() and sum(c.isalpha() for c in t) >= 3)


_CONTACT_RE = re.compile(
    r"@|https?://|www\.|linkedin\.|github\.|(?:\+?\d[\d ().-]{6,}\d)"
    r"|^[A-Z][A-Za-z.' -]+,\s*[A-Z]{2}\b")


def _name_headline_or_contact(text: str, first: bool) -> bool:
    """A top line that needs no placing: the name (line 1), contact details (email, phone, URL,
    a place), or a short headline. A sentence of profile text above the first heading is not."""
    t = text.strip()
    if _CONTACT_RE.search(t) and len(t.split()) <= 14:
        return True
    words = t.split()
    return len(words) <= (6 if first else 10) and not t.endswith((".", "!", "?"))


def _top_matter(lines: dict[int, str]) -> set[int]:
    """The name, headline and contact lines at the top: lines before the first heading after
    line 1 (at most eight), provided one exists. Any other sentence up there is reported."""
    heads = [n for n in sorted(lines) if n > 1 and (
        lines[n].strip().startswith("#") or _is_heading(lines[n]))]
    if not heads or heads[0] > 9:
        return set()
    first = min(lines) if lines else 1
    return {n for n in lines if n < heads[0] and not _BULLET_RE.match(lines[n])
            and not _dated(lines[n]) and _name_headline_or_contact(lines[n], n == first)}


def _slug(text: str, fallback: str) -> str:
    return _KEY_RE.sub("_", (text or "").lower()).strip("_")[:40] or fallback


def _join(lines: dict[int, str], refs) -> str:
    """The referenced lines, exactly as written, joined with single spaces (a wrapped bullet
    becomes one). Only the list marker of the first line is dropped."""
    parts = []
    for i, n in enumerate(refs):
        t = _EMPHASIS_RE.sub("", lines[n].strip())
        parts.append(_BULLET_RE.sub("", t, count=1) if i == 0 else t)
    return " ".join(p for p in parts if p)


def _check_bullet_lines(lines: dict[int, str], refs: list[int], where: str) -> None:
    """One bullet is consecutive lines and only its first carries a list marker; anything else
    would merge two bullets into one variant (D-IMPORT-3 review)."""
    if refs and refs != list(range(refs[0], refs[0] + len(refs))):
        raise _RefError(f"{where} joins lines {refs} that are not consecutive")
    if any(_BULLET_RE.match(lines[n]) for n in refs[1:]):
        raise _RefError(f"{where} joins lines {refs} that are separate bullets")


def _skill_items(lines: dict[int, str], refs, label: str) -> list[str]:
    items: list[str] = []
    for n in refs:
        t = _BULLET_RE.sub("", lines[n].strip(), count=1)
        head, sep, rest = t.partition(":")
        if sep and len(head) <= 40:
            t = rest
        items += [i.strip(" *_") for i in re.split(r"[,;|\u2022\u00b7]", t) if i.strip(" *_")]
    return items


_OPEN_END = (",", "&", "-", "/", "(", "\u2013", "~", "$")
_OPEN_WORDS = frozenset({"a", "an", "and", "the", "or", "to", "of", "for", "with", "in", "on",
                         "at", "by", "from", "into", "across", "including"})


def _continues(prev: str, text: str) -> bool:
    """Whether `text` reads as the rest of `prev`: it starts lowercase, or `prev` stops on a
    comma, an ampersand, a dash or a word that cannot end a sentence ("and", "to", "of"). A
    capitalized line after a finished-looking one is never joined: it may be its own bullet with a
    glyph the marker pattern does not know, and fusing two bullets would hide one (review)."""
    # A figure such as "$450K" or "~$450K" can land at the start of a wrapped line.
    head = text.lstrip("~$")
    if not text or not head or not head[0].isalnum():
        return False
    if text[0].islower():
        return True
    words = prev.rstrip().split()
    return prev.rstrip().endswith(_OPEN_END) or bool(words and words[-1].lower() in _OPEN_WORDS)


_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DATE_WORDS = frozenset({"present", "current", "now", "to", "and", "since", "until", "ongoing"})
_LOCATION_RE = re.compile(
    r"^[A-Z][A-Za-z.' -]{1,30},\s*(?:[A-Z]{2}|[A-Z][a-z]+(?: [A-Z][a-z]+)?)"
    r"(?:\s*[|\u2022\u00b7]\s*.*)?$"
)
_MINOR_WORDS = frozenset(
    {"and", "of", "the", "for", "to", "in", "on", "at", "&", "a", "an", "with"})


_TRAILING_DATE_RE = re.compile(
    r"(?:\b(?:" + "|".join(_MONTHS) + r")[a-z]*\.?\s+)?\b(?:19|20)\d{2}\s*[-\u2013\u2014]?\s*$",
    re.IGNORECASE,
)


def _not_context(text: str) -> bool:
    """Whether a line directly under a role header is plainly not a context sentence: a date or
    date range, a location, or a sub-heading (a trailing colon, or a few Title Case words with no
    sentence punctuation). Such a line is left for the operator to place."""
    words = re.findall(r"[A-Za-z]+|\d+", text)
    if words and all(w.isdigit() or w.lower()[:3] in _MONTHS or w.lower() in _DATE_WORDS
                     for w in words) and any(w.isdigit() for w in words):
        return True
    if len(text) <= 60 and _LOCATION_RE.match(text):
        return True
    if text.endswith(":") or _TRAILING_DATE_RE.search(text):
        return True
    toks = text.split()
    return (len(toks) <= 10 and not text.endswith((".", "!", "?", ","))
            and all(t[0].isupper() or t.lower() in _MINOR_WORDS or not t[0].isalpha()
                    for t in toks))


def _wraps(prev: str, text: str) -> bool:
    """Whether `text`, a line with no list marker that follows a bullet's last line, is that
    bullet's wrapped tail whatever character it starts with ("ACME's ...", "(see ...)", "$450K"):
    the bullet has not ended (no closing punctuation), and the line is not a date, a location, a
    heading or a sub-heading. A bullet that ended with a full stop is never extended (#102)."""
    prev, text = prev.rstrip(), text.strip()
    if not prev or not text or prev[-1] in ".!?:;":
        return False
    first = text.split()[0].strip("(),;:")
    starts_ok = (text[0].islower() or text[0] == "(" or text[0].isdigit()
                 or (len(first) >= 2 and first.isupper() and first.isalpha()))
    return starts_ok and not (_dated(text) or _is_heading(text) or _not_context(text))


def _attach_continuations(lines: dict[int, str], rng: tuple[int, int], header_lines,
                          rmap) -> None:
    """A wrapped line the role map left out joins the block it continues (D-IMPORT-6): line n is
    unplaced, carries no list marker, line n-1 ends a context or bullet block of the same role, and
    n reads as the rest of n-1 (`_continues`). A proof point that ended on n-1 inside that block is
    extended with it. The text stays the operator's own, copied verbatim; only where it lands is
    decided here. Mutates rmap.

    When the role map placed no context line at all, an unplaced line directly under the last
    header line starts the context block only if the next unplaced line continues it (`_continues`)
    and it is not a date, a location or a sub-heading (#84). A lone line, or a pair that does not
    read as one wrapped sentence, stays in the unplaced warning: surfaced, never misplaced. The
    text is the operator's own, copied as written."""
    first, last = rng
    if not rmap.context_lines and header_lines:
        under = max(header_lines) + 1
        placed = {n for b in rmap.bullets for n in b.lines} | set(header_lines)
        nxt = under + 1

        def free(n: int) -> bool:
            return (first <= n <= last and n in lines and n not in placed
                    and bool(lines[n].strip()) and not _BULLET_RE.match(lines[n]))

        if (free(under) and free(nxt) and not _not_context(lines[under].strip())
                and _continues(lines[under].strip(), lines[nxt].strip())):
            rmap.context_lines = [under]
    blocks: list[list[int]] = [rmap.context_lines] + [b.lines for b in rmap.bullets]
    owner = {n: blk for blk in blocks for n in blk}
    taken = set(owner) | set(header_lines)
    for n in sorted(x for x in lines if first <= x <= last):
        if n in taken or _BULLET_RE.match(lines[n]):
            continue
        blk = owner.get(n - 1)
        if blk is None or blk[-1] != n - 1:
            continue
        bullet_block = any(blk is b.lines for b in rmap.bullets)
        if _continues(lines[n - 1].strip(), lines[n].strip()) or (
                bullet_block and _wraps(lines[n - 1].strip(), lines[n].strip())):
            blk.append(n)
            owner[n] = blk
            taken.add(n)
            for pp in rmap.proof_points:
                if pp.lines and pp.lines[-1] == n - 1 and set(pp.lines) <= set(blk):
                    pp.lines.append(n)


def _mend_proof_points(rmap) -> None:
    """A proof point cites a bullet; when that bullet wrapped, the proof point is the whole of it,
    one claim and not a first half (#102). A proof point that stops inside its bullet's lines is
    extended to the bullet's last line, and one that lies wholly inside another is dropped. Only
    line numbers move; the text is still the operator's own."""
    blocks = [b.lines for b in rmap.bullets]
    for pp in rmap.proof_points:
        if not pp.lines:
            continue
        for blk in blocks:
            if pp.lines[-1] in blk and blk[-1] != pp.lines[-1]:
                pp.lines = [*pp.lines, *[n for n in blk if n > pp.lines[-1]]]
                break
    keep = []
    for pp in rmap.proof_points:
        mine = set(pp.lines)
        if any(o is not pp and mine < set(o.lines) for o in rmap.proof_points):
            continue
        keep.append(pp)
    rmap.proof_points = keep


def place_unplaced(value, markdown: str, choices: list[tuple[str, str]]):
    """The operator's placement of lines the import left out (D-IMPORT-6). Each choice is
    (line text, target): "skills" adds the line's items to an "Additional skills" group, "role:N"
    (0-based) adds it to that role as a new bullet, anything else leaves it out. A line must be a
    line of the source as written; anything else is refused, so a tampered form cannot write
    text the operator never wrote (hard rule 2). accept_import re-checks verbatim after this."""
    from kestrel.llm.schemas import P7SkillGroup, P7Slot, P7Variant

    source = {t.strip() for t in number_lines(markdown).values()}
    value = value.model_copy(deep=True)
    slot_keys = {s.key for r in value.roles for s in r.slots}
    group = None
    for text, target in choices:
        text = text.strip()
        if not text or target not in {"skills"} | {f"role:{i}" for i in range(len(value.roles))}:
            continue
        if text not in source:
            raise ImportRejected(f"placed line is not in the source: {text[:80]!r}")
        if target == "skills":
            if group is None:
                taken = {g.key for g in value.skill_groups}
                group = P7SkillGroup(key=_unique("additional_skills", taken),
                                     label="Additional skills", items=[], default_visible=True)
                value.skill_groups.append(group)
            group.items += _skill_items({0: text}, [0], group.label)
            continue
        i = int(target.split(":", 1)[1])
        role = value.roles[i]
        body = _join({0: text}, [0])
        if not body:
            continue
        role.slots.append(P7Slot(
            key=_unique(f"r{i + 1}_added_{len(role.slots) + 1}", slot_keys), optional=False,
            default_variant_key="original",
            variants=[P7Variant(key="original", text=body, tags=[], derived=False)],
        ))
    value.skill_groups = [g for g in value.skill_groups if g.items]
    return value


class _RefError(ValueError):
    """A reference outside the source or outside its role: surfaced, never defaulted away."""


def _settle_role_ranges(outline):
    """Resolve the two overlaps a model commonly answers with, in code, before the strict check.

    1. A promotion: one employer, two titles with their own dates on consecutive header lines
       (for example "Acme | Director" then "Manager, Operations", each with its own dates).
       A smaller model maps that as two roles sharing the same body, which the overlap check
       rejected twice and failed the import (D-IMPORT-5). A role that starts inside another
       role's header block is merged into it: the header lines are combined, the range widened,
       the most recent title kept and the earlier start date taken. Every value is still copied
       from the source lines, so nothing is invented (hard rule 2).
    2. A role whose range runs into the next role's header: its last line is pulled back to the
       line before the next role starts.
    Anything else that overlaps is left for the strict check, which re-asks once and then fails
    plainly."""
    roles = sorted(outline.roles, key=lambda r: (r.first_line, r.last_line))
    kept: list = []
    for role in roles:
        prev = kept[-1] if kept else None
        if prev is not None and prev.first_line <= role.first_line <= prev.last_line:
            head_end = max([prev.first_line, *prev.header_lines])
            if role.first_line <= head_end + 1:  # a second title in the same header block
                kept[-1] = prev.model_copy(update={
                    "header_lines": sorted({*prev.header_lines, *role.header_lines,
                                            role.first_line}),
                    "last_line": max(prev.last_line, role.last_line),
                    "start_date": role.start_date or prev.start_date,
                })
                continue
            kept[-1] = prev.model_copy(update={
                "last_line": role.first_line - 1,
                "header_lines": [n for n in prev.header_lines if n < role.first_line],
            })
        kept.append(role)
    return outline.model_copy(update={"roles": kept})


def _extend_role_ranges(outline, lines: dict[int, str]):
    """A role's range always reaches the line before the next role's header, or before the next
    section heading, whichever comes first (#102: a model ended a role one bullet early and that
    bullet belonged to no role). It is only ever extended, and never across another role's header
    or a heading."""
    roles = sorted(outline.roles, key=lambda r: (r.first_line, r.last_line))
    skip = _non_role_lines(lines)
    stops = sorted({n for n in lines if _is_section_heading(lines[n])})
    headers = sorted(n for n in lines if n not in skip and _dated(lines[n]))
    out = []
    for i, r in enumerate(roles):
        nxt = roles[i + 1] if i + 1 < len(roles) else None
        bound = [min([nxt.first_line, *nxt.header_lines])] if nxt is not None else []
        bound += [n for n in stops if n > r.first_line]
        # Any dated header after this role's own header block is a role of its own, listed in the
        # outline or not: the range never reaches across it.
        own = set(r.header_lines)
        n = max(own) + 1 if own else 0
        while n in lines and _dated(lines[n]) and not _COMPANY_SEP_RE.search(lines[n]):
            own.add(n)
            n += 1
        bound += [n for n in headers if n > r.first_line and n not in own]
        limit = (min(bound) - 1) if bound else max(lines, default=r.last_line)
        if limit > r.last_line and r.last_line in lines:
            r = r.model_copy(update={"last_line": limit})
        out.append(r)
    return outline.model_copy(update={"roles": out})


def _repair_outline(outline, lines: dict[int, str]):
    """Fix, in code, the range mistakes a model commonly makes, instead of failing the import
    (#102: "role 2 header lines [28] fall outside the role" failed both the first try and the
    repair). Only line NUMBERS move; every label and every text is still copied from the source.

    - A role that ends before it starts has its two numbers swapped.
    - A header line outside its own role's range: the range is widened to reach it when no other
      role's header lies in between; when another role's range holds that line without claiming
      it, that role is pulled back to end just before it; otherwise the line is dropped from this
      role's header (it stays in the source and is reported as unplaced or as a missed role).
    - A role that overlaps one already kept is left out; its lines are reported, never lost.
    A reference to a line that does not exist is not touched: that stays a hard error (rule 2)."""
    every = set(lines)
    roles = []
    for r in outline.roles:
        a, b = r.first_line, r.last_line
        if a in every and b in every and a > b:
            r = r.model_copy(update={"first_line": b, "last_line": a})
        roles.append(r)
    for i, r in enumerate(roles):
        heads = [n for n in r.header_lines if n in every]
        outside = [n for n in heads if not r.first_line <= n <= r.last_line]
        for h in outside:
            others = [o for j, o in enumerate(roles) if j != i]
            lo, hi = min(r.first_line, h), max(r.last_line, h)
            blocked = any(lo <= n <= hi and not r.first_line <= n <= r.last_line
                          for o in others for n in o.header_lines)
            owner = next((o for o in others if o.first_line <= h <= o.last_line), None)
            if owner is not None and h not in owner.header_lines and h > owner.first_line:
                k = roles.index(owner)
                roles[k] = owner.model_copy(update={"last_line": h - 1})
                blocked = False
                owner = None
            if owner is None and not blocked:
                r = r.model_copy(update={"first_line": lo, "last_line": hi})
            else:
                r = r.model_copy(update={"header_lines": [n for n in r.header_lines if n != h]
                                         or [r.first_line]})
            roles[i] = r
    kept: list = []
    for r in sorted(roles, key=lambda x: (x.first_line, x.last_line)):
        if any(r.first_line <= k.last_line and k.first_line <= r.last_line for k in kept):
            log.warning("resume_import_role_dropped", reason="overlap")
            continue
        kept.append(r)
    return outline.model_copy(update={"roles": kept})


def _check_refs(refs, allowed: set[int], where: str) -> list[int]:
    out = [int(n) for n in refs]
    bad = [n for n in out if n not in allowed]
    if bad:
        raise _RefError(f"{where} referenced line(s) {bad} that do not exist there")
    return out


REF_RETRIES = 1  # one corrective re-ask on an invalid reference, the guard's repair in spirit


def _unique(key: str, taken: set[str]) -> str:
    out, n = key, 2
    while out in taken:
        out, n = f"{key}_{n}", n + 1
    taken.add(out)
    return out


def _in_header(value: str, header: str) -> bool:
    v = _WS.sub(" ", _EMPHASIS_RE.sub("", value or "")).strip().lower()
    return not v or v in _WS.sub(" ", _EMPHASIS_RE.sub("", header)).lower()


def _plain_failure(err: object) -> str:
    """A failure as the page may show it. A failed call to the model (a guard `request error:`
    text, or the exception itself) becomes a fixed sentence by kind, never the URL, status line or
    provider text it carried; the type, status and reason code go to the log. Kestrel's own
    reference errors (line numbers, a role label) pass through unchanged."""
    from kestrel.errors import classify_model_failure, is_model_failure

    text = str(err)
    for prefix in ("request error: ", "repair request error: "):
        if text.startswith(prefix) and "HTTP " not in text and "://" not in text:
            return text[len(prefix):]  # the guard's own fixed sentence
    if isinstance(err, BaseException):
        if not is_model_failure(err):
            log.warning("resume_import_role_failed", error_type=type(err).__name__)
            return "an unexpected problem; the details are in the log"
    elif "request error" not in text and "HTTP " not in text and "://" not in text:
        return text
    code, status, sentence = classify_model_failure(err)
    log.warning("resume_import_model_failed", error_type=type(err).__name__, status=status,
                reason=code)
    return sentence


async def _ask(client, spec, user: str, schema: dict, check, on_quarantine, book=None):
    """Run one P7 call and its reference check. An invalid reference gets one corrective re-ask
    naming the error (the guard already spent its repair on JSON shape, not on references), then
    surfaces as an error. Returns (value, None) or (None, error).

    `book` (D-PRIV-1): the import's stand-in codes when the call goes online under confidential
    mode. The line text sent carries the codes; the model answers in line numbers and Kestrel
    copies the ORIGINAL lines locally, so the verbatim guarantee is untouched. Only the labels
    the model writes itself (company, title) have the exact codes restored."""
    messages = [{"role": "system", "content": spec.system}, {"role": "user", "content": user}]
    for attempt in range(REF_RETRIES + 1):
        sent = book.encode_messages(messages) if book is not None else messages
        res = await run_prompt(client, spec, sent, schema=schema,
                               on_quarantine=on_quarantine, reply_filter=book)
        if not res.ok or res.value is None:
            return None, res.error
        try:
            return check(res.value), None
        except _RefError as exc:
            if attempt == REF_RETRIES:
                return None, str(exc)
            # The history holds real text; the next send codes it again.
            said = book.decode(res.raw or "") if book is not None else (res.raw or "")
            messages = [*messages, {"role": "assistant", "content": said or ""},
                        {"role": "user", "content": f"That answer was invalid: {exc}. Answer "
                         "again with the same JSON shape, using only the numbered lines."}]
    return None, "no answer"  # pragma: no cover (the loop always returns)


async def propose_import(
    client, markdown: str, *, on_quarantine=None, source_format: str | None = None,
    mapping=None,
) -> ImportProposal:
    """Run P7 v2 and flag the proposal. Writes nothing. A structural failure returns an error.
    `markdown` is the converted source (kestrel.docs.convert) or the pasted text; every variant
    is copied from it, and the verbatim check still runs against exactly that text."""
    import asyncio

    from kestrel.llm.schemas import (
        P7Outline,
        P7ProofPoint,
        P7ResumeImport,
        P7Role,
        P7RoleMap,
        P7Slot,
        P7SummaryVariant,
        P7Variant,
    )

    def fail(msg: str) -> ImportProposal:
        return ImportProposal(value=None, flags=ImportFlags(), error=msg,
                              source_format=source_format)

    lines = number_lines(markdown)
    if not lines:
        return fail("There is no text to import.")
    every = set(lines)
    # D-PRIV-1: one set of stand-in codes for this import. The line TEXT the model reads is coded
    # before any view shortens it (a cut must never leave half a name), while `lines`, which
    # every copied bullet comes from, stays the original text.
    book = mapping.codebook(lines.values()) if mapping else None
    shown = {n: book.encode(t) for n, t in lines.items()} if book is not None else lines

    def check_outline(outline):
        outline = _extend_role_ranges(
            _repair_outline(_settle_role_ranges(outline), lines), lines)
        ranges: list[tuple[int, int]] = []
        for i, role in enumerate(outline.roles):
            first, last = _check_refs([role.first_line, role.last_line], every, f"role {i + 1}")
            if first > last:
                raise _RefError(f"role {i + 1} starts at line {first} after it ends ({last})")
            for a, b in ranges:
                if first <= b and a <= last:
                    raise _RefError(f"role {i + 1} (lines {first}-{last}) overlaps another role")
            ranges.append((first, last))
            heads = _check_refs(role.header_lines, every, f"role {i + 1} header")
            if any(not first <= n <= last for n in heads):
                raise _RefError(f"role {i + 1} header lines {heads} fall outside the role")
        summary = [_check_refs(b.lines, every, "the summary") for b in outline.summary_blocks]
        skills = [(g, _check_refs(g.lines, every, f"skills {g.label!r}"))
                  for g in outline.skill_groups]
        grouped_lines = {n for refs in summary for n in refs} | {
            n for _g, refs in skills for n in refs}
        for i, (a, b) in enumerate(ranges):
            if any(a <= n <= b for n in grouped_lines):
                raise _RefError(f"role {i + 1} (lines {a}-{b}) overlaps the summary or skills "
                                "lines")
        return outline, ranges, summary, skills

    spec = load_prompt("resume_outline/v1")
    outline_user = spec.user.replace("{numbered_lines}",
                                     _view(shown, sorted(lines), LAYOUT_WIDTH))
    got, err = await _ask(client, spec, outline_user, line_ref_schema(P7Outline, sorted(lines)),
                          check_outline, on_quarantine, book)
    if err is not None:
        return fail(f"Mapping the resume's layout failed: {_plain_failure(err)}")
    outline, ranges, summary, skills = got
    n_first = len(outline.roles)
    grouped = {n for refs in summary for n in refs} | {n for _g, refs in skills for n in refs}
    missed = _missed_headers(lines, ranges, grouped)
    merged = _merged_headers(outline, lines)
    reasked = False
    if missed or merged:
        # A header-shaped line outside every role is probably a role the outline skipped, and a
        # company header inside a range is a role it merged into the one before (#102). One
        # re-ask names those lines; the answer goes through the same reference check, and is
        # kept only if it maps at least as many roles as the first.
        reasked = True
        named = "\n".join(f"[{n}] {shown[n].strip()[:HEADER_WIDTH]}" for n in [*missed, *merged])
        again = (outline_user + "\n\nYour previous answer left these lines outside every role "
                 "or inside another role, but each looks like a role header (a company with a "
                 "date range). Answer again with the same JSON shape and give each its own "
                 "role, or leave one out only if it is not a role:\n" + named)
        got2, err2 = await _ask(client, spec, again,
                                line_ref_schema(P7Outline, sorted(lines)), check_outline,
                                on_quarantine, book)
        if err2 is None and len(got2[0].roles) >= n_first:
            outline, ranges, summary, skills = got2
            grouped = ({n for refs in summary for n in refs}
                       | {n for _g, refs in skills for n in refs})
        missed = _missed_headers(lines, ranges, grouped)
        merged = _merged_headers(outline, lines)
    split = 0
    if merged:
        # Still merged after the second ask: split on the headers, in code. Every label comes
        # from its own header line, so nothing is invented; the review says it was done.
        try:
            before = len(outline.roles)
            fixed, f_ranges, f_summary, f_skills = check_outline(
                _split_merged(outline, lines, merged))
            outline, ranges, summary, skills = fixed, f_ranges, f_summary, f_skills
            split = len(outline.roles) - before
            grouped = ({n for refs in summary for n in refs}
                       | {n for _g, refs in skills for n in refs})
        except _RefError as exc:
            log.warning("resume_import_split_failed", error_type=type(exc).__name__)
    log.info("resume_import_outline", roles=len(outline.roles), first_answer_roles=n_first,
             reasked=reasked, missed_roles=len(missed), split_roles=split)

    role_spec = load_prompt("resume_role/v1")
    sem = asyncio.Semaphore(ROLE_CONCURRENCY)

    def role_label(i: int) -> str:
        r = outline.roles[i]
        return (" | ".join(x for x in (r.company_label, r.title) if x) or f"role {i + 1}")[:80]

    async def map_role(i: int):
        first, last = ranges[i]
        nums = [n for n in sorted(lines) if first <= n <= last]
        inside = set(nums)
        where = f"role {i + 1}"

        def check(rmap):
            _check_refs(rmap.context_lines, inside, f"{where} context")
            seen: set[int] = set()
            for b in rmap.bullets:
                refs = _check_refs(b.lines, inside, f"{where} bullet {b.key!r}")
                _check_bullet_lines(lines, refs, f"{where} bullet {b.key!r}")
                if seen.intersection(refs):
                    raise _RefError(f"{where} placed line(s) {sorted(seen & set(refs))} in two "
                                    "bullets")
                seen.update(refs)
            for pp in rmap.proof_points:
                refs = _check_refs(pp.lines, inside, f"{where} proof point {pp.key!r}")
                _check_bullet_lines(lines, refs, f"{where} proof point {pp.key!r}")
            return rmap

        msg = role_spec.user.replace("{role_label}", role_label(i)).replace(
            "{numbered_lines}", _view(shown, nums))
        async with sem:
            return await _ask(client, role_spec, msg, line_ref_schema(P7RoleMap, nums), check,
                              on_quarantine, book)

    results = await asyncio.gather(*(map_role(i) for i in range(len(outline.roles))),
                                   return_exceptions=True)
    errors = []
    for i, r in enumerate(results):
        if isinstance(r, BaseException):  # a hook or transport fault in one role, reported
            errors.append(f"Role {i + 1} ({role_label(i)}): {_plain_failure(r)}")
        elif r[1] is not None:
            errors.append(f"Role {i + 1} ({role_label(i)}): {_plain_failure(r[1])}")
    if errors:
        return fail("; ".join(errors))

    for i, (rmap, _err) in enumerate(results):
        _attach_continuations(lines, ranges[i], outline.roles[i].header_lines, rmap)
        _mend_proof_points(rmap)

    roles: list = []
    proofs: list = []
    accounted: set[int] = set()
    slot_keys: set[str] = set()
    proof_keys: set[str] = set()
    unverified: list[str] = []
    dates_taken: list[str] = []
    for i, (rmap, _err) in enumerate(results):
        role = outline.roles[i]
        accounted.update(role.header_lines)
        accounted.update(rmap.context_lines)
        header = " ".join(lines[n] for n in role.header_lines)
        for name in ("company_label", "title"):
            if not _in_header(getattr(role, name), header):
                unverified.append(f"role {i + 1} {name.replace('_', ' ')}: "
                                  f"{getattr(role, name)!r}")
        if not (role.company_label or "").strip():
            unverified.append(f"role {i + 1} company: not clear from its header line; "
                              "add it under Edit role details")
        # Dates come from the role's own header line, never from the model: a substring check
        # passed a start date that was only some other date in the same header (the end date, or
        # one from another role's line), so the model's date is compared with the parsed one.
        start, end = role.start_date or "", role.end_date or ""
        parsed = _header_dates(lines, role.header_lines)
        if parsed is not None:
            for label, said, own in (("start date", start, parsed[0]),
                                     ("end date", end, parsed[1])):
                if not _same_date(said, own):
                    dates_taken.append(f"role {i + 1} {label}: {own} (the model said "
                                       f"{said or 'nothing'})")
            start, end = parsed
        else:
            for name, said in (("start date", start), ("end date", end)):
                if said and not _in_header(said, header):
                    unverified.append(f"role {i + 1} {name}: {said!r}")
            start = start if _in_header(start, header) else ""
            end = end if _in_header(end, header) else ""
        slots = []
        for b in rmap.bullets:
            accounted.update(b.lines)
            text = _join(lines, b.lines)
            if not text:
                continue
            key = _unique(f"r{i + 1}_{_slug(b.key, f'b{len(slots) + 1}')}", slot_keys)
            slots.append(P7Slot(
                key=key, optional=b.optional, default_variant_key="original",
                variants=[P7Variant(key="original", text=text, tags=list(b.tags)[:6],
                                    derived=False)],
            ))
        company = (role.company_label or "")[:_LABEL_MAX]
        roles.append(P7Role(
            company_label=company, title=(role.title or "")[:_LABEL_MAX],
            start_date=start[:40], end_date=end[:40],
            context_line=_join(lines, rmap.context_lines), slots=slots,
        ))
        for pp in rmap.proof_points:
            claim = _join(lines, pp.lines)
            if not claim:
                continue
            proofs.append(P7ProofPoint(
                key=_unique(f"r{i + 1}_{_slug(pp.key, f'p{len(proofs) + 1}')}", proof_keys),
                claim=claim, evidence=", ".join(x for x in (company, role.title) if x),
                metric=pp.metric if pp.metric and pp.metric in claim else None,
            ))
    for refs in summary:
        accounted.update(refs)
    skill_keys: set[str] = set()
    for _g, refs in skills:
        accounted.update(refs)

    value = P7ResumeImport(
        roles=roles,
        summary_variants=[
            P7SummaryVariant(key=f"summary_{k + 1}", text=_join(lines, refs), derived=False)
            for k, refs in enumerate(summary) if refs
        ],
        skill_groups=[
            {"key": _unique(_slug(g.label, f"skills_{k + 1}"), skill_keys),
             "label": g.label[:_LABEL_MAX], "items": _skill_items(lines, refs, g.label),
             "default_visible": True}
            for k, (g, refs) in enumerate(skills) if refs
        ],
        proof_points=proofs,
    )
    flags = flag_proposal(value, markdown)
    # Coverage (hard rule 4 in spirit), shown and never silently dropped: every line inside a
    # role that is not its header, context or a bullet (a missed wrap, a skipped bullet), and
    # every bullet line outside all roles that the summary and skills did not take (a role the
    # outline missed).
    # Every non-blank line outside all role ranges and the summary and skills groups is reported,
    # not only bullets: a role header the outline skipped must never vanish (#102). Markdown
    # headings and the name and contact block above the first mapped line are furniture.
    in_role = {n for n in lines for a, b in ranges if a <= n <= b}
    used = accounted | grouped | in_role
    top = _top_matter(lines)

    def furniture(n: int) -> bool:
        """Lines that need no placing: markdown headings, the name and contact block above the
        first heading, and a heading whose section was read (the line under it is used)."""
        text = lines[n].strip()
        if text.startswith("#") or n in top:
            return True
        return _is_heading(text) and (n + 1) in used

    # Every non-blank line that is not a header, context, bullet, summary or skill line is
    # reported, prose and headings outside any role included (#102: a summary the model left
    # out, and a section after the skills, vanished silently). A heading over text that was not
    # placed is listed with it.
    flags.unplaced = [
        lines[n].strip() for n in sorted(lines)
        if n not in accounted and lines[n].strip()
        and (n in in_role or (not furniture(n) and n not in grouped))
    ]
    flags.missed_roles = [lines[n].strip() for n in missed]
    flags.reasked = reasked
    flags.split_roles = split
    log.info("resume_import_result", roles=len(roles), unplaced=len(flags.unplaced),
             missed_roles=len(flags.missed_roles), split_roles=split)
    flags.unverified = unverified
    flags.dates_taken = dates_taken
    return ImportProposal(value=value, flags=flags, source_format=source_format)


class ImportRejected(UserFacingError, ValueError):
    """accept_import refused because the proposal did not pass its own re-validation."""


class ImportTrackRefused(UserFacingError, ValueError):
    """accept_import was given a track Kestrel does not have. Nothing is written."""


class ProofPointsNotSaved(UserFacingError):
    """The resume is saved, but its proof points could not be written to profile.yml. Carries
    the cause on `__cause__`; the page shows `reason`, one of Kestrel's own sentences."""

    def __init__(self, master_id: int, reason: str) -> None:
        super().__init__(reason)
        self.master_id = master_id
        self.reason = reason


def _validate_for_accept(value, markdown: str) -> None:
    """The write boundary enforces its own invariants, never trusting the caller (hard rule 2):
    the proposal must be verbatim-clean, every default_variant_key must name a real variant of its
    slot, and slot keys must be unique across the master (they key the P3 schema and the config)."""
    flags = flag_proposal(value, markdown)
    if not flags.clean:
        raise ImportRejected(
            f"proposal not clean: non_verbatim={flags.non_verbatim} "
            f"too_many_derived={flags.too_many_derived}"
        )
    seen: set[str] = set()
    for role in value.roles or []:
        for slot in role.slots or []:
            if slot.key in seen:
                raise ImportRejected(f"duplicate slot key across the master: {slot.key!r}")
            seen.add(slot.key)
            keys = {v.key for v in (slot.variants or [])}
            if not keys:
                # A zero-variant slot has nothing to render and would give the P3 schema an
                # empty enum (an unsatisfiable grammar that quarantines every selection call).
                raise ImportRejected(
                    f"slot {slot.key!r} has no variants; every slot needs at least one variant "
                    "or must be dropped from the proposal"
                )
            if slot.default_variant_key and slot.default_variant_key not in keys:
                raise ImportRejected(
                    f"slot {slot.key!r} default_variant_key {slot.default_variant_key!r} "
                    f"is not one of its variants {sorted(keys)}"
                )


def open_ended_roles(value) -> list[tuple[int, object]]:
    """EVERY role whose end date is open (empty, "Present", "Current", "Now", or a date that has
    not passed) and that names a company, as (index in the document, role). The proposal offers
    to mark each as a current employer (D-PRIV-1, D-PRIV-2): a second concurrent job must not
    show in confidential mode just because it is not the newest. Nothing is set unless the
    operator ticks an offer."""
    from kestrel.docs.current_employer import is_open_ended

    return [(i, r) for i, r in enumerate(getattr(value, "roles", None) or [])
            if (r.company_label or "").strip() and is_open_ended(r.end_date)]


async def accept_import(
    session: AsyncSession, value, *, track: str, doc_kind: str, name: str, source_markdown: str,
    current_employer: str | None = None, current_employers: dict[int, str] | None = None,
) -> int:
    """The ONLY writer, and the enforcement boundary. Re-validates the proposal (verbatim, valid
    defaults, unique slot keys) and raises ImportRejected on any violation before writing anything.
    Creates the doc_master, sections, slots, variants, and merges proof points. Returns the id.

    `current_employer` is the "Show it as" text from the proposal's opt-in (D-PRIV-1): when given,
    and the newest role is open-ended, that role is marked as the current employer. None (the
    operator did not tick it) sets nothing."""
    from kestrel.db.enums import TRACKS

    if track not in TRACKS:
        raise ImportTrackRefused("That track is not one Kestrel knows, so nothing was imported.")
    _validate_for_accept(value, source_markdown)
    master = DocMaster(
        track=track,
        name=name,
        doc_kind=doc_kind,
        summary_variants=[{"key": s.key, "text": s.text} for s in (value.summary_variants or [])],
        skill_groups=[
            {
                "key": g.key,
                "label": g.label,
                "items": list(g.items),
                "default_visible": g.default_visible,
            }
            for g in (value.skill_groups or [])
        ],
    )
    session.add(master)
    await session.flush()

    # Role index -> "Show it as" text for every role the operator ticked. `current_employer` (one
    # text) is the older single form: it marks the first open-ended role.
    marks: dict[int, str] = dict(current_employers or {})
    if current_employer:
        offers = open_ended_roles(value)
        if offers:
            marks.setdefault(offers[0][0], current_employer)
    open_idx = {i for i, _r in open_ended_roles(value)}
    marks = {i: t for i, t in marks.items() if i in open_idx and (t or "").strip()}
    marked_sections: list[tuple[DocSection, str]] = []
    for i, role in enumerate(value.roles or []):
        section = DocSection(
            doc_master_id=master.id,
            section_kind="role",
            org_label=role.company_label,
            title=role.title,
            start_date=role.start_date,
            end_date=role.end_date,
            context_line=role.context_line,
            sort_order=i,
        )
        session.add(section)
        await session.flush()
        if i in marks:
            marked_sections.append((section, marks[i]))
        for j, slot in enumerate(role.slots or []):
            row = DocSlot(
                doc_section_id=section.id,
                key=slot.key,
                sort_order=j,
                optional=slot.optional,
                default_variant_key=slot.default_variant_key,
            )
            session.add(row)
            await session.flush()
            for v in slot.variants or []:
                session.add(
                    DocVariant(
                        doc_slot_id=row.id,
                        key=v.key,
                        text=v.text,
                        tags=list(v.tags),
                        derived=v.derived,
                    )
                )

    if marked_sections:
        from kestrel.docs.current_employer import set_current_employer

        for section, shown_as in marked_sections:  # each ticked role stays marked (D-PRIV-2)
            await set_current_employer(session, section, shown_as, exclusive=False)
    points = _proof_point_dicts(value.proof_points)
    previous_stamp = await changed_at(session, track)
    if points:
        # Stamped with the resume row, in the one commit below. The profile.yml write comes after
        # it; a crash or a failed write in between leaves the resume and no points, which
        # readiness reports as "review your resume", never as proof points with no resume.
        await mark_proof_points_changed(session, track)
    if track == "employment" and doc_kind == "resume":
        # Suggested target titles (P15, D-TTL-3): one queued run read from this resume. The
        # suggestions wait for an Accept; nothing is added to the profile here.
        # On an online Tier 2 it waits for Suggest again: the import did not ask to send the
        # resume a second time.
        from kestrel.services.title_suggest import _runs_online, request_title_suggestions

        if not await _runs_online(session):
            await request_title_suggestions(session, "import")
    await session.commit()
    await _merge_proof_points(session, track, points, previous_stamp, master.id)
    return master.id


def _proof_point_dicts(proof_points) -> list[dict]:
    return [
        {"key": p.key, "claim": p.claim, "evidence": p.evidence, "metric": p.metric}
        for p in proof_points or []
    ]


async def _restore_stamp(session: AsyncSession, track: str, previous: str | None) -> None:
    """Put the proof-points stamp back to what it was before this import (and commit)."""
    from kestrel.db.models import Setting
    from kestrel.services.profile_readiness import KEY

    row = await session.get(Setting, KEY)
    if row is not None and isinstance(row.value, dict):
        value = dict(row.value)
        if previous:
            value[track] = previous
        else:
            value.pop(track, None)
        row.value = value
        await session.commit()


async def _merge_proof_points(
    session: AsyncSession, track: str, points: list[dict], previous_stamp: str | None,
    master_id: int,
) -> None:
    """Write the accepted proof points to profile.yml, the file scoring reads (issue #68), AFTER
    the resume row and the stamp are committed. When nothing was new the stamp goes back to what
    it was. When the write fails the stamp is cleared too, and ProofPointsNotSaved says so in
    plain words: the resume is kept, the operator is told, and readiness asks to review it."""
    import asyncio

    import structlog

    from kestrel.services import profile_edit
    from kestrel.web.errors import fixed_reason, plain_error

    if not points:
        return
    try:
        added = await asyncio.to_thread(profile_edit.add_proof_points, track, points)
    except Exception as exc:
        structlog.get_logger().warning(
            "import_proof_points_not_saved", error_type=type(exc).__name__)
        await _restore_stamp(session, track, previous_stamp)
        reason = plain_error(exc) if isinstance(exc, UserFacingError) else fixed_reason(exc)
        raise ProofPointsNotSaved(master_id, reason) from exc
    if not added:
        await _restore_stamp(session, track, previous_stamp)


# ------------------------------------------------------------- P7c: wordings for one slot
# D-IMPORT-3. Rewordings used to be generated for every bullet at import, which tripled the
# import's output. They are now asked for one slot at a time, when the operator wants them, and
# each is reviewed and added explicitly. A suggestion whose numbers differ from the original's is
# refused here and again at the write, whatever the model says (hard rule 2: never change a fact).
_NUM_RE = re.compile(
    r"(\$)?(\d[\d,]*(?:\.\d+)?)\s*(%|percent\b|k\b|m\b|mm\b|bn?\b|thousand\b|million\b|"
    r"billion\b)?", re.I,
)
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9,
          "billion": 1e9}


def _numbers(text: str) -> set[str]:
    """Each number as a canonical value with its unit, so "$2.5M" and "$2.5 million" match and
    "$40 million" vs "$40 billion" do not. A trailing "+" ("400+") is emphasis, not a new fact."""
    out: set[str] = set()
    for cur, num, unit in _NUM_RE.findall(text or ""):
        value = float(num.replace(",", ""))
        u = (unit or "").lower()
        if u in ("%", "percent"):
            out.add(f"{_fmt(value)}%")
            continue
        out.add(f"{cur}{_fmt(value * _SCALE.get(u, 1))}")
    return out


def _fmt(value: float) -> str:
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def facts_changed(original: str, candidate: str) -> list[str]:
    """Numbers present in one wording and not the other, or [] when they match. Order is not
    compared, so a swapped pair ("from 10 to 5" as "from 5 to 10") is not caught here; the
    operator's review is the check for that (D-IMPORT-3)."""
    return sorted(_numbers(original) ^ _numbers(candidate))


@dataclass
class VariantSuggestion:
    text: str
    tags: list[str]
    problem: str | None = None  # why it cannot be added, or None


async def suggest_variants(
    client, *, role_label: str, text: str, tags: list[str], on_quarantine=None, session=None,
    master_id: int | None = None,
) -> tuple[list[VariantSuggestion], str | None]:
    """(suggestions, error). Writes nothing but a refusal or quarantine row (the caller commits).
    With a session the call goes through the hosted path (pipeline._shared.run_prompt_hosted,
    D-PRIV-1): on an online tier under confidential mode the role label, bullet and tags carry
    the employer's token, and the reply has only that token restored."""
    spec = load_prompt("variant_suggest/v1")
    user = (spec.user.replace("{role_label}", role_label[:_LABEL_MAX])
            .replace("{bullet_text}", text).replace("{tags}", ", ".join(tags) or "none"))
    messages = [{"role": "system", "content": spec.system}, {"role": "user", "content": user}]
    if session is not None:
        from kestrel.pipeline._shared import make_quarantine_hook, run_prompt_hosted

        res = await run_prompt_hosted(
            session, client, spec, messages, quarantine_tier=4, master_id=master_id,
            on_quarantine=on_quarantine or make_quarantine_hook(session, tier=4,
                                                                opportunity_id=None),
        )
    else:
        res = await run_prompt(client, spec, messages, on_quarantine=on_quarantine)
    if not res.ok or res.value is None:
        return [], res.error
    out: list[VariantSuggestion] = []
    for v in res.value.variants[:_MAX_DERIVED_PER_SLOT]:
        cand = (v.text or "").strip()
        if not cand or cand == text.strip():
            continue
        diff = facts_changed(text, cand)
        problem = f"changes the numbers ({', '.join(diff)})" if diff else None
        out.append(VariantSuggestion(text=cand, tags=list(v.tags)[:6], problem=problem))
    return out, None
