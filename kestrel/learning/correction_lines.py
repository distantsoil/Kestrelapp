"""The correction lines a score would carry, for the Score again dialog (issue 170, D-SEC-7).

The dialog lists exactly what the scoring prompt states as the operator's facts for each role:
`operator_correction_lines(await scoring_corrections(s, opp))`, the same two calls Tier 2 and
Tier 3 make, so what is listed is what is sent. Read only: nothing here writes a row, queues a
task or calls a model."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Opportunity
from kestrel.learning.corrections import scoring_corrections
from kestrel.pipeline.context import operator_correction_lines
from kestrel.web.redact import confidential_once, redact_text


async def lines_for(s: AsyncSession, opp_ids: Iterable[int]) -> list[str]:
    """Every line these roles' scores would state, each role's own block in turn with repeats
    left out. Never cut: each role's block is at most 20 lines, but several roles' blocks
    together can be more, and every line sent is listed (D-SCORE-3). Redacted in confidential
    mode, as the send would be."""
    ids = sorted({int(i) for i in opp_ids})
    if not ids:
        return []
    opps = (await s.execute(select(Opportunity).where(Opportunity.id.in_(ids)))).scalars()
    seen: dict[str, None] = {}
    for opp in sorted(opps, key=lambda o: o.id):
        for line in operator_correction_lines(await scoring_corrections(s, opp)):
            seen.setdefault(line)
    conf, mapping = await confidential_once(s)
    return [redact_text(x, mapping, enabled=conf) or x for x in seen]


# ------------------------------------------------------------------ binding a confirmation
# An online send's confirmation is bound to the correction lines it showed (D-SCORE-3), the way it
# is bound to the model: a Score again task carries `online_consent.CORRECTIONS_KEY`, the digest
# of the role's lines at confirmation, and Tier 2 holds the task (never sends it) when the lines
# it would send now differ. The digest is of the unredacted lines, exactly what Tier 2 formats.


def lines_digest(lines: Iterable[str]) -> str:
    """A stable digest of correction lines, "" when there are none (nothing to bind)."""
    import hashlib
    import json

    items = list(lines)
    if not items:
        return ""
    return hashlib.sha256(json.dumps(items, ensure_ascii=False).encode("utf-8")).hexdigest()


async def role_digest(s: AsyncSession, opp: Opportunity) -> str:
    """The digest of the lines a score of this role would state now."""
    return lines_digest(operator_correction_lines(await scoring_corrections(s, opp)))


async def dialog_digest(s: AsyncSession, opp_ids: Iterable[int]) -> str:
    """One digest for every role a dialog lists: "" when none of them has a line. The Score again
    dialog posts it, and the server refuses the send when it no longer matches."""
    ids = sorted({int(i) for i in opp_ids})
    if not ids:
        return ""
    opps = (await s.execute(select(Opportunity).where(Opportunity.id.in_(ids)))).scalars()
    parts = []
    for opp in sorted(opps, key=lambda o: o.id):
        d = await role_digest(s, opp)
        if d:
            parts.append(f"{opp.id}:{d}")
    return lines_digest(parts)


def corrections_differ(payload: dict | None, current: str) -> bool:
    """True when the task carries a confirmed digest and the lines now differ from it. A task
    with no digest (a run's approval, standing Hosted mode) is covered by the acknowledged Hosted
    mode disclosure, which names corrections (D-PRIV-145)."""
    from kestrel.services.online_consent import CORRECTIONS_KEY

    got = (payload or {}).get(CORRECTIONS_KEY)
    return got is not None and str(got) != current


async def held_for_corrections(
    s: AsyncSession, tiers_settings: Iterable[object], payload: dict | None,
) -> bool:
    """The release test for a corrections hold: True while the role's lines still differ from the
    confirmed ones and Tier 2 still goes online under any of `tiers_settings`."""
    from kestrel.services.online_consent import CORRECTIONS_KEY, tier_goes_online

    got = (payload or {}).get(CORRECTIONS_KEY)
    if got is None:
        return False
    if not any(tier_goes_online(x, "tier2") for x in tiers_settings):  # type: ignore[arg-type]
        return False
    oid = (payload or {}).get("opportunity_id")
    opp = await s.get(Opportunity, int(oid)) if isinstance(oid, int) else None
    if opp is None:
        return False
    return corrections_differ(payload, await role_digest(s, opp))
