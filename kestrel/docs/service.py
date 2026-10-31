"""Tie the document engine together: resolve a config, render HTML then PDF, write the file, and
record a doc_render row with its page count and defaults_used flag. The page-count guard warns when
the result exceeds settings.docs.max_pages and points at optional slots to hide."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from kestrel import paths
from kestrel.config import Settings
from kestrel.db.models import DocRender, Opportunity, Org
from kestrel.docs.catalog import MasterCatalog, load_catalog, resolve
from kestrel.docs.render import filename_for, render_html, render_pdf

# Without an opportunity there is no target org, so the filename says so instead of borrowing the
# operator's OWN employer from the first roles block (which is what an earlier cut did, and which
# put the current employer's name on every general-purpose PDF).
_GENERAL_ORG = "General"
_KIND_LABEL = {"resume": "Resume", "capability_statement": "Capability"}


@dataclass
class RenderResult:
    doc_render_id: int
    filename: str
    pdf_path: str
    page_count: int
    over_page_limit: bool
    defaults_used: bool
    html: str
    hide_suggestions: list = None  # optional slots currently shown, to try hiding when over-limit


async def _target_org_role(
    session: AsyncSession, catalog: MasterCatalog, opportunity_id: int | None
) -> tuple[str, str]:
    """The `{Org}` and `{RoleSlug}` of SPEC-MODULES 1.4 are the TARGET organization and the role
    being pursued, taken from the opportunity. Never the operator's own employer: that name comes
    from the resume's roles blocks and belongs in no filename, confidential mode or not."""
    kind_label = _KIND_LABEL.get(catalog.master.doc_kind, "Document")
    if opportunity_id is None:
        return _GENERAL_ORG, kind_label
    opp = await session.get(Opportunity, opportunity_id)
    if opp is None:
        return _GENERAL_ORG, kind_label
    org = await session.get(Org, opp.org_id) if opp.org_id else None
    org_name = (org.name if org else None) or opp.org_name or _GENERAL_ORG
    return org_name, opp.title or kind_label


def _hide_suggestions(catalog, config: dict) -> list[str]:
    """Optional slots that are currently rendered (not already 'hide'); candidates to drop when the
    document runs over the page limit. Ranking by model confidence is deferred (P3 returns none)."""
    chosen = config.get("slots") or {}
    return [
        sl.key
        for sl in catalog.all_slots()
        if sl.optional and chosen.get(sl.key, sl.default_variant_key) != "hide"
    ]


async def render_document(
    session: AsyncSession,
    settings: Settings,
    master_id: int,
    *,
    config: dict,
    opportunity_id: int | None = None,
    defaults_used: bool = False,
    rationale: dict | None = None,
) -> RenderResult | None:
    catalog = await load_catalog(session, master_id)
    if catalog is None:
        return None
    doc = resolve(catalog, config)
    # A configured variant key that does not exist is substituted with the slot default. That is a
    # fallback, so flag it (defaults_used) rather than render it silently (hard rule 2, SPEC 1.3).
    defaults_used = defaults_used or bool(doc.unresolved)
    html = render_html(doc)
    rendered = await render_pdf(html)

    org, role = await _target_org_role(session, catalog, opportunity_id)
    filename = filename_for(doc.header, org, role)
    docs_dir = paths.data_dir() / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)

    over = rendered.page_count > settings.docs.max_pages
    row = DocRender(
        opportunity_id=opportunity_id,
        doc_master_id=master_id,
        config=config,
        rationale=rationale or {},
        template=config.get("layout"),
        filename=filename,
        page_count=rendered.page_count,
        defaults_used=defaults_used,
    )
    session.add(row)
    await session.flush()
    # Write to an id-prefixed path so tailoring the SAME master for two opportunities never
    # overwrites the earlier PDF (the download name stays the clean {LastName}_{Org}_{Role}.pdf).
    pdf_path = docs_dir / f"{row.id}_{filename}"
    pdf_path.write_bytes(rendered.pdf)
    row.pdf_path = str(pdf_path)
    await session.commit()
    await session.refresh(row)
    return RenderResult(
        doc_render_id=row.id,
        filename=filename,
        pdf_path=str(pdf_path),
        page_count=rendered.page_count,
        over_page_limit=over,
        defaults_used=defaults_used,
        hide_suggestions=_hide_suggestions(catalog, config) if over else [],
        html=html,
    )


async def refresh_pdf(
    session: AsyncSession, settings: Settings, render: DocRender
) -> DocRender | None:
    """Write the PDF for this render's CURRENT picks. A swap, a hide or a look change on the
    Tailor page edits the stored config but not the file, so the file is made again here, from the
    same resolve() and render_html() the preview uses, whenever the page hands it over. Returns
    None when the document the render came from no longer exists. A render failure (no browser)
    propagates so the caller can say so; the old file is left as it was."""
    catalog = await load_catalog(session, render.doc_master_id)
    if catalog is None:
        return None
    config = dict(render.config or {})
    config["redact_orgs"] = bool(config.get("redact_orgs") or settings.confidential_mode)
    doc = resolve(catalog, config)
    rendered = await render_pdf(render_html(doc))
    org, role = await _target_org_role(session, catalog, render.opportunity_id)
    filename = render.filename or filename_for(doc.header, org, role)
    docs_dir = paths.data_dir() / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = docs_dir / f"{render.id}_{filename}"
    pdf_path.write_bytes(rendered.pdf)
    render.pdf_path = str(pdf_path)
    render.filename = filename
    render.page_count = rendered.page_count
    config = dict(render.config or {})
    config.pop("stale", None)
    render.config = config
    await session.commit()
    await session.refresh(render)
    return render
