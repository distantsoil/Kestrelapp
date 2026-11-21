"""The correction lines a score would carry, for the Score again dialog (issue 170, D-SEC-7).

The dialog lists exactly what the scoring prompt states as the operator's facts for each role:
`operator_correction_lines(await scoring_corrections(s, opp))`, the same two calls Tier 2 and
Tier 3 make, so what is listed is what is sent. Read only: nothing here writes a row, queues a
task or calls a model."""

from __future__ import annotations

import weakref
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Opportunity
from kestrel.learning.corrections import scoring_corrections
from kestrel.pipeline.context import operator_correction_lines


async def send_mapping(s: AsyncSession):
    """The employer map an online scoring send hides with, or None when it hides nothing,
    decided by the one rule every online send uses (`pipeline._shared.employer_hiding`). It does
    not depend on the tier or the model: it is asked only for a send that goes online, and Tier 2
    asks the same function, so the panel and the send never disagree (D-PRIV-673). With
    confidential mode on and no employer chosen the send is refused, and the panels say so."""
    from kestrel.pipeline._shared import employer_hiding
    from kestrel.services.setting_overrides import load_settings_with_db

    _status, mapping = await employer_hiding(s, await load_settings_with_db(s))
    return mapping or None


async def lines_for(s: AsyncSession, opp_ids: Iterable[int]) -> list[str]:
    """Every line these roles' scores would state, each role's own block in turn with repeats
    left out. Never cut: each role's block is at most 20 lines, but several roles' blocks
    together can be more, and every line sent is listed (D-SCORE-3). Hidden as the send hides
    them (`send_mapping`): each name of the current employer reads as its "Show it as" text,
    where the send carries a one-time code in its place."""
    ids = sorted({int(i) for i in opp_ids})
    if not ids:
        return []
    opps = (await s.execute(select(Opportunity).where(Opportunity.id.in_(ids)))).scalars()
    seen: dict[str, None] = {}
    for opp in sorted(opps, key=lambda o: o.id):
        for line in operator_correction_lines(await scoring_corrections(s, opp)):
            seen.setdefault(line)
    mapping = await send_mapping(s)
    return [(mapping.redact(x) or x) if mapping else x for x in seen]


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


HIDDEN_MARK = "\x00employer hidden"


def bound_digest(lines: Iterable[str], hidden: bool) -> str:
    """The digest a confirmation binds: the unredacted lines, and whether the send hides the
    current employer (D-PRIV-673). Turning confidential mode on or off after the confirmation
    therefore changes it, so a send the panel showed hidden is never sent named. "" when there
    are no lines."""
    items = list(lines)
    if not items:
        return ""
    return lines_digest([HIDDEN_MARK, *items] if hidden else items)


async def role_digest(s: AsyncSession, opp: Opportunity) -> str:
    """The digest of the lines a score of this role would state now, as it would send them."""
    lines = operator_correction_lines(await scoring_corrections(s, opp))
    return bound_digest(lines, bool(lines) and await send_mapping(s) is not None)


async def _role_digests(s: AsyncSession, opp_ids: Iterable[int]) -> dict[int, str]:
    """Each listed role's digest, read once, in this session."""
    ids = sorted({int(i) for i in opp_ids})
    if not ids:
        return {}
    opps = (await s.execute(select(Opportunity).where(Opportunity.id.in_(ids)))).scalars()
    return {opp.id: await role_digest(s, opp) for opp in sorted(opps, key=lambda o: o.id)}


def _panel_digest(digests: dict[int, str]) -> str:
    return lines_digest([f"{i}:{d}" for i, d in sorted(digests.items()) if d])


async def dialog_digest(s: AsyncSession, opp_ids: Iterable[int]) -> str:
    """One digest for every role a panel lists: "" when none of them has a line. Every panel
    that confirms an online scoring send posts it, and `confirm_corrections` compares it."""
    return _panel_digest(await _role_digests(s, opp_ids))


# ------------------------------------------------------------------ the one way to bind lines
# Issue #178 round 2 (D-PRIV-674): a scoring task can carry an online approval only together with
# a ConfirmedCorrections, and only `confirm_corrections` makes one. It reads the lines once, in
# the session that enqueues, compares them with what the panel posted, and the task binds those
# very digests, never a second reading. So a role is never queued online with lines nobody saw.
_SEAL = object()


class CorrectionsNotConfirmed(ValueError):
    """An online scoring task without a matching confirmation: a programming error, refused."""


@dataclass(frozen=True, eq=False)
class ConfirmedCorrections:
    """The digests of the lines a person confirmed, per role, as read when they confirmed.

    Only an instance `confirm_corrections` issued counts, and only with the digests it was issued
    with: the registry below holds them, so a copy, a `dataclasses.replace`, an object built by
    hand, an unpickled one or an instance edited in place is refused (round 3 finding 2). This
    stops a mistake in Kestrel's own code, not code that sets out to defeat it from inside the
    same process."""

    digests: tuple[tuple[int, str], ...]
    posted: str
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _SEAL:
            raise CorrectionsNotConfirmed("only confirm_corrections makes a ConfirmedCorrections")

    def _refuse_copy(self, *_args, **_kwargs):
        raise CorrectionsNotConfirmed("a ConfirmedCorrections cannot be copied or pickled")

    __copy__ = __deepcopy__ = __reduce__ = __reduce_ex__ = __replace__ = __getstate__ = (
        _refuse_copy)

    def for_role(self, opp_id: int) -> str:
        for oid, digest in issued_digests(self):
            if oid == int(opp_id):
                return digest
        raise CorrectionsNotConfirmed(f"role {opp_id} was not in the confirmed panel")


# Each issued confirmation -> the digests it was issued with. Weak keys: a confirmation lives only
# as long as the request that made it.
_ISSUED: weakref.WeakKeyDictionary[ConfirmedCorrections, tuple[tuple[int, str], ...]] = (
    weakref.WeakKeyDictionary())


def issued_digests(value: object) -> tuple[tuple[int, str], ...]:
    """The digests `confirm_corrections` issued with this very object; raises for anything else,
    including an issued object whose fields were changed afterward."""
    if type(value) is not ConfirmedCorrections:
        raise CorrectionsNotConfirmed("an online scoring task needs the person's confirmed "
                                      "correction lines")
    issued = _ISSUED.get(value)
    if issued is None or issued != value.digests:
        raise CorrectionsNotConfirmed("this confirmation was not issued by confirm_corrections")
    return issued


def _observe(event: str, **facts: object) -> None:
    """A no-op hook the contract tests replace to record every confirmation attempt."""


async def confirm_corrections(
    s: AsyncSession, opp_ids: Iterable[int], posted: object,
) -> ConfirmedCorrections | None:
    """The person's confirmation of these roles' lines, or None when the digest their panel
    posted (`corrections_digest`; missing reads as "", no lines) is not the current one. Call it
    with the session that then enqueues, before the commit."""
    ids = list(dict.fromkeys(int(i) for i in opp_ids))
    digests = await _role_digests(s, ids)
    current = _panel_digest(digests)
    got = str(posted or "")
    _observe("confirm", ids=tuple(ids), posted=got, current=current, ok=got == current)
    if got != current:
        return None
    issued = tuple(sorted(digests.items()))
    confirmed = ConfirmedCorrections(issued, got, _SEAL)
    _ISSUED[confirmed] = issued
    return confirmed


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
