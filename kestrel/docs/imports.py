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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import DocMaster, DocSection, DocSlot, DocVariant, Profile
from kestrel.errors import UserFacingError
from kestrel.llm.guard import run_prompt
from kestrel.llm.prompts import load_prompt

_WS = re.compile(r"\s+")
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


_OPEN_END = (",", "&", "-", "/", "(", "\u2013")
_OPEN_WORDS = frozenset({"a", "an", "and", "the", "or", "to", "of", "for", "with", "in", "on",
                         "at", "by", "from", "into", "across", "including"})


def _continues(prev: str, text: str) -> bool:
    """Whether `text` reads as the rest of `prev`: it starts lowercase, or `prev` stops on a
    comma, an ampersand, a dash or a word that cannot end a sentence ("and", "to", "of"). A
    capitalized line after a finished-looking one is never joined: it may be its own bullet with a
    glyph the marker pattern does not know, and fusing two bullets would hide one (review)."""
    if not text or not text[0].isalnum():
        return False
    if text[0].islower():
        return True
    words = prev.rstrip().split()
    return prev.rstrip().endswith(_OPEN_END) or bool(words and words[-1].lower() in _OPEN_WORDS)


def _attach_continuations(lines: dict[int, str], rng: tuple[int, int], header_lines,
                          rmap) -> None:
    """A wrapped line the role map left out joins the block it continues (D-IMPORT-6): line n is
    unplaced, carries no list marker, line n-1 ends a context or bullet block of the same role, and
    n reads as the rest of n-1 (`_continues`). A proof point that ended on n-1 inside that block is
    extended with it. The text stays the operator's own, copied verbatim; only where it lands is
    decided here. Mutates rmap."""
    first, last = rng
    blocks: list[list[int]] = [rmap.context_lines] + [b.lines for b in rmap.bullets]
    owner = {n: blk for blk in blocks for n in blk}
    taken = set(owner) | set(header_lines)
    for n in sorted(x for x in lines if first <= x <= last):
        if n in taken or _BULLET_RE.match(lines[n]):
            continue
        blk = owner.get(n - 1)
        if blk is None or blk[-1] != n - 1:
            continue
        if _continues(lines[n - 1].strip(), lines[n].strip()):
            blk.append(n)
            owner[n] = blk
            taken.add(n)
            for pp in rmap.proof_points:
                if pp.lines and pp.lines[-1] == n - 1 and set(pp.lines) <= set(blk):
                    pp.lines.append(n)


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
       (the operator's "Brightline | Ops Manager" then "Manager, Software, Systems & Support").
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
        outline = _settle_role_ranges(outline)
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
        return outline, ranges, summary, skills

    spec = load_prompt("resume_outline/v1")
    outline_user = spec.user.replace("{numbered_lines}",
                                     _view(shown, sorted(lines), LAYOUT_WIDTH))
    got, err = await _ask(client, spec, outline_user, line_ref_schema(P7Outline, sorted(lines)),
                          check_outline, on_quarantine, book)
    if err is not None:
        return fail(f"Mapping the resume's layout failed: {err}")
    outline, ranges, summary, skills = got

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
                _check_refs(pp.lines, inside, f"{where} proof point {pp.key!r}")
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
            errors.append(f"Role {i + 1} ({role_label(i)}): {type(r).__name__}: {r}")
        elif r[1] is not None:
            errors.append(f"Role {i + 1} ({role_label(i)}): {r[1]}")
    if errors:
        return fail("; ".join(errors))

    for i, (rmap, _err) in enumerate(results):
        _attach_continuations(lines, ranges[i], outline.roles[i].header_lines, rmap)

    roles: list = []
    proofs: list = []
    accounted: set[int] = set()
    slot_keys: set[str] = set()
    proof_keys: set[str] = set()
    unverified: list[str] = []
    for i, (rmap, _err) in enumerate(results):
        role = outline.roles[i]
        accounted.update(role.header_lines)
        accounted.update(rmap.context_lines)
        header = " ".join(lines[n] for n in role.header_lines)
        for name in ("company_label", "title", "start_date", "end_date"):
            if not _in_header(getattr(role, name), header):
                unverified.append(f"role {i + 1} {name.replace('_', ' ')}: "
                                  f"{getattr(role, name)!r}")
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
            start_date=(role.start_date or "")[:40], end_date=(role.end_date or "")[:40],
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
    in_role = {n for n in lines for a, b in ranges if a <= n <= b}
    flags.unplaced = [
        lines[n].strip() for n in sorted(lines)
        if n not in accounted and (n in in_role or _BULLET_RE.match(lines[n]))
    ]
    flags.unverified = unverified
    return ImportProposal(value=value, flags=flags, source_format=source_format)


class ImportRejected(UserFacingError, ValueError):
    """accept_import refused because the proposal did not pass its own re-validation."""


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


def open_ended_newest_role(value):
    """The newest role (the first, on a reverse-chronological resume) when its end date is empty
    or "Present", "Current", "Now", and it names a company; else None. The proposal offers to mark
    it as the current employer (D-PRIV-1); nothing is set unless the operator ticks the offer."""
    from kestrel.docs.current_employer import is_open_ended

    roles = list(getattr(value, "roles", None) or [])
    if not roles:
        return None
    first = roles[0]
    if not (first.company_label or "").strip() or not is_open_ended(first.end_date):
        return None
    return first


async def accept_import(
    session: AsyncSession, value, *, track: str, doc_kind: str, name: str, source_markdown: str,
    current_employer: str | None = None,
) -> int:
    """The ONLY writer, and the enforcement boundary. Re-validates the proposal (verbatim, valid
    defaults, unique slot keys) and raises ImportRejected on any violation before writing anything.
    Creates the doc_master, sections, slots, variants, and merges proof points. Returns the id.

    `current_employer` is the "Show it as" text from the proposal's opt-in (D-PRIV-1): when given,
    and the newest role is open-ended, that role is marked as the current employer. None (the
    operator did not tick it) sets nothing."""
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

    newest = open_ended_newest_role(value) if current_employer else None
    newest_section: DocSection | None = None
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
        if newest is not None and i == 0:
            newest_section = section
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

    if newest_section is not None and current_employer:
        from kestrel.docs.current_employer import set_current_employer

        await set_current_employer(session, newest_section, current_employer)
    await _merge_proof_points(session, track, value.proof_points or [])
    if track == "employment" and doc_kind == "resume":
        # Suggested target titles (P15, D-TTL-3): one queued run read from this resume. The
        # suggestions wait for an Accept; nothing is added to the profile here.
        from kestrel.services.title_suggest import request_title_suggestions

        await request_title_suggestions(session, "import")
    await session.commit()
    return master.id


async def _merge_proof_points(session: AsyncSession, track: str, proof_points) -> None:
    profile = (
        await session.execute(select(Profile).where(Profile.track == track).limit(1))
    ).scalar_one_or_none()
    if profile is None:
        return
    existing = {p.get("key") for p in (profile.proof_points or []) if isinstance(p, dict)}
    # Keys are positional (r1_cost), so a later import can reuse one for a different claim: the
    # same claim is skipped, a different one gets a fresh key rather than being dropped.
    claims = {p.get("claim") for p in (profile.proof_points or []) if isinstance(p, dict)}
    new = []
    for p in proof_points:
        if p.claim in claims:
            continue
        key = _unique(p.key, existing)
        claims.add(p.claim)
        new.append({"key": key, "claim": p.claim, "evidence": p.evidence, "metric": p.metric})
    if new:
        profile.proof_points = (profile.proof_points or []) + new


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
