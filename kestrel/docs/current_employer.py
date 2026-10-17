"""The operator's current employer, and what confidential mode shows instead (D-PRIV-1).

The mark lives on the role row: `DocSection.org_label_redacted` is the "Show it as" text of a
role the operator marked as their current employer. This module is the one writer (the Documents
role editor, the import opt-in, the Confirm button) and the one reader that turns marks into an
`kestrel.privacy.EmployerMap`.

* At most one employer is current per document; marking a role unmarks every other employer in
  that master. Roles in the master with the same normalized name (a promotion split in two) are
  marked together, and a rename carries the mark to them.
* Another document is never marked or unmarked from here. When it already marks the same name,
  saving the "Show it as" text updates it, so one name reads one way everywhere.
* The map is the union of every document's CURRENT marks. Unticking a role removes the name
  unless another document still marks it (the editor says so).
* Fail closed: with nothing marked anywhere, the newest open-ended role of each document (no end
  date, or Present/Current/Now) is treated as current, shown as the suggested text, until the
  operator confirms or changes it."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import DocMaster, DocSection
from kestrel.privacy import Employer, EmployerMap, clean_names, default_names

DEFAULT_PLACEHOLDER = "a company in your industry"
# The kinds whose org_label names an employer.
EMPLOYER_KINDS = ("role", "proof_engagement")
_OPEN_END = frozenset({"", "present", "current", "now"})
_PLACEHOLDER_MAX = 200


def is_open_ended(end_date: str | None) -> bool:
    """True for an empty end date or "Present", "Current", "Now" in any case."""
    return (end_date or "").strip().casefold() in _OPEN_END


_HINTS: dict[tuple, str] = {}


def _profile_stamp() -> tuple:
    from kestrel import paths

    stamp = []
    for name in ("profile.yml", "profile.example.yml"):
        path = paths.config_file(name)
        try:
            stamp.append((str(path), path.stat().st_mtime_ns) if path is not None else None)
        except OSError:
            stamp.append(None)
    return tuple(stamp)


def suggested_placeholder(track: str | None = "employment") -> str:
    """The prefilled "Show it as" text: from the profile's industry when one is stored, else a
    neutral default. The operator always sees and can change it before anything is saved. The
    profile read is cached until the file changes (a request builds many maps)."""
    key = (track, _profile_stamp())
    if key not in _HINTS:
        _HINTS.clear()
        _HINTS[key] = _read_placeholder(track)
    return _HINTS[key]


def _read_placeholder(track: str | None) -> str:
    industry = None
    if track:
        try:
            from kestrel.pipeline.profile import load_profile

            industry = getattr(load_profile(track), "industry", None)
        except (OSError, ValueError) as exc:  # an unreadable profile only loses the hint
            import structlog

            structlog.get_logger().warning("placeholder_hint_failed", error=str(exc))
    if isinstance(industry, str) and industry.strip():
        return f"a company in {industry.strip()}"[:_PLACEHOLDER_MAX]
    return DEFAULT_PLACEHOLDER


def clean_placeholder(text: object) -> str:
    """The operator's "Show it as" text, trimmed and bounded."""
    return str(text or "").strip()[:_PLACEHOLDER_MAX]


def _norm(name: str | None) -> str:
    from kestrel.ingest.orgs import normalized_name

    return normalized_name(name or "")


def names_for(section: DocSection, names: list[str] | None = None,
              previous_name: str | None = None) -> list[str]:
    """The names to hide for a marked role: the operator's list when given, else what the row
    already holds, else the default for its label. A rename adds the new label's defaults and
    keeps the old entries (the old spelling still appears in older text)."""
    if names is not None:
        return clean_names(names)
    held = list(section.hide_names or [])
    if not held:
        return default_names(section.org_label)
    if previous_name and _norm(previous_name) != _norm(section.org_label):
        return clean_names([*held, *default_names(section.org_label)])
    return clean_names(held)


async def set_current_employer(
    session: AsyncSession, section: DocSection, placeholder: str | None, *,
    previous_name: str | None = None, names: list[str] | None = None,
) -> None:
    """Mark `section` as the current employer shown as `placeholder` with its names to hide
    (`names`; default `names_for`), or unmark it (None).

    Same master: every employer row whose normalized name matches the section's name, or its
    `previous_name` (a rename), is marked with it; every other one is unmarked. Other masters:
    rows that ALREADY mark the name (or the previous one) get the new "Show it as"; nothing there
    is marked or unmarked. Does not commit."""
    hide = names_for(section, names, previous_name) if placeholder else None
    names = {n for n in (_norm(section.org_label), _norm(previous_name)) if n}
    siblings = (
        await session.execute(
            select(DocSection).where(
                DocSection.doc_master_id == section.doc_master_id,
                DocSection.section_kind.in_(EMPLOYER_KINDS),
            )
        )
    ).scalars().all()
    for sec in siblings:
        same = sec.id == section.id or _norm(sec.org_label) in names
        if placeholder:
            sec.org_label_redacted = placeholder if same else None
            sec.hide_names = (clean_names([*hide, *default_names(sec.org_label)])
                              if same and sec.id != section.id else hide if same else None)
        elif same:
            sec.org_label_redacted = None
            sec.hide_names = None
    section.org_label_redacted = placeholder or None
    section.hide_names = hide
    if placeholder and names:
        others = (
            await session.execute(
                select(DocSection).where(
                    DocSection.doc_master_id != section.doc_master_id,
                    DocSection.org_label_redacted.is_not(None),
                )
            )
        ).scalars().all()
        for sec in others:
            if _norm(sec.org_label) in names:
                sec.org_label_redacted = placeholder
                sec.hide_names = clean_names([*hide, *default_names(sec.org_label)])
    if placeholder:
        await set_confirmed(session)  # the operator chose it: the Today ask can go
    await touch_master(session, section.doc_master_id)


async def touch_master(session: AsyncSession, master_id: int | None) -> None:
    """Record that a document was just saved (doc_master.updated_at, D-PRIV-1)."""
    from kestrel.db.base import utcnow_iso

    master = await session.get(DocMaster, master_id) if master_id else None
    if master is not None:
        master.updated_at = utcnow_iso()


async def _rows(session: AsyncSession) -> list[tuple[DocSection, DocMaster]]:
    return list((
        await session.execute(
            select(DocSection, DocMaster)
            .join(DocMaster, DocMaster.id == DocSection.doc_master_id)
            .order_by(DocMaster.id, DocSection.sort_order, DocSection.id)
        )
    ).all())


def is_sample(master: DocMaster) -> bool:
    """The fictional sample document `kestrel.docs.seed` writes. Its marks apply to itself only,
    and not at all once the operator has documents of their own (D-PRIV-1)."""
    from kestrel.docs.seed import SAMPLE_EMAIL

    return ((master.header or {}).get("email") or "") == SAMPLE_EMAIL


def _marked(sec: DocSection) -> bool:
    return bool((sec.org_label or "").strip() and (sec.org_label_redacted or "").strip())


def _employers_of(master_id: int, rows, cross: dict[str, Employer]) -> list[Employer]:
    """One document's employers (D-PRIV-1): its own marks; else EVERY open-ended role, inferred;
    plus another document's mark for a name that is open-ended (current) here. A name that is a
    past role here is never hidden here by another document's mark."""
    mine = [sec for sec, m in rows if m.id == master_id]
    own = [sec for sec in mine if _marked(sec)]
    current = [sec for sec in mine if sec.section_kind == "role"
               and (sec.org_label or "").strip() and is_open_ended(sec.end_date)]
    out: dict[str, Employer] = {}
    for sec in own:
        out[_norm(sec.org_label)] = Employer(sec.org_label.strip(), sec.org_label_redacted.strip(),
                                             names=tuple(names_for(sec)),
                                             section_id=sec.id, master_id=master_id)
    track = next((m.track for sec, m in rows if m.id == master_id), None)
    for sec in current:
        key = _norm(sec.org_label)
        if key in out:
            continue
        if key in cross:
            e = cross[key]
            out[key] = Employer(sec.org_label.strip(), e.shown_as, section_id=sec.id,
                                names=tuple(clean_names([*e.names,
                                                         *default_names(sec.org_label)])),
                                master_id=master_id)
        elif not own:
            out[key] = Employer(sec.org_label.strip(), suggested_placeholder(track),
                                names=tuple(default_names(sec.org_label)),
                                inferred=True, section_id=sec.id, master_id=master_id)
    return list(out.values())


async def load_employer_map(session: AsyncSession, *, master_id: int | None = None,
                            infer: bool = True) -> EmployerMap:
    """The employer map for one document's outputs and sends (`master_id`), or the union across
    documents (list views, the API, MCP, the pipeline). Every other employer label is protected.
    `infer=False` leaves out the fail-closed inference."""
    rows = await _rows(session)
    real = {m.id for _sec, m in rows if not is_sample(m)}
    masters = sorted({m.id for _sec, m in rows})
    usable = [mid for mid in masters if mid in real] or masters  # samples only when alone
    cross: dict[str, Employer] = {}
    for sec, m in rows:  # later rows win for one name
        if m.id in real and _marked(sec):
            cross[_norm(sec.org_label)] = Employer(sec.org_label.strip(),
                                                   sec.org_label_redacted.strip(),
                                                   names=tuple(names_for(sec)))
    by_name: dict[str, Employer] = {}
    if master_id is not None:
        for e in _employers_of(master_id, rows, cross):
            if infer or not e.inferred:
                by_name[_norm(e.name)] = e
    else:
        # The combined map (list views, the API, MCP, the pipeline) follows the job change: an
        # employer is hidden only if it is current (marked, or open-ended) in the NEWEST
        # document that names it; a past role there is no longer hidden.
        stamp = {m.id: (m.updated_at or "", m.id) for _sec, m in rows}
        newest: dict[str, int] = {}
        for sec, m in rows:
            key = _norm(sec.org_label)
            if (m.id in usable and key
                    and (key not in newest or stamp[m.id] > stamp[newest[key]])):
                newest[key] = m.id
        per = {mid: {_norm(e.name): e for e in _employers_of(mid, rows, cross)}
               for mid in set(newest.values())}
        for key, mid in newest.items():
            e = per[mid].get(key)
            if e is not None and (infer or not e.inferred):
                by_name[key] = e
    employers = list(by_name.values())
    names = set(by_name)
    protected = sorted({sec.org_label.strip() for sec, _m in rows
                        if sec.section_kind in EMPLOYER_KINDS and (sec.org_label or "").strip()
                        and _norm(sec.org_label) not in names})
    return EmployerMap(employers, protected)


async def inferred_employers(session: AsyncSession) -> list[Employer]:
    """The inferred (unconfirmed) current employers across documents."""
    return [e for e in (await load_employer_map(session)).employers if e.inferred]


CONFIRMED_KEY = "confidential.employer_confirmed"


async def is_confirmed(session: AsyncSession) -> bool:
    from kestrel.db.models import Setting

    row = await session.get(Setting, CONFIRMED_KEY)
    return bool(row is not None and row.value)


async def set_confirmed(session: AsyncSession, value: bool = True) -> None:
    """Stored when the operator confirms or chooses the employer Kestrel hides. Does not commit."""
    from kestrel.db.models import Setting

    row = await session.get(Setting, CONFIRMED_KEY)
    if row is None:
        session.add(Setting(key=CONFIRMED_KEY, value=value))
    else:
        row.value = value


async def marks_elsewhere(session: AsyncSession) -> dict[str, list[dict]]:
    """Normalized employer name -> every document marking it: {master_id, doc, shown_as}."""
    out: dict[str, list[dict]] = {}
    for sec, m in await _rows(session):
        if not _marked(sec):
            continue
        key = _norm(sec.org_label)
        if key and not any(e["master_id"] == m.id for e in out.get(key, [])):
            out.setdefault(key, []).append(
                {"master_id": m.id, "doc": m.name or "another document",
                 "shown_as": sec.org_label_redacted})
    return out


def also_marked(marks: dict[str, list[dict]], section: DocSection) -> list[dict]:
    """The OTHER documents marking this section's employer name."""
    return [e for e in marks.get(_norm(section.org_label), [])
            if e["master_id"] != section.doc_master_id]


async def current_employers(session: AsyncSession) -> list[dict]:
    """What confidential mode hides, for Settings and the Documents banner: each employer with
    its "Show it as", whether it was inferred, and the role to confirm or change it at."""
    emap = await load_employer_map(session)
    out: list[dict] = []
    for e in emap.employers:
        sec = await session.get(DocSection, e.section_id) if e.section_id else None
        out.append({"employer": e.name, "shown_as": e.shown_as, "inferred": e.inferred,
                    "section_id": e.section_id,
                    "href": role_href(sec) if sec is not None else "/documents"})
    return out


def role_href(section: DocSection) -> str:
    """The Documents page opened at this role's details."""
    return f"/documents?master_id={section.doc_master_id}&section={section.id}#role-{section.id}"


async def choose_href(session: AsyncSession) -> str:
    """Where "Choose your current employer" goes: the first role of the first document (the
    newest role on a reverse-chronological resume), else the Documents page."""
    first = (
        await session.execute(
            select(DocSection)
            .join(DocMaster, DocMaster.id == DocSection.doc_master_id)
            .where(DocSection.section_kind == "role")
            .order_by(DocMaster.track, DocMaster.doc_kind, DocMaster.id,
                      DocSection.sort_order, DocSection.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    return role_href(first) if first is not None else "/documents"
