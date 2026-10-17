"""Load a doc_master's full catalog and resolve a render config into ordered blocks whose bullet
text is doc_variant.text BYTE-FOR-BYTE (hard rule 2). The catalog also produces the inputs for the
dynamic P3 schema (build_doc_select_schema) so the model can only choose real variant keys.

Resolution applies, in this order: redact_orgs (the current employer only: org_label ->
org_label_redacted for the names the operator marked, D-PRIV-1), per-slot selection
("hide" drops an optional slot; an unknown key is reported, never silently substituted here — the
selection step owns the defaults fallback), max_roles_shown, and the layout's section order. The
config's optional `font` and `header_align` set the look (kestrel.docs.styles, D-DOC-1)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import DocLayout, DocMaster, DocSection, DocSlot, DocVariant
from kestrel.privacy import EmployerMap, from_pairs

# section_order tokens -> the doc_section.section_kind they render (resume + capability).
_KIND_FOR_TOKEN = {
    "experience": "role",
    "service_lines": "service_line",
    "proof_engagements": "proof_engagement",
    "education": "credential",
}
_HEADING = {
    "summary": "Summary",
    "positioning": "Positioning",
    "experience": "Experience",
    "service_lines": "Service Lines",
    "proof_engagements": "Proof Engagements",
    "skills": "Skills",
    "differentiators": "Differentiators",
    "education": "Education",
    "certifications": "Certifications",
    "licenses": "Licenses",
    "projects": "Projects",
}


@dataclass
class SlotView:
    key: str
    optional: bool
    default_variant_key: str | None
    variants: list[DocVariant]


@dataclass
class SectionView:
    section: DocSection
    slots: list[SlotView]


@dataclass
class MasterCatalog:
    master: DocMaster
    sections: list[SectionView]
    layouts: list[DocLayout]
    # D-PRIV-1: the confidential employer map across EVERY document (the same object as
    # web.redact.redaction_map), so a name marked on the resume is also hidden in a capability
    # statement that mentions it. Empty for a catalog built by hand.
    redaction: EmployerMap | None = None

    @property
    def summary_variants(self) -> list[dict]:
        return list(self.master.summary_variants or [])

    @property
    def skill_groups(self) -> list[dict]:
        return list(self.master.skill_groups or [])

    def all_slots(self) -> list[SlotView]:
        return [sl for sec in self.sections for sl in sec.slots]


async def load_catalog(session: AsyncSession, master_id: int) -> MasterCatalog | None:
    master = await session.get(DocMaster, master_id)
    if master is None:
        return None
    sections = (
        (
            await session.execute(
                select(DocSection)
                .where(DocSection.doc_master_id == master_id)
                .order_by(DocSection.sort_order, DocSection.id)
            )
        )
        .scalars()
        .all()
    )
    section_views: list[SectionView] = []
    for sec in sections:
        slots = (
            (
                await session.execute(
                    select(DocSlot)
                    .where(DocSlot.doc_section_id == sec.id)
                    .order_by(DocSlot.sort_order, DocSlot.id)
                )
            )
            .scalars()
            .all()
        )
        slot_views = []
        for sl in slots:
            variants = (
                (
                    await session.execute(
                        select(DocVariant)
                        .where(DocVariant.doc_slot_id == sl.id)
                        .order_by(DocVariant.id)
                    )
                )
                .scalars()
                .all()
            )
            slot_views.append(SlotView(sl.key, sl.optional, sl.default_variant_key, list(variants)))
        section_views.append(SectionView(sec, slot_views))
    layouts = (await session.execute(select(DocLayout).order_by(DocLayout.id))).scalars().all()
    kind_layouts = [
        lay for lay in layouts if _layout_kind(lay) is None or _layout_kind(lay) == master.doc_kind
    ]
    return MasterCatalog(
        master=master, sections=section_views, layouts=kind_layouts or list(layouts),
        redaction=await _employer_map(session, master_id),
    )


async def _employer_map(session: AsyncSession, master_id: int) -> EmployerMap | None:
    """THIS document's employer map (D-PRIV-1: its marks or inference, plus other documents'
    marks only for names that are current here), or None when confidential mode is off (a
    per-render redact_orgs flag then uses the document's own marks)."""
    from kestrel.docs.current_employer import load_employer_map
    from kestrel.services.setting_overrides import load_settings_with_db

    if not (await load_settings_with_db(session)).confidential_mode:
        return None
    return await load_employer_map(session, master_id=master_id)


def _layout_kind(layout: DocLayout) -> str | None:
    """A layout may scope itself to a doc_kind via metadata or a naming convention."""
    if layout.key in ("capability", "capability_short"):
        return "capability_statement"
    if layout.key in ("executive", "condensed", "technical", "classic", "modern", "compact"):
        return "resume"
    return None


# --------------------------------------------------------------------- schema inputs for P3
def schema_inputs(catalog: MasterCatalog) -> dict:
    """The exact inputs build_doc_select_schema needs: real layout/summary/slot/skill-group keys."""
    slots = [
        {
            "key": sl.key,
            "optional": sl.optional,
            "variants": [{"key": v.key, "tags": v.tags or [], "text": v.text} for v in sl.variants],
        }
        for sl in catalog.all_slots()
    ]
    return {
        "layouts": [
            {"key": lay.key, "description": lay.description or ""} for lay in catalog.layouts
        ],
        "summary_variants": [{"key": s.get("key")} for s in catalog.summary_variants],
        "slots": slots,
        "skill_groups": [{"key": g.get("key")} for g in catalog.skill_groups],
    }


# ---------------------------------------------------------------------------- default config
def default_config(catalog: MasterCatalog) -> dict:
    layout = catalog.layouts[0].key if catalog.layouts else "executive"
    summary = catalog.summary_variants[0]["key"] if catalog.summary_variants else None
    slots = {}
    for sl in catalog.all_slots():
        default = sl.default_variant_key or (sl.variants[0].key if sl.variants else None)
        slots[sl.key] = default
    skill_groups = {g["key"]: bool(g.get("default_visible", True)) for g in catalog.skill_groups}
    return {
        "layout": layout,
        "summary": summary,
        "slots": slots,
        "skill_groups": skill_groups,
        "redact_orgs": False,
    }


# ------------------------------------------------------------------------------- resolution
@dataclass
class Bullet:
    key: str
    text: str


@dataclass
class RoleBlock:
    org: str
    title: str | None
    dates: str
    context: str | None
    bullets: list[Bullet]


@dataclass
class Block:
    type: str  # summary | roles | skills | entries
    heading: str
    text: str | None = None
    roles: list[RoleBlock] = field(default_factory=list)
    skills: list[dict] = field(default_factory=list)
    # Optional-section entries (D-DOC-3), the operator's typed text: kestrel.docs.sections.Entry
    entries: list = field(default_factory=list)


@dataclass
class RenderDoc:
    header: dict
    blocks: list[Block]
    # Slot keys (plus "summary" / "layout") whose configured key was not in the catalog, and the
    # key that was asked for, so a page can NAME the miss rather than render a fallback silently.
    unresolved: list[str] = field(default_factory=list)
    unresolved_keys: dict[str, str | None] = field(default_factory=dict)
    # The look (kestrel.docs.styles.resolve_style): style class, font, header alignment. Empty
    # means the classic default; presentation only, it never changes which text is printed.
    style: dict[str, str] = field(default_factory=dict)

    def bullet_texts(self) -> list[str]:
        out = []
        for b in self.blocks:
            for r in b.roles:
                out.extend(bl.text for bl in r.bullets)
        return out


def employer_map(catalog: MasterCatalog) -> EmployerMap:
    """The map a render redacts with: the cross-document map `load_catalog` attached, or, for a
    catalog built by hand, this document's own marks. Every other employer label is protected."""
    if catalog.redaction is not None:
        return catalog.redaction
    marked, others = [], []
    for sv in catalog.sections:
        sec = sv.section
        label = (sec.org_label or "").strip()
        shown = (getattr(sec, "org_label_redacted", None) or "").strip()
        if label and shown:
            marked.append((label, shown))
        elif label and sec.section_kind in ("role", "proof_engagement"):
            others.append(label)
    return from_pairs(marked, others)


def _org_label(sec: DocSection, emap: EmployerMap | None) -> str:
    # Redaction swaps the CURRENT EMPLOYER only (SPEC.md 9, D-PRIV-1): a past employer, and a
    # service-line label (a service name, not an employer), print as written.
    label = sec.org_label or ""
    if emap and sec.section_kind in ("role", "proof_engagement"):
        return emap.redact_name(label) or label
    return label


def org_redactor(catalog: MasterCatalog) -> Callable[[str], str]:
    """The confidential-mode substitution for free text: the current employer (kestrel.privacy
    matching rules) becomes its "Show it as" text, exactly as _org_label swaps it in the role
    header. Applied to bullets, titles, context lines, the summary, skills, the header's free
    text and the optional sections. It substitutes the operator's own label into the operator's
    own text at render time, never generated text, and never touches what is stored (hard
    rule 2)."""
    emap = employer_map(catalog)
    if not emap:
        return lambda text: text
    return lambda text: emap.redact(text) if text else text


# Header fields that are free text, so confidential mode substitutes the employer in them. The
# name, email, phone and links are identifiers: a substitution inside them would corrupt them.
_REDACTED_HEADER_FIELDS = ("headline", "contact", "location")


def redact_header(header: dict, scrub: Callable[[str], str]) -> dict:
    """The document header with the employer substituted in its free-text fields (the same
    org_redactor transform the role headers and entries get, never generated text)."""
    out = dict(header)
    for key in _REDACTED_HEADER_FIELDS:
        val = out.get(key)
        if isinstance(val, str):
            out[key] = scrub(val)
        elif isinstance(val, list):
            out[key] = [scrub(v) if isinstance(v, str) else v for v in val]
    return out


def redact_skill_groups(groups: list[dict], scrub: Callable[[str], str]) -> list[dict]:
    """Skill groups with the employer substituted in each label and item."""
    return [
        {**g, "label": scrub(str(g.get("label") or "")) if g.get("label") else g.get("label"),
         "items": [scrub(str(i)) for i in (g.get("items") or [])]}
        for g in groups
    ]


def _dates(sec: DocSection) -> str:
    if sec.start_date and sec.end_date:
        return f"{sec.start_date} - {sec.end_date}"
    return sec.start_date or sec.end_date or ""


def _layout(catalog: MasterCatalog, key: str) -> DocLayout | None:
    for lay in catalog.layouts:
        if lay.key == key:
            return lay
    return None


def document_header(master: DocMaster) -> dict:
    """The name and contact line at the top of every render: the master's own header where it
    has one, and the operator's About you block (profile.yml, that track) for the rest
    (D-UI-23). An imported resume has no header of its own, so without this the PDF went out
    with no name. Only the operator's file is read, never the shipped example: a fictional
    name on a real resume is worse than none."""
    from kestrel.services.profile_edit import load_state

    state = load_state()
    block = state.tracks.get(master.track, {}) if master.track in state.present else {}
    fields = {
        "name": block.get("full_name"), "email": block.get("email"),
        "phone": block.get("phone"), "location": block.get("location"),
        "headline": block.get("headline"),
    }
    header = {k: str(v).strip() for k, v in fields.items() if v and str(v).strip()}
    for key, val in (master.header or {}).items():
        if val not in (None, "", [], {}):
            header[key] = val
    return header


def resolve(catalog: MasterCatalog, config: dict) -> RenderDoc:
    """Config -> ordered blocks with byte-for-byte variant text. `unresolved` lists any slot whose
    configured variant key does not exist (the caller decides on any defaults fallback)."""
    redact = bool(config.get("redact_orgs"))
    emap = employer_map(catalog) if redact else None
    scrub = org_redactor(catalog) if emap else None
    chosen: dict[str, str] = config.get("slots") or {}
    unresolved: list[str] = []
    unresolved_keys: dict[str, str | None] = {}

    def _miss(what: str, key: str | None) -> None:
        unresolved.append(what)
        unresolved_keys[what] = key

    layout_key = config.get("layout")
    layout = _layout(catalog, layout_key or "")
    if layout_key and layout is None:
        _miss("layout", layout_key)  # falls back to the kind's default section order
    order = (layout.section_order if layout and layout.section_order else None) or _default_order(
        catalog.master.doc_kind
    )
    max_roles = layout.max_roles_shown if layout else None
    # Parts the operator hid for this render only (the Tailor page): a flag on the config, honored
    # here so the preview and the PDF agree. Hiding never deletes the operator's text.
    hide_summary = bool(config.get("hide_summary"))
    hide_skills = bool(config.get("hide_skills"))
    summary_key = config.get("summary")
    if summary_key and not hide_summary and _summary_text(catalog, summary_key) is None:
        _miss("summary", summary_key)  # no summary block is rendered for an unknown key

    def role_blocks(kind: str) -> list[RoleBlock]:
        out: list[RoleBlock] = []
        for sec in catalog.sections:
            if sec.section.section_kind != kind:
                continue
            bullets: list[Bullet] = []
            for sl in sec.slots:
                key = chosen.get(sl.key, sl.default_variant_key)
                if key == "hide":
                    continue
                variant = next((v for v in sl.variants if v.key == key), None)
                if variant is None:
                    _miss(sl.key, key)
                    variant = next(
                        (v for v in sl.variants if v.key == sl.default_variant_key), None
                    ) or (sl.variants[0] if sl.variants else None)
                if variant is not None:
                    # The operator's own text, with only the marked employer name substituted
                    # under confidential mode (the same transform as every other part).
                    text = scrub(variant.text) if scrub is not None else variant.text
                    bullets.append(Bullet(variant.key, text))
            out.append(
                RoleBlock(
                    org=_org_label(sec.section, emap),
                    title=(scrub(sec.section.title)
                           if scrub is not None and sec.section.title else sec.section.title),
                    dates=_dates(sec.section),
                    context=(scrub(sec.section.context_line)
                             if scrub is not None and sec.section.context_line
                             else sec.section.context_line),
                    bullets=bullets,
                )
            )
        return out[:max_roles] if max_roles else out

    from kestrel.docs.sections import KINDS, entries_of, to_entry

    # Optional sections print in every layout when they have entries (D-DOC-3): a layout whose
    # order does not name one gets it at the end, in the fixed order.
    order = list(order) + [k for k in KINDS if k not in order]

    def entry(token: str, e: dict):
        out = to_entry(token, e)
        if scrub is not None:
            out.lead, out.sub, out.dates, out.detail, out.extra = (
                scrub(out.lead), scrub(out.sub), scrub(out.dates), scrub(out.detail),
                scrub(out.extra),
            )
        return out

    blocks: list[Block] = []
    for token in order:
        heading = _HEADING.get(token, token.title())
        entries = (
            [entry(token, e) for e in entries_of(catalog.master, token)]
            if token in KINDS else []
        )
        if token == "education":
            # Legacy credential sections (slots and variants) and typed entries share a heading.
            roles = role_blocks("credential")
            if roles or entries:
                blocks.append(Block(type="roles", heading=heading, roles=roles, entries=entries))
        elif token in KINDS:
            if entries:
                blocks.append(Block(type="entries", heading=heading, entries=entries))
        elif token in ("summary", "positioning"):
            text = None if hide_summary else _summary_text(catalog, config.get("summary"))
            if text and scrub is not None:
                text = scrub(text)
            if text:
                blocks.append(Block(type="summary", heading=heading, text=text))
        elif token in ("skills", "differentiators"):
            groups = [] if hide_skills else _skills(catalog, config.get("skill_groups") or {})
            if groups and scrub is not None:
                groups = redact_skill_groups(groups, scrub)
            if groups:
                blocks.append(Block(type="skills", heading=heading, skills=groups))
        elif token in _KIND_FOR_TOKEN:
            roles = role_blocks(_KIND_FOR_TOKEN[token])
            if roles:
                blocks.append(Block(type="roles", heading=heading, roles=roles))
    from kestrel.docs.styles import resolve_style

    header = document_header(catalog.master)
    if scrub is not None:
        header = redact_header(header, scrub)
    return RenderDoc(
        header=header,
        blocks=blocks,
        unresolved=unresolved,
        unresolved_keys=unresolved_keys,
        style=resolve_style(layout.key if layout else None, config,
                            getattr(catalog.master, "look", None)),
    )


def valid_slot_keys(catalog: MasterCatalog, slot_key: str) -> set[str] | None:
    """The keys a slot may be set to: its real variant keys, plus "hide" when it is optional. None
    when the slot does not exist. This is the single rule the swap route validates against, so an
    operator-supplied key can never enter a config unchecked (hard rule 2 applies to every writer,
    not only the model)."""
    for sl in catalog.all_slots():
        if sl.key == slot_key:
            keys = {v.key for v in sl.variants}
            if sl.optional:
                keys.add("hide")
            return keys
    return None


def operator_slot_keys(catalog: MasterCatalog, slot_key: str) -> set[str] | None:
    """What the OPERATOR may set a slot to on the Tailor page: its real variant keys, plus "hide"
    for any slot (a part can be left off one resume whether or not the import marked it optional).
    The model's own choices stay on `valid_slot_keys`, which allows "hide" only for optional
    slots. None when the slot does not exist."""
    for sl in catalog.all_slots():
        if sl.key == slot_key:
            return {v.key for v in sl.variants} | {"hide"}
    return None


def _default_order(doc_kind: str) -> list[str]:
    if doc_kind == "capability_statement":
        return ["positioning", "service_lines", "proof_engagements", "differentiators"]
    return ["summary", "experience", "skills", "education"]


def _summary_text(catalog: MasterCatalog, key: str | None) -> str | None:
    for s in catalog.summary_variants:
        if s.get("key") == key:
            return s.get("text")
    return None


def _skills(catalog: MasterCatalog, visible: dict) -> list[dict]:
    out = []
    for g in catalog.skill_groups:
        if visible.get(g.get("key"), g.get("default_visible", True)):
            out.append({"label": g.get("label", g.get("key")), "items": g.get("items", [])})
    return out
