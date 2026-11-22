"""RESUME_DIGEST for Tier 2 and the deeper read (PROMPTS.md, owner's option C, D-SCORE-25).

The operator's current resume, as their own lines, so the full review sees more than the proof
points. Read from the database, never written, never sent anywhere by this module: the caller
places the block in the prompt, and an online send is covered by the hosted disclosure (the
`tier2` line in config.HOSTED_TIER_SENDS names it) and its redaction. Every bullet is
`doc_variant.text` byte for byte (hard rule 2); nothing here calls a model.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import DocMaster, DocSection, DocSlot, DocVariant
from kestrel.pipeline.context import digest_budget, resume_digest_block, resume_digest_lines

# The document each track's digest is read from, in order of preference.
_KINDS = {"employment": ("resume",), "consulting": ("capability_statement", "resume")}
# Roles and service lines carry bullets; a credential section's lines are not experience.
_BULLET_SECTIONS = ("role", "service_line")


async def current_master(session: AsyncSession, track: str) -> DocMaster | None:
    """The newest active document of the track's preferred kind, or None."""
    for kind in _KINDS.get(track, ("resume",)):
        row = (
            await session.execute(
                select(DocMaster)
                .where(DocMaster.track == track, DocMaster.doc_kind == kind,
                       DocMaster.is_active.is_(True))
                .order_by(DocMaster.updated_at.desc(), DocMaster.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is not None:
            return row
    return None


async def role_bullets(session: AsyncSession, master_id: int) -> list[list[str]]:
    """Each role's bullets in document order: the slot's default variant, else its first."""
    sections = (
        await session.execute(
            select(DocSection)
            .where(DocSection.doc_master_id == master_id,
                   DocSection.section_kind.in_(_BULLET_SECTIONS))
            .order_by(DocSection.sort_order, DocSection.id)
        )
    ).scalars().all()
    out: list[list[str]] = []
    for sec in sections:
        slots = (
            await session.execute(
                select(DocSlot).where(DocSlot.doc_section_id == sec.id)
                .order_by(DocSlot.sort_order, DocSlot.id)
            )
        ).scalars().all()
        texts: list[str] = []
        for slot in slots:
            variants = (
                await session.execute(
                    select(DocVariant).where(DocVariant.doc_slot_id == slot.id)
                    .order_by(DocVariant.id)
                )
            ).scalars().all()
            chosen = next((v for v in variants if v.key == slot.default_variant_key), None)
            chosen = chosen or (variants[0] if variants else None)
            if chosen is not None and chosen.text and chosen.text.strip():
                texts.append(chosen.text)
        if texts:
            out.append(texts)
    return out


def skill_groups(master: DocMaster) -> list[tuple[str, list[str]]]:
    """The master's skills groups as (label, items), shown groups only, as stored."""
    out: list[tuple[str, list[str]]] = []
    for g in master.skill_groups or []:
        if not isinstance(g, dict) or g.get("default_visible") is False:
            continue
        items = [str(i) for i in g.get("items") or [] if str(i).strip()]
        if items:
            out.append((str(g.get("label") or "Skills"), items))
    return out


async def resume_digest(session: AsyncSession, track: str, profile_block: str) -> str:
    """The RESUME_DIGEST block for this track, capped to what `profile_block` leaves of the
    profile allowance (`context.digest_budget`), or "" when there is no resume or no room."""
    master = await current_master(session, track)
    if master is None:
        return ""
    budget = digest_budget(profile_block)
    if budget <= 0:
        return ""
    lines = resume_digest_lines(skill_groups(master), await role_bullets(session, master.id),
                                budget)
    return resume_digest_block(lines)


async def digests_for_tracks() -> dict[str, str]:
    """Each track's digest from the app database, for `kestrel bench` (the harness itself never
    opens the database)."""
    from kestrel.db.engine import get_sessionmaker
    from kestrel.pipeline.context import profile_full_v4
    from kestrel.pipeline.profile import load_profile

    out: dict[str, str] = {}
    async with get_sessionmaker()() as session:
        for track in _KINDS:
            out[track] = await resume_digest(session, track, profile_full_v4(load_profile(track)))
    return out
