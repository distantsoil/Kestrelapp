"""Refresh proof points from the current resume (issue #208 item 6c, D-SCORE-26).

Proof points were written once, at import, by adding the ticked ones to profile.yml
(`profile_edit.add_proof_points` only ever adds), so they drift from the resume the operator keeps
editing in Documents: points from roles no longer on it stay, new bullets never arrive, and a
point cut at a line wrap stays cut. This module proposes a reviewed diff against the newest active
resume and applies only the rows the operator ticks.

INVARIANT (hard rule 2): every claim it writes is the text of one of the operator's own resume
bullets, byte for byte (`doc_variant.text`), and nothing is written until the operator approves.
The plan is always recomputed on the server at apply time; the form names rows by id only, so a
posted form can never carry text into the profile. Nothing here calls a model or the network.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import DocSection, DocSlot, DocVariant
from kestrel.pipeline.resume_digest import current_master

Action = Literal["add", "fix", "remove"]
# A proof point shorter than this is never matched as a fragment of a longer bullet: a few
# words appear in many bullets.
_MIN_FRAGMENT = 20
# Bullets from the first roles (the newest, at the top of the resume) start ticked to add.
_TICKED_ROLES = 2
# Fewer proof points than this gets a warning on Profile (item 6d).
FEW_PROOF_POINTS = 5

_REASONS = {
    "fix": "This was cut off. Your resume has the whole line.",
    "remove": "This is not on your current resume.",
    "remove_dup": "Another proof point already has this whole line.",
    "add": "On your current resume, not yet a proof point.",
}


@dataclass(frozen=True)
class Bullet:
    text: str  # the operator's own words, byte for byte
    role: str  # the role's title as the resume has it
    role_index: int


@dataclass
class Row:
    id: str
    action: Action
    reason: str
    old: str = ""  # the proof point's claim now ("fix", "remove")
    new: str = ""  # the resume line it becomes or adds ("fix", "add")
    key: str = ""  # the proof point's key ("fix", "remove")
    index: int = -1  # the proof point's position in the list ("fix", "remove"): keys can repeat
    ticked: bool = True
    bullet: Bullet | None = None


@dataclass
class Plan:
    has_resume: bool
    rows: list[Row] = field(default_factory=list)
    kept: int = 0  # proof points that already match a resume line exactly

    @property
    def changes(self) -> int:
        return len(self.rows)


def _norm(text: object) -> str:
    return " ".join(str(text or "").split()).casefold()


def _row_id(action: str, key: str, text: str) -> str:
    return hashlib.sha256(f"{action}\x1f{key}\x1f{text}".encode()).hexdigest()[:16]


async def resume_bullets(session: AsyncSession, track: str) -> list[Bullet] | None:
    """Every bullet of the newest active resume, in document order (each slot's default variant,
    else its first), or None when there is no resume."""
    master = await current_master(session, track)
    if master is None:
        return None
    sections = (
        await session.execute(
            select(DocSection).where(DocSection.doc_master_id == master.id,
                                     DocSection.section_kind.in_(("role", "service_line")))
            .order_by(DocSection.sort_order, DocSection.id)
        )
    ).scalars().all()
    out: list[Bullet] = []
    for i, sec in enumerate(sections):
        slots = (
            await session.execute(select(DocSlot).where(DocSlot.doc_section_id == sec.id)
                                  .order_by(DocSlot.sort_order, DocSlot.id))
        ).scalars().all()
        for slot in slots:
            variants = (
                await session.execute(select(DocVariant).where(DocVariant.doc_slot_id == slot.id)
                                      .order_by(DocVariant.id))
            ).scalars().all()
            chosen = next((v for v in variants if v.key == slot.default_variant_key), None)
            chosen = chosen or (variants[0] if variants else None)
            if chosen is not None and chosen.text and chosen.text.strip():
                out.append(Bullet(text=chosen.text, role=sec.title or "", role_index=i))
    return out


def plan_refresh(points: list[dict], bullets: list[Bullet] | None) -> Plan:
    """The diff between the profile's proof points and the resume's bullets. Deterministic.

    - keep: a point whose claim is a resume bullet (ignoring case and spacing);
    - fix: a point whose claim is part of exactly one bullet no other point already has: it
      becomes that whole bullet;
    - remove: a point on no bullet (or part of a bullet another point now has in full);
    - add: a bullet no point has (ticked for the newest two roles, unticked for older ones)."""
    if bullets is None:
        return Plan(has_resume=False)
    plan = Plan(has_resume=True)
    by_norm = {}
    for b in bullets:
        by_norm.setdefault(_norm(b.text), b)
    covered: set[str] = set()
    pending: list[tuple[int, dict]] = []
    for i, p in enumerate(points):
        claim = _norm(p.get("claim"))
        if claim and claim in by_norm:
            covered.add(claim)
            plan.kept += 1
        else:
            pending.append((i, p))
    for i, p in pending:
        claim_raw = str(p.get("claim") or "")
        claim = _norm(claim_raw)
        key = str(p.get("key") or "")
        hits = [n for n, b in by_norm.items() if len(claim) >= _MIN_FRAGMENT and claim in n]
        if len(hits) == 1 and hits[0] not in covered:
            b = by_norm[hits[0]]
            covered.add(hits[0])
            plan.rows.append(Row(id=_row_id("fix", f"{i}:{key}", b.text), action="fix",
                                 reason=_REASONS["fix"], old=claim_raw, new=b.text, key=key,
                                 index=i, bullet=b))
        else:
            why = _REASONS["remove_dup"] if hits and all(h in covered for h in hits) else (
                _REASONS["remove"])
            # Unchecked by default: the operator wrote it, and the resume not having it is a
            # reason to look, not to delete (review).
            plan.rows.append(Row(id=_row_id("remove", f"{i}:{key}", claim_raw),
                                 action="remove", reason=why, old=claim_raw, key=key, index=i,
                                 ticked=False))
    for n, b in by_norm.items():
        if n in covered:
            continue
        plan.rows.append(Row(id=_row_id("add", "", b.text), action="add", reason=_REASONS["add"],
                             new=b.text, ticked=b.role_index < _TICKED_ROLES, bullet=b))
    return plan


def apply_rows(points: list[dict], plan: Plan, approved: set[str]) -> tuple[list[dict], int]:
    """The proof point list with only the approved rows applied, and how many were applied. A
    fixed or added claim is the bullet's own text byte for byte; a fixed point keeps its key, its
    metric and its evidence; an added point gets the role's title as its evidence."""
    rows = [r for r in plan.rows if r.id in approved]
    fixes = {r.index: r for r in rows if r.action == "fix"}
    removes = {r.index for r in rows if r.action == "remove"}
    out: list[dict] = []
    for i, p in enumerate(points):
        if i in removes:
            continue
        if i in fixes:
            p = {**p, "claim": fixes[i].new}
        out.append(dict(p))
    taken = {str(p.get("key") or "") for p in out}
    for r in rows:
        if r.action != "add" or r.bullet is None:
            continue
        base = f"r{r.bullet.role_index + 1}_refresh"
        key, i = base, 2
        while key in taken:
            key, i = f"{base}_{i}", i + 1
        taken.add(key)
        out.append({"key": key, "claim": r.bullet.text, "evidence": r.bullet.role,
                    "metric": None})
    return out, len(rows)


def warnings(points: list, narrative: str | None) -> list[str]:
    """Profile warnings for what Tier 2 reads (item 6d): an empty narrative and too few proof
    points both leave the model little to go on."""
    out: list[str] = []
    if not str(narrative or "").strip():
        out.append("Your narrative is empty. Scoring reads it to understand what you do; add a "
                   "few sentences below.")
    n = len([p for p in points or [] if p])
    if n < FEW_PROOF_POINTS:
        out.append(f"You have {n} proof point{'' if n == 1 else 's'}. Scoring can only credit "
                   f"what they and your resume show; {FEW_PROOF_POINTS} or more works best.")
    return out
